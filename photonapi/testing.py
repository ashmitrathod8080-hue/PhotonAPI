import json
import io
import time
import copy
import threading
from urllib.parse import urlencode
from functools import wraps


class TestResponse:
    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = dict(headers)
        self._body = body

    @property
    def text(self):
        if isinstance(self._body, bytes):
            return self._body.decode("utf-8")
        return self._body

    @property
    def json(self):
        return json.loads(self.text)

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def __repr__(self):
        return f"<TestResponse {self.status_code}>"


class TestClient:
    def __init__(self, app):
        self.app = app
        self.cookies = {}

    def _build_environ(self, method, path, body=None, headers=None,
                       content_type=None, query_string=""):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            content_type = content_type or "application/json"
        elif isinstance(body, str):
            body = body.encode()
        elif body is None:
            body = b""

        environ = {
            "REQUEST_METHOD": method.upper(),
            "PATH_INFO": path,
            "QUERY_STRING": query_string,
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "HTTP_HOST": "testserver",
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": content_type or "",
            "REMOTE_ADDR": "127.0.0.1",
        }

        if self.cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            environ["HTTP_COOKIE"] = cookie_str

        if headers:
            for key, value in headers.items():
                wsgi_key = f"HTTP_{key.upper().replace('-', '_')}"
                environ[wsgi_key] = value

        return environ

    def _make_request(self, method, path, body=None, headers=None,
                      content_type=None, query=None):
        qs = urlencode(query) if query else ""
        environ = self._build_environ(method, path, body, headers, content_type, qs)

        response_started = []

        def start_response(status, response_headers, exc_info=None):
            status_code = int(status.split(" ", 1)[0])
            response_started.append((status_code, response_headers))

        result = self.app(environ, start_response)

        status_code, resp_headers = response_started[0]

        response_body = b""
        for chunk in result:
            if isinstance(chunk, bytes):
                response_body += chunk
            elif isinstance(chunk, str):
                response_body += chunk.encode()

        resp = TestResponse(status_code, resp_headers, response_body)

        for key, value in resp_headers:
            if key == "Set-Cookie":
                parts = value.split(";")
                if "=" in parts[0]:
                    name, val = parts[0].split("=", 1)
                    if "Max-Age=0" in value:
                        self.cookies.pop(name.strip(), None)
                    else:
                        self.cookies[name.strip()] = val.strip()

        return resp

    def get(self, path, headers=None, query=None):
        return self._make_request("GET", path, headers=headers, query=query)

    def post(self, path, json=None, data=None, headers=None, content_type=None):
        body = json if json is not None else data
        if json is not None:
            content_type = content_type or "application/json"
        return self._make_request("POST", path, body=body, headers=headers, content_type=content_type)

    def put(self, path, json=None, data=None, headers=None, content_type=None):
        body = json if json is not None else data
        if json is not None:
            content_type = content_type or "application/json"
        return self._make_request("PUT", path, body=body, headers=headers, content_type=content_type)

    def patch(self, path, json=None, data=None, headers=None, content_type=None):
        body = json if json is not None else data
        if json is not None:
            content_type = content_type or "application/json"
        return self._make_request("PATCH", path, body=body, headers=headers, content_type=content_type)

    def delete(self, path, headers=None, query=None):
        return self._make_request("DELETE", path, headers=headers, query=query)

    def options(self, path, headers=None):
        return self._make_request("OPTIONS", path, headers=headers)


class MockModel:
    def __init__(self, predictions=None, latency=0):
        self._predictions = predictions or {"label": "mock", "confidence": 1.0}
        self._latency = latency
        self._calls = []

    def preprocess(self, data):
        return data

    def predict(self, data):
        self._calls.append(data)
        if self._latency:
            time.sleep(self._latency)
        if callable(self._predictions):
            return self._predictions(data)
        return copy.deepcopy(self._predictions)

    @property
    def call_count(self):
        return len(self._calls)

    @property
    def last_call(self):
        return self._calls[-1] if self._calls else None


class Factory:
    def __init__(self, model=None, defaults=None):
        self._model = model
        self._defaults = defaults or {}
        self._sequence = 0
        self._lock = threading.Lock()

    def _next_seq(self):
        with self._lock:
            self._sequence += 1
            return self._sequence

    def build(self, **overrides):
        data = {}
        seq = self._next_seq()
        for key, value in self._defaults.items():
            if callable(value):
                data[key] = value(seq)
            else:
                data[key] = value
        data.update(overrides)
        return data

    def create(self, **overrides):
        data = self.build(**overrides)
        if self._model:
            return self._model.create(**data)
        return data

    def create_batch(self, count, **overrides):
        return [self.create(**overrides) for _ in range(count)]


class DatabaseFixture:
    def __init__(self, db):
        self.db = db
        self._tables_state = {}

    def snapshot(self):
        for name in self.db._models:
            rows = self.db.query(f"SELECT * FROM {name}")
            self._tables_state[name] = rows

    def restore(self):
        for name in self._tables_state:
            self.db.execute(f"DELETE FROM {name}")
            for row in self._tables_state[name]:
                cols = [k for k in row.keys() if k != "id"]
                vals = [row[k] for k in cols]
                placeholders = ", ".join(["?"] * len(cols))
                self.db.execute(
                    f"INSERT INTO {name} ({', '.join(cols)}) VALUES ({placeholders})",
                    vals
                )

    def reset(self):
        for name in self.db._models:
            self.db.execute(f"DELETE FROM {name}")

    def seed(self, model, data_list):
        created = []
        for data in data_list:
            created.append(model.create(**data))
        return created


class LoadTester:
    def __init__(self, client):
        self.client = client

    def run(self, method, path, n=100, concurrency=10, body=None, headers=None):
        results = {"total": n, "success": 0, "failure": 0, "latencies": []}
        errors = []
        lock = threading.Lock()

        def make_request():
            start = time.perf_counter()
            try:
                if method.upper() == "GET":
                    resp = self.client.get(path, headers=headers)
                elif method.upper() == "POST":
                    resp = self.client.post(path, json=body, headers=headers)
                elif method.upper() == "PUT":
                    resp = self.client.put(path, json=body, headers=headers)
                elif method.upper() == "DELETE":
                    resp = self.client.delete(path, headers=headers)
                else:
                    resp = self.client.get(path, headers=headers)

                elapsed = time.perf_counter() - start
                with lock:
                    results["latencies"].append(elapsed * 1000)
                    if resp.ok:
                        results["success"] += 1
                    else:
                        results["failure"] += 1
            except Exception as e:
                with lock:
                    results["failure"] += 1
                    errors.append(str(e))

        threads = []
        for i in range(n):
            t = threading.Thread(target=make_request)
            threads.append(t)

        batch_size = concurrency
        for i in range(0, len(threads), batch_size):
            batch = threads[i:i + batch_size]
            for t in batch:
                t.start()
            for t in batch:
                t.join()

        latencies = sorted(results["latencies"])
        ln = len(latencies)
        if ln > 0:
            results["avg_ms"] = round(sum(latencies) / ln, 2)
            results["min_ms"] = round(latencies[0], 2)
            results["max_ms"] = round(latencies[-1], 2)
            results["p50_ms"] = round(latencies[int(ln * 0.5)], 2)
            results["p90_ms"] = round(latencies[int(ln * 0.9)], 2) if ln > 1 else results["avg_ms"]
            results["p95_ms"] = round(latencies[int(ln * 0.95)], 2) if ln > 1 else results["avg_ms"]
            results["p99_ms"] = round(latencies[int(ln * 0.99)], 2) if ln > 1 else results["avg_ms"]
            results["rps"] = round(ln / (sum(latencies) / 1000), 1) if sum(latencies) > 0 else 0
        else:
            results["avg_ms"] = 0
            results["rps"] = 0

        del results["latencies"]
        if errors:
            results["errors"] = errors[:10]

        return results


def assert_status(response, expected):
    assert response.status_code == expected, \
        f"Expected status {expected}, got {response.status_code}: {response.text}"


def assert_json_contains(response, expected_subset):
    data = response.json
    for key, value in expected_subset.items():
        assert key in data, f"Key '{key}' not found in response"
        assert data[key] == value, f"Expected {key}={value}, got {data[key]}"


def assert_header(response, header, expected_value=None):
    val = response.headers.get(header)
    assert val is not None, f"Header '{header}' not found"
    if expected_value is not None:
        assert val == expected_value, f"Expected header {header}={expected_value}, got {val}"
