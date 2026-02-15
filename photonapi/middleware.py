import time
import uuid
import gzip
import logging
import threading


class MiddlewarePipeline:
    def __init__(self):
        self._stack = []

    def add(self, middleware):
        self._stack.append(middleware)

    def run(self, req, res, final_handler):
        chain = list(self._stack)

        def execute(index):
            if index >= len(chain):
                return final_handler(req, res)

            mw = chain[index]
            called_next = False

            def next_mw():
                nonlocal called_next
                called_next = True
                return execute(index + 1)

            result = mw(req, res, next_mw)
            if not called_next:
                return result
            return result

        return execute(0)


class CORSMiddleware:
    def __init__(self, allow_origins="*", allow_methods=None, allow_headers=None,
                 expose_headers=None, max_age=86400, allow_credentials=False):
        self.allow_origins = allow_origins
        self.allow_methods = allow_methods or ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
        self.allow_headers = allow_headers or ["Content-Type", "Authorization", "X-Requested-With"]
        self.expose_headers = expose_headers or []
        self.max_age = max_age
        self.allow_credentials = allow_credentials

    def __call__(self, req, res, next_fn):
        origin = req.headers.get("Origin", "")

        if isinstance(self.allow_origins, list):
            allowed = origin if origin in self.allow_origins else ""
        else:
            allowed = self.allow_origins

        res.set_header("Access-Control-Allow-Origin", allowed)
        res.set_header("Access-Control-Allow-Methods", ", ".join(self.allow_methods))
        res.set_header("Access-Control-Allow-Headers", ", ".join(self.allow_headers))

        if self.expose_headers:
            res.set_header("Access-Control-Expose-Headers", ", ".join(self.expose_headers))
        if self.allow_credentials:
            res.set_header("Access-Control-Allow-Credentials", "true")

        res.set_header("Access-Control-Max-Age", str(self.max_age))

        if req.method == "OPTIONS":
            res.status_code = 204
            res.body = ""
            return res

        return next_fn()


class LoggingMiddleware:
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("photonapi")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                "\033[36m%(asctime)s\033[0m | %(message)s", datefmt="%H:%M:%S"
            ))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    def __call__(self, req, res, next_fn):
        start = time.perf_counter()
        result = next_fn()
        elapsed = (time.perf_counter() - start) * 1000
        status = res.status_code
        color = "\033[32m" if status < 400 else "\033[33m" if status < 500 else "\033[31m"
        self.logger.info(f"{color}{status}\033[0m {req.method:6s} {req.path} ({elapsed:.1f}ms)")
        return result


class SecurityHeadersMiddleware:
    def __init__(self, csp=None, hsts=False, hsts_max_age=31536000):
        self.csp = csp
        self.hsts = hsts
        self.hsts_max_age = hsts_max_age

    def __call__(self, req, res, next_fn):
        result = next_fn()
        res.set_header("X-Content-Type-Options", "nosniff")
        res.set_header("X-Frame-Options", "DENY")
        res.set_header("X-XSS-Protection", "1; mode=block")
        res.set_header("Referrer-Policy", "strict-origin-when-cross-origin")
        res.set_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.csp:
            res.set_header("Content-Security-Policy", self.csp)
        if self.hsts:
            res.set_header("Strict-Transport-Security",
                           f"max-age={self.hsts_max_age}; includeSubDomains")
        return result


class GZipMiddleware:
    def __init__(self, min_size=500, level=6):
        self.min_size = min_size
        self.level = level

    def __call__(self, req, res, next_fn):
        result = next_fn()
        accept_encoding = req.headers.get("Accept-Encoding", "")
        if "gzip" not in accept_encoding:
            return result

        body = res.body
        if isinstance(body, str):
            body = body.encode("utf-8")
        if len(body) < self.min_size:
            return result

        compressed = gzip.compress(body, compresslevel=self.level)
        if len(compressed) < len(body):
            res.body = compressed
            res.set_header("Content-Encoding", "gzip")
            res.set_header("Content-Length", str(len(compressed)))
            res.set_header("Vary", "Accept-Encoding")
        return result


class RequestIDMiddleware:
    def __init__(self, header="X-Request-ID", generator=None):
        self.header = header
        self.generator = generator or (lambda: str(uuid.uuid4()))

    def __call__(self, req, res, next_fn):
        request_id = req.headers.get(self.header) or self.generator()
        req.id = request_id
        res.set_header(self.header, request_id)
        return next_fn()


class TrustedProxyMiddleware:
    def __init__(self, trusted_proxies=None):
        self.trusted_proxies = set(trusted_proxies or ["127.0.0.1", "::1"])

    def __call__(self, req, res, next_fn):
        if req.remote_addr in self.trusted_proxies:
            forwarded = req.headers.get("X-Forwarded-For")
            if forwarded:
                req.remote_addr = forwarded.split(",")[0].strip()
            proto = req.headers.get("X-Forwarded-Proto")
            if proto:
                req.scheme = proto
            host = req.headers.get("X-Forwarded-Host")
            if host:
                req.host = host
        return next_fn()


class IPFilterMiddleware:
    def __init__(self, whitelist=None, blacklist=None):
        self.whitelist = set(whitelist) if whitelist else None
        self.blacklist = set(blacklist) if blacklist else None

    def __call__(self, req, res, next_fn):
        ip = req.remote_addr
        if self.blacklist and ip in self.blacklist:
            res.json({"error": "Access denied"}, 403)
            return res
        if self.whitelist and ip not in self.whitelist:
            res.json({"error": "Access denied"}, 403)
            return res
        return next_fn()


class TimeoutMiddleware:
    def __init__(self, timeout_seconds=30):
        self.timeout = timeout_seconds

    def __call__(self, req, res, next_fn):
        result = [None]
        error = [None]
        done = threading.Event()

        def run():
            try:
                result[0] = next_fn()
            except Exception as e:
                error[0] = e
            finally:
                done.set()

        t = threading.Thread(target=run)
        t.start()
        if not done.wait(timeout=self.timeout):
            res.json({"error": "Request timed out"}, 504)
            return res

        if error[0]:
            raise error[0]
        return result[0]


class SessionMiddleware:
    def __init__(self, session_manager):
        self._sm = session_manager

    def __call__(self, req, res, next_fn):
        session_id = req.cookies.get("session_id")
        if session_id:
            req.session = self._sm.get(session_id) or {}
        else:
            req.session = {}
        result = next_fn()
        if hasattr(req, "_session_modified") and req._session_modified:
            new_id = self._sm.create(req.session)
            res.set_cookie("session_id", new_id, httponly=True, samesite="Lax")
        return result
