"""
Authentication & Security module for PhotonAPI.

Provides JWT, OAuth2, API key auth, session management,
password hashing, CSRF protection, and input sanitization.
All built with stdlib — optional bcrypt/argon2 for password hashing.
"""

import hashlib
import hmac
import base64
import json
import time
import os
import re
import secrets
import threading
import html as html_module
from functools import wraps
from datetime import datetime, timezone
from collections import OrderedDict


# ─── JWT Authentication ─────────────────────────────────────────────

class JWT:
    """
    JSON Web Token implementation using HMAC-SHA256.
    No external dependencies — uses stdlib hmac + hashlib.
    """

    def __init__(self, secret_key=None, algorithm="HS256", expiry=3600,
                 issuer=None, audience=None):
        self.secret_key = secret_key or secrets.token_hex(32)
        self.algorithm = algorithm
        self.expiry = expiry
        self.issuer = issuer
        self.audience = audience
        self._revoked = set()
        self._lock = threading.Lock()

    @staticmethod
    def _b64encode(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    @staticmethod
    def _b64decode(s):
        padding = 4 - len(s) % 4
        if padding != 4:
            s += "=" * padding
        return base64.urlsafe_b64decode(s)

    def _sign(self, message):
        return hmac.new(
            self.secret_key.encode(),
            message.encode(),
            hashlib.sha256
        ).digest()

    def encode(self, payload, expiry=None):
        """Create a JWT token from a payload dict."""
        header = {"alg": self.algorithm, "typ": "JWT"}

        now = int(time.time())
        claims = {**payload}
        claims["iat"] = now
        claims["exp"] = now + (expiry or self.expiry)

        if self.issuer:
            claims["iss"] = self.issuer
        if self.audience:
            claims["aud"] = self.audience

        if "jti" not in claims:
            claims["jti"] = secrets.token_hex(16)

        header_b64 = self._b64encode(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = self._b64encode(json.dumps(claims, separators=(",", ":"), default=str).encode())

        message = f"{header_b64}.{payload_b64}"
        signature = self._b64encode(self._sign(message))

        return f"{message}.{signature}"

    def decode(self, token, verify=True):
        """Decode and verify a JWT token. Returns the payload dict."""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise JWTError("Invalid token format")

            header_b64, payload_b64, sig_b64 = parts

            if verify:
                message = f"{header_b64}.{payload_b64}"
                expected_sig = self._b64encode(self._sign(message))
                if not hmac.compare_digest(sig_b64, expected_sig):
                    raise JWTError("Invalid signature")

            payload = json.loads(self._b64decode(payload_b64))

            if verify:
                now = int(time.time())
                if "exp" in payload and payload["exp"] < now:
                    raise JWTError("Token expired")
                if self.issuer and payload.get("iss") != self.issuer:
                    raise JWTError("Invalid issuer")
                if self.audience and payload.get("aud") != self.audience:
                    raise JWTError("Invalid audience")

                jti = payload.get("jti")
                if jti and jti in self._revoked:
                    raise JWTError("Token has been revoked")

            return payload

        except JWTError:
            raise
        except Exception as e:
            raise JWTError(f"Token decode failed: {e}")

    def revoke(self, token):
        """Revoke a token by its JTI."""
        try:
            payload = self.decode(token, verify=False)
            jti = payload.get("jti")
            if jti:
                with self._lock:
                    self._revoked.add(jti)
        except Exception:
            pass

    def refresh(self, token, expiry=None):
        """Issue a new token from an existing (valid) token."""
        payload = self.decode(token)
        payload.pop("iat", None)
        payload.pop("exp", None)
        payload.pop("jti", None)
        return self.encode(payload, expiry=expiry)

    def require(self, roles=None, permissions=None):
        """Decorator to protect routes with JWT auth."""
        def decorator(fn):
            @wraps(fn)
            def wrapper(req, res, *args, **kwargs):
                auth_header = req.headers.get("Authorization", "")
                if not auth_header.startswith("Bearer "):
                    res.json({"error": "Missing or invalid Authorization header"}, 401)
                    return res

                token = auth_header[7:]
                try:
                    payload = self.decode(token)
                except JWTError as e:
                    res.json({"error": str(e)}, 401)
                    return res

                if roles:
                    user_roles = payload.get("roles", [])
                    if not any(r in user_roles for r in roles):
                        res.json({"error": "Insufficient role"}, 403)
                        return res

                if permissions:
                    user_perms = payload.get("permissions", [])
                    if not all(p in user_perms for p in permissions):
                        res.json({"error": "Insufficient permissions"}, 403)
                        return res

                req.user = payload
                req.token = token
                return fn(req, res, *args, **kwargs)
            return wrapper
        return decorator


class JWTError(Exception):
    """Raised when JWT validation fails."""
    pass


# ─── OAuth2 Support ──────────────────────────────────────────────────

class OAuth2Provider:
    """
    Generic OAuth2 provider supporting Google, GitHub, etc.
    Requires `urllib.request` (stdlib) for token exchange.
    """

    PROVIDERS = {
        "google": {
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "userinfo_url": "https://www.googleapis.com/oauth2/v2/userinfo",
            "scopes": ["openid", "email", "profile"],
        },
        "github": {
            "auth_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "userinfo_url": "https://api.github.com/user",
            "scopes": ["read:user", "user:email"],
        },
    }

    def __init__(self, provider, client_id, client_secret, redirect_uri,
                 scopes=None, auth_url=None, token_url=None, userinfo_url=None):
        config = self.PROVIDERS.get(provider, {})
        self.provider = provider
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes or config.get("scopes", [])
        self.auth_url = auth_url or config.get("auth_url", "")
        self.token_url = token_url or config.get("token_url", "")
        self.userinfo_url = userinfo_url or config.get("userinfo_url", "")
        self._states = {}
        self._lock = threading.Lock()

    def get_auth_url(self, state=None):
        """Generate the OAuth2 authorization URL."""
        state = state or secrets.token_hex(16)
        with self._lock:
            self._states[state] = time.time()

        from urllib.parse import urlencode
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
        }
        if self.provider == "google":
            params["access_type"] = "offline"

        return f"{self.auth_url}?{urlencode(params)}", state

    def exchange_code(self, code, state=None):
        """Exchange authorization code for access token."""
        if state:
            with self._lock:
                if state not in self._states:
                    raise OAuth2Error("Invalid state parameter")
                if time.time() - self._states[state] > 600:
                    del self._states[state]
                    raise OAuth2Error("State expired")
                del self._states[state]

        import urllib.request
        from urllib.parse import urlencode

        data = urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }).encode()

        headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
        req = urllib.request.Request(self.token_url, data=data, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                if "error" in result:
                    raise OAuth2Error(result.get("error_description", result["error"]))
                return result
        except urllib.error.URLError as e:
            raise OAuth2Error(f"Token exchange failed: {e}")

    def get_user_info(self, access_token):
        """Fetch user info from the provider."""
        import urllib.request

        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        req = urllib.request.Request(self.userinfo_url, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            raise OAuth2Error(f"User info fetch failed: {e}")

    def register_routes(self, app, jwt=None, callback_handler=None):
        """Auto-register /auth/<provider>/login and /auth/<provider>/callback routes."""
        provider = self

        @app.get(f"/auth/{self.provider}/login")
        def oauth_login(req, res):
            url, state = provider.get_auth_url()
            res.set_cookie(f"oauth_state_{provider.provider}", state, max_age=600, httponly=True)
            return res.redirect(url)

        @app.get(f"/auth/{self.provider}/callback")
        def oauth_callback(req, res):
            code = req.get_query("code")
            state = req.get_query("state")
            error = req.get_query("error")

            if error:
                return {"error": error}, 400

            if not code:
                return {"error": "No authorization code"}, 400

            try:
                token_data = provider.exchange_code(code, state)
                access_token = token_data.get("access_token")
                user_info = provider.get_user_info(access_token)

                if callback_handler:
                    return callback_handler(req, res, user_info, token_data)

                if jwt:
                    token = jwt.encode({
                        "sub": str(user_info.get("id", user_info.get("sub", ""))),
                        "email": user_info.get("email", ""),
                        "name": user_info.get("name", ""),
                        "provider": provider.provider,
                    })
                    return {"token": token, "user": user_info}

                return {"user": user_info, "access_token": access_token}

            except OAuth2Error as e:
                return {"error": str(e)}, 400


class OAuth2Error(Exception):
    """Raised when OAuth2 flow fails."""
    pass


# ─── API Key Authentication ─────────────────────────────────────────

class APIKeyAuth:
    """
    API key authentication with key management.
    Keys stored in-memory with optional external store.
    """

    def __init__(self, header_name="X-API-Key", query_param="api_key",
                 key_store=None):
        self.header_name = header_name
        self.query_param = query_param
        self._keys = {}  # key -> metadata
        self._lock = threading.Lock()

        if key_store:
            self._keys.update(key_store)

    def create_key(self, name, scopes=None, rate_limit=None, expires_in=None):
        """Generate a new API key."""
        key = f"pk_{secrets.token_hex(24)}"
        meta = {
            "name": name,
            "scopes": scopes or ["*"],
            "rate_limit": rate_limit,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": None,
            "active": True,
        }
        if expires_in:
            meta["expires_at"] = (datetime.now(timezone.utc).timestamp() + expires_in)

        with self._lock:
            self._keys[key] = meta

        return key, meta

    def revoke_key(self, key):
        """Revoke an API key."""
        with self._lock:
            if key in self._keys:
                self._keys[key]["active"] = False
                return True
        return False

    def validate_key(self, key):
        """Validate an API key and return its metadata."""
        with self._lock:
            meta = self._keys.get(key)

        if not meta:
            return None
        if not meta["active"]:
            return None
        if meta["expires_at"] and time.time() > meta["expires_at"]:
            return None
        return meta

    def require(self, scopes=None):
        """Decorator to require API key authentication."""
        def decorator(fn):
            @wraps(fn)
            def wrapper(req, res, *args, **kwargs):
                key = (req.headers.get(self.header_name) or
                       req.get_query(self.query_param))

                if not key:
                    res.json({"error": f"API key required (header '{self.header_name}' or query '{self.query_param}')"}, 401)
                    return res

                meta = self.validate_key(key)
                if not meta:
                    res.json({"error": "Invalid or expired API key"}, 401)
                    return res

                if scopes and "*" not in meta["scopes"]:
                    if not all(s in meta["scopes"] for s in scopes):
                        res.json({"error": "Insufficient API key scope"}, 403)
                        return res

                req.api_key = key
                req.api_key_meta = meta
                return fn(req, res, *args, **kwargs)
            return wrapper
        return decorator

    def list_keys(self):
        """List all API keys (masked)."""
        with self._lock:
            result = []
            for key, meta in self._keys.items():
                result.append({
                    "key": key[:8] + "..." + key[-4:],
                    **{k: v for k, v in meta.items()},
                })
            return result


# ─── Session Management ─────────────────────────────────────────────

class SessionManager:
    """
    Server-side session management with signed cookies.
    Sessions stored in-memory with configurable TTL.
    """

    def __init__(self, secret_key=None, cookie_name="session_id",
                 max_age=86400, secure=False, httponly=True, samesite="Lax"):
        self.secret_key = secret_key or secrets.token_hex(32)
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.secure = secure
        self.httponly = httponly
        self.samesite = samesite
        self._sessions = OrderedDict()
        self._max_sessions = 10000
        self._lock = threading.Lock()

    def _sign(self, session_id):
        sig = hmac.new(self.secret_key.encode(), session_id.encode(), hashlib.sha256).hexdigest()[:16]
        return f"{session_id}.{sig}"

    def _verify(self, signed_id):
        if "." not in signed_id:
            return None
        session_id, sig = signed_id.rsplit(".", 1)
        expected = hmac.new(self.secret_key.encode(), session_id.encode(), hashlib.sha256).hexdigest()[:16]
        if hmac.compare_digest(sig, expected):
            return session_id
        return None

    def create(self, data=None):
        """Create a new session."""
        session_id = secrets.token_hex(24)
        with self._lock:
            self._sessions[session_id] = {
                "data": data or {},
                "created_at": time.time(),
                "last_accessed": time.time(),
            }
            if len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
        return self._sign(session_id)

    def get(self, signed_id):
        """Retrieve session data."""
        session_id = self._verify(signed_id)
        if not session_id:
            return None

        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            if time.time() - session["created_at"] > self.max_age:
                del self._sessions[session_id]
                return None
            session["last_accessed"] = time.time()
            return session["data"]

    def update(self, signed_id, data):
        """Update session data."""
        session_id = self._verify(signed_id)
        if not session_id:
            return False

        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["data"].update(data)
                self._sessions[session_id]["last_accessed"] = time.time()
                return True
        return False

    def destroy(self, signed_id):
        """Destroy a session."""
        session_id = self._verify(signed_id)
        if session_id:
            with self._lock:
                self._sessions.pop(session_id, None)

    def middleware(self):
        """Returns middleware that attaches session to request."""
        manager = self

        def session_middleware(req, res, next_fn):
            signed_id = req.cookies.get(manager.cookie_name)
            session_data = None

            if signed_id:
                session_data = manager.get(signed_id)

            if session_data is None:
                signed_id = manager.create()
                session_data = {}

            req.session = session_data
            req.session_id = signed_id

            def save_session():
                manager.update(signed_id, req.session)
                res.set_cookie(
                    manager.cookie_name, signed_id,
                    max_age=manager.max_age,
                    httponly=manager.httponly,
                    secure=manager.secure,
                    samesite=manager.samesite,
                )

            result = next_fn()
            save_session()
            return result

        return session_middleware

    def cleanup(self):
        """Remove expired sessions."""
        now = time.time()
        with self._lock:
            expired = [sid for sid, s in self._sessions.items()
                       if now - s["created_at"] > self.max_age]
            for sid in expired:
                del self._sessions[sid]
        return len(expired)


# ─── Password Hashing ───────────────────────────────────────────────

class PasswordHasher:
    """
    Password hashing with multiple backend support.
    Falls back to PBKDF2 (stdlib) if bcrypt/argon2 aren't installed.
    """

    def __init__(self, algorithm="auto", rounds=None):
        self.algorithm = algorithm
        self.rounds = rounds
        self._backend = self._detect_backend()

    def _detect_backend(self):
        if self.algorithm == "bcrypt" or self.algorithm == "auto":
            try:
                import bcrypt  # type: ignore[import-not-found]
                return "bcrypt"
            except ImportError:
                pass

        if self.algorithm == "argon2" or self.algorithm == "auto":
            try:
                import argon2  # type: ignore[import-not-found]
                return "argon2"
            except ImportError:
                pass

        return "pbkdf2"

    def hash(self, password):
        """Hash a password. Returns a string safe for storage."""
        if self._backend == "bcrypt":
            import bcrypt  # type: ignore[import-not-found]
            rounds = self.rounds or 12
            salt = bcrypt.gensalt(rounds=rounds)
            return bcrypt.hashpw(password.encode(), salt).decode()

        elif self._backend == "argon2":
            import argon2  # type: ignore[import-not-found]
            ph = argon2.PasswordHasher()
            return ph.hash(password)

        else:
            salt = os.urandom(32)
            rounds = self.rounds or 600000
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
            return f"pbkdf2:sha256:{rounds}${salt.hex()}${dk.hex()}"

    def verify(self, password, hashed):
        """Verify a password against its hash."""
        if self._backend == "bcrypt" and not hashed.startswith("pbkdf2:"):
            import bcrypt  # type: ignore[import-not-found]
            try:
                return bcrypt.checkpw(password.encode(), hashed.encode())
            except Exception:
                return False

        elif self._backend == "argon2" and not hashed.startswith("pbkdf2:"):
            import argon2  # type: ignore[import-not-found]
            ph = argon2.PasswordHasher()
            try:
                return ph.verify(hashed, password)
            except Exception:
                return False

        elif hashed.startswith("pbkdf2:"):
            parts = hashed.split("$")
            if len(parts) != 3:
                return False
            header = parts[0]
            salt_hex = parts[1]
            dk_hex = parts[2]
            rounds = int(header.split(":")[2])
            salt = bytes.fromhex(salt_hex)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
            return hmac.compare_digest(dk.hex(), dk_hex)

        return False

    def needs_rehash(self, hashed):
        """Check if a hash needs to be rehashed (e.g., algorithm upgrade)."""
        if self._backend == "argon2" and not hashed.startswith("$argon2"):
            return True
        if self._backend == "bcrypt" and not hashed.startswith("$2"):
            return True
        return False


# ─── CSRF Protection ────────────────────────────────────────────────

class CSRFProtection:
    """
    Cross-Site Request Forgery protection.
    Uses double-submit cookie pattern.
    """

    def __init__(self, secret_key=None, cookie_name="csrf_token",
                 header_name="X-CSRF-Token", field_name="csrf_token",
                 max_age=3600, exempt_methods=None):
        self.secret_key = secret_key or secrets.token_hex(32)
        self.cookie_name = cookie_name
        self.header_name = header_name
        self.field_name = field_name
        self.max_age = max_age
        self.exempt_methods = exempt_methods or ["GET", "HEAD", "OPTIONS"]
        self._exempt_routes = set()

    def generate_token(self, session_id=""):
        """Generate a CSRF token."""
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = f"{session_id}:{timestamp}:{nonce}"
        sig = hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{timestamp}.{nonce}.{sig}"

    def validate_token(self, token, session_id=""):
        """Validate a CSRF token."""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return False
            timestamp, nonce, sig = parts
            if int(time.time()) - int(timestamp) > self.max_age:
                return False
            message = f"{session_id}:{timestamp}:{nonce}"
            expected = hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()[:32]
            return hmac.compare_digest(sig, expected)
        except Exception:
            return False

    def exempt(self, fn):
        """Mark a route as CSRF-exempt."""
        self._exempt_routes.add(fn.__name__)
        return fn

    def middleware(self):
        """Returns CSRF middleware."""
        csrf = self

        def csrf_middleware(req, res, next_fn):
            if req.method in csrf.exempt_methods:
                token = csrf.generate_token(getattr(req, "session_id", ""))
                res.set_cookie(csrf.cookie_name, token, max_age=csrf.max_age,
                               httponly=False, samesite="Strict")
                req.csrf_token = token
                return next_fn()

            # Check for exempt route
            # Get token from header or form body
            token = (req.headers.get(csrf.header_name) or
                     (req.form.get(csrf.field_name, [None])[0] if isinstance(req.form.get(csrf.field_name), list) else req.form.get(csrf.field_name)) or
                     (req.json or {}).get(csrf.field_name))

            if not token:
                token = req.cookies.get(csrf.cookie_name)

            cookie_token = req.cookies.get(csrf.cookie_name)

            if not token or not cookie_token:
                res.json({"error": "CSRF token missing"}, 403)
                return res

            if not csrf.validate_token(token):
                res.json({"error": "CSRF token invalid or expired"}, 403)
                return res

            return next_fn()

        return csrf_middleware


# ─── Input Sanitization ─────────────────────────────────────────────

class InputSanitizer:
    """
    Sanitize user input to prevent XSS and injection attacks.
    """

    # Dangerous HTML tags and attributes
    DANGEROUS_TAGS = re.compile(r'<\s*(script|iframe|object|embed|form|style|link|meta|base)[^>]*>.*?</\s*\1\s*>', re.I | re.S)
    DANGEROUS_SELF_CLOSING = re.compile(r'<\s*(script|iframe|object|embed|form|style|link|meta|base)[^>]*/?\s*>', re.I)
    EVENT_HANDLERS = re.compile(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', re.I)
    JAVASCRIPT_URI = re.compile(r'(?:href|src|action)\s*=\s*["\']?\s*javascript:', re.I)
    SQL_INJECTION_PATTERNS = re.compile(
        r"(\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|CREATE|EXEC|EXECUTE)\b.*\b(FROM|INTO|TABLE|WHERE|SET)\b)",
        re.I
    )

    @classmethod
    def sanitize_html(cls, text):
        """Remove dangerous HTML tags and attributes."""
        if not isinstance(text, str):
            return text
        text = cls.DANGEROUS_TAGS.sub("", text)
        text = cls.DANGEROUS_SELF_CLOSING.sub("", text)
        text = cls.EVENT_HANDLERS.sub("", text)
        text = cls.JAVASCRIPT_URI.sub("", text)
        return text

    @classmethod
    def escape_html(cls, text):
        """Escape HTML special characters."""
        if not isinstance(text, str):
            return text
        return html_module.escape(text)

    @classmethod
    def sanitize_sql_param(cls, value):
        """Basic SQL injection check — use parameterized queries instead."""
        if isinstance(value, str) and cls.SQL_INJECTION_PATTERNS.search(value):
            raise ValueError(f"Potentially dangerous SQL detected in input")
        return value

    @classmethod
    def sanitize_dict(cls, data, escape=True):
        """Recursively sanitize all string values in a dict."""
        if isinstance(data, dict):
            return {k: cls.sanitize_dict(v, escape) for k, v in data.items()}
        elif isinstance(data, list):
            return [cls.sanitize_dict(item, escape) for item in data]
        elif isinstance(data, str):
            text = cls.sanitize_html(data)
            if escape:
                text = cls.escape_html(text)
            return text
        return data

    @classmethod
    def middleware(cls, escape=False):
        """Returns sanitization middleware."""
        def sanitize_middleware(req, res, next_fn):
            if req._json is not None:
                req._json = cls.sanitize_dict(req._json, escape=escape)
            return next_fn()
        return sanitize_middleware


# ─── Auth Decorators (convenience) ───────────────────────────────────

def require_auth(auth_provider=None):
    if callable(auth_provider) and not isinstance(auth_provider, JWT):
        fn = auth_provider
        @wraps(fn)
        def wrapper(req, res, *args, **kwargs):
            if not req.headers.get("Authorization"):
                res.json({"error": "Authentication required"}, 401)
                return res
            return fn(req, res, *args, **kwargs)
        return wrapper

    def decorator(fn):
        @wraps(fn)
        def wrapper(req, res, *args, **kwargs):
            auth_header = req.headers.get("Authorization", "")
            if not auth_header:
                res.json({"error": "Authentication required"}, 401)
                return res
            if auth_provider and isinstance(auth_provider, JWT):
                token = auth_header.replace("Bearer ", "").strip()
                payload = auth_provider.decode(token)
                if payload is None:
                    res.json({"error": "Invalid or expired token"}, 401)
                    return res
                req.user = payload
            return fn(req, res, *args, **kwargs)
        return wrapper
    return decorator


def require_role(auth_provider, *roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(req, res, *args, **kwargs):
            auth_header = req.headers.get("Authorization", "")
            if not auth_header:
                res.json({"error": "Authentication required"}, 401)
                return res
            if isinstance(auth_provider, JWT):
                token = auth_header.replace("Bearer ", "").strip()
                payload = auth_provider.decode(token)
                if payload is None:
                    res.json({"error": "Invalid or expired token"}, 401)
                    return res
                req.user = payload
            user = getattr(req, "user", None)
            if not user:
                res.json({"error": "Authentication required"}, 401)
                return res
            user_role = user.get("role", "")
            user_roles = user.get("roles", [])
            if user_role:
                user_roles = list(set(user_roles + [user_role]))
            if not any(r in user_roles for r in roles):
                res.json({"error": "Insufficient role", "required": list(roles)}, 403)
                return res
            return fn(req, res, *args, **kwargs)
        return wrapper
    return decorator
