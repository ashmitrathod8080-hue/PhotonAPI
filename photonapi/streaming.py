import json
import time
import threading
from collections import defaultdict


class StreamResponse:
    def __init__(self, generator, content_type="text/plain", status=200, headers=None):
        self.generator = generator
        self.content_type = content_type
        self.status = status
        self.headers = headers or {}

    def __iter__(self):
        for chunk in self.generator:
            if isinstance(chunk, str):
                yield chunk.encode("utf-8")
            else:
                yield chunk


class SSEResponse(StreamResponse):
    def __init__(self, generator, headers=None):
        extra = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        if headers:
            extra.update(headers)
        super().__init__(generator, content_type="text/event-stream", headers=extra)


def sse_event(data, event=None, id=None, retry=None):
    lines = []
    if id is not None:
        lines.append(f"id: {id}")
    if event:
        lines.append(f"event: {event}")
    if retry is not None:
        lines.append(f"retry: {retry}")

    payload = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
    for line in payload.split("\n"):
        lines.append(f"data: {line}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def stream_text(generator):
    return StreamResponse(generator, content_type="text/plain")


def stream_json_lines(generator):
    def gen():
        for item in generator:
            yield json.dumps(item) + "\n"
    return StreamResponse(gen(), content_type="application/x-ndjson")


def stream_sse(generator, event_name=None):
    counter = [0]

    def gen():
        for item in generator:
            counter[0] += 1
            yield sse_event(item, event=event_name, id=counter[0])
        yield sse_event({"done": True}, event="done", id=counter[0] + 1)

    return SSEResponse(gen())


class EventBus:
    def __init__(self):
        self._channels = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, channel):
        q = EventQueue()
        with self._lock:
            self._channels[channel].append(q)
        return q

    def unsubscribe(self, channel, q):
        with self._lock:
            if q in self._channels[channel]:
                self._channels[channel].remove(q)

    def publish(self, channel, data, event=None):
        with self._lock:
            subscribers = list(self._channels.get(channel, []))
        for q in subscribers:
            q.put(data, event)

    def broadcast(self, data, event=None):
        with self._lock:
            all_queues = set()
            for subs in self._channels.values():
                all_queues.update(subs)
        for q in all_queues:
            q.put(data, event)

    @property
    def stats(self):
        with self._lock:
            return {
                ch: len(subs) for ch, subs in self._channels.items()
            }


class EventQueue:
    def __init__(self, max_size=100):
        self._queue = []
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._max_size = max_size
        self._closed = False

    def put(self, data, event=None):
        with self._lock:
            if len(self._queue) >= self._max_size:
                self._queue.pop(0)
            self._queue.append((data, event))
        self._event.set()

    def get(self, timeout=30):
        if self._closed:
            return None, None
        self._event.wait(timeout=timeout)
        with self._lock:
            if self._queue:
                item = self._queue.pop(0)
                if not self._queue:
                    self._event.clear()
                return item
        return None, None

    def close(self):
        self._closed = True
        self._event.set()


def sse_channel(app, path, event_bus, channel):
    @app.get(path)
    def sse_stream(req, res):
        q = event_bus.subscribe(channel)
        counter = [0]

        def generate():
            try:
                while True:
                    data, event = q.get(timeout=30)
                    if data is None:
                        yield sse_event("", event="ping")
                        continue
                    counter[0] += 1
                    yield sse_event(data, event=event, id=counter[0])
            except GeneratorExit:
                event_bus.unsubscribe(channel, q)

        return SSEResponse(generate())


class ChunkedResponse(StreamResponse):
    def __init__(self, generator, content_type="application/octet-stream", headers=None):
        extra = {"Transfer-Encoding": "chunked"}
        if headers:
            extra.update(headers)
        super().__init__(generator, content_type=content_type, headers=extra)


def stream_file(filepath, chunk_size=8192):
    import mimetypes
    content_type, _ = mimetypes.guess_type(filepath)
    content_type = content_type or "application/octet-stream"

    def gen():
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    return StreamResponse(gen(), content_type=content_type)
