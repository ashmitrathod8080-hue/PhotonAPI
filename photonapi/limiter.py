import time
import threading
from collections import defaultdict
from functools import wraps


class TokenBucket:
    def __init__(self, capacity, refill_rate):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, cost=1):
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            if self.tokens >= cost:
                self.tokens -= cost
                return True
            return False

    @property
    def remaining(self):
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            return min(self.capacity, self.tokens + elapsed * self.refill_rate)


class SlidingWindowCounter:
    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window = window_seconds
        self.lock = threading.Lock()
        self._hits = []

    def hit(self, cost=1):
        with self.lock:
            now = time.monotonic()
            cutoff = now - self.window
            self._hits = [t for t in self._hits if t > cutoff]
            if len(self._hits) + cost <= self.limit:
                for _ in range(cost):
                    self._hits.append(now)
                return True
            return False

    @property
    def remaining(self):
        with self.lock:
            now = time.monotonic()
            cutoff = now - self.window
            current = len([t for t in self._hits if t > cutoff])
            return max(0, self.limit - current)

    @property
    def reset_at(self):
        with self.lock:
            if self._hits:
                return self._hits[0] + self.window
            return time.monotonic() + self.window


class FixedWindowCounter:
    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window = window_seconds
        self.lock = threading.Lock()
        self._count = 0
        self._window_start = time.monotonic()

    def hit(self, cost=1):
        with self.lock:
            now = time.monotonic()
            if now - self._window_start >= self.window:
                self._count = 0
                self._window_start = now
            if self._count + cost <= self.limit:
                self._count += cost
                return True
            return False

    @property
    def remaining(self):
        with self.lock:
            now = time.monotonic()
            if now - self._window_start >= self.window:
                return self.limit
            return max(0, self.limit - self._count)

    @property
    def reset_at(self):
        return self._window_start + self.window


class RedisRateLimiter:
    def __init__(self, redis_client):
        self._redis = redis_client

    def check(self, key, limit, window):
        try:
            pipe = self._redis.pipeline()
            now = time.time()
            window_key = f"rl:{key}:{int(now // window)}"
            pipe.incr(window_key)
            pipe.expire(window_key, int(window) + 1)
            results = pipe.execute()
            current = results[0]
            return current <= limit, max(0, limit - current)
        except Exception:
            return True, limit


def _parse_rate(rate_string):
    parts = rate_string.strip().split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid rate format: '{rate_string}'. Use 'count/period' like '10/minute'")

    count = int(parts[0])
    period = parts[1].strip().lower()

    multipliers = {
        "s": 1, "sec": 1, "second": 1,
        "m": 60, "min": 60, "minute": 60,
        "h": 3600, "hr": 3600, "hour": 3600,
        "d": 86400, "day": 86400,
    }

    if period not in multipliers:
        raise ValueError(f"Unknown time period: '{period}'. Use second/minute/hour/day")

    return count, multipliers[period]


class RateLimiter:
    def __init__(self, key_func=None, default_limits=None, strategy="sliding-window",
                 headers_enabled=True, enabled=True, redis_client=None,
                 on_exceeded=None):
        self.key_func = key_func or (lambda req: req.remote_addr)
        self.default_limits = default_limits or []
        self.strategy = strategy
        self.headers_enabled = headers_enabled
        self.enabled = enabled
        self.on_exceeded = on_exceeded
        self._buckets = defaultdict(dict)
        self._exempt_routes = set()
        self._route_limits = {}
        self._lock = threading.Lock()
        self._redis_limiter = RedisRateLimiter(redis_client) if redis_client else None
        self._whitelist = set()
        self._blacklist = set()

    def whitelist_key(self, key):
        self._whitelist.add(key)

    def blacklist_key(self, key):
        self._blacklist.add(key)

    def _get_bucket(self, key, scope, limit, window):
        bucket_key = f"{key}:{scope}"
        with self._lock:
            if bucket_key not in self._buckets:
                if self.strategy == "token-bucket":
                    self._buckets[bucket_key] = TokenBucket(limit, limit / window)
                elif self.strategy == "fixed-window":
                    self._buckets[bucket_key] = FixedWindowCounter(limit, window)
                else:
                    self._buckets[bucket_key] = SlidingWindowCounter(limit, window)
        return self._buckets[bucket_key]

    def _cleanup_expired(self):
        with self._lock:
            now = time.monotonic()
            expired = []
            for key, bucket in self._buckets.items():
                if isinstance(bucket, SlidingWindowCounter):
                    if not bucket._hits or (now - bucket._hits[-1]) > bucket.window * 2:
                        expired.append(key)
            for key in expired:
                del self._buckets[key]

    def limit(self, rate_string, key_func=None, cost=1, error_message=None, scope=None):
        count, window = _parse_rate(rate_string)

        def decorator(fn):
            fn_scope = scope or f"{fn.__module__}.{fn.__name__}"
            self._route_limits[fn_scope] = (count, window, rate_string)

            @wraps(fn)
            def wrapper(req, res, *args, **kwargs):
                if not self.enabled:
                    return fn(req, res, *args, **kwargs)

                kfn = key_func or self.key_func
                client_key = kfn(req)

                if client_key in self._whitelist:
                    return fn(req, res, *args, **kwargs)

                if client_key in self._blacklist:
                    return self._rate_exceeded(req, res, None, count, "Access denied")

                if self.headers_enabled:
                    res.set_header("X-RateLimit-Limit", str(count))

                if self._redis_limiter:
                    allowed, remaining = self._redis_limiter.check(
                        f"{client_key}:{fn_scope}", count, window
                    )
                    if self.headers_enabled:
                        res.set_header("X-RateLimit-Remaining", str(remaining))
                    if not allowed:
                        return self._rate_exceeded(req, res, None, count, error_message)
                    return fn(req, res, *args, **kwargs)

                bucket = self._get_bucket(client_key, fn_scope, count, window)
                actual_cost = cost(req) if callable(cost) else cost

                if isinstance(bucket, (SlidingWindowCounter, FixedWindowCounter)):
                    if not bucket.hit(actual_cost):
                        return self._rate_exceeded(req, res, bucket, count, error_message)
                    if self.headers_enabled:
                        res.set_header("X-RateLimit-Remaining", str(bucket.remaining))
                        res.set_header("X-RateLimit-Reset", str(int(bucket.reset_at)))
                else:
                    if not bucket.consume(actual_cost):
                        return self._rate_exceeded(req, res, bucket, count, error_message)
                    if self.headers_enabled:
                        res.set_header("X-RateLimit-Remaining", str(int(bucket.remaining)))

                return fn(req, res, *args, **kwargs)
            return wrapper
        return decorator

    def _rate_exceeded(self, req, res, bucket, limit, custom_message):
        msg = custom_message or "Rate limit exceeded"
        if callable(msg):
            msg = msg(req)

        if self.on_exceeded:
            self.on_exceeded(req)

        res.status_code = 429
        retry_after = 60
        if bucket and hasattr(bucket, 'reset_at'):
            retry_after = max(1, int(bucket.reset_at - time.monotonic()))
        res.set_header("Retry-After", str(retry_after))
        res.json({"error": msg, "limit": limit, "retry_after": retry_after})
        return res

    def exempt(self, fn):
        name = f"{fn.__module__}.{fn.__name__}"
        self._exempt_routes.add(name)

        @wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        return wrapper

    def shared_limit(self, rate_string, scope_name, key_func=None, cost=1):
        count, window = _parse_rate(rate_string)

        def decorator(fn):
            @wraps(fn)
            def wrapper(req, res, *args, **kwargs):
                if not self.enabled:
                    return fn(req, res, *args, **kwargs)

                kfn = key_func or self.key_func
                client_key = kfn(req)

                if client_key in self._whitelist:
                    return fn(req, res, *args, **kwargs)

                if self._redis_limiter:
                    allowed, remaining = self._redis_limiter.check(
                        f"{client_key}:{scope_name}", count, window
                    )
                    if not allowed:
                        return self._rate_exceeded(req, res, None, count, None)
                    return fn(req, res, *args, **kwargs)

                bucket = self._get_bucket(client_key, scope_name, count, window)
                actual_cost = cost(req) if callable(cost) else cost

                if isinstance(bucket, (SlidingWindowCounter, FixedWindowCounter)):
                    if not bucket.hit(actual_cost):
                        return self._rate_exceeded(req, res, bucket, count, None)
                else:
                    if not bucket.consume(actual_cost):
                        return self._rate_exceeded(req, res, bucket, count, None)
                return fn(req, res, *args, **kwargs)
            return wrapper
        return decorator

    def per_user(self, rate_string, user_field="user_id"):
        return self.limit(
            rate_string,
            key_func=lambda req: getattr(req, user_field, req.remote_addr),
        )

    def per_endpoint(self, rate_string):
        return self.limit(rate_string, scope=None)

    def dynamic(self, rate_func):
        def decorator(fn):
            @wraps(fn)
            def wrapper(req, res, *args, **kwargs):
                if not self.enabled:
                    return fn(req, res, *args, **kwargs)

                rate_string = rate_func(req)
                count, window = _parse_rate(rate_string)
                client_key = self.key_func(req)
                scope = f"{fn.__module__}.{fn.__name__}"
                bucket = self._get_bucket(client_key, scope, count, window)

                if isinstance(bucket, (SlidingWindowCounter, FixedWindowCounter)):
                    if not bucket.hit():
                        return self._rate_exceeded(req, res, bucket, count, None)
                else:
                    if not bucket.consume():
                        return self._rate_exceeded(req, res, bucket, count, None)
                return fn(req, res, *args, **kwargs)
            return wrapper
        return decorator

    def reset(self):
        with self._lock:
            self._buckets.clear()

    @property
    def stats(self):
        with self._lock:
            return {
                "total_buckets": len(self._buckets),
                "strategy": self.strategy,
                "whitelisted": len(self._whitelist),
                "blacklisted": len(self._blacklist),
            }


def get_remote_address(req):
    forwarded = req.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = req.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return req.remote_addr


def get_api_key(req):
    return req.headers.get("X-Api-Key", req.remote_addr)


def get_user_id(req):
    return getattr(req, "user_id", None) or req.remote_addr
