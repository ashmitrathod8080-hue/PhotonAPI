import time
import uuid
import threading
import json
from functools import wraps
from collections import defaultdict


_trace_context = threading.local()


def get_current_span():
    return getattr(_trace_context, "current_span", None)


def set_current_span(span):
    _trace_context.current_span = span


class SpanContext:
    def __init__(self, trace_id=None, span_id=None, parent_id=None, sampled=True):
        self.trace_id = trace_id or uuid.uuid4().hex
        self.span_id = span_id or uuid.uuid4().hex[:16]
        self.parent_id = parent_id
        self.sampled = sampled


class Span:
    def __init__(self, name, context=None, kind="internal", attributes=None):
        self.name = name
        self.context = context or SpanContext()
        self.kind = kind
        self.attributes = attributes or {}
        self.events = []
        self.status = "ok"
        self.status_message = ""
        self.start_time = time.time()
        self.end_time = None
        self._children = []

    def set_attribute(self, key, value):
        self.attributes[key] = value
        return self

    def add_event(self, name, attributes=None):
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": attributes or {},
        })
        return self

    def set_status(self, status, message=""):
        self.status = status
        self.status_message = message

    def end(self):
        self.end_time = time.time()

    @property
    def duration_ms(self):
        end = self.end_time or time.time()
        return round((end - self.start_time) * 1000, 2)

    def to_dict(self):
        return {
            "trace_id": self.context.trace_id,
            "span_id": self.context.span_id,
            "parent_id": self.context.parent_id,
            "name": self.name,
            "kind": self.kind,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "status_message": self.status_message,
            "attributes": self.attributes,
            "events": self.events,
        }

    def __enter__(self):
        self._parent = get_current_span()
        if self._parent:
            self.context.parent_id = self._parent.context.span_id
            self.context.trace_id = self._parent.context.trace_id
            self._parent._children.append(self)
        set_current_span(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.set_status("error", str(exc_val))
            self.add_event("exception", {
                "type": exc_type.__name__,
                "message": str(exc_val),
            })
        self.end()
        set_current_span(self._parent)
        return False


class Tracer:
    def __init__(self, service_name="photonapi", exporter=None, sample_rate=1.0):
        self.service_name = service_name
        self.sample_rate = sample_rate
        self._exporters = []
        self._spans = []
        self._lock = threading.Lock()
        self._max_spans = 10000

        if exporter:
            self._exporters.append(exporter)

    def start_span(self, name, kind="internal", attributes=None):
        parent = get_current_span()
        ctx = SpanContext()
        if parent:
            ctx.trace_id = parent.context.trace_id
            ctx.parent_id = parent.context.span_id

        span = Span(name, ctx, kind, attributes)
        return span

    def _record_span(self, span):
        with self._lock:
            self._spans.append(span.to_dict())
            if len(self._spans) > self._max_spans:
                self._spans = self._spans[-self._max_spans:]

        for exporter in self._exporters:
            try:
                exporter.export(span)
            except Exception:
                pass

    def trace(self, name=None, kind="internal"):
        def decorator(fn):
            span_name = name or fn.__name__

            @wraps(fn)
            def wrapper(*args, **kwargs):
                span = self.start_span(span_name, kind=kind)
                with span:
                    span.set_attribute("function", fn.__name__)
                    span.set_attribute("module", fn.__module__)
                    result = fn(*args, **kwargs)
                    return result

            return wrapper
        return decorator

    def get_recent_traces(self, n=50):
        with self._lock:
            return self._spans[-n:]

    def add_exporter(self, exporter):
        self._exporters.append(exporter)

    def middleware(self):
        tracer = self

        def tracing_middleware(req, res, next_fn):
            trace_parent = req.headers.get("Traceparent")
            trace_id = None
            parent_id = None

            if trace_parent:
                parts = trace_parent.split("-")
                if len(parts) >= 3:
                    trace_id = parts[1]
                    parent_id = parts[2]

            span = tracer.start_span(
                f"{req.method} {req.path}",
                kind="server",
                attributes={
                    "http.method": req.method,
                    "http.url": req.path,
                    "http.host": req.host,
                    "http.scheme": req.scheme,
                    "http.remote_addr": req.remote_addr,
                    "service.name": tracer.service_name,
                }
            )

            if trace_id:
                span.context.trace_id = trace_id
            if parent_id:
                span.context.parent_id = parent_id

            with span:
                res.set_header("X-Trace-Id", span.context.trace_id)
                traceparent = f"00-{span.context.trace_id}-{span.context.span_id}-01"
                res.set_header("Traceparent", traceparent)

                result = next_fn()

                span.set_attribute("http.status_code", res.status_code)
                if res.status_code >= 400:
                    span.set_status("error", f"HTTP {res.status_code}")

                tracer._record_span(span)
                return result

        return tracing_middleware


class ConsoleExporter:
    def export(self, span):
        d = span.to_dict()
        status_icon = "✓" if d["status"] == "ok" else "✗"
        print(f"  TRACE {status_icon} {d['name']} ({d['duration_ms']}ms) [{d['trace_id'][:8]}]")


class JaegerExporter:
    def __init__(self, endpoint="http://localhost:14268/api/traces"):
        self.endpoint = endpoint

    def export(self, span):
        import urllib.request
        d = span.to_dict()
        payload = json.dumps({
            "batch": [{
                "process": {"serviceName": span.attributes.get("service.name", "photonapi")},
                "spans": [{
                    "traceId": d["trace_id"],
                    "spanId": d["span_id"],
                    "parentSpanId": d["parent_id"] or "",
                    "operationName": d["name"],
                    "startTime": int(d["start_time"] * 1_000_000),
                    "duration": int(d["duration_ms"] * 1000),
                    "tags": [{"key": k, "value": str(v)} for k, v in d["attributes"].items()],
                }]
            }]
        }).encode()

        try:
            req = urllib.request.Request(
                self.endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass


class ZipkinExporter:
    def __init__(self, endpoint="http://localhost:9411/api/v2/spans"):
        self.endpoint = endpoint

    def export(self, span):
        import urllib.request
        d = span.to_dict()
        zipkin_span = [{
            "traceId": d["trace_id"],
            "id": d["span_id"],
            "name": d["name"],
            "timestamp": int(d["start_time"] * 1_000_000),
            "duration": int(d["duration_ms"] * 1000),
            "localEndpoint": {"serviceName": span.attributes.get("service.name", "photonapi")},
            "tags": {k: str(v) for k, v in d["attributes"].items()},
        }]
        if d["parent_id"]:
            zipkin_span[0]["parentId"] = d["parent_id"]

        try:
            req = urllib.request.Request(
                self.endpoint,
                data=json.dumps(zipkin_span).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass
