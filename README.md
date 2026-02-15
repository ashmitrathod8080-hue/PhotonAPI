# ⚡ PhotonAPI

### Ship ML models. No assembly required.

The all-in-one Python framework built for AI developers. Get auth, database, model serving, caching, and monitoring out of the box — no configuration needed.

Go from notebook to live API before your coffee gets cold.

```
pip install photonapi   # coming soon
python example.py       # try it now
```

---

## Get started in 60 seconds

```python
from photonapi import PhotonAPI, ModelRegistry

app = PhotonAPI()
models = ModelRegistry(app)

@models.register("sentiment")
class SentimentModel:
    def predict(self, data):
        return {"label": "positive", "score": 0.95}

app.run()
```

That's it. You just deployed a model with predict, batch, health, and info endpoints. No YAML, no Docker, no config files.

---

## Why PhotonAPI?

Most Python frameworks make you glue together 15 packages just to serve a model behind an API. PhotonAPI ships everything you need in one import.

**Zero dependencies.** Runs on the Python standard library. Nothing to install, nothing to break.

**AI-first.** Model registry, batch prediction, A/B testing, drift detection, LLM streaming — built in from day one.

**Built to scale.** JWT auth, rate limiting, circuit breakers, health checks, Prometheus metrics, distributed tracing. This isn't a toy.

**Unreasonably fast to build with.** Go from idea to deployed API in minutes. One file, one command, done.

---

## Serve any model

Register a class, get a full API. Supports sklearn, ONNX, PyTorch, TensorFlow, and HuggingFace out of the box.

```python
from photonapi import ModelRegistry

models = ModelRegistry(app, cache_size=500)

@models.register("sentiment", version="v1", cache=True, monitor=True)
class SentimentModel:
    def predict(self, data):
        return {"label": "positive", "confidence": 0.95}

    def batch_predict(self, items):
        return [self.predict(item) for item in items]
```

You get these endpoints automatically:
- `POST /models/sentiment/predict`
- `POST /models/sentiment/batch`
- `GET /models/sentiment/health`
- `GET /models/sentiment/info`
- `GET /models` — list all registered models

Plus `ModelMonitor` for drift detection, A/B testing between versions, hot-swapping, and `Pipeline` for chaining preprocessing steps.

---

## Stream like ChatGPT

Stream tokens to the client in real-time with Server-Sent Events. Same pattern OpenAI uses.

```python
from photonapi import SSEResponse, sse_event

@app.get("/chat")
def chat(req, res):
    def generate():
        for token in my_llm(prompt):
            yield sse_event({"token": token})
        yield sse_event({"done": True}, event="done")
    return SSEResponse(generate())
```

Also includes `EventBus` for pub/sub, `sse_channel()` for managed endpoints, `ChunkedResponse`, and `stream_file()`.

---

## Auth in 5 lines

JWT, OAuth2, API keys, sessions, password hashing — all built in.

```python
from photonapi import JWT, PasswordHasher, require_auth

jwt = JWT(secret_key="your-secret", expiry=3600)
hasher = PasswordHasher()

@app.post("/register")
def register(req, res):
    hashed = hasher.hash(req.json["password"])
    user = User.create(email=req.json["email"], password_hash=hashed)
    token = jwt.encode({"user_id": user["id"], "role": "user"})
    return {"token": token}, 201

@app.get("/me")
@require_auth(jwt)
def me(req, res):
    return {"user": req.user}
```

Password hashing uses bcrypt or argon2 if installed, falls back to PBKDF2 (standard library). Also ships `CSRFProtection`, `InputSanitizer`, `APIKeyAuth`, `SessionManager`, `OAuth2Provider`, and `require_role`.

---

## Database with zero setup

SQLite works instantly. Postgres and MySQL with one line change.

```python
from photonapi import auto_crud, ForeignKey, Index

db = app.init_db("app.db")

User = db.model("users",
    username=str,
    email=str,
    _email_idx=Index("email", unique=True),
)

Post = db.model("posts",
    title=str,
    body=str,
    author_id=ForeignKey("users"),
)

auto_crud(app, Post, prefix="/api/posts")
```

That gives you GET, POST, PUT, DELETE on `/api/posts` — full CRUD, no boilerplate.

Query directly when you need to:

```python
Post.create(title="Hello", body="World")
Post.find(1)
Post.where(author_id=1)
Post.paginate(page=1, per_page=20)
Post.upsert({"title": "Hello"}, body="Updated")
Post.bulk_create([{"title": "A"}, {"title": "B"}])
```

Swap backends in one line:

```python
db = app.init_db(backend="postgresql", host="localhost", database="mydb")
```

Both come with connection pooling. Migrations are built in too:

```python
from photonapi import MigrationManager

migrations = MigrationManager(db)

@migrations.migration("001_add_status")
def add_status(db):
    db.execute("ALTER TABLE posts ADD COLUMN status TEXT DEFAULT 'draft'")

migrations.run()
```

---

## Validate everything

Bad input gets a clean 422 with field-level errors. No exceptions to catch, no edge cases to handle.

```python
from photonapi import Schema, String, Email, Integer, validate

contact_schema = Schema(
    name=String(min_length=2, max_length=50),
    email=Email(),
    age=Integer(min_value=18, max_value=120),
)

@app.post("/contact")
@validate(contact_schema)
def contact(req, res):
    return {"received": req.validated}
```

Supports `min_value`, `max_value`, `min_length`, `max_length`, `choices`, `pattern`, `nullable`, `coerce`, `each` (list items), nested `schema`. Export to OpenAPI with `to_openapi()`.

---

## Rate limiting that scales

Three strategies out of the box. Redis backend for distributed setups.

```python
from photonapi import RateLimiter, get_remote_address

limiter = RateLimiter(key_func=get_remote_address, strategy="sliding-window")

@app.get("/api/data")
@limiter.limit("10/minute")
def data(req, res):
    return {"data": [1, 2, 3]}
```

Auto-sends `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers. 429 with `Retry-After` when limits are hit. Supports shared limits, per-user, per-endpoint, dynamic limits, whitelist/blacklist.

---

## Caching

```python
from photonapi import CacheManager

cache = CacheManager(app)

@cache.cached(ttl=300, prefix="api")
def expensive_query():
    return db.execute("SELECT ...")
```

LRU with TTL. Redis cache for distributed. Tiered caching (memory → Redis) for the best of both.

---

## Middleware stack

```python
from photonapi import (
    CORSMiddleware, LoggingMiddleware, SecurityHeadersMiddleware,
    GZipMiddleware, RequestIDMiddleware,
)

app.use(LoggingMiddleware())
app.use(CORSMiddleware(allow_origins="*"))
app.use(SecurityHeadersMiddleware())
app.use(GZipMiddleware(min_size=1000))
app.use(RequestIDMiddleware())
```

Write your own in 5 lines:

```python
def auth_check(req, res, next_fn):
    if not req.headers.get("Authorization"):
        return res.json({"error": "no token"}, 401)
    return next_fn()

app.use(auth_check)
```

Also ships `IPFilterMiddleware`, `TrustedProxyMiddleware`, `TimeoutMiddleware`, `SessionMiddleware`.

---

## Monitoring & observability

Prometheus metrics, Kubernetes health probes, distributed tracing — ops tooling built in.

```python
from photonapi import MetricsCollector, HealthCheck, Tracer

metrics = MetricsCollector(app)
health = HealthCheck(app)
health.add_check("database", lambda: health._check_database(db))

tracer = Tracer(service_name="my-api")
with tracer.start_span("db-query") as span:
    result = db.execute("SELECT ...")
    span.set_attribute("rows", len(result))
```

- `GET /health/live` — liveness probe
- `GET /health/ready` — readiness probe
- `GET /health/startup` — startup probe
- `GET /metrics` — Prometheus-format metrics

W3C Traceparent compatible. Exporters for Console, Jaeger, Zipkin.

---

## Error handling & resilience

```python
from photonapi import NotFound, CircuitBreaker

app.enable_error_handler(debug=True)

@app.get("/item/<int:id>")
def get_item(req, res, id):
    if not item:
        raise NotFound("Item not found")

breaker = CircuitBreaker(failure_threshold=5, timeout=30)
result = breaker.call(external_api)
```

Structured errors (`NotFound`, `BadRequest`, etc.), circuit breaker, retry with exponential backoff, request timeouts.

---

## Background tasks

```python
from photonapi import background, periodic

app.enable_tasks(workers=4)

@background
def send_email(to, subject):
    ...

@periodic(60)
def cleanup():
    ...
```

Also supports `after_response` for post-response work.

---

## Routing & blueprints

```python
@app.get("/users/<int:id>")
def get_user(req, res, id):
    return {"id": id}

from photonapi import Blueprint

api = Blueprint("api", prefix="/api/v1")

@api.get("/users")
def list_users(req, res):
    return {"users": []}

app.register(api)
```

Path params auto-convert: `<int:id>`, `<str:name>`, `<path:filepath>`.

---

## OpenAPI & docs

Auto-generated OpenAPI 3.1 spec. Swagger UI at `/docs` out of the box.

```python
from photonapi import generate_openapi_spec

@app.get("/openapi.json")
def spec(req, res):
    return generate_openapi_spec(app, title="My API", version="1.0")
```

---

## Testing

```python
from photonapi import TestClient

client = TestClient(app)
resp = client.get("/ping")
assert resp.status_code == 200

resp = client.post("/api/posts", json={"title": "Test"})
assert resp.status_code == 201
```

---

## CLI

```
python -m photonapi new myapp      # scaffold a project
python -m photonapi run            # start the server
python -m photonapi routes         # list all routes
python -m photonapi db migrate     # run migrations
python -m photonapi test           # run tests
```

---

## Everything else

| Feature | What it does |
|---------|-------------|
| **Debug Toolbar** | Request timing, SQL queries, memory usage in dev mode |
| **Logging** | JSON + colored formatters, sensitive data filtering, request ID tracking |
| **Hot Reload** | Auto-restart on file save (`.py`, `.html`, `.css`, `.js`) |
| **Templates** | Variables, if/else, for loops — no Jinja needed |
| **Hooks** | `@app.before_request`, `@app.on_startup`, `@app.on_shutdown` |
| **Graceful Shutdown** | SIGINT/SIGTERM handling, request draining |
| **Static Files** | Path traversal protection built in |
| **Cookies** | HttpOnly, SameSite, Max-Age secure defaults |

---

## Architecture

25 modules. 8000+ lines. 120+ exports. Zero dependencies.

```
photonapi/
├── app.py             Core framework & server
├── auth.py            JWT, OAuth2, API keys, sessions
├── cache.py           LRU, Redis, tiered caching
├── cli.py             CLI scaffolding
├── database.py        ORM, pooling, multi-backend
├── docs.py            Auto API docs
├── errors.py          Circuit breaker, retry
├── limiter.py         Rate limiting
├── logging_ext.py     JSON & color logging
├── middleware.py      CORS, security, gzip
├── migration.py       DB migrations
├── ml.py              Model serving, A/B testing
├── monitoring.py      Metrics, health checks
├── openapi.py         OpenAPI 3.1 spec
├── profiler.py        Debug toolbar
├── reloader.py        Hot reload
├── request.py         Request parsing
├── response.py        Response helpers
├── router.py          Routing & blueprints
├── streaming.py       SSE, EventBus
├── tasks.py           Background jobs
├── testing.py         TestClient
├── tracing.py         Distributed tracing
└── validation.py      Schema validation
```

---

## Optional dependencies

Everything works with zero installs. Add these only if you want them:

| Package | Unlocks |
|---------|---------|
| `bcrypt` / `argon2-cffi` | Stronger password hashing |
| `redis` | Distributed caching & rate limiting |
| `psycopg2-binary` | PostgreSQL |
| `pymysql` | MySQL |
| `scikit-learn` | sklearn model loading |
| `onnxruntime` | ONNX model loading |
| `torch` | PyTorch model loading |
| `tensorflow` | TensorFlow model loading |
| `transformers` | HuggingFace model loading |

---

## License

MIT

---

Built by [Ashmit](https://github.com/ashmitrathod8080-hue).
