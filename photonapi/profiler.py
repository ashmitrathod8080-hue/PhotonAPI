import time
import threading
import cProfile
import pstats
import io
import tracemalloc
from functools import wraps
from collections import defaultdict


class RequestProfiler:
    def __init__(self, app=None, enabled=True, slow_threshold_ms=500):
        self.enabled = enabled
        self.slow_threshold = slow_threshold_ms / 1000
        self._profiles = []
        self._slow_requests = []
        self._lock = threading.Lock()
        self._max_profiles = 1000

        if app:
            self.init_app(app)

    def init_app(self, app):
        profiler = self

        @app.get("/debug/profiler")
        def profiler_stats(req, res):
            return {
                "total_profiled": len(profiler._profiles),
                "slow_requests": profiler._slow_requests[-20:],
                "slow_threshold_ms": profiler.slow_threshold * 1000,
            }

        @app.get("/debug/profiler/slow")
        def slow_requests(req, res):
            return {"slow_requests": profiler._slow_requests[-50:]}

    def middleware(self):
        profiler = self

        def profiling_middleware(req, res, next_fn):
            if not profiler.enabled:
                return next_fn()

            start = time.perf_counter()
            result = next_fn()
            elapsed = time.perf_counter() - start

            entry = {
                "method": req.method,
                "path": req.path,
                "status": res.status_code,
                "duration_ms": round(elapsed * 1000, 2),
                "timestamp": time.time(),
            }

            with profiler._lock:
                profiler._profiles.append(entry)
                if len(profiler._profiles) > profiler._max_profiles:
                    profiler._profiles = profiler._profiles[-profiler._max_profiles:]

                if elapsed > profiler.slow_threshold:
                    profiler._slow_requests.append(entry)
                    if len(profiler._slow_requests) > 200:
                        profiler._slow_requests = profiler._slow_requests[-200:]

            return result

        return profiling_middleware


class SQLProfiler:
    def __init__(self, enabled=True, log_queries=True):
        self.enabled = enabled
        self.log_queries = log_queries
        self._queries = []
        self._lock = threading.Lock()
        self._max_queries = 5000

    def record(self, sql, params=None, duration_ms=0):
        if not self.enabled:
            return

        entry = {
            "sql": sql,
            "params": str(params) if params else None,
            "duration_ms": round(duration_ms, 2),
            "timestamp": time.time(),
        }

        with self._lock:
            self._queries.append(entry)
            if len(self._queries) > self._max_queries:
                self._queries = self._queries[-self._max_queries:]

        if self.log_queries:
            color = "\033[31m" if duration_ms > 100 else "\033[33m" if duration_ms > 10 else "\033[36m"
            print(f"  {color}SQL\033[0m ({duration_ms:.1f}ms): {sql[:120]}")

    def get_slow_queries(self, threshold_ms=50):
        with self._lock:
            return [q for q in self._queries if q["duration_ms"] > threshold_ms]

    def get_stats(self):
        with self._lock:
            if not self._queries:
                return {"total": 0}

            durations = [q["duration_ms"] for q in self._queries]
            return {
                "total": len(self._queries),
                "avg_ms": round(sum(durations) / len(durations), 2),
                "max_ms": round(max(durations), 2),
                "min_ms": round(min(durations), 2),
                "slow_count": sum(1 for d in durations if d > 50),
            }

    def clear(self):
        with self._lock:
            self._queries.clear()


class CPUProfiler:
    def __init__(self):
        self._profiler = None
        self._active = False

    def start(self):
        self._profiler = cProfile.Profile()
        self._profiler.enable()
        self._active = True

    def stop(self):
        if self._profiler and self._active:
            self._profiler.disable()
            self._active = False

    def get_stats(self, top_n=30):
        if not self._profiler:
            return {}

        stream = io.StringIO()
        ps = pstats.Stats(self._profiler, stream=stream)
        ps.sort_stats("cumulative")
        ps.print_stats(top_n)
        return {"profile": stream.getvalue()}

    def profile(self, fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            prof = cProfile.Profile()
            prof.enable()
            try:
                result = fn(*args, **kwargs)
                return result
            finally:
                prof.disable()
                stream = io.StringIO()
                ps = pstats.Stats(prof, stream=stream)
                ps.sort_stats("cumulative")
                ps.print_stats(20)
                print(f"\n  Profile for {fn.__name__}:\n{stream.getvalue()}")
        return wrapper


class MemoryProfiler:
    def __init__(self):
        self._snapshots = []
        self._tracking = False

    def start(self):
        tracemalloc.start()
        self._tracking = True

    def stop(self):
        if self._tracking:
            tracemalloc.stop()
            self._tracking = False

    def snapshot(self, label=""):
        if not self._tracking:
            self.start()

        snap = tracemalloc.take_snapshot()
        stats = snap.statistics("lineno")

        top = []
        for stat in stats[:20]:
            top.append({
                "file": str(stat.traceback),
                "size_kb": round(stat.size / 1024, 2),
                "count": stat.count,
            })

        current, peak = tracemalloc.get_traced_memory()
        entry = {
            "label": label,
            "current_mb": round(current / (1024 * 1024), 2),
            "peak_mb": round(peak / (1024 * 1024), 2),
            "top_allocations": top,
            "timestamp": time.time(),
        }

        self._snapshots.append(entry)
        return entry

    def get_current(self):
        if not self._tracking:
            return {"tracking": False}

        current, peak = tracemalloc.get_traced_memory()
        return {
            "current_mb": round(current / (1024 * 1024), 2),
            "peak_mb": round(peak / (1024 * 1024), 2),
        }


class DebugToolbar:
    def __init__(self, app=None, enabled=True):
        self.enabled = enabled
        self.request_profiler = RequestProfiler(enabled=enabled)
        self.sql_profiler = SQLProfiler(enabled=enabled)
        self.memory_profiler = MemoryProfiler()

        if app:
            self.init_app(app)

    def init_app(self, app):
        if not app.debug:
            return

        self.request_profiler.init_app(app)
        toolbar = self

        @app.get("/debug/sql")
        def sql_stats(req, res):
            return {
                "stats": toolbar.sql_profiler.get_stats(),
                "slow_queries": toolbar.sql_profiler.get_slow_queries(),
            }

        @app.get("/debug/memory")
        def memory_stats(req, res):
            snap = toolbar.memory_profiler.snapshot(label="manual")
            return snap

        @app.get("/debug/overview")
        def debug_overview(req, res):
            return {
                "profiler": {
                    "total_requests": len(toolbar.request_profiler._profiles),
                    "slow_count": len(toolbar.request_profiler._slow_requests),
                },
                "sql": toolbar.sql_profiler.get_stats(),
                "memory": toolbar.memory_profiler.get_current(),
            }

    def middleware(self):
        return self.request_profiler.middleware()
