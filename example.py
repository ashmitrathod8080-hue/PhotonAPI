from photonapi import (
    PhotonAPI, Blueprint, RateLimiter,
    CORSMiddleware, LoggingMiddleware, SecurityHeadersMiddleware,
    RequestIDMiddleware, GZipMiddleware,
    get_remote_address, Schema, Field, validate,
    String, Email, Integer,
    auto_crud, background, after_response, periodic,
    ModelRegistry, Pipeline, SSEResponse, sse_event, stream_sse,
    EventBus, sse_channel,
    ForeignKey, Index,
    JWT, APIKeyAuth, PasswordHasher, require_auth, require_role,
    CacheManager,
    MetricsCollector, HealthCheck,
    LogManager,
    ErrorHandler, NotFound, BadRequest,
    TestClient,
    generate_openapi_spec,
    MigrationManager,
    DebugToolbar,
)
import time
import re

app = PhotonAPI(
    debug=True,
    static_dir="./static",
    template_dir="./templates",
    title="PhotonAPI",
    version="2.0.0",
    shutdown_timeout=10,
)


limiter = RateLimiter(key_func=get_remote_address, strategy="sliding-window")
jwt = JWT(secret_key="change-me-in-production", algorithm="HS256", expiry=3600)
hasher = PasswordHasher()
cache = CacheManager(app)
metrics = MetricsCollector(app)
health = HealthCheck(app)
log_manager = LogManager(app)
events = EventBus()

app.use(RequestIDMiddleware())
app.use(LoggingMiddleware())
app.use(CORSMiddleware(allow_origins="*"))
app.use(SecurityHeadersMiddleware())
app.use(GZipMiddleware(min_size=1000))

if app.debug:
    toolbar = DebugToolbar(app)

db = app.init_db("app.db")
app.enable_tasks(workers=2)
app.enable_error_handler()
health.add_check("database", lambda: health._check_database(db))


User = db.model("users",
    username=str,
    email=str,
    password_hash=str,
    role=(str, "nullable"),
    _email_idx=Index("email", unique=True),
    _username_idx=Index("username", unique=True),
)

Post = db.model("posts",
    title=str,
    body=str,
    author_id=ForeignKey("users"),
    published=(bool, "nullable"),
)

Contact = db.model("contacts", name=str, email=str, message=str)

auto_crud(app, Post, prefix="/api/posts")


@app.get("/")
def index(req, res):
    return app.render("index.html")

@app.get("/ping")
def ping(req, res):
    return {"message": "pong", "framework": "PhotonAPI", "version": "2.0.0"}


@app.get("/users/<int:user_id>")
def get_user(req, res, user_id):
    user = User.find(user_id)
    if not user:
        raise NotFound(f"User {user_id} not found")
    safe = {k: v for k, v in user.items() if k != "password_hash"}
    return {"data": safe}

@app.get("/files/<path:filepath>")
def get_file(req, res, filepath):
    return {"file": filepath}


register_schema = Schema(
    username=String(min_length=3, max_length=30),
    email=Email(),
    password=String(min_length=8),
    role=Field(str, required=False, default="user", choices=["user", "admin"]),
)

@app.route("/auth/register", methods=["POST"])
@limiter.limit("10/hour")
@validate(register_schema)
def register(req, res):
    data = req.validated
    if User.exists(email=data["email"]):
        return {"error": "Email already registered"}, 409
    if User.exists(username=data["username"]):
        return {"error": "Username taken"}, 409
    hashed = hasher.hash(data["password"])
    user = User.create(
        username=data["username"],
        email=data["email"],
        password_hash=hashed,
        role=data.get("role", "user"),
    )
    token = jwt.encode({"user_id": user["id"], "role": user["role"]})
    return {"token": token, "user_id": user["id"]}, 201


login_schema = Schema(
    email=Email(),
    password=String(min_length=1),
)

@app.route("/auth/login", methods=["POST"])
@limiter.limit("20/minute")
@validate(login_schema)
def login(req, res):
    data = req.validated
    user = User.first(email=data["email"])
    if not user or not hasher.verify(data["password"], user["password_hash"]):
        return {"error": "Invalid credentials"}, 401
    token = jwt.encode({"user_id": user["id"], "role": user["role"]})
    return {"token": token, "user_id": user["id"]}


@app.get("/auth/me")
@require_auth(jwt)
def me(req, res):
    user = User.find(req.user["user_id"])
    if not user:
        return {"error": "User not found"}, 404
    safe = {k: v for k, v in user.items() if k != "password_hash"}
    return {"data": safe}


contact_schema = Schema(
    name=String(min_length=2, max_length=50),
    email=Email(),
    message=String(min_length=10, max_length=1000),
)

@app.route("/api/contact", methods=["POST"])
@limiter.limit("5/hour")
@validate(contact_schema)
def contact_form(req, res):
    data = req.validated
    Contact.create(**data)
    send_notification(data["name"], data["email"])
    return {"message": "Thanks! We'll be in touch.", "data": data}, 201


@background
def send_notification(name, email):
    time.sleep(2)
    print(f"  📧  Notification sent to {email} (from {name})")


@app.get("/api/data")
@limiter.limit("5/minute")
def api_data(req, res):
    return {"data": [1, 2, 3, 4, 5], "cached": False}

@app.route("/api/submit", methods=["POST"])
@limiter.limit("3/minute", error_message="Slow down, you're posting too fast")
def submit(req, res):
    body = req.json
    return {"received": body}, 201


@app.get("/api/search")
@limiter.shared_limit("10/minute", "search-group")
def search(req, res):
    q = req.get_query("q", "")
    return {"query": q, "results": []}

@app.get("/api/suggest")
@limiter.shared_limit("10/minute", "search-group")
def suggest(req, res):
    q = req.get_query("q", "")
    return {"query": q, "suggestions": [f"{q} example", f"{q} tutorial"]}


@app.get("/set-cookie")
def set_cookie(req, res):
    res.set_cookie("session", "abc123", max_age=3600, httponly=True)
    return {"message": "cookie set"}

@app.get("/get-cookie")
def get_cookie(req, res):
    session = req.cookies.get("session", "none")
    return {"session": session}


admin = Blueprint("admin", prefix="/admin")

@admin.get("/dashboard")
@require_auth(jwt)
@require_role(jwt, "admin")
def dashboard(req, res):
    stats = {
        "users": User.count(),
        "posts": Post.count(),
        "contacts": Contact.count(),
    }
    return {"section": "admin", "page": "dashboard", "stats": stats}

@admin.get("/users")
@require_auth(jwt)
@require_role(jwt, "admin")
def admin_users(req, res):
    page = int(req.get_query("page", "1"))
    return User.paginate(page=page, per_page=20)

app.register(admin)


@app.before_request
def add_request_id(req, res):
    if not hasattr(req, "id"):
        import uuid
        req.id = str(uuid.uuid4())[:8]
        res.set_header("X-Request-Id", req.id)

@app.after_request
def add_powered_by(req, res):
    res.set_header("X-Powered-By", "PhotonAPI/2.0")


@app.error(404)
def not_found(req, res, exc):
    res.json({"error": "Nothing here", "path": req.path, "hint": "Check the URL"}, 404)

@app.error(500)
def server_error(req, res, exc):
    res.json({"error": "Something broke", "details": str(exc) if app.debug else None}, 500)


@app.get("/old-path")
def old_path(req, res):
    return res.redirect("/ping")

@app.get("/search")
def web_search(req, res):
    q = req.get_query("q", "")
    page = req.get_query("page", "1")
    return {"query": q, "page": int(page)}


models = ModelRegistry(app, cache_size=500)


@models.register("sentiment", version="v1", cache=True, monitor=True)
class SentimentModel:
    positive = {"good", "great", "love", "amazing", "awesome", "excellent",
                "happy", "best", "fantastic", "wonderful", "beautiful", "perfect",
                "brilliant", "outstanding", "superb", "nice", "cool", "fun"}
    negative = {"bad", "terrible", "hate", "awful", "worst", "horrible",
                "sad", "angry", "poor", "ugly", "boring", "disappointing",
                "broken", "useless", "trash", "annoying", "stupid", "fail"}

    def preprocess(self, data):
        text = data.get("text", "")
        return re.findall(r'\w+', text.lower())

    def predict(self, words):
        pos = sum(1 for w in words if w in self.positive)
        neg = sum(1 for w in words if w in self.negative)
        total = len(words) or 1
        score = (pos - neg) / total
        confidence = min(abs(score) * 5, 1.0)
        if score > 0.05:
            label = "positive"
        elif score < -0.05:
            label = "negative"
        else:
            label = "neutral"
        return {
            "label": label,
            "confidence": round(confidence, 3),
            "scores": {"positive": pos, "negative": neg, "total_words": len(words)},
        }

    def batch_predict(self, items):
        return [self.predict(item) for item in items]


@models.register("similarity", version="v1", cache=True)
class SimilarityModel:
    def preprocess(self, data):
        a = set(re.findall(r'\w+', data.get("text_a", "").lower()))
        b = set(re.findall(r'\w+', data.get("text_b", "").lower()))
        return a, b

    def predict(self, inputs):
        a, b = inputs
        if not a and not b:
            return {"similarity": 0.0}
        intersection = len(a & b)
        union = len(a | b)
        jaccard = intersection / union if union else 0
        return {
            "similarity": round(jaccard, 4),
            "shared_words": sorted(a & b),
            "only_in_a": sorted(a - b),
            "only_in_b": sorted(b - a),
        }


text_pipeline = Pipeline("text-analysis")
text_pipeline.add("clean", lambda text: text.strip().lower())
text_pipeline.add("tokenize", lambda text: re.findall(r'\w+', text))
text_pipeline.add("analyze", lambda tokens: {
    "word_count": len(tokens),
    "unique_words": len(set(tokens)),
    "char_count": sum(len(t) for t in tokens),
    "avg_word_length": round(sum(len(t) for t in tokens) / max(len(tokens), 1), 2),
    "longest_word": max(tokens, key=len) if tokens else "",
    "top_words": sorted(
        [(w, tokens.count(w)) for w in set(tokens)],
        key=lambda x: -x[1]
    )[:10],
})
text_pipeline.serve(app, "/api/pipeline/text")


sse_channel(app, "/events/notifications", events, "notifications")


@app.get("/api/stream")
def stream_demo(req, res):
    prompt = req.get_query("prompt", "Tell me about PhotonAPI")
    response_text = (
        f"You asked: \"{prompt}\". "
        "PhotonAPI is a production-grade Python web framework built from scratch. "
        "It features JWT auth, rate limiting, database ORM with PostgreSQL/MySQL/SQLite, "
        "ML model serving with batch prediction and A/B testing, "
        "real-time SSE streaming, caching, monitoring, health checks, "
        "distributed tracing, circuit breakers, and a CLI — all with zero core dependencies."
    )
    words = response_text.split()

    def generate():
        for i, word in enumerate(words):
            time.sleep(0.08)
            yield sse_event({"token": word + " ", "index": i})
        yield sse_event({"done": True, "total_tokens": len(words)}, event="done")

    return SSEResponse(generate())


@app.route("/api/notify", methods=["POST"])
@require_auth(jwt)
def send_event(req, res):
    data = req.json
    events.publish("notifications", data or {"message": "New notification"})
    return {"sent": True}


@app.get("/openapi.json")
def openapi_spec(req, res):
    spec = generate_openapi_spec(app, title=app.title, version=app.version)
    return spec


@app.on_startup
def on_start():
    print("  \033[2m  Startup hooks executed\033[0m")

@app.on_shutdown
def on_stop():
    print("  \033[2m  Shutdown hooks executed\033[0m")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
