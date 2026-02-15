import os
import re
import json
import signal
import traceback
import mimetypes
import threading
import time

from .request import Request
from .response import Response, JSONResponse, HTMLResponse
from .router import Router, Blueprint
from .middleware import MiddlewarePipeline
from .streaming import StreamResponse


class PhotonAPI(Router):
    def __init__(self, debug=False, static_dir=None, static_url="/static",
                 template_dir=None, docs_url="/docs", title="PhotonAPI",
                 version="1.0.0", shutdown_timeout=30):
        super().__init__()
        self.debug = debug
        self.static_dir = static_dir
        self.static_url = static_url.rstrip("/")
        self.template_dir = template_dir
        self.docs_url = docs_url
        self.title = title
        self.version = version
        self.shutdown_timeout = shutdown_timeout
        self._middleware = MiddlewarePipeline()
        self._error_handlers = {}
        self._blueprints = []
        self._startup_hooks = []
        self._shutdown_hooks = []
        self._template_globals = {}
        self._started = False
        self._shutting_down = False
        self._active_requests = 0
        self._active_lock = threading.Lock()
        self.db = None
        self._task_queue = None
        self._scheduler = None
        self._server = None
        self._error_handler = None
        self.state = {}

        if self.docs_url:
            self._register_docs_route()

    def use(self, middleware):
        self._middleware.add(middleware)
        return self

    def register(self, blueprint):
        self._blueprints.append(blueprint)
        for route in blueprint.routes:
            full_path = blueprint.prefix + route.path
            self.add_route(full_path, route.handler, route.methods, route.name)
        return self

    def error(self, status_code_or_exception):
        def decorator(fn):
            if isinstance(status_code_or_exception, int):
                self._error_handlers[status_code_or_exception] = fn
            else:
                self._error_handlers[status_code_or_exception] = fn
            return fn
        return decorator

    def init_db(self, path="app.db", backend="sqlite", **kwargs):
        from .database import Database
        self.db = Database(path, backend=backend, **kwargs)
        return self.db

    def enable_tasks(self, workers=2, max_retries=0):
        from .tasks import TaskQueue
        self._task_queue = TaskQueue(workers=workers, max_retries=max_retries)
        return self._task_queue

    def enable_scheduler(self):
        from .tasks import get_scheduler
        self._scheduler = get_scheduler()
        return self._scheduler

    def enable_error_handler(self, debug=None):
        from .errors import ErrorHandler
        self._error_handler = ErrorHandler(
            app=self,
            debug=debug if debug is not None else self.debug
        )
        return self._error_handler

    def _register_docs_route(self):
        docs_url = self.docs_url

        def docs_handler(req, res):
            from .docs import generate_docs_html
            html = generate_docs_html(self, title=self.title, version=self.version)
            res.html(html)
            return res
        self.add_route(docs_url, docs_handler, ["GET"], "docs")

    def on_startup(self, fn):
        self._startup_hooks.append(fn)
        return fn

    def on_shutdown(self, fn):
        self._shutdown_hooks.append(fn)
        return fn

    def template_global(self, name=None):
        def decorator(fn):
            self._template_globals[name or fn.__name__] = fn
            return fn
        return decorator

    def render(self, template_name, **context):
        if self.template_dir is None:
            raise RuntimeError("template_dir not set on PhotonAPI instance")

        filepath = os.path.join(self.template_dir, template_name)
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Template not found: {template_name}")

        with open(filepath, "r") as f:
            template = f.read()

        ctx = {**self._template_globals, **context}
        return self._render_template(template, ctx)

    def _render_template(self, template, context):
        def replace_blocks(match):
            expr = match.group(1).strip()
            try:
                return str(eval(expr, {"__builtins__": {}}, context))
            except Exception:
                return ""

        def replace_conditionals(match):
            condition = match.group(1).strip()
            true_block = match.group(2)
            else_block = match.group(3) if match.group(3) else ""
            try:
                if eval(condition, {"__builtins__": {}}, context):
                    return true_block
                return else_block
            except Exception:
                return ""

        def replace_loops(match):
            var_name = match.group(1).strip()
            iterable_name = match.group(2).strip()
            body = match.group(3)
            result = []
            iterable = context.get(iterable_name, [])
            for item in iterable:
                loop_ctx = {**context, var_name: item}
                rendered = self._render_template(body, loop_ctx)
                result.append(rendered)
            return "".join(result)

        result = re.sub(
            r'\{%\s*for\s+(\w+)\s+in\s+(\w+)\s*%\}(.*?)\{%\s*endfor\s*%\}',
            replace_loops, template, flags=re.DOTALL
        )

        result = re.sub(
            r'\{%\s*if\s+(.*?)\s*%\}(.*?)(?:\{%\s*else\s*%\}(.*?))?\{%\s*endif\s*%\}',
            replace_conditionals, result, flags=re.DOTALL
        )

        result = re.sub(r'\{\{\s*(.*?)\s*\}\}', replace_blocks, result)
        return result

    def _serve_static(self, req, res):
        if self.static_dir is None:
            return False

        if not req.path.startswith(self.static_url + "/"):
            return False

        relative_path = req.path[len(self.static_url) + 1:]
        file_path = os.path.join(self.static_dir, relative_path)
        file_path = os.path.normpath(file_path)

        if not file_path.startswith(os.path.normpath(self.static_dir)):
            res.status_code = 403
            res.text("Forbidden", 403)
            return True

        if not os.path.isfile(file_path):
            return False

        content_type, _ = mimetypes.guess_type(file_path)
        res.send_file(file_path, content_type)
        return True

    def _handle_error(self, req, res, status_code, exception=None):
        if self._error_handler and exception:
            for exc_type in type(exception).__mro__:
                if exc_type in self._error_handler._handlers:
                    handler_result = self._error_handler._handlers[exc_type](exception, req)
                    if isinstance(handler_result, dict):
                        res.json(handler_result, getattr(exception, "status_code", 500))
                    return

            if hasattr(exception, "status_code"):
                from .errors import PhotonError
                if isinstance(exception, PhotonError):
                    res.json(exception.to_dict(), exception.status_code)
                    return

        if status_code in self._error_handlers:
            handler = self._error_handlers[status_code]
            handler(req, res, exception)
            return

        if status_code == 404:
            res.json({"error": "Not Found", "path": req.path}, 404)
        elif status_code == 405:
            res.json({"error": "Method Not Allowed", "method": req.method, "path": req.path}, 405)
        elif status_code == 500:
            if self.debug and exception:
                res.json({
                    "error": "Internal Server Error",
                    "exception": str(exception),
                    "traceback": traceback.format_exc()
                }, 500)
            else:
                res.json({"error": "Internal Server Error"}, 500)

    def __call__(self, environ, start_response):
        req = Request(environ)
        res = Response()

        with self._active_lock:
            self._active_requests += 1

        try:
            if self._shutting_down:
                res.json({"error": "Server is shutting down"}, 503)
                res.set_header("Connection", "close")
                return res.as_wsgi(start_response)

            if self._serve_static(req, res):
                return res.as_wsgi(start_response)

            def dispatch(req, res):
                result, params = self.resolve(req.path, req.method)

                if result is None:
                    self._handle_error(req, res, 404)
                    return res

                if result == "METHOD_NOT_ALLOWED":
                    self._handle_error(req, res, 405)
                    return res

                route = result
                req.params = params

                for hook in self._before_hooks:
                    hook_result = hook(req, res)
                    if hook_result is not None:
                        return hook_result

                try:
                    handler_result = route.handler(req, res, **params)
                except Exception as e:
                    self._handle_error(req, res, 500, e)
                    return res

                if isinstance(handler_result, StreamResponse):
                    return handler_result
                elif isinstance(handler_result, Response):
                    res = handler_result
                elif isinstance(handler_result, dict):
                    res.json(handler_result)
                elif isinstance(handler_result, str):
                    res.html(handler_result)
                elif isinstance(handler_result, tuple) and len(handler_result) == 2:
                    body, status = handler_result
                    if isinstance(body, dict):
                        res.json(body, status)
                    else:
                        res.html(str(body), status)

                for hook in self._after_hooks:
                    hook(req, res)

                return res

            result = self._middleware.run(req, res, dispatch)

            if isinstance(result, StreamResponse):
                hop_by_hop = {"connection", "keep-alive", "proxy-authenticate",
                              "proxy-authorization", "te", "trailers",
                              "transfer-encoding", "upgrade"}
                status_line = f"{result.status} OK"
                headers = [("Content-Type", result.content_type)]
                for k, v in result.headers.items():
                    if k.lower() not in hop_by_hop:
                        headers.append((k, v))
                start_response(status_line, headers)
                return result

            return res.as_wsgi(start_response)
        finally:
            with self._active_lock:
                self._active_requests -= 1

    def _graceful_shutdown(self, signum=None, frame=None):
        if self._shutting_down:
            return

        self._shutting_down = True
        print("\n  \033[33m⏳\033[0m Graceful shutdown initiated...")

        deadline = time.time() + self.shutdown_timeout
        while self._active_requests > 0 and time.time() < deadline:
            time.sleep(0.1)

        if self._active_requests > 0:
            print(f"  \033[31m⚠\033[0m  {self._active_requests} requests still in flight, forcing shutdown")

        if self._task_queue:
            self._task_queue.shutdown(wait=True)
        if self._scheduler:
            self._scheduler.shutdown()
        if self.db:
            self.db.close()

        for hook in self._shutdown_hooks:
            try:
                hook()
            except Exception:
                pass

        print("  \033[32m✓\033[0m  Shutdown complete")

        if self._server:
            self._server.shutdown()

    def run(self, host="127.0.0.1", port=8000, reload=False, workers=None):
        if reload:
            from .reloader import run_with_reload
            import sys
            script = sys.argv[0]
            run_with_reload(script, watch_dirs=["."])
            return

        from wsgiref.simple_server import make_server, WSGIServer
        WSGIServer.allow_reuse_address = True

        for hook in self._startup_hooks:
            hook()

        self._started = True

        signal.signal(signal.SIGINT, self._graceful_shutdown)
        signal.signal(signal.SIGTERM, self._graceful_shutdown)

        docs_msg = f" | Docs: http://{host}:{port}{self.docs_url}" if self.docs_url else ""
        route_count = len(self.routes)
        print(f"\n  \033[1m⚡ PhotonAPI v{self.version}\033[0m running at \033[4mhttp://{host}:{port}\033[0m")
        print(f"  \033[2m  Routes: {route_count} | Debug: {self.debug}{docs_msg}\033[0m\n")

        self._server = make_server(host, port, self)

        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if not self._shutting_down:
                self._graceful_shutdown()
