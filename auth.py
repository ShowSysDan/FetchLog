"""
Authentication for FetchLog — shared-session SSO with the sibling apps.

FetchLog joins the same single-sign-on family as Leash and 321Theater. The
mechanism is deliberately framework-agnostic:

  * Sessions are stored *server-side* in the PostgreSQL ``<shared>.app_sessions``
    table. The browser cookie (default name ``session``) carries only an opaque
    256-bit random session id (``secrets.token_urlsafe(32)``) — never any signed
    payload — so apps do NOT need to share a Flask ``SECRET_KEY`` to share login
    state. They only need the same database, the same ``shared`` schema, and the
    same cookie settings (name / path / SameSite / domain).

  * Users live in the shared ``<shared>.users`` table, which is owned and created
    by 321Theater. FetchLog treats it as **read-only** and verifies passwords
    with Werkzeug (scrypt). It never creates or modifies users.

  * Which accounts may sign in is controlled by per-user flags in the shared
    directory, set by an admin in 321Theater: ``is_app_user`` ("user of the
    shared apps") and ``is_app_admin`` ("administrator of the shared apps").
    They are independent ``0/1`` columns; 321Theater applies no behavior of its
    own, so the consuming app owns their meaning. FetchLog gates login on
    ``is_app_user`` by default (any account with the user flag set may sign in)
    and carries ``is_app_admin`` in the session for future admin-only features.
    The gating flag is selectable via the ``require_flag`` config option.

``psycopg2`` and ``werkzeug`` are imported lazily inside the methods that need
them, so importing this module is cheap and safe even when auth is disabled and
those packages are not installed.
"""

import json
import logging
import re
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("fetchlog.auth")

# Session id format produced by every app in the family: secrets.token_urlsafe(32).
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")

# Postgres identifiers cannot be passed as bound parameters, so any schema name
# that gets interpolated into SQL must be validated against this whitelist.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Pre-computed dummy scrypt hash, identical in shape to a real one, used to
# normalise response time when a username does not exist (anti-enumeration).
# This is the same dummy literal the sibling apps use.
_DUMMY_HASH = (
    "scrypt:32768:8:1$dummy$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)

DEFAULT_COOKIE_NAME = "session"
DEFAULT_SHARED_SCHEMA = "shared"
DEFAULT_LIFETIME_HOURS = 12
DEFAULT_REQUIRE_FLAG = "is_app_user"
_VALID_FLAGS = ("is_app_user", "is_app_admin")


def _validate_identifier(name: str, what: str = "schema") -> str:
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"Invalid {what} name: {name!r}")
    return name


class AuthManager:
    """Reads/writes the shared session store and validates logins.

    All public methods are synchronous (blocking psycopg2). Callers in the async
    web server should invoke them via ``starlette.concurrency.run_in_threadpool``
    so the event loop is never blocked on database I/O.
    """

    def __init__(self, config: dict):
        config = config or {}
        self.enabled = bool(config.get("enabled", False))
        self.host = config.get("host") or "localhost"
        self.port = int(config.get("port") or 5432)
        self.dbname = config.get("dbname") or ""
        self.user = config.get("user") or ""
        self.password = config.get("password") or ""
        self.shared_schema = _validate_identifier(
            config.get("shared_schema") or DEFAULT_SHARED_SCHEMA, "shared_schema")

        self.cookie_name = config.get("cookie_name") or DEFAULT_COOKIE_NAME
        self.cookie_domain = config.get("cookie_domain") or None
        self.cookie_secure = bool(config.get("cookie_secure", False))
        self.cookie_samesite = (config.get("cookie_samesite") or "lax").lower()
        self.lifetime_hours = int(config.get("session_lifetime_hours") or DEFAULT_LIFETIME_HOURS)

        self.require_flag = config.get("require_flag") or DEFAULT_REQUIRE_FLAG
        if self.require_flag not in _VALID_FLAGS:
            raise ValueError(
                f"auth: require_flag must be one of {list(_VALID_FLAGS)}, "
                f"got {self.require_flag!r}")

        self._local = threading.local()

    # ---------- connection management ----------

    def _get_conn(self):
        import psycopg2
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                dbname=self.dbname,
                user=self.user,
                password=self.password,
                connect_timeout=10,
            )
            # Autocommit keeps every short auth query independent: a failed
            # statement never leaves the connection in an aborted-transaction
            # state that would poison the next request.
            conn.autocommit = True
            self._local.conn = conn
        return self._local.conn

    def _reset_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None

    def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        import psycopg2
        import psycopg2.extras
        last_err = None
        for attempt in (1, 2):
            try:
                conn = self._get_conn()
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(sql, params)
                row = cur.fetchone()
                cur.close()
                return dict(row) if row else None
            except psycopg2.Error as exc:
                last_err = exc
                logger.warning("auth: db error (attempt %d): %s", attempt, exc)
                self._reset_conn()
        raise last_err

    def _execute(self, sql: str, params: tuple = ()) -> None:
        import psycopg2
        last_err = None
        for attempt in (1, 2):
            try:
                conn = self._get_conn()
                cur = conn.cursor()
                cur.execute(sql, params)
                cur.close()
                return
            except psycopg2.Error as exc:
                last_err = exc
                logger.warning("auth: db error (attempt %d): %s", attempt, exc)
                self._reset_conn()
        raise last_err

    # ---------- initialization ----------

    def connect_and_init(self) -> bool:
        """Connect and make sure the shared session table exists.

        Returns True on success. On failure logs the cause and returns False;
        the caller keeps auth enabled so the server fails *closed* (every
        request is denied) rather than silently running without protection.
        """
        try:
            self._ensure_session_table()
            return True
        except Exception:
            logger.exception(
                "auth: failed to initialize shared session store "
                "(host=%s port=%s db=%s schema=%s). Is the database reachable "
                "and does the '%s' schema contain the 'users' table?",
                self.host, self.port, self.dbname, self.shared_schema,
                self.shared_schema,
            )
            return False

    def _ensure_session_table(self) -> None:
        s = self.shared_schema
        # Idempotent — the siblings run the same migration; whichever app boots
        # first wins. The FK to <schema>.users requires that table to pre-exist
        # (321Theater owns it).
        self._execute(f'CREATE SCHEMA IF NOT EXISTS "{s}"')
        self._execute(f'''
            CREATE TABLE IF NOT EXISTS "{s}".app_sessions (
                sid         TEXT PRIMARY KEY,
                user_id     INTEGER REFERENCES "{s}".users(id) ON DELETE CASCADE,
                data        TEXT NOT NULL DEFAULT '{{}}',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at  TIMESTAMP NOT NULL
            )
        ''')
        self._execute(
            f'CREATE INDEX IF NOT EXISTS idx_app_sessions_expires '
            f'ON "{s}".app_sessions(expires_at)')
        self._execute(
            f'CREATE INDEX IF NOT EXISTS idx_app_sessions_user '
            f'ON "{s}".app_sessions(user_id)')

    # ---------- user lookups ----------

    def get_user_by_username(self, username: str) -> Optional[dict]:
        s = self.shared_schema
        return self._fetchone(
            f'SELECT id, username, password_hash, role, display_name, '
            f'       must_change_password, is_readonly, theme, '
            f'       is_app_user, is_app_admin '
            f'FROM "{s}".users WHERE username = %s LIMIT 1',
            (username,),
        )

    def get_user_by_id(self, user_id) -> Optional[dict]:
        s = self.shared_schema
        return self._fetchone(
            f'SELECT id, username, role, display_name, is_readonly, '
            f'       is_app_user, is_app_admin '
            f'FROM "{s}".users WHERE id = %s LIMIT 1',
            (user_id,),
        )

    @staticmethod
    def verify_password(stored_hash: str, password: str) -> bool:
        try:
            from werkzeug.security import check_password_hash
            return check_password_hash(stored_hash, password)
        except Exception:
            return False

    # ---------- login / session lifecycle ----------

    def login(self, username: str, password: str) -> dict:
        """Validate credentials and the admin gate.

        Returns ``{"ok": True, "user": <row>}`` or ``{"ok": False, "error": msg}``.
        """
        username = (username or "").strip()
        if not username or not password:
            return {"ok": False, "error": "Username and password are required."}

        try:
            user = self.get_user_by_username(username)
        except Exception:
            logger.exception("auth: user lookup failed for %r", username)
            return {"ok": False, "error": "Authentication service is unavailable."}

        if user is None:
            # Normalise timing so a missing user is indistinguishable from a
            # wrong password.
            self.verify_password(_DUMMY_HASH, password)
            logger.warning("auth: login attempt for unknown user %r", username)
            return {"ok": False, "error": "Invalid username or password."}

        if not self.verify_password(user.get("password_hash", ""), password):
            logger.warning("auth: bad password for user %r", username)
            return {"ok": False, "error": "Invalid username or password."}

        if not bool(user.get(self.require_flag)):
            logger.warning("auth: login denied for %r (%s not set) - no FetchLog access",
                           username, self.require_flag)
            return {"ok": False, "error": "Your account does not have access to FetchLog."}

        logger.info("auth: user %r logged in (is_app_user=%s, is_app_admin=%s)",
                    username, bool(user.get("is_app_user")), bool(user.get("is_app_admin")))
        return {"ok": True, "user": user}

    def _session_payload(self, user: dict) -> dict:
        """Build the session data dict.

        Includes the keys both siblings rely on (note Leash reads ``role`` while
        321Theater reads ``user_role``) so a session minted by FetchLog is fully
        usable in the other apps too.
        """
        now = datetime.utcnow()
        return {
            "logged_in": True,
            "user_id": user["id"],
            "username": user.get("username"),
            "display_name": user.get("display_name") or user.get("username"),
            "role": user.get("role"),        # consumed by Leash
            "user_role": user.get("role"),   # consumed by 321Theater
            "theme": user.get("theme") or "dark",
            "is_readonly": bool(user.get("is_readonly", 0)),
            "is_app_user": bool(user.get("is_app_user", 0)),
            "is_app_admin": bool(user.get("is_app_admin", 0)),
            "login_time": now.isoformat(),
            "_role_checked_at": now.timestamp(),
        }

    def create_session(self, user: dict) -> str:
        sid = secrets.token_urlsafe(32)
        expires = datetime.utcnow() + timedelta(hours=self.lifetime_hours)
        data = json.dumps(self._session_payload(user), default=str)
        s = self.shared_schema
        self._execute(
            f'INSERT INTO "{s}".app_sessions (sid, user_id, data, last_seen, expires_at) '
            f'VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s) '
            f'ON CONFLICT (sid) DO UPDATE SET '
            f'  data = EXCLUDED.data, '
            f'  last_seen = CURRENT_TIMESTAMP, '
            f'  expires_at = EXCLUDED.expires_at',
            (sid, user["id"], data, expires),
        )
        return sid

    def load_session(self, sid: str) -> Optional[dict]:
        """Return the session payload for a sid, or None if missing/expired."""
        s = self.shared_schema
        try:
            row = self._fetchone(
                f'SELECT data, expires_at FROM "{s}".app_sessions WHERE sid = %s',
                (sid,),
            )
        except Exception:
            logger.exception("auth: failed to load session")
            return None
        if not row:
            return None

        expires = row.get("expires_at")
        if isinstance(expires, str):
            try:
                expires = datetime.fromisoformat(expires.split(".")[0].replace("Z", ""))
            except Exception:
                expires = None
        if expires is None or expires < datetime.utcnow():
            self.delete_session(sid)
            return None

        try:
            data = json.loads(row["data"]) if row.get("data") else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def delete_session(self, sid: str) -> None:
        s = self.shared_schema
        try:
            self._execute(f'DELETE FROM "{s}".app_sessions WHERE sid = %s', (sid,))
        except Exception:
            logger.warning("auth: failed to delete session", exc_info=True)

    def authenticate_request(self, cookie_value: Optional[str]) -> Optional[dict]:
        """Resolve a request cookie to an authorised user, or None.

        The access flag is re-read from ``shared.users`` on every request so the
        session data is never trusted for authorization — a flag turned off in
        321Theater takes effect on the next request.
        """
        if not self.enabled:
            return None
        if not cookie_value or not _SID_RE.match(cookie_value):
            return None
        data = self.load_session(cookie_value)
        if not data:
            return None
        user_id = data.get("user_id")
        if user_id is None:
            return None
        try:
            user = self.get_user_by_id(user_id)
        except Exception:
            logger.exception("auth: failed to re-read user %r", user_id)
            return None
        if user is None:
            return None
        if not bool(user.get(self.require_flag)):
            return None
        return user

    # ---------- cookie helpers ----------

    def set_cookie(self, response, sid: str) -> None:
        response.set_cookie(
            key=self.cookie_name,
            value=sid,
            max_age=self.lifetime_hours * 3600,
            path="/",
            domain=self.cookie_domain,
            secure=self.cookie_secure,
            httponly=True,
            samesite=self.cookie_samesite,
        )

    def clear_cookie(self, response) -> None:
        response.delete_cookie(
            key=self.cookie_name,
            path="/",
            domain=self.cookie_domain,
        )
