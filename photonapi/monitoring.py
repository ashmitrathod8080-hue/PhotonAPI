import time
import threading
import os
import json
from collections import defaultdict
from functools import wraps


class Counter:
    def __init__(self, name, description="", labels=None):
        self.name = name
        self.description = description
        self._labels = labels or []
        self._values = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, amount=1, **labels):
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] += amount

    def get(self, **labels):
        key = tuple(sorted(labels.items()))
        return self._values.get(key, 0)

    def collect(self):
        with self._lock:
            return dict(self._values)


class Gauge:
    def __init__(self, name, description="", labels=None):
        self.name = name
        self.description = description
        self._labels = labels or []
        self._values = defaultdict(float)
        self._lock = threading.Lock()

    def set(self, value, **labels):
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = value

    def inc(self, amount=1, **labels):
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] += amount

    def dec(self, amount=1, **labels):
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] -= amount

    def get(self, **labels):
        key = tuple(sorted(labels.items()))
        return self._values.get(key, 0)

    def collect(self):
        with self._lock:
            return dict(self._values)


class Histogram:
    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self, name, description="", labels=None, buckets=None):
        self.name = name
        self.description = description
        self._labels = labels or []
        self._buckets = buckets or self.DEFAULT_BUCKETS
        self._observations = defaultdict(list)
        self._lock = threading.Lock()

    def observe(self, value, **labels):
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._observations[key].append(value)

    def percentile(self, p, **labels):
        key = tuple(sorted(labels.items()))
        with self._lock:
            vals = sorted(self._observations.get(key, []))
        if not vals:
            return 0
        idx = int(len(vals) * p / 100)
        return vals[min(idx, len(vals) - 1)]

    def collect(self):
        result = {}
        with self._lock:
            for key, vals in self._observations.items():
                sorted_vals = sorted(vals)
                n = len(sorted_vals)
                if n == 0:
                    continue
                result[key] = {
                    "count": n,
                    "sum": sum(sorted_vals),
                    "avg": sum(sorted_vals) / n,
                    "min": sorted_vals[0],
                    "max": sorted_vals[-1],
                    "p50": sorted_vals[int(n * 0.5)],
                    "p90": sorted_vals[int(n * 0.9)] if n > 1 else sorted_vals[0],
                    "p95": sorted_vals[int(n * 0.95)] if n > 1 else sorted_vals[0],
                    "p99": sorted_vals[int(n * 0.99)] if n > 1 else sorted_vals[0],
                    "buckets": {str(b): sum(1 for v in sorted_vals if v <= b) for b in self._buckets},
                }
        return result


class MetricsRegistry:
    def __init__(self):
        self._counters = {}
        self._gauges = {}
        self._histograms = {}
        self._custom = {}

    def counter(self, name, description="", labels=None):
        if name not in self._counters:
            self._counters[name] = Counter(name, description, labels)
        return self._counters[name]

    def gauge(self, name, description="", labels=None):
        if name not in self._gauges:
            self._gauges[name] = Gauge(name, description, labels)
        return self._gauges[name]

    def histogram(self, name, description="", labels=None, buckets=None):
        if name not in self._histograms:
            self._histograms[name] = Histogram(name, description, labels, buckets)
        return self._histograms[name]

    def collect_all(self):
        result = {}
        for name, c in self._counters.items():
            result[name] = {"type": "counter", "values": self._format_metric(c.collect())}
        for name, g in self._gauges.items():
            result[name] = {"type": "gauge", "values": self._format_metric(g.collect())}
        for name, h in self._histograms.items():
            result[name] = {"type": "histogram", "values": self._format_metric(h.collect())}
        return result

    def _format_metric(self, data):
        formatted = {}
        for key, value in data.items():
            label_str = ",".join(f'{k}="{v}"' for k, v in key) if key else "default"
            formatted[label_str] = value
        return formatted

    def to_prometheus(self):
        lines = []
        for name, c in self._counters.items():
            lines.append(f"# HELP {name} {c.description}")
            lines.append(f"# TYPE {name} counter")
            for key, val in c.collect().items():
                labels = "{" + ",".join(f'{k}="{v}"' for k, v in key) + "}" if key else ""
                lines.append(f"{name}{labels} {val}")

        for name, g in self._gauges.items():
            lines.append(f"# HELP {name} {g.description}")
            lines.append(f"# TYPE {name} gauge")
            for key, val in g.collect().items():
                labels = "{" + ",".join(f'{k}="{v}"' for k, v in key) + "}" if key else ""
                lines.append(f"{name}{labels} {val}")

        for name, h in self._histograms.items():
            lines.append(f"# HELP {name} {h.description}")
            lines.append(f"# TYPE {name} histogram")
            for key, data in h.collect().items():
                labels = ",".join(f'{k}="{v}"' for k, v in key) if key else ""
                base_labels = "{" + labels + "}" if labels else ""
                for bucket, count in data.get("buckets", {}).items():
                    le_labels = "{" + (labels + "," if labels else "") + f'le="{bucket}"' + "}"
                    lines.append(f"{name}_bucket{le_labels} {count}")
                inf_labels = "{" + (labels + "," if labels else "") + 'le="+Inf"' + "}"
                lines.append(f"{name}_bucket{inf_labels} {data['count']}")
                lines.append(f"{name}_sum{base_labels} {data['sum']}")
                lines.append(f"{name}_count{base_labels} {data['count']}")

        return "\n".join(lines) + "\n"


class MetricsCollector:
    def __init__(self, app=None, endpoint="/metrics", enable_default=True):
        self.registry = MetricsRegistry()
        self._start_time = time.time()
        self.app = app

        if enable_default:
            self.request_count = self.registry.counter(
                "http_requests_total", "Total HTTP requests",
                ["method", "endpoint", "status"]
            )
            self.request_latency = self.registry.histogram(
                "http_request_duration_seconds", "Request latency in seconds",
                ["method", "endpoint"]
            )
            self.active_connections = self.registry.gauge(
                "http_active_connections", "Active connections"
            )
            self.response_size = self.registry.histogram(
                "http_response_size_bytes", "Response body size",
                ["method", "endpoint"]
            )

        if app:
            self.init_app(app, endpoint)

    def init_app(self, app, endpoint="/metrics"):
        self.app = app
        collector = self

        @app.get(endpoint)
        def metrics_endpoint(req, res):
            accept = req.headers.get("Accept", "")
            if "application/json" in accept:
                return collector.registry.collect_all()
            res.text(collector.registry.to_prometheus())
            res.set_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            return res

        @app.get(f"{endpoint}/json")
        def metrics_json(req, res):
            data = collector.registry.collect_all()
            data["uptime_seconds"] = round(time.time() - collector._start_time, 1)
            data["process"] = {
                "pid": os.getpid(),
            }
            return data

    def middleware(self):
        collector = self

        def metrics_middleware(req, res, next_fn):
            collector.active_connections.inc()
            start = time.perf_counter()

            try:
                result = next_fn()
                return result
            finally:
                elapsed = time.perf_counter() - start
                collector.request_count.inc(
                    method=req.method, endpoint=req.path, status=str(res.status_code)
                )
                collector.request_latency.observe(
                    elapsed, method=req.method, endpoint=req.path
                )
                body = res.body
                if isinstance(body, str):
                    body = body.encode()
                if isinstance(body, bytes):
                    collector.response_size.observe(
                        len(body), method=req.method, endpoint=req.path
                    )
                collector.active_connections.dec()

        return metrics_middleware


class HealthCheck:
    def __init__(self, app=None):
        self._checks = {}
        self._startup_complete = False
        if app:
            self.init_app(app)

    def init_app(self, app):
        health = self

        @app.get("/health/live")
        def liveness(req, res):
            return {"status": "alive"}

        @app.get("/health/ready")
        def readiness(req, res):
            results = health.run_checks()
            all_ok = all(r["status"] == "healthy" for r in results.values())
            status_code = 200 if all_ok else 503
            overall = "ready" if all_ok else "degraded"
            return {"status": overall, "checks": results}, status_code

        @app.get("/health/startup")
        def startup(req, res):
            if health._startup_complete:
                return {"status": "started"}
            return {"status": "starting"}, 503

    def add_check(self, name, fn):
        self._checks[name] = fn
        return fn

    def check(self, name):
        def decorator(fn):
            self._checks[name] = fn
            return fn
        return decorator

    def mark_started(self):
        self._startup_complete = True

    def run_checks(self):
        results = {}
        for name, fn in self._checks.items():
            try:
                start = time.time()
                result = fn()
                elapsed = time.time() - start
                if isinstance(result, dict):
                    results[name] = {**result, "latency_ms": round(elapsed * 1000, 1)}
                elif result:
                    results[name] = {"status": "healthy", "latency_ms": round(elapsed * 1000, 1)}
                else:
                    results[name] = {"status": "unhealthy", "latency_ms": round(elapsed * 1000, 1)}
            except Exception as e:
                results[name] = {"status": "unhealthy", "error": str(e)}
        return results

    def check_database(self, db):
        def _check():
            try:
                db.query("SELECT 1")
                return {"status": "healthy"}
            except Exception as e:
                return {"status": "unhealthy", "error": str(e)}
        self._checks["database"] = _check

    def check_redis(self, redis_client):
        def _check():
            try:
                redis_client.ping()
                return {"status": "healthy"}
            except Exception as e:
                return {"status": "unhealthy", "error": str(e)}
        self._checks["redis"] = _check

    def check_disk(self, path="/", min_free_gb=1):
        def _check():
            try:
                stat = os.statvfs(path)
                free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
                return {
                    "status": "healthy" if free_gb > min_free_gb else "unhealthy",
                    "free_gb": round(free_gb, 2),
                }
            except Exception as e:
                return {"status": "unhealthy", "error": str(e)}
        self._checks["disk"] = _check

    def check_memory(self, max_percent=90):
        def _check():
            try:
                import resource
                usage = resource.getrusage(resource.RUSAGE_SELF)
                mem_mb = usage.ru_maxrss / 1024
                if os.uname().sysname == "Darwin":
                    mem_mb = usage.ru_maxrss / (1024 * 1024)
                return {"status": "healthy", "rss_mb": round(mem_mb, 1)}
            except Exception as e:
                return {"status": "unknown", "error": str(e)}
        self._checks["memory"] = _check

    def check_models(self, model_registry):
        def _check():
            all_healthy = True
            details = {}
            for name, entry in model_registry.entries.items():
                healthy = entry.loaded
                details[name] = "loaded" if healthy else "not_loaded"
                if not healthy:
                    all_healthy = False
            return {"status": "healthy" if all_healthy else "unhealthy", "models": details}
        self._checks["models"] = _check
