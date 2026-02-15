import os
import sys
import json
import shutil
import argparse
import importlib
import time


PROJECT_TEMPLATES = {
    "api": {
        "files": {
            "app.py": '''\
from photonapi import PhotonAPI, CORSMiddleware, LoggingMiddleware

app = PhotonAPI(debug=True, title="{name}", version="1.0.0")
app.use(LoggingMiddleware())
app.use(CORSMiddleware())

@app.get("/")
def index(req, res):
    return {{"message": "Welcome to {name}"}}

@app.get("/health")
def health(req, res):
    return {{"status": "ok"}}

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
''',
            "requirements.txt": "# No dependencies required — PhotonAPI is zero-dep\n",
            "README.md": "# {name}\n\nBuilt with PhotonAPI.\n\n```\npython app.py\n```\n",
        },
        "dirs": ["static", "templates"],
    },
    "ml": {
        "files": {
            "app.py": '''\
from photonapi import PhotonAPI, CORSMiddleware, LoggingMiddleware, ModelRegistry

app = PhotonAPI(debug=True, title="{name}", version="1.0.0")
app.use(LoggingMiddleware())
app.use(CORSMiddleware())

models = ModelRegistry(app, cache_size=500)

@models.register("example", version="v1", cache=True)
class ExampleModel:
    def preprocess(self, data):
        return data.get("text", "")

    def predict(self, text):
        return {{"label": "example", "input_length": len(text)}}

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
''',
            "requirements.txt": "# Add ML dependencies here\n# torch\n# scikit-learn\n# transformers\n",
            "README.md": "# {name}\n\nML service built with PhotonAPI.\n\n```\npython app.py\n```\n",
        },
        "dirs": ["static", "templates", "models"],
    },
    "full": {
        "files": {
            "app.py": '''\
from photonapi import (
    PhotonAPI, Blueprint, CORSMiddleware, LoggingMiddleware,
    SecurityHeadersMiddleware, RateLimiter, get_remote_address,
    Schema, Field, validate, auto_crud, background,
)

app = PhotonAPI(
    debug=True,
    static_dir="./static",
    template_dir="./templates",
    title="{name}",
    version="1.0.0",
)

app.use(LoggingMiddleware())
app.use(CORSMiddleware())
app.use(SecurityHeadersMiddleware())

limiter = RateLimiter(key_func=get_remote_address)
db = app.init_db("app.db")
app.enable_tasks(workers=2)

@app.get("/")
def index(req, res):
    return app.render("index.html", title="{name}")

@app.get("/health")
def health(req, res):
    return {{"status": "ok"}}

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
''',
            "templates/index.html": "<html><body><h1>{{ title }}</h1></body></html>\n",
            "requirements.txt": "# No dependencies required\n",
            "README.md": "# {name}\n\nFull-stack app built with PhotonAPI.\n\n```\npython app.py\n```\n",
        },
        "dirs": ["static", "templates", "tests"],
    },
}


def cmd_new(args):
    name = args.name
    template = getattr(args, "template", "api")
    target = os.path.join(os.getcwd(), name)

    if os.path.exists(target):
        print(f"  ✗ Directory '{name}' already exists")
        return 1

    tmpl = PROJECT_TEMPLATES.get(template, PROJECT_TEMPLATES["api"])

    os.makedirs(target, exist_ok=True)
    for d in tmpl.get("dirs", []):
        os.makedirs(os.path.join(target, d), exist_ok=True)

    for filepath, content in tmpl["files"].items():
        full_path = os.path.join(target, filepath)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content.format(name=name))

    print(f"  ⚡ Created '{name}' ({template} template)")
    print(f"  cd {name} && python app.py")
    return 0


def cmd_run(args):
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8000)
    reload_flag = getattr(args, "reload", True)

    entry = None
    for candidate in ["app.py", "main.py", "server.py"]:
        if os.path.exists(candidate):
            entry = candidate
            break

    if not entry:
        print("  ✗ No app.py, main.py, or server.py found")
        return 1

    if reload_flag:
        from .reloader import run_with_reload
        run_with_reload(entry)
    else:
        os.environ["PHOTON_HOST"] = host
        os.environ["PHOTON_PORT"] = str(port)
        exec(open(entry).read())
    return 0


def cmd_routes(args):
    entry = None
    for candidate in ["app.py", "main.py", "server.py"]:
        if os.path.exists(candidate):
            entry = candidate
            break

    if not entry:
        print("  ✗ No app file found")
        return 1

    sys.path.insert(0, os.getcwd())
    spec = importlib.util.spec_from_file_location("_app", entry)
    mod = importlib.util.module_from_spec(spec)

    import io
    from contextlib import redirect_stdout, redirect_stderr
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
        except Exception:
            pass

    app = None
    for name in dir(mod):
        obj = getattr(mod, name)
        if hasattr(obj, "routes") and hasattr(obj, "resolve"):
            app = obj
            break

    if not app:
        print("  ✗ No PhotonAPI instance found")
        return 1

    method_colors = {
        "GET": "\033[32m", "POST": "\033[34m", "PUT": "\033[33m",
        "DELETE": "\033[31m", "PATCH": "\033[35m",
    }
    reset = "\033[0m"

    print(f"\n  ⚡ {len(app.routes)} routes\n")
    for route in app.routes:
        for method in route.methods:
            color = method_colors.get(method, "")
            print(f"  {color}{method:7s}{reset} {route.path:40s} → {route.name}")
    print()
    return 0


def cmd_db_migrate(args):
    entry = None
    for candidate in ["app.py", "main.py", "server.py"]:
        if os.path.exists(candidate):
            entry = candidate
            break

    if not entry:
        print("  ✗ No app file found")
        return 1

    try:
        from .migration import MigrationManager
        migrations_dir = getattr(args, "dir", "migrations")
        mgr = MigrationManager(migrations_dir=migrations_dir)
        mgr.run_pending()
    except Exception as e:
        print(f"  ✗ Migration failed: {e}")
        return 1
    return 0


def cmd_db_rollback(args):
    try:
        from .migration import MigrationManager
        migrations_dir = getattr(args, "dir", "migrations")
        mgr = MigrationManager(migrations_dir=migrations_dir)
        steps = getattr(args, "steps", 1)
        mgr.rollback(steps=steps)
    except Exception as e:
        print(f"  ✗ Rollback failed: {e}")
        return 1
    return 0


def cmd_db_seed(args):
    seed_file = getattr(args, "file", "seeds.py")
    if not os.path.exists(seed_file):
        print(f"  ✗ Seed file '{seed_file}' not found")
        return 1

    exec(open(seed_file).read())
    print("  ✓ Database seeded")
    return 0


def cmd_test(args):
    test_dir = getattr(args, "dir", "tests")
    try:
        import pytest  # type: ignore[import-not-found]
        sys.exit(pytest.main([test_dir, "-v"]))
    except ImportError:
        import unittest
        loader = unittest.TestLoader()
        if os.path.isdir(test_dir):
            suite = loader.discover(test_dir)
        else:
            suite = loader.discover(".", pattern="test_*.py")
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(
        prog="photon",
        description="PhotonAPI CLI",
    )
    sub = parser.add_subparsers(dest="command")

    new_p = sub.add_parser("new", help="Create a new project")
    new_p.add_argument("name", help="Project name")
    new_p.add_argument("-t", "--template", default="api", choices=["api", "ml", "full"])

    run_p = sub.add_parser("run", help="Start development server")
    run_p.add_argument("--host", default="127.0.0.1")
    run_p.add_argument("--port", type=int, default=8000)
    run_p.add_argument("--no-reload", dest="reload", action="store_false")

    sub.add_parser("routes", help="List all routes")

    db_p = sub.add_parser("db", help="Database commands")
    db_sub = db_p.add_subparsers(dest="db_command")
    mig_p = db_sub.add_parser("migrate", help="Run migrations")
    mig_p.add_argument("--dir", default="migrations")
    rb_p = db_sub.add_parser("rollback", help="Rollback migrations")
    rb_p.add_argument("--steps", type=int, default=1)
    seed_p = db_sub.add_parser("seed", help="Seed database")
    seed_p.add_argument("--file", default="seeds.py")

    test_p = sub.add_parser("test", help="Run tests")
    test_p.add_argument("--dir", default="tests")

    args = parser.parse_args()

    commands = {
        "new": cmd_new,
        "run": cmd_run,
        "routes": cmd_routes,
        "test": cmd_test,
    }

    if args.command == "db":
        db_commands = {
            "migrate": cmd_db_migrate,
            "rollback": cmd_db_rollback,
            "seed": cmd_db_seed,
        }
        fn = db_commands.get(args.db_command)
        if fn:
            sys.exit(fn(args))
        else:
            db_p.print_help()
    elif args.command in commands:
        sys.exit(commands[args.command](args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
