import json
from datetime import datetime, timezone


class Response:
    def __init__(self, body="", status=200, content_type="text/html"):
        self.body = body
        self.status_code = status
        self._headers = {"Content-Type": content_type}
        self._cookies = []
        self._status_phrases = {
            200: "OK", 201: "Created", 204: "No Content",
            301: "Moved Permanently", 302: "Found", 304: "Not Modified",
            400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
            404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
            429: "Too Many Requests",
            500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable",
        }

    @property
    def status(self):
        phrase = self._status_phrases.get(self.status_code, "Unknown")
        return f"{self.status_code} {phrase}"

    def set_header(self, key, value):
        self._headers[key] = value
        return self

    def get_header(self, key):
        return self._headers.get(key)

    def set_cookie(self, name, value, max_age=None, path="/", domain=None,
                   secure=False, httponly=False, samesite="Lax"):
        parts = [f"{name}={value}", f"Path={path}", f"SameSite={samesite}"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if domain:
            parts.append(f"Domain={domain}")
        if secure:
            parts.append("Secure")
        if httponly:
            parts.append("HttpOnly")
        self._cookies.append("; ".join(parts))
        return self

    def delete_cookie(self, name, path="/"):
        self.set_cookie(name, "", max_age=0, path=path)
        return self

    def html(self, content, status=200):
        self.body = content
        self.status_code = status
        self._headers["Content-Type"] = "text/html; charset=utf-8"
        return self

    def text(self, content, status=200):
        self.body = content
        self.status_code = status
        self._headers["Content-Type"] = "text/plain; charset=utf-8"
        return self

    def json(self, data, status=200):
        self.body = json.dumps(data, default=str)
        self.status_code = status
        self._headers["Content-Type"] = "application/json; charset=utf-8"
        return self

    def redirect(self, url, permanent=False):
        self.status_code = 301 if permanent else 302
        self._headers["Location"] = url
        self.body = ""
        return self

    def send_file(self, filepath, content_type=None):
        import mimetypes
        if content_type is None:
            content_type, _ = mimetypes.guess_type(filepath)
            content_type = content_type or "application/octet-stream"
        with open(filepath, "rb") as f:
            self.body = f.read()
        self._headers["Content-Type"] = content_type
        return self

    def build_headers(self):
        headers = list(self._headers.items())
        for cookie in self._cookies:
            headers.append(("Set-Cookie", cookie))
        return headers

    def as_wsgi(self, start_response):
        body = self.body
        if isinstance(body, str):
            body = body.encode("utf-8")
        start_response(self.status, self.build_headers())
        return [body]

    def __repr__(self):
        return f"<Response {self.status}>"


class JSONResponse(Response):
    def __init__(self, data, status=200):
        super().__init__(content_type="application/json; charset=utf-8")
        self.body = json.dumps(data, default=str)
        self.status_code = status


class HTMLResponse(Response):
    def __init__(self, content, status=200):
        super().__init__(content_type="text/html; charset=utf-8")
        self.body = content
        self.status_code = status


class RedirectResponse(Response):
    def __init__(self, url, permanent=False):
        super().__init__()
        self.status_code = 301 if permanent else 302
        self._headers["Location"] = url
