import sqlite3
import json
import threading
import time
import os
from datetime import datetime
from contextlib import contextmanager


TYPE_MAP = {
    int: "INTEGER",
    float: "REAL",
    str: "TEXT",
    bool: "INTEGER",
    bytes: "BLOB",
    datetime: "TEXT",
}

PG_TYPE_MAP = {
    int: "INTEGER",
    float: "DOUBLE PRECISION",
    str: "TEXT",
    bool: "BOOLEAN",
    bytes: "BYTEA",
    datetime: "TIMESTAMP",
}

MYSQL_TYPE_MAP = {
    int: "INT",
    float: "DOUBLE",
    str: "TEXT",
    bool: "TINYINT(1)",
    bytes: "BLOB",
    datetime: "DATETIME",
}


class ConnectionPool:
    def __init__(self, create_fn, max_size=10, timeout=30):
        self._create = create_fn
        self._max_size = max_size
        self._timeout = timeout
        self._pool = []
        self._in_use = 0
        self._lock = threading.Lock()
        self._available = threading.Condition(self._lock)

    def acquire(self):
        with self._available:
            while True:
                if self._pool:
                    conn = self._pool.pop()
                    self._in_use += 1
                    return conn
                if self._in_use < self._max_size:
                    self._in_use += 1
                    return self._create()
                self._available.wait(timeout=self._timeout)
                if not self._pool and self._in_use >= self._max_size:
                    raise RuntimeError("Connection pool exhausted")

    def release(self, conn):
        with self._available:
            self._pool.append(conn)
            self._in_use -= 1
            self._available.notify()

    def close_all(self):
        with self._lock:
            for conn in self._pool:
                try:
                    conn.close()
                except Exception:
                    pass
            self._pool.clear()
            self._in_use = 0

    @property
    def stats(self):
        return {
            "pool_size": len(self._pool),
            "in_use": self._in_use,
            "max_size": self._max_size,
        }


class Database:
    def __init__(self, path=":memory:", backend="sqlite", pool_size=5,
                 host=None, port=None, user=None, password=None, database=None):
        self.path = path
        self.backend = backend
        self._local = threading.local()
        self._models = {}
        self._pool = None
        self._sql_profiler = None

        if backend == "postgresql":
            self._init_pg(host, port, user, password, database, pool_size)
        elif backend == "mysql":
            self._init_mysql(host, port, user, password, database, pool_size)

    def _init_pg(self, host, port, user, password, database, pool_size):
        try:
            import psycopg2  # type: ignore[import-not-found]
        except ImportError:
            raise RuntimeError("Install psycopg2: pip install psycopg2-binary")

        def create_conn():
            conn = psycopg2.connect(
                host=host or "localhost", port=port or 5432,
                user=user or "postgres", password=password or "",
                database=database or "photonapi",
            )
            conn.autocommit = True
            return conn

        self._pool = ConnectionPool(create_conn, max_size=pool_size)

    def _init_mysql(self, host, port, user, password, database, pool_size):
        try:
            import pymysql  # type: ignore[import-not-found]
        except ImportError:
            raise RuntimeError("Install pymysql: pip install pymysql")

        def create_conn():
            return pymysql.connect(
                host=host or "localhost", port=port or 3306,
                user=user or "root", password=password or "",
                database=database or "photonapi",
                autocommit=True,
                cursorclass=pymysql.cursors.DictCursor,
            )

        self._pool = ConnectionPool(create_conn, max_size=pool_size)

    @property
    def connection(self):
        if self._pool:
            if not hasattr(self._local, "conn") or self._local.conn is None:
                self._local.conn = self._pool.acquire()
            return self._local.conn

        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.path)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def release_connection(self):
        if self._pool and hasattr(self._local, "conn") and self._local.conn:
            self._pool.release(self._local.conn)
            self._local.conn = None

    def execute(self, sql, params=None):
        start = time.time()
        conn = self.connection
        cursor = conn.cursor()
        cursor.execute(sql, params or ([] if self.backend == "sqlite" else ()))
        if self.backend != "postgresql":
            conn.commit()

        elapsed_ms = (time.time() - start) * 1000
        if self._sql_profiler:
            self._sql_profiler.record(sql, params, elapsed_ms)

        return cursor

    def query(self, sql, params=None):
        cursor = self.execute(sql, params)
        rows = cursor.fetchall()

        if self.backend == "sqlite":
            return [dict(row) for row in rows]
        elif self.backend == "mysql":
            return list(rows)
        elif self.backend == "postgresql":
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(zip(columns, row)) for row in rows]
        return [dict(row) for row in rows]

    def query_one(self, sql, params=None):
        cursor = self.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            return None
        if self.backend == "sqlite":
            return dict(row)
        elif self.backend == "mysql":
            return dict(row)
        elif self.backend == "postgresql":
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return dict(zip(columns, row))
        return dict(row)

    def raw(self, sql, params=None):
        return self.query(sql, params)

    @contextmanager
    def transaction(self):
        conn = self.connection
        if self.backend == "sqlite":
            conn.execute("BEGIN")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        elif self.backend == "postgresql":
            old_autocommit = conn.autocommit
            conn.autocommit = False
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = old_autocommit
        elif self.backend == "mysql":
            conn.begin()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self):
        if self._pool:
            self._pool.close_all()
        elif hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    def model(self, table_name, **fields):
        m = Model(self, table_name, fields)
        self._models[table_name] = m
        return m

    def _placeholder(self):
        return "%s" if self.backend in ("postgresql", "mysql") else "?"

    def _auto_id(self):
        if self.backend == "postgresql":
            return "SERIAL PRIMARY KEY"
        if self.backend == "mysql":
            return "INT AUTO_INCREMENT PRIMARY KEY"
        return "INTEGER PRIMARY KEY AUTOINCREMENT"

    def _now_default(self):
        if self.backend == "postgresql":
            return "DEFAULT NOW()"
        if self.backend == "mysql":
            return "DEFAULT CURRENT_TIMESTAMP"
        return "DEFAULT (datetime('now'))"


class ForeignKey:
    def __init__(self, reference_model, on_delete="CASCADE", on_update="CASCADE"):
        self.reference_model = reference_model
        self.on_delete = on_delete
        self.on_update = on_update


class ManyToMany:
    def __init__(self, target_model, through=None):
        self.target_model = target_model
        self.through = through


class Index:
    def __init__(self, *columns, unique=False, name=None):
        self.columns = columns
        self.unique = unique
        self.name = name


class Model:
    def __init__(self, db, name, fields):
        self.db = db
        self.name = name
        self.fields = {}
        self._foreign_keys = {}
        self._many_to_many = {}
        self._indexes = []

        for fname, ftype in fields.items():
            if isinstance(ftype, ForeignKey):
                self._foreign_keys[fname] = ftype
                self.fields[fname] = int
            elif isinstance(ftype, ManyToMany):
                self._many_to_many[fname] = ftype
            elif isinstance(ftype, Index):
                self._indexes.append(ftype)
            else:
                self.fields[fname] = ftype

        self._create_table()

    def _create_table(self):
        type_map = {
            "sqlite": TYPE_MAP,
            "postgresql": PG_TYPE_MAP,
            "mysql": MYSQL_TYPE_MAP,
        }.get(self.db.backend, TYPE_MAP)

        cols = [f"id {self.db._auto_id()}"]

        for field_name, field_type in self.fields.items():
            nullable = False
            actual_type = field_type
            if isinstance(field_type, tuple):
                actual_type, *opts = field_type
                nullable = "nullable" in opts

            sql_type = type_map.get(actual_type, "TEXT")
            col_def = f"{field_name} {sql_type}"
            if not nullable:
                col_def += " NOT NULL"
            cols.append(col_def)

        cols.append(f"created_at TEXT {self.db._now_default()}")
        cols.append(f"updated_at TEXT {self.db._now_default()}")

        constraints = []
        for fk_name, fk in self._foreign_keys.items():
            ref_table = fk.reference_model if isinstance(fk.reference_model, str) else fk.reference_model.name
            constraints.append(
                f"FOREIGN KEY ({fk_name}) REFERENCES {ref_table}(id) "
                f"ON DELETE {fk.on_delete} ON UPDATE {fk.on_update}"
            )

        all_parts = cols + constraints
        sql = f"CREATE TABLE IF NOT EXISTS {self.name} ({', '.join(all_parts)})"
        self.db.execute(sql)

        for idx in self._indexes:
            idx_name = idx.name or f"idx_{self.name}_{'_'.join(idx.columns)}"
            unique = "UNIQUE " if idx.unique else ""
            col_list = ", ".join(idx.columns)
            try:
                self.db.execute(
                    f"CREATE {unique}INDEX IF NOT EXISTS {idx_name} ON {self.name} ({col_list})"
                )
            except Exception:
                pass

    def add_index(self, *columns, unique=False, name=None):
        idx_name = name or f"idx_{self.name}_{'_'.join(columns)}"
        unique_str = "UNIQUE " if unique else ""
        col_list = ", ".join(columns)
        self.db.execute(
            f"CREATE {unique_str}INDEX IF NOT EXISTS {idx_name} ON {self.name} ({col_list})"
        )

    def create(self, **data):
        ph = self.db._placeholder()
        all_fields = {**self.fields, **{k: int for k in self._foreign_keys}}
        field_names = [k for k in data.keys() if k in all_fields]
        values = [self._serialize(data[k]) for k in field_names]
        placeholders = ", ".join([ph] * len(field_names))

        sql = f"INSERT INTO {self.name} ({', '.join(field_names)}) VALUES ({placeholders})"

        if self.db.backend == "postgresql":
            sql += " RETURNING id"
            cursor = self.db.execute(sql, values)
            row = cursor.fetchone()
            return self.find(row[0])
        else:
            cursor = self.db.execute(sql, values)
            return self.find(cursor.lastrowid)

    def find(self, id):
        ph = self.db._placeholder()
        row = self.db.query_one(f"SELECT * FROM {self.name} WHERE id = {ph}", [id])
        return self._deserialize(row) if row else None

    def all(self, limit=100, offset=0, order_by="id DESC"):
        ph = self.db._placeholder()
        rows = self.db.query(
            f"SELECT * FROM {self.name} ORDER BY {order_by} LIMIT {ph} OFFSET {ph}",
            [limit, offset]
        )
        return [self._deserialize(r) for r in rows]

    def where(self, limit=100, order_by=None, **conditions):
        ph = self.db._placeholder()
        clauses = []
        values = []
        for k, v in conditions.items():
            if isinstance(v, tuple) and len(v) == 2:
                op, val = v
                clauses.append(f"{k} {op} {ph}")
                values.append(self._serialize(val))
            elif isinstance(v, list):
                phs = ", ".join([ph] * len(v))
                clauses.append(f"{k} IN ({phs})")
                values.extend(self._serialize(x) for x in v)
            elif v is None:
                clauses.append(f"{k} IS NULL")
            else:
                clauses.append(f"{k} = {ph}")
                values.append(self._serialize(v))

        where_sql = " AND ".join(clauses)
        order = f" ORDER BY {order_by}" if order_by else ""
        rows = self.db.query(
            f"SELECT * FROM {self.name} WHERE {where_sql}{order} LIMIT {ph}",
            values + [limit]
        )
        return [self._deserialize(r) for r in rows]

    def first(self, **conditions):
        results = self.where(limit=1, **conditions)
        return results[0] if results else None

    def update(self, id, **data):
        ph = self.db._placeholder()
        all_fields = {**self.fields, **{k: int for k in self._foreign_keys}}
        field_names = [k for k in data.keys() if k in all_fields]
        if not field_names:
            return self.find(id)

        if self.db.backend == "postgresql":
            sets = [f"{k} = {ph}" for k in field_names] + ["updated_at = NOW()"]
        elif self.db.backend == "mysql":
            sets = [f"{k} = {ph}" for k in field_names] + ["updated_at = CURRENT_TIMESTAMP"]
        else:
            sets = [f"{k} = {ph}" for k in field_names] + ["updated_at = datetime('now')"]

        values = [self._serialize(data[k]) for k in field_names]
        values.append(id)
        self.db.execute(
            f"UPDATE {self.name} SET {', '.join(sets)} WHERE id = {ph}", values
        )
        return self.find(id)

    def delete(self, id):
        ph = self.db._placeholder()
        self.db.execute(f"DELETE FROM {self.name} WHERE id = {ph}", [id])
        return True

    def count(self, **conditions):
        ph = self.db._placeholder()
        if conditions:
            clauses = [f"{k} = {ph}" for k in conditions]
            values = [self._serialize(v) for v in conditions.values()]
            row = self.db.query_one(
                f"SELECT COUNT(*) as c FROM {self.name} WHERE {' AND '.join(clauses)}", values
            )
        else:
            row = self.db.query_one(f"SELECT COUNT(*) as c FROM {self.name}")
        return row["c"] if row else 0

    def exists(self, **conditions):
        return self.count(**conditions) > 0

    def delete_where(self, **conditions):
        ph = self.db._placeholder()
        clauses = [f"{k} = {ph}" for k in conditions]
        values = [self._serialize(v) for v in conditions.values()]
        self.db.execute(f"DELETE FROM {self.name} WHERE {' AND '.join(clauses)}", values)
        return True

    def upsert(self, unique_fields, **data):
        conditions = {k: data[k] for k in unique_fields if k in data}
        existing = self.first(**conditions)
        if existing:
            return self.update(existing["id"], **data)
        return self.create(**data)

    def bulk_create(self, items):
        results = []
        with self.db.transaction():
            for item in items:
                results.append(self.create(**item))
        return results

    def paginate(self, page=1, per_page=20, order_by="id DESC", **conditions):
        offset = (page - 1) * per_page
        if conditions:
            items = self.where(limit=per_page, order_by=order_by, **conditions)
            total = self.count(**conditions)
        else:
            items = self.all(limit=per_page, offset=offset, order_by=order_by)
            total = self.count()
        return {
            "data": items, "page": page, "per_page": per_page, "total": total,
            "pages": (total + per_page - 1) // per_page,
            "has_next": page * per_page < total, "has_prev": page > 1,
        }

    def related(self, id, relation_name):
        if relation_name in self._foreign_keys:
            fk = self._foreign_keys[relation_name]
            row = self.find(id)
            if row and row.get(relation_name):
                return fk.reference_model.find(row[relation_name])
            return None
        if relation_name in self._many_to_many:
            m2m = self._many_to_many[relation_name]
            target = m2m.target_model
            through = m2m.through or f"{self.name}_{target.name}"
            ph = self.db._placeholder()
            rows = self.db.query(
                f"SELECT {target.name}.* FROM {target.name} "
                f"INNER JOIN {through} ON {through}.{target.name}_id = {target.name}.id "
                f"WHERE {through}.{self.name}_id = {ph}", [id]
            )
            return [target._deserialize(r) for r in rows]
        return None

    def query(self):
        return QueryBuilder(self)

    def _serialize(self, value):
        if isinstance(value, bool):
            return 1 if value else 0 if self.db.backend == "sqlite" else value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return value

    def _deserialize(self, row):
        if row is None:
            return None
        result = dict(row)
        for field_name, field_type in self.fields.items():
            actual_type = field_type
            if isinstance(field_type, tuple):
                actual_type = field_type[0]
            if field_name in result and result[field_name] is not None:
                if actual_type == bool and self.db.backend == "sqlite":
                    result[field_name] = bool(result[field_name])
        return result


class QueryBuilder:
    def __init__(self, model):
        self._model = model
        self._select_cols = ["*"]
        self._where_clauses = []
        self._where_values = []
        self._order = None
        self._limit_val = None
        self._offset_val = None
        self._joins = []

    def select(self, *columns):
        self._select_cols = list(columns)
        return self

    def where(self, **conditions):
        ph = self._model.db._placeholder()
        for k, v in conditions.items():
            if isinstance(v, tuple) and len(v) == 2:
                op, val = v
                self._where_clauses.append(f"{k} {op} {ph}")
                self._where_values.append(val)
            else:
                self._where_clauses.append(f"{k} = {ph}")
                self._where_values.append(v)
        return self

    def where_raw(self, clause, *params):
        self._where_clauses.append(clause)
        self._where_values.extend(params)
        return self

    def order_by(self, column, direction="ASC"):
        self._order = f"{column} {direction}"
        return self

    def limit(self, n):
        self._limit_val = n
        return self

    def offset(self, n):
        self._offset_val = n
        return self

    def join(self, table, on_clause):
        self._joins.append(f"INNER JOIN {table} ON {on_clause}")
        return self

    def left_join(self, table, on_clause):
        self._joins.append(f"LEFT JOIN {table} ON {on_clause}")
        return self

    def build(self):
        cols = ", ".join(self._select_cols)
        sql = f"SELECT {cols} FROM {self._model.name}"
        for j in self._joins:
            sql += f" {j}"
        if self._where_clauses:
            sql += f" WHERE {' AND '.join(self._where_clauses)}"
        if self._order:
            sql += f" ORDER BY {self._order}"
        if self._limit_val is not None:
            sql += f" LIMIT {self._limit_val}"
        if self._offset_val is not None:
            sql += f" OFFSET {self._offset_val}"
        return sql, self._where_values

    def execute(self):
        sql, params = self.build()
        return self._model.db.query(sql, params)

    def first(self):
        self._limit_val = 1
        results = self.execute()
        return results[0] if results else None

    def count(self):
        self._select_cols = ["COUNT(*) as c"]
        result = self.execute()
        return result[0]["c"] if result else 0


def auto_crud(app, model, prefix=None, limiter=None, rate=None):
    prefix = prefix or f"/{model.name}"

    @app.get(prefix)
    def list_all(req, res):
        page = int(req.get_query("page", "0"))
        per_page = int(req.get_query("per_page", "20"))
        limit = int(req.get_query("limit", str(per_page)))
        offset = int(req.get_query("offset", "0"))
        order = req.get_query("order_by", "id DESC")

        if page > 0:
            return model.paginate(page=page, per_page=per_page, order_by=order)

        items = model.all(limit=limit, offset=offset, order_by=order)
        total = model.count()
        return {"data": items, "total": total, "limit": limit, "offset": offset}

    @app.get(f"{prefix}/<int:id>")
    def get_one(req, res, id):
        item = model.find(id)
        if not item:
            return {"error": "Not found"}, 404
        return {"data": item}

    @app.post(prefix)
    def create_one(req, res):
        data = req.json
        if not data:
            return {"error": "JSON body required"}, 400
        item = model.create(**data)
        return {"data": item}, 201

    @app.route(f"{prefix}/<int:id>", methods=["PUT", "PATCH"])
    def update_one(req, res, id):
        existing = model.find(id)
        if not existing:
            return {"error": "Not found"}, 404
        data = req.json
        if not data:
            return {"error": "JSON body required"}, 400
        item = model.update(id, **data)
        return {"data": item}

    @app.delete(f"{prefix}/<int:id>")
    def delete_one(req, res, id):
        existing = model.find(id)
        if not existing:
            return {"error": "Not found"}, 404
        model.delete(id)
        return {"message": "Deleted"}, 200
