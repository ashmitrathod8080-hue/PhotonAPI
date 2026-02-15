import os
import time
import json
import hashlib
import sqlite3
import threading
from datetime import datetime


class Migration:
    def __init__(self, version, name, up_sql=None, down_sql=None,
                 up_fn=None, down_fn=None):
        self.version = version
        self.name = name
        self.up_sql = up_sql
        self.down_sql = down_sql
        self.up_fn = up_fn
        self.down_fn = down_fn

    def __repr__(self):
        return f"<Migration {self.version}: {self.name}>"


class MigrationManager:
    def __init__(self, db=None, db_path="app.db", migrations_dir="migrations"):
        self.db = db
        self.db_path = db_path
        self.migrations_dir = migrations_dir
        self._migrations = []
        self._connection = None

    @property
    def connection(self):
        if self._connection is None:
            if self.db:
                self._connection = self.db.connection
            else:
                self._connection = sqlite3.connect(self.db_path)
                self._connection.row_factory = sqlite3.Row
        return self._connection

    def _ensure_table(self):
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                applied_at TEXT DEFAULT (datetime('now')),
                checksum TEXT
            )
        """)
        self.connection.commit()

    def _get_applied(self):
        self._ensure_table()
        cursor = self.connection.execute(
            "SELECT version, name, applied_at FROM _migrations ORDER BY version"
        )
        return [dict(row) for row in cursor.fetchall()]

    def add(self, version, name, up_sql=None, down_sql=None, up_fn=None, down_fn=None):
        m = Migration(version, name, up_sql, down_sql, up_fn, down_fn)
        self._migrations.append(m)
        self._migrations.sort(key=lambda x: x.version)
        return m

    def migration(self, version, name):
        def decorator(cls):
            instance = cls()
            up_fn = getattr(instance, "up", None)
            down_fn = getattr(instance, "down", None)
            self.add(version, name, up_fn=up_fn, down_fn=down_fn)
            return cls
        return decorator

    def load_from_dir(self):
        if not os.path.isdir(self.migrations_dir):
            return

        files = sorted(f for f in os.listdir(self.migrations_dir) if f.endswith(".sql"))
        for fname in files:
            parts = fname.replace(".sql", "").split("_", 1)
            version = parts[0]
            name = parts[1] if len(parts) > 1 else fname

            filepath = os.path.join(self.migrations_dir, fname)
            with open(filepath) as f:
                content = f.read()

            up_sql = content
            down_sql = None

            if "-- DOWN" in content:
                sections = content.split("-- DOWN", 1)
                up_sql = sections[0].replace("-- UP", "").strip()
                down_sql = sections[1].strip()
            elif "-- UP" in content:
                up_sql = content.replace("-- UP", "").strip()

            self.add(version, name, up_sql=up_sql, down_sql=down_sql)

    def get_pending(self):
        applied_versions = {m["version"] for m in self._get_applied()}
        return [m for m in self._migrations if m.version not in applied_versions]

    def run_pending(self):
        self.load_from_dir()
        pending = self.get_pending()

        if not pending:
            print("  ✓ No pending migrations")
            return []

        applied = []
        for m in pending:
            try:
                self._apply(m)
                applied.append(m)
                print(f"  ✓ Applied: {m.version} — {m.name}")
            except Exception as e:
                print(f"  ✗ Failed: {m.version} — {m.name}: {e}")
                raise

        return applied

    def _apply(self, migration):
        if migration.up_sql:
            for statement in self._split_sql(migration.up_sql):
                self.connection.execute(statement)

        if migration.up_fn:
            migration.up_fn(self.connection)

        checksum = hashlib.md5(
            (migration.up_sql or "").encode()
        ).hexdigest()

        self.connection.execute(
            "INSERT INTO _migrations (version, name, checksum) VALUES (?, ?, ?)",
            [migration.version, migration.name, checksum]
        )
        self.connection.commit()

    def rollback(self, steps=1):
        self.load_from_dir()
        applied = self._get_applied()

        if not applied:
            print("  ✓ Nothing to rollback")
            return

        to_rollback = applied[-steps:]
        to_rollback.reverse()

        migration_map = {m.version: m for m in self._migrations}

        for record in to_rollback:
            m = migration_map.get(record["version"])
            if not m:
                print(f"  ✗ Migration {record['version']} not found in codebase")
                continue

            try:
                if m.down_sql:
                    for statement in self._split_sql(m.down_sql):
                        self.connection.execute(statement)

                if m.down_fn:
                    m.down_fn(self.connection)

                self.connection.execute(
                    "DELETE FROM _migrations WHERE version = ?",
                    [record["version"]]
                )
                self.connection.commit()
                print(f"  ↩ Rolled back: {record['version']} — {record['name']}")
            except Exception as e:
                print(f"  ✗ Rollback failed: {record['version']}: {e}")
                raise

    def status(self):
        self.load_from_dir()
        applied = self._get_applied()
        applied_versions = {m["version"] for m in applied}

        result = []
        for m in self._migrations:
            result.append({
                "version": m.version,
                "name": m.name,
                "applied": m.version in applied_versions,
            })
        return result

    def generate(self, name, up_sql="", down_sql=""):
        os.makedirs(self.migrations_dir, exist_ok=True)
        version = datetime.now().strftime("%Y%m%d%H%M%S")
        safe_name = name.lower().replace(" ", "_")
        filename = f"{version}_{safe_name}.sql"
        filepath = os.path.join(self.migrations_dir, filename)

        content = f"-- UP\n{up_sql}\n\n-- DOWN\n{down_sql}\n"
        with open(filepath, "w") as f:
            f.write(content)

        print(f"  ✓ Created: {filepath}")
        return filepath

    def _split_sql(self, sql):
        statements = []
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                statements.append(stmt)
        return statements
