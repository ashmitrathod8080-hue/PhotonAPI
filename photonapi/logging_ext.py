import logging
import json
import time
import threading
import os
import re
import uuid
from datetime import datetime, timezone
from functools import wraps


_context = threading.local()


def get_request_id():
    return getattr(_context, "request_id", None)


def get_correlation_id():
    return getattr(_context, "correlation_id", None)


def set_context(**kwargs):
    for k, v in kwargs.items():
        setattr(_context, k, v)


def clear_context():
    _context.__dict__.clear()


class SensitiveFilter(logging.Filter):
    DEFAULT_PATTERNS = [
        (re.compile(r'("password"\s*:\s*)"[^"]*"', re.I), r'\1"***"'),
        (re.compile(r'("secret"\s*:\s*)"[^"]*"', re.I), r'\1"***"'),
        (re.compile(r'("token"\s*:\s*)"[^"]*"', re.I), r'\1"***"'),
        (re.compile(r'("api_key"\s*:\s*)"[^"]*"', re.I), r'\1"***"'),
        (re.compile(r'("authorization"\s*:\s*)"[^"]*"', re.I), r'\1"***"'),
        (re.compile(r'("credit_card"\s*:\s*)"[^"]*"', re.I), r'\1"***"'),
        (re.compile(r'("ssn"\s*:\s*)"[^"]*"', re.I), r'\1"***"'),
    ]

    def __init__(self, extra_patterns=None):
        super().__init__()
        self.patterns = list(self.DEFAULT_PATTERNS)
        if extra_patterns:
            for field in extra_patterns:
                self.patterns.append(
                    (re.compile(rf'("{field}"\s*:\s*)"[^"]*"', re.I), r'\1"***"')
                )

    def filter(self, record):
        if hasattr(record, "msg") and isinstance(record.msg, str):
            for pattern, replacement in self.patterns:
                record.msg = pattern.sub(replacement, record.msg)
        return True


class JSONFormatter(logging.Formatter):
    def __init__(self, include_fields=None, exclude_fields=None):
        super().__init__()
        self.include_fields = include_fields
        self.exclude_fields = exclude_fields or set()

    def format(self, record):
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = get_request_id()
        if request_id:
            log_data["request_id"] = request_id

        correlation_id = get_correlation_id()
        if correlation_id:
            log_data["correlation_id"] = correlation_id

        if record.exc_info and record.exc_info[1]:
            log_data["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": self.formatException(record.exc_info),
            }

        extra_keys = set(record.__dict__.keys()) - {
            "name", "msg", "args", "created", "relativeCreated", "exc_info",
            "exc_text", "stack_info", "lineno", "funcName", "pathname",
            "filename", "module", "levelno", "levelname", "thread",
            "threadName", "process", "processName", "message", "msecs",
            "taskName",
        }
        for key in extra_keys:
            if key not in self.exclude_fields and not key.startswith("_"):
                log_data[key] = record.__dict__[key]

        if self.include_fields:
            log_data = {k: v for k, v in log_data.items() if k in self.include_fields}

        for key in self.exclude_fields:
            log_data.pop(key, None)

        return json.dumps(log_data, default=str)


class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"
    DIM = "\033[2m"

    def format(self, record):
        color = self.COLORS.get(record.levelname, "")
        request_id = get_request_id()
        rid = f" [{request_id}]" if request_id else ""
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        msg = record.getMessage()
        level = record.levelname.ljust(8)

        result = f"{self.DIM}{ts}{self.RESET} {color}{level}{self.RESET}{rid} {msg}"
        if record.exc_info and record.exc_info[1]:
            result += f"\n{self.formatException(record.exc_info)}"
        return result


class RotatingBuffer:
    def __init__(self, max_size=10000):
        self.max_size = max_size
        self._buffer = []
        self._lock = threading.Lock()

    def append(self, record):
        with self._lock:
            self._buffer.append(record)
            if len(self._buffer) > self.max_size:
                self._buffer = self._buffer[-self.max_size:]

    def get_recent(self, n=100, level=None):
        with self._lock:
            records = self._buffer
            if level:
                records = [r for r in records if r.get("level") == level]
            return records[-n:]


class BufferHandler(logging.Handler):
    def __init__(self, buffer):
        super().__init__()
        self._buffer = buffer

    def emit(self, record):
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": get_request_id(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
        self._buffer.append(entry)


class LogManager:
    def __init__(self, app=None, level="INFO", format="color", json_output=False,
                 sensitive_fields=None, buffer_size=5000):
        self.log_buffer = RotatingBuffer(max_size=buffer_size)
        self._loggers = {}
        self.root_level = getattr(logging, level.upper(), logging.INFO)

        root = logging.getLogger("photonapi")
        root.setLevel(self.root_level)
        root.handlers.clear()

        if json_output or format == "json":
            handler = logging.StreamHandler()
            handler.setFormatter(JSONFormatter())
        else:
            handler = logging.StreamHandler()
            handler.setFormatter(ColorFormatter())

        if sensitive_fields:
            handler.addFilter(SensitiveFilter(sensitive_fields))
        else:
            handler.addFilter(SensitiveFilter())

        root.addHandler(handler)
        root.addHandler(BufferHandler(self.log_buffer))

        self.root = root

        if app:
            self.init_app(app)

    def init_app(self, app):
        manager = self

        @app.get("/logs/recent")
        def recent_logs(req, res):
            n = int(req.get_query("n", "50"))
            level = req.get_query("level")
            return {"logs": manager.log_buffer.get_recent(n, level)}

    def get_logger(self, name):
        if name not in self._loggers:
            logger = logging.getLogger(f"photonapi.{name}")
            self._loggers[name] = logger
        return self._loggers[name]

    def middleware(self):
        log = self.root

        def logging_middleware(req, res, next_fn):
            request_id = req.headers.get("X-Request-Id") or uuid.uuid4().hex[:8]
            correlation_id = req.headers.get("X-Correlation-Id") or request_id

            set_context(request_id=request_id, correlation_id=correlation_id)
            res.set_header("X-Request-Id", request_id)
            res.set_header("X-Correlation-Id", correlation_id)

            start = time.perf_counter()
            try:
                result = next_fn()
                elapsed = (time.perf_counter() - start) * 1000
                status = res.status_code
                color = "\033[32m" if status < 400 else "\033[33m" if status < 500 else "\033[31m"
                log.info(
                    f"{color}{status}\033[0m {req.method:6s} {req.path} ({elapsed:.1f}ms)",
                    extra={"method": req.method, "path": req.path,
                           "status": status, "latency_ms": round(elapsed, 1)}
                )
                return result
            except Exception as e:
                elapsed = (time.perf_counter() - start) * 1000
                log.error(
                    f"{req.method} {req.path} failed ({elapsed:.1f}ms): {e}",
                    exc_info=True,
                    extra={"method": req.method, "path": req.path, "latency_ms": round(elapsed, 1)}
                )
                raise
            finally:
                clear_context()

        return logging_middleware


class ScopedLogger:
    def __init__(self, logger, **context):
        self._logger = logger
        self._context = context

    def _log(self, level, msg, *args, **kwargs):
        extra = kwargs.pop("extra", {})
        extra.update(self._context)
        kwargs["extra"] = extra
        getattr(self._logger, level)(msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        self._log("debug", msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._log("info", msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._log("warning", msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._log("error", msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._log("critical", msg, *args, **kwargs)

    def child(self, **extra_context):
        ctx = {**self._context, **extra_context}
        return ScopedLogger(self._logger, **ctx)
