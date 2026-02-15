import threading
import queue
import time
import traceback
import sched
from functools import wraps
from datetime import datetime


class TaskQueue:
    def __init__(self, workers=2, max_retries=0, retry_delay=1.0):
        self._queue = queue.PriorityQueue()
        self._results = {}
        self._workers = []
        self._running = True
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._lock = threading.Lock()
        self._counter = 0
        self._callbacks = {}

        for i in range(workers):
            t = threading.Thread(target=self._worker, daemon=True, name=f"photon-worker-{i}")
            t.start()
            self._workers.append(t)

    def _worker(self):
        while self._running:
            try:
                priority, _, task_id, fn, args, kwargs, retries = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                result = fn(*args, **kwargs)
                self._results[task_id] = {
                    "status": "done", "result": result, "error": None,
                    "completed_at": datetime.now().isoformat(),
                }
                if task_id in self._callbacks:
                    try:
                        self._callbacks[task_id](result)
                    except Exception:
                        pass
            except Exception as e:
                if retries < self._max_retries:
                    time.sleep(self._retry_delay)
                    with self._lock:
                        self._counter += 1
                        self._queue.put((priority, self._counter, task_id, fn, args, kwargs, retries + 1))
                else:
                    self._results[task_id] = {
                        "status": "failed", "result": None,
                        "error": str(e), "traceback": traceback.format_exc(),
                        "completed_at": datetime.now().isoformat(),
                    }
            finally:
                self._queue.task_done()

    def submit(self, fn, *args, priority=0, callback=None, **kwargs):
        with self._lock:
            self._counter += 1
            task_id = f"task-{int(time.time()*1000)}-{self._counter}"
        self._results[task_id] = {
            "status": "pending", "result": None, "error": None,
            "submitted_at": datetime.now().isoformat(),
        }
        if callback:
            self._callbacks[task_id] = callback
        self._queue.put((priority, self._counter, task_id, fn, args, kwargs, 0))
        return task_id

    def get_status(self, task_id):
        return self._results.get(task_id, {"status": "unknown"})

    def wait(self, task_id, timeout=None):
        start = time.monotonic()
        while True:
            status = self.get_status(task_id)
            if status["status"] in ("done", "failed"):
                return status
            if timeout and (time.monotonic() - start) > timeout:
                return {"status": "timeout"}
            time.sleep(0.05)

    def cancel(self, task_id):
        if task_id in self._results:
            if self._results[task_id]["status"] == "pending":
                self._results[task_id]["status"] = "cancelled"
                return True
        return False

    @property
    def stats(self):
        statuses = {}
        for r in self._results.values():
            s = r["status"]
            statuses[s] = statuses.get(s, 0) + 1
        return {
            "queued": self._queue.qsize(),
            "total": len(self._results),
            "workers": len(self._workers),
            **statuses,
        }

    def shutdown(self, wait=True):
        self._running = False
        if wait:
            for w in self._workers:
                w.join(timeout=5)


class Scheduler:
    def __init__(self):
        self._jobs = {}
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._running:
            now = time.time()
            for job_id, job in list(self._jobs.items()):
                if not job["active"]:
                    continue
                if now >= job["next_run"]:
                    try:
                        threading.Thread(
                            target=job["fn"], args=job.get("args", ()),
                            kwargs=job.get("kwargs", {}), daemon=True
                        ).start()
                    except Exception:
                        pass
                    job["run_count"] += 1
                    job["last_run"] = now
                    if job["interval"]:
                        job["next_run"] = now + job["interval"]
                    else:
                        job["active"] = False
            time.sleep(0.5)

    def every(self, seconds, fn=None, *args, **kwargs):
        def decorator(f):
            job_id = f"job-{f.__name__}-{int(time.time()*1000)}"
            self._jobs[job_id] = {
                "fn": f, "interval": seconds, "next_run": time.time() + seconds,
                "active": True, "run_count": 0, "last_run": None,
                "args": args, "kwargs": kwargs,
            }
            f._job_id = job_id
            return f

        if fn:
            return decorator(fn)
        return decorator

    def at(self, run_time, fn, *args, **kwargs):
        if isinstance(run_time, str):
            run_time = datetime.fromisoformat(run_time)
        target = run_time.timestamp() if isinstance(run_time, datetime) else run_time
        job_id = f"job-{fn.__name__}-{int(time.time()*1000)}"
        self._jobs[job_id] = {
            "fn": fn, "interval": None, "next_run": target,
            "active": True, "run_count": 0, "last_run": None,
            "args": args, "kwargs": kwargs,
        }
        return job_id

    def cancel(self, job_id):
        if job_id in self._jobs:
            self._jobs[job_id]["active"] = False
            return True
        return False

    @property
    def jobs(self):
        return {
            jid: {
                "fn": j["fn"].__name__,
                "interval": j["interval"],
                "active": j["active"],
                "run_count": j["run_count"],
                "last_run": j["last_run"],
            }
            for jid, j in self._jobs.items()
        }

    def shutdown(self):
        self._running = False


_default_queue = None
_default_scheduler = None


def get_task_queue(workers=2, max_retries=0):
    global _default_queue
    if _default_queue is None:
        _default_queue = TaskQueue(workers=workers, max_retries=max_retries)
    return _default_queue


def get_scheduler():
    global _default_scheduler
    if _default_scheduler is None:
        _default_scheduler = Scheduler()
    return _default_scheduler


def background(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        tq = get_task_queue()
        task_id = tq.submit(fn, *args, **kwargs)
        return task_id
    wrapper._is_background = True
    wrapper._original = fn
    return wrapper


def after_response(fn):
    @wraps(fn)
    def decorator(handler_fn):
        @wraps(handler_fn)
        def wrapper(req, res, *args, **kwargs):
            result = handler_fn(req, res, *args, **kwargs)
            tq = get_task_queue()
            tq.submit(fn, req)
            return result
        return wrapper
    return decorator


def periodic(seconds):
    def decorator(fn):
        scheduler = get_scheduler()
        scheduler.every(seconds, fn)
        return fn
    return decorator
