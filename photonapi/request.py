import json
from urllib.parse import parse_qs, urlparse


class Request:
    def __init__(self, environ):
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "/")
        self.query_string = environ.get("QUERY_STRING", "")
        self.content_type = environ.get("CONTENT_TYPE", "")
        self.content_length = int(environ.get("CONTENT_LENGTH") or 0)
        self.host = environ.get("HTTP_HOST", "localhost")
        self.scheme = environ.get("wsgi.url_scheme", "http")
        self.remote_addr = environ.get("REMOTE_ADDR", "127.0.0.1")
        self.params = {}

        self._body = None
        self._json = None
        self._form = None
        self._headers = None
        self._cookies = None

    @property
    def headers(self):
        if self._headers is None:
            self._headers = {}
            for key, value in self.environ.items():
                if key.startswith("HTTP_"):
                    header_name = key[5:].replace("_", "-").title()
                    self._headers[header_name] = value
                elif key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                    header_name = key.replace("_", "-").title()
                    self._headers[header_name] = value
            self._headers = CaseInsensitiveDict(self._headers)
        return self._headers

    @property
    def cookies(self):
        if self._cookies is None:
            self._cookies = {}
            raw = self.environ.get("HTTP_COOKIE", "")
            if raw:
                for chunk in raw.split(";"):
                    chunk = chunk.strip()
                    if "=" in chunk:
                        k, v = chunk.split("=", 1)
                        self._cookies[k.strip()] = v.strip()
        return self._cookies

    @property
    def body(self):
        if self._body is None:
            try:
                wsgi_input = self.environ.get("wsgi.input")
                self._body = wsgi_input.read(self.content_length) if wsgi_input else b""
            except Exception:
                self._body = b""
        return self._body

    @property
    def json(self):
        if self._json is None:
            try:
                self._json = json.loads(self.body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json = None
        return self._json

    @property
    def form(self):
        if self._form is None:
            if "application/x-www-form-urlencoded" in self.content_type:
                self._form = parse_qs(self.body.decode("utf-8"))
            else:
                self._form = {}
        return self._form

    @property
    def query(self):
        return parse_qs(self.query_string)

    def get_query(self, key, default=None):
        vals = self.query.get(key)
        return vals[0] if vals else default

    @property
    def url(self):
        return f"{self.scheme}://{self.host}{self.path}"

    @property
    def is_json(self):
        return "application/json" in self.content_type

    @property
    def is_xhr(self):
        return self.headers.get("X-Requested-With") == "XMLHttpRequest"

    def __repr__(self):
        return f"<Request {self.method} {self.path}>"


class CaseInsensitiveDict(dict):
    def __getitem__(self, key):
        return super().__getitem__(key.title())

    def __setitem__(self, key, value):
        super().__setitem__(key.title(), value)

    def __contains__(self, key):
        return super().__contains__(key.title())

    def get(self, key, default=None):
        return super().get(key.title(), default)
