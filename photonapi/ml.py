import json
import hashlib
import time
import threading
import os
import random
from collections import OrderedDict, deque
from datetime import datetime
from functools import wraps


class LRUCache:
    def __init__(self, max_size=1000):
        self.max_size = max_size
        self.cache = OrderedDict()
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def _make_key(self, data):
        raw = json.dumps(data, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, data):
        key = self._make_key(data)
        with self._lock:
            if key in self.cache:
                self.hits += 1
                self.cache.move_to_end(key)
                return self.cache[key], True
            self.misses += 1
            return None, False

    def put(self, data, result):
        key = self._make_key(data)
        with self._lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = result
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)

    def clear(self):
        with self._lock:
            self.cache.clear()
            self.hits = 0
            self.misses = 0

    @property
    def stats(self):
        total = self.hits + self.misses
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total > 0 else 0,
        }


class ModelEntry:
    def __init__(self, name, version, instance, cache_enabled, cache_size, metadata=None):
        self.name = name
        self.version = version
        self.instance = instance
        self.cache = LRUCache(cache_size) if cache_enabled else None
        self.request_count = 0
        self.error_count = 0
        self.total_latency = 0
        self.loaded = False
        self.loaded_at = None
        self.metadata = metadata or {}
        self._lock = threading.Lock()
        self._prediction_log = deque(maxlen=1000)

    def load(self):
        if hasattr(self.instance, "load"):
            self.instance.load()
        self.loaded = True
        self.loaded_at = datetime.now().isoformat()

    def unload(self):
        if hasattr(self.instance, "unload"):
            self.instance.unload()
        self.loaded = False

    def predict(self, data):
        if self.cache:
            cached, hit = self.cache.get(data)
            if hit:
                return cached

        start = time.time()
        try:
            processed = data
            if hasattr(self.instance, "preprocess"):
                processed = self.instance.preprocess(data)

            result = self.instance.predict(processed)

            if hasattr(self.instance, "postprocess"):
                result = self.instance.postprocess(result)

            latency = time.time() - start
            with self._lock:
                self.request_count += 1
                self.total_latency += latency
                self._prediction_log.append({
                    "timestamp": datetime.now().isoformat(),
                    "latency_ms": round(latency * 1000, 2),
                })

            if self.cache:
                self.cache.put(data, result)

            return result
        except Exception:
            with self._lock:
                self.error_count += 1
            raise

    def batch_predict(self, items):
        results = []
        if hasattr(self.instance, "batch_predict"):
            start = time.time()
            preprocessed = items
            if hasattr(self.instance, "batch_preprocess"):
                preprocessed = self.instance.batch_preprocess(items)
            elif hasattr(self.instance, "preprocess"):
                preprocessed = [self.instance.preprocess(item) for item in items]

            raw_results = self.instance.batch_predict(preprocessed)

            if hasattr(self.instance, "batch_postprocess"):
                results = self.instance.batch_postprocess(raw_results)
            elif hasattr(self.instance, "postprocess"):
                results = [self.instance.postprocess(r) for r in raw_results]
            else:
                results = raw_results

            latency = time.time() - start
            with self._lock:
                self.request_count += len(items)
                self.total_latency += latency
        else:
            for item in items:
                results.append(self.predict(item))
        return results

    def health(self):
        return {
            "model": self.name,
            "version": self.version,
            "status": "healthy" if self.loaded else "not_loaded",
            "loaded_at": self.loaded_at,
        }

    def info(self):
        avg_latency = (self.total_latency / self.request_count * 1000) if self.request_count > 0 else 0
        result = {
            "name": self.name,
            "version": self.version,
            "status": "healthy" if self.loaded else "not_loaded",
            "loaded_at": self.loaded_at,
            "requests": self.request_count,
            "errors": self.error_count,
            "avg_latency_ms": round(avg_latency, 2),
            "metadata": self.metadata,
        }
        if self.cache:
            result["cache"] = self.cache.stats
        return result


class ABTest:
    def __init__(self, name, models, weights=None):
        self.name = name
        self.models = models
        self.weights = weights or [1.0 / len(models)] * len(models)
        self._results = {m: {"requests": 0, "total_latency": 0} for m in models}
        self._lock = threading.Lock()

    def select(self):
        return random.choices(list(self.models.keys()), weights=self.weights, k=1)[0]

    def record(self, model_name, latency):
        with self._lock:
            self._results[model_name]["requests"] += 1
            self._results[model_name]["total_latency"] += latency

    def stats(self):
        with self._lock:
            result = {"name": self.name, "models": {}}
            for name, data in self._results.items():
                avg = (data["total_latency"] / data["requests"] * 1000) if data["requests"] > 0 else 0
                result["models"][name] = {
                    "requests": data["requests"],
                    "avg_latency_ms": round(avg, 2),
                    "weight": self.weights[list(self.models.keys()).index(name)],
                }
            return result


class DynamicBatcher:
    def __init__(self, model_entry, max_batch_size=32, max_wait_ms=50):
        self._model = model_entry
        self._max_batch_size = max_batch_size
        self._max_wait_ms = max_wait_ms
        self._queue = []
        self._lock = threading.Lock()
        self._results = {}
        self._event = threading.Event()
        self._running = True
        self._thread = threading.Thread(target=self._batch_loop, daemon=True)
        self._thread.start()

    def _batch_loop(self):
        while self._running:
            time.sleep(self._max_wait_ms / 1000)
            with self._lock:
                if not self._queue:
                    continue
                batch = self._queue[:self._max_batch_size]
                self._queue = self._queue[self._max_batch_size:]

            ids = [item[0] for item in batch]
            inputs = [item[1] for item in batch]

            try:
                results = self._model.batch_predict(inputs)
                for req_id, result in zip(ids, results):
                    self._results[req_id] = ("ok", result)
            except Exception as e:
                for req_id in ids:
                    self._results[req_id] = ("error", str(e))

            self._event.set()
            self._event.clear()

    def predict(self, data, timeout=5.0):
        req_id = f"{id(data)}-{time.monotonic()}"
        with self._lock:
            self._queue.append((req_id, data))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if req_id in self._results:
                status, result = self._results.pop(req_id)
                if status == "error":
                    raise RuntimeError(result)
                return result
            self._event.wait(timeout=0.01)

        raise TimeoutError("Batch prediction timed out")

    def stop(self):
        self._running = False


class ModelLoader:
    @staticmethod
    def load_sklearn(path):
        try:
            import pickle
            with open(path, "rb") as f:
                return pickle.load(f)
        except ImportError:
            raise RuntimeError("scikit-learn required: pip install scikit-learn")

    @staticmethod
    def load_onnx(path):
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]
            return ort.InferenceSession(path)
        except ImportError:
            raise RuntimeError("ONNX Runtime required: pip install onnxruntime")

    @staticmethod
    def load_pytorch(path, model_class=None):
        try:
            import torch  # type: ignore[import-not-found]
            if model_class:
                model = model_class()
                model.load_state_dict(torch.load(path, map_location="cpu"))
                model.eval()
                return model
            return torch.load(path, map_location="cpu")
        except ImportError:
            raise RuntimeError("PyTorch required: pip install torch")

    @staticmethod
    def load_tensorflow(path):
        try:
            import tensorflow as tf  # type: ignore[import-not-found]
            return tf.saved_model.load(path)
        except ImportError:
            raise RuntimeError("TensorFlow required: pip install tensorflow")

    @staticmethod
    def load_huggingface(model_name, task="text-classification"):
        try:
            from transformers import pipeline  # type: ignore[import-not-found]
            return pipeline(task, model=model_name)
        except ImportError:
            raise RuntimeError("Transformers required: pip install transformers")


class ModelMonitor:
    def __init__(self, window_size=1000):
        self._predictions = deque(maxlen=window_size)
        self._lock = threading.Lock()

    def record(self, input_data, prediction, actual=None):
        with self._lock:
            self._predictions.append({
                "timestamp": datetime.now().isoformat(),
                "input": input_data,
                "prediction": prediction,
                "actual": actual,
            })

    def drift_score(self):
        with self._lock:
            if len(self._predictions) < 10:
                return 0.0
            half = len(self._predictions) // 2
            recent = list(self._predictions)[half:]
            older = list(self._predictions)[:half]

            def avg_confidence(preds):
                confs = []
                for p in preds:
                    pred = p.get("prediction", {})
                    if isinstance(pred, dict):
                        conf = pred.get("confidence", pred.get("score"))
                        if conf is not None:
                            confs.append(float(conf))
                return sum(confs) / len(confs) if confs else 0.5

            old_avg = avg_confidence(older)
            new_avg = avg_confidence(recent)
            return round(abs(new_avg - old_avg), 4)

    def accuracy(self):
        with self._lock:
            labeled = [p for p in self._predictions if p.get("actual") is not None]
            if not labeled:
                return None
            correct = sum(1 for p in labeled if p["prediction"] == p["actual"])
            return round(correct / len(labeled), 4)

    def stats(self):
        return {
            "total_predictions": len(self._predictions),
            "drift_score": self.drift_score(),
            "accuracy": self.accuracy(),
        }


class ModelRegistry:
    def __init__(self, app=None, cache_size=1000):
        self.entries = {}
        self.cache_size = cache_size
        self.app = None
        self._ab_tests = {}
        self._monitors = {}
        self._batchers = {}
        if app:
            self.init_app(app)

    def init_app(self, app):
        self.app = app

        @app.get("/models")
        def list_models(req, res):
            return {"models": [e.info() for e in self.entries.values()]}

    def register(self, name, version="v1", cache=False, metadata=None,
                 monitor=False, batch_size=None, warmup_data=None):
        def decorator(cls):
            instance = cls()
            entry = ModelEntry(name, version, instance, cache, self.cache_size, metadata)
            entry.load()
            self.entries[name] = entry

            if warmup_data:
                for data in warmup_data:
                    try:
                        entry.predict(data)
                    except Exception:
                        pass

            if monitor:
                self._monitors[name] = ModelMonitor()

            if batch_size and batch_size > 1:
                self._batchers[name] = DynamicBatcher(entry, max_batch_size=batch_size)

            if self.app:
                self._register_routes(name, entry)

            return cls
        return decorator

    def register_instance(self, name, instance, version="v1", cache=False,
                          metadata=None, monitor=False):
        entry = ModelEntry(name, version, instance, cache, self.cache_size, metadata)
        entry.load()
        self.entries[name] = entry
        if monitor:
            self._monitors[name] = ModelMonitor()
        if self.app:
            self._register_routes(name, entry)
        return entry

    def _register_routes(self, name, entry):
        prefix = f"/models/{name}"

        @self.app.route(f"{prefix}/predict", methods=["POST"])
        def model_predict(req, res):
            data = req.json
            if not data:
                return {"error": "JSON body required"}, 400
            try:
                if name in self._batchers:
                    result = self._batchers[name].predict(data)
                else:
                    result = entry.predict(data)

                if name in self._monitors:
                    self._monitors[name].record(data, result)

                return {"prediction": result, "model": name, "version": entry.version}
            except Exception as e:
                return {"error": str(e), "model": name}, 500

        @self.app.route(f"{prefix}/batch", methods=["POST"])
        def model_batch(req, res):
            data = req.json
            if not data or "inputs" not in data:
                return {"error": "JSON body with 'inputs' list required"}, 400
            try:
                results = entry.batch_predict(data["inputs"])
                return {"predictions": results, "model": name, "count": len(results)}
            except Exception as e:
                return {"error": str(e), "model": name}, 500

        @self.app.get(f"{prefix}/health")
        def model_health(req, res):
            return entry.health()

        @self.app.get(f"{prefix}/info")
        def model_info(req, res):
            info = entry.info()
            if name in self._monitors:
                info["monitoring"] = self._monitors[name].stats()
            return info

        if entry.cache:
            @self.app.route(f"{prefix}/cache/clear", methods=["POST"])
            def clear_cache(req, res):
                entry.cache.clear()
                return {"message": f"Cache cleared for {name}"}

    def hot_swap(self, name, new_instance, new_version=None):
        if name not in self.entries:
            raise ValueError(f"Model '{name}' not found")
        old_entry = self.entries[name]
        old_entry.unload()
        new_entry = ModelEntry(
            name, new_version or old_entry.version, new_instance,
            old_entry.cache is not None, self.cache_size, old_entry.metadata,
        )
        new_entry.load()
        self.entries[name] = new_entry
        return new_entry

    def ab_test(self, name, model_names, weights=None):
        models = {n: self.entries[n] for n in model_names if n in self.entries}
        if len(models) < 2:
            raise ValueError("A/B test requires at least 2 registered models")
        test = ABTest(name, models, weights)
        self._ab_tests[name] = test

        if self.app:
            @self.app.route(f"/ab/{name}/predict", methods=["POST"])
            def ab_predict(req, res):
                data = req.json
                if not data:
                    return {"error": "JSON body required"}, 400
                selected = test.select()
                start = time.time()
                try:
                    result = models[selected].predict(data)
                    test.record(selected, time.time() - start)
                    return {"prediction": result, "model": selected, "ab_test": name}
                except Exception as e:
                    return {"error": str(e)}, 500

            @self.app.get(f"/ab/{name}/stats")
            def ab_stats(req, res):
                return test.stats()

        return test

    def get(self, name):
        return self.entries.get(name)


class Pipeline:
    def __init__(self, name=None):
        self.name = name or "pipeline"
        self._steps = []

    def step(self, name=None):
        def decorator(fn):
            step_name = name or fn.__name__
            self._steps.append((step_name, fn))
            return fn

        if callable(name):
            fn = name
            self._steps.append((fn.__name__, fn))
            return fn
        return decorator

    def add(self, name, fn):
        self._steps.append((name, fn))
        return self

    def run(self, data, debug=False):
        trace = [] if debug else None
        current = data

        for step_name, fn in self._steps:
            start = time.time()
            try:
                current = fn(current)
                if debug:
                    trace.append({
                        "step": step_name,
                        "output_type": type(current).__name__,
                        "ms": round((time.time() - start) * 1000, 2),
                    })
            except Exception as e:
                if debug:
                    trace.append({
                        "step": step_name,
                        "error": str(e),
                        "ms": round((time.time() - start) * 1000, 2),
                    })
                raise

        if debug:
            return {"output": current, "pipeline": self.name, "steps": trace}
        return current

    def serve(self, app, path):
        pipeline = self

        @app.route(path, methods=["POST"])
        def run_pipeline(req, res):
            data = req.json
            if not data or "input" not in data:
                return {"error": "JSON body with 'input' field required"}, 400
            debug = data.get("debug", False)
            try:
                result = pipeline.run(data["input"], debug=debug)
                return {"result": result, "pipeline": pipeline.name}
            except Exception as e:
                return {"error": str(e), "pipeline": pipeline.name}, 500

    def parallel(self, *pipelines):
        def run_all(data):
            results = {}
            threads = []
            errors = {}

            def run_one(p_name, p):
                try:
                    results[p_name] = p.run(data)
                except Exception as e:
                    errors[p_name] = str(e)

            for p in pipelines:
                t = threading.Thread(target=run_one, args=(p.name, p))
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            if errors:
                raise RuntimeError(f"Pipeline errors: {errors}")
            return results

        merged = Pipeline(f"{self.name}_parallel")
        merged.add("parallel", run_all)
        return merged
