import re
from .response import Response


class Route:
    def __init__(self, path, handler, methods, name=None):
        self.path = path
        self.handler = handler
        self.methods = [m.upper() for m in methods]
        self.name = name or handler.__name__
        self._regex, self._param_names = self._compile(path)

    def _compile(self, path):
        param_names = []
        pattern = "^"
        for segment in path.strip("/").split("/"):
            if not segment:
                continue
            pattern += "/"
            if segment.startswith("<") and segment.endswith(">"):
                inner = segment[1:-1]
                if ":" in inner:
                    type_hint, pname = inner.split(":", 1)
                else:
                    type_hint, pname = "str", inner
                param_names.append((pname, type_hint))
                if type_hint == "int":
                    pattern += r"(\d+)"
                elif type_hint == "path":
                    pattern += r"(.+)"
                else:
                    pattern += r"([^/]+)"
            else:
                pattern += re.escape(segment)
        if not path.strip("/"):
            pattern += "/"
        pattern += "$"
        return re.compile(pattern), param_names

    def match(self, path):
        if not path.endswith("/"):
            path = path + "/"
        check_path = path if path != "/" else "/"
        if check_path == "/" and self.path == "/":
            return {}

        m = self._regex.match(path.rstrip("/") if path != "/" else "/")
        if not m:
            m = self._regex.match(path)
        if not m:
            return None

        params = {}
        for (pname, ptype), value in zip(self._param_names, m.groups()):
            if ptype == "int":
                try:
                    params[pname] = int(value)
                except ValueError:
                    return None
            else:
                params[pname] = value
        return params


class Router:
    def __init__(self):
        self.routes = []
        self._before_hooks = []
        self._after_hooks = []

    def add_route(self, path, handler, methods=None, name=None):
        methods = methods or ["GET"]
        route = Route(path, handler, methods, name)
        self.routes.append(route)
        return route

    def resolve(self, path, method):
        method_mismatch = False
        for route in self.routes:
            params = route.match(path)
            if params is not None:
                if method.upper() in route.methods:
                    return route, params
                else:
                    method_mismatch = True
        if method_mismatch:
            return "METHOD_NOT_ALLOWED", None
        return None, None

    def before_request(self, fn):
        self._before_hooks.append(fn)
        return fn

    def after_request(self, fn):
        self._after_hooks.append(fn)
        return fn

    def _make_decorator(self, path, methods, name=None):
        def decorator(fn):
            self.add_route(path, fn, methods, name)
            return fn
        return decorator

    def get(self, path, name=None):
        return self._make_decorator(path, ["GET"], name)

    def post(self, path, name=None):
        return self._make_decorator(path, ["POST"], name)

    def put(self, path, name=None):
        return self._make_decorator(path, ["PUT"], name)

    def delete(self, path, name=None):
        return self._make_decorator(path, ["DELETE"], name)

    def patch(self, path, name=None):
        return self._make_decorator(path, ["PATCH"], name)

    def route(self, path, methods=None, name=None):
        methods = methods or ["GET"]
        return self._make_decorator(path, methods, name)


class Blueprint(Router):
    def __init__(self, name, prefix=""):
        super().__init__()
        self.name = name
        self.prefix = prefix.rstrip("/")
        self._middleware = []

    def use(self, middleware_fn):
        self._middleware.append(middleware_fn)
        return middleware_fn
