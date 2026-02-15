import json
import hashlib
import time
import gzip
import threading
from collections import OrderedDict
from functools import wraps


class LRUCache:
    def __init__(self, max_size=1000, ttl=None):
        self.max_size = max_size
        self.ttl = ttl
        self._data = OrderedDict()
        self._expiry = {}
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def _make_key(self, data):
        raw = json.dumps(data, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, key):
        if not isinstance(key, str):
            key = self._make_key(key)
        with self._lock:
            if key in self._data:
                if key in self._expiry and time.time() > self._expiry[key]:
                    del self._data[key]
                    del self._expiry[key]
                    self.misses += 1
                    return None, False
                self.hits += 1
                self._data.move_to_end(key)
                return self._data[key], True
            self.misses += 1
            return None, False

    def put(self, key, value, ttl=None):
        if not isinstance(key, str):
            key = self._make_key(key)
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = value
            exp = ttl or self.ttl
            if exp:
                self._expiry[key] = time.time() + exp
            if len(self._data) > self.max_size:
                oldest_key, _ = self._data.popitem(last=False)
                self._expiry.pop(oldest_key, None)

    def delete(self, key):
        if not isinstance(key, str):
            key = self._make_key(key)
        with self._lock:
            self._data.pop(key, None)
            self._expiry.pop(key, None)

    def clear(self):
        with self._lock:
            self._data.clear()
            self._expiry.clear()
            self.hits = 0
            self.misses = 0

    def cleanup(self):
        now = time.time()
        with self._lock:
            expired = [k for k, exp in self._expiry.items() if now > exp]
            for k in expired:
                self._data.pop(k, None)
                del self._expiry[k]
        return len(expired)

    @property
    def stats(self):
        total = self.hits + self.misses
        return {
            "size": len(self._data),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total > 0 else 0,
        }


class RedisCache:
    def __init__(self, url="redis://localhost:6379/0", prefix="photon:",
                 default_ttl=3600, compression=False):
        self.prefix = prefix
        self.default_ttl = default_ttl
        self.compression = compression
        self.hits = 0
        self.misses = 0
        self._client = None
        self._url = url

    @property
    def client(self):
        if self._client is None:
            try:
                import redis  # type: ignore[import-not-found]
                self._client = redis.from_url(self._url, decode_responses=not self.compression)
            except ImportError:
                raise RuntimeError("Install 'redis' package: pip install redis")
        return self._client

    def _key(self, key):
        if not isinstance(key, str):
            key = hashlib.md5(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()
        return f"{self.prefix}{key}"

    def _serialize(self, value):
        data = json.dumps(value, default=str).encode()
        if self.compression:
            data = gzip.compress(data)
        return data

    def _deserialize(self, data):
        if data is None:
            return None
        if isinstance(data, str):
            data = data.encode()
        if self.compression:
            data = gzip.decompress(data)
        return json.loads(data)

    def get(self, key):
        raw = self.client.get(self._key(key))
        if raw is not None:
            self.hits += 1
            return self._deserialize(raw), True
        self.misses += 1
        return None, False

    def put(self, key, value, ttl=None):
        exp = ttl or self.default_ttl
        self.client.setex(self._key(key), exp, self._serialize(value))

    def delete(self, key):
        self.client.delete(self._key(key))

    def clear(self):
        keys = self.client.keys(f"{self.prefix}*")
        if keys:
            self.client.delete(*keys)
        self.hits = 0
        self.misses = 0

    @property
    def stats(self):
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total > 0 else 0,
            "backend": "redis",
        }


class TieredCache:
    def __init__(self, l1_size=500, l1_ttl=60, redis_url=None,
                 redis_ttl=3600, compression=False):
        self.l1 = LRUCache(max_size=l1_size, ttl=l1_ttl)
        self.l2 = None
        if redis_url:
            self.l2 = RedisCache(url=redis_url, default_ttl=redis_ttl, compression=compression)

    def get(self, key):
        val, hit = self.l1.get(key)
        if hit:
            return val, True

        if self.l2:
            val, hit = self.l2.get(key)
            if hit:
                self.l1.put(key, val)
                return val, True

        return None, False

    def put(self, key, value, ttl=None):
        self.l1.put(key, value, ttl=ttl)
        if self.l2:
            self.l2.put(key, value, ttl=ttl)

    def delete(self, key):
        self.l1.delete(key)
        if self.l2:
            self.l2.delete(key)

    def clear(self):
        self.l1.clear()
        if self.l2:
            self.l2.clear()

    @property
    def stats(self):
        result = {"l1": self.l1.stats}
        if self.l2:
            result["l2"] = self.l2.stats
        return result


class CacheManager:
    def __init__(self, app=None, backend="memory", max_size=1000, ttl=3600,
                 redis_url=None, compression=False):
        self.backend_type = backend
        if backend == "tiered" and redis_url:
            self._cache = TieredCache(l1_size=max_size, redis_url=redis_url, compression=compression)
        elif backend == "redis" and redis_url:
            self._cache = RedisCache(url=redis_url, default_ttl=ttl, compression=compression)
        else:
            self._cache = LRUCache(max_size=max_size, ttl=ttl)

        self._namespaces = {}
        if app:
            self.init_app(app)

    def init_app(self, app):
        app.cache = self

        @app.get("/cache/stats")
        def cache_stats(req, res):
            return {"cache": self.stats}

        @app.route("/cache/clear", methods=["POST"])
        def cache_clear(req, res):
            ns = (req.json or {}).get("namespace")
            if ns:
                self.clear_namespace(ns)
                return {"message": f"Namespace '{ns}' cleared"}
            self.clear()
            return {"message": "Cache cleared"}

    def namespace(self, ns):
        if ns not in self._namespaces:
            if isinstance(self._cache, LRUCache):
                self._namespaces[ns] = LRUCache(
                    max_size=self._cache.max_size,
                    ttl=self._cache.ttl
                )
            else:
                self._namespaces[ns] = self._cache
        return self._namespaces[ns]

    def get(self, key, namespace=None):
        cache = self._namespaces.get(namespace, self._cache) if namespace else self._cache
        return cache.get(key)

    def put(self, key, value, ttl=None, namespace=None):
        cache = self._namespaces.get(namespace, self._cache) if namespace else self._cache
        cache.put(key, value, ttl=ttl)

    def delete(self, key, namespace=None):
        cache = self._namespaces.get(namespace, self._cache) if namespace else self._cache
        cache.delete(key)

    def clear(self):
        self._cache.clear()
        for ns_cache in self._namespaces.values():
            ns_cache.clear()

    def clear_namespace(self, ns):
        if ns in self._namespaces:
            self._namespaces[ns].clear()

    @property
    def stats(self):
        result = {"main": self._cache.stats, "backend": self.backend_type}
        if self._namespaces:
            result["namespaces"] = {ns: c.stats for ns, c in self._namespaces.items()}
        return result

    def cached(self, ttl=None, namespace=None, key_func=None):
        def decorator(fn):
            @wraps(fn)
            def wrapper(req, res, *args, **kwargs):
                if key_func:
                    cache_key = key_func(req)
                else:
                    cache_key = f"{req.method}:{req.path}:{req.query_string}"

                val, hit = self.get(cache_key, namespace=namespace)
                if hit:
                    res.set_header("X-Cache", "HIT")
                    if isinstance(val, dict):
                        res.json(val)
                    else:
                        res.body = val
                    return res

                result = fn(req, res, *args, **kwargs)
                cache_val = result
                if isinstance(result, dict):
                    cache_val = result
                elif isinstance(result, tuple):
                    cache_val = result[0]

                self.put(cache_key, cache_val, ttl=ttl, namespace=namespace)
                res.set_header("X-Cache", "MISS")
                return result
            return wrapper
        return decorator


def warm_cache(cache, warmup_data):
    for key, value in warmup_data.items():
        cache.put(key, value)
    return len(warmup_data)
