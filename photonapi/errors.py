import time
import traceback
import threading
import json
from functools import wraps


class PhotonError(Exception):
    def __init__(self, message="An error occurred", status_code=500, details=None, error_code=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details
        self.error_code = error_code

    def to_dict(self):
        result = {"error": self.message, "status": self.status_code}
        if self.error_code:
            result["code"] = self.error_code
        if self.details:
            result["details"] = self.details
        return result


class BadRequest(PhotonError):
    def __init__(self, message="Bad request", **kwargs):
        super().__init__(message, status_code=400, **kwargs)


class Unauthorized(PhotonError):
    def __init__(self, message="Unauthorized", **kwargs):
        super().__init__(message, status_code=401, **kwargs)


class Forbidden(PhotonError):
    def __init__(self, message="Forbidden", **kwargs):
        super().__init__(message, status_code=403, **kwargs)


class NotFound(PhotonError):
    def __init__(self, message="Not found", **kwargs):
        super().__init__(message, status_code=404, **kwargs)


class MethodNotAllowed(PhotonError):
    def __init__(self, message="Method not allowed", **kwargs):
        super().__init__(message, status_code=405, **kwargs)


class Conflict(PhotonError):
    def __init__(self, message="Conflict", **kwargs):
        super().__init__(message, status_code=409, **kwargs)


class ValidationError(PhotonError):
    def __init__(self, message="Validation failed", errors=None, **kwargs):
        super().__init__(message, status_code=422, details=errors, **kwargs)


class RateLimitExceeded(PhotonError):
    def __init__(self, message="Rate limit exceeded", retry_after=None, **kwargs):
        super().__init__(message, status_code=429, **kwargs)
        self.retry_after = retry_after


class InternalError(PhotonError):
    def __init__(self, message="Internal server error", **kwargs):
        super().__init__(message, status_code=500, **kwargs)


class ServiceUnavailable(PhotonError):
    def __init__(self, message="Service unavailable", **kwargs):
        super().__init__(message, status_code=503, **kwargs)


class TimeoutError(PhotonError):
    def __init__(self, message="Request timeout", **kwargs):
        super().__init__(message, status_code=504, **kwargs)


class ErrorHandler:
    def __init__(self, app=None, debug=False):
        self.debug = debug
        self._handlers = {}
        self._global_handler = None
        if app:
            self.init_app(app)

    def init_app(self, app):
        self.debug = app.debug
        app._error_handler_ext = self

    def handler(self, exc_class):
        def decorator(fn):
            self._handlers[exc_class] = fn
            return fn
        return decorator

    def catch_all(self, fn):
        self._global_handler = fn
        return fn

    def handle(self, req, res, exc):
        exc_type = type(exc)

        for cls in exc_type.__mro__:
            if cls in self._handlers:
                return self._handlers[cls](req, res, exc)

        if self._global_handler:
            return self._global_handler(req, res, exc)

        if isinstance(exc, PhotonError):
            data = exc.to_dict()
            if self.debug:
                data["traceback"] = traceback.format_exc()
            res.json(data, exc.status_code)
            if isinstance(exc, RateLimitExceeded) and exc.retry_after:
                res.set_header("Retry-After", str(exc.retry_after))
            return res

        status = 500
        data = {"error": "Internal server error", "status": status}
        if self.debug:
            data["exception"] = f"{exc_type.__name__}: {exc}"
            data["traceback"] = traceback.format_exc()
        res.json(data, status)
        return res


class RetryConfig:
    def __init__(self, max_retries=3, base_delay=0.1, max_delay=30,
                 exponential_base=2, retry_on=None):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.retry_on = retry_on or (Exception,)

    def get_delay(self, attempt):
        delay = self.base_delay * (self.exponential_base ** attempt)
        return min(delay, self.max_delay)


def retry(config=None, max_retries=3, base_delay=0.1, retry_on=None):
    if config is None:
        config = RetryConfig(max_retries=max_retries, base_delay=base_delay, retry_on=retry_on)

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(config.max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except config.retry_on as e:
                    last_exc = e
                    if attempt < config.max_retries:
                        delay = config.get_delay(attempt)
                        time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold=5, recovery_timeout=30,
                 half_open_max=3, on_state_change=None):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max
        self.on_state_change = on_state_change

        self._state = self.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0
        self._half_open_calls = 0
        self._lock = threading.Lock()

    @property
    def state(self):
        with self._lock:
            if self._state == self.OPEN:
                if time.time() - self._last_failure_time > self.recovery_timeout:
                    self._transition(self.HALF_OPEN)
            return self._state

    def _transition(self, new_state):
        old = self._state
        self._state = new_state
        if new_state == self.HALF_OPEN:
            self._half_open_calls = 0
        if self.on_state_change and old != new_state:
            self.on_state_change(old, new_state)

    def record_success(self):
        with self._lock:
            if self._state == self.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.half_open_max:
                    self._transition(self.CLOSED)
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == self.CLOSED:
                self._failure_count = 0

    def record_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._state == self.HALF_OPEN:
                self._transition(self.OPEN)
            elif self._failure_count >= self.failure_threshold:
                self._transition(self.OPEN)

    def __call__(self, fn):
        breaker = self

        @wraps(fn)
        def wrapper(*args, **kwargs):
            state = breaker.state
            if state == breaker.OPEN:
                raise ServiceUnavailable(
                    f"Circuit breaker open for {fn.__name__}",
                    details={"state": "open", "retry_after": breaker.recovery_timeout}
                )

            try:
                result = fn(*args, **kwargs)
                breaker.record_success()
                return result
            except Exception as e:
                breaker.record_failure()
                raise

        wrapper.breaker = breaker
        return wrapper

    @property
    def stats(self):
        return {
            "state": self.state,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
        }


class RequestTimeout:
    def __init__(self, default_timeout=30):
        self.default_timeout = default_timeout

    def middleware(self, timeout=None):
        t = timeout or self.default_timeout

        def timeout_middleware(req, res, next_fn):
            result_container = [None]
            exception_container = [None]

            def run():
                try:
                    result_container[0] = next_fn()
                except Exception as e:
                    exception_container[0] = e

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(timeout=t)

            if thread.is_alive():
                res.json({"error": "Request timeout", "timeout_seconds": t}, 504)
                return res

            if exception_container[0]:
                raise exception_container[0]

            return result_container[0]

        return timeout_middleware

    def timeout(self, seconds):
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                result_container = [None]
                exception_container = [None]

                def run():
                    try:
                        result_container[0] = fn(*args, **kwargs)
                    except Exception as e:
                        exception_container[0] = e

                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                thread.join(timeout=seconds)

                if thread.is_alive():
                    raise TimeoutError(f"Function timed out after {seconds}s")

                if exception_container[0]:
                    raise exception_container[0]

                return result_container[0]
            return wrapper
        return decorator
