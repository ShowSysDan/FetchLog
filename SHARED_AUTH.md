# Shared-Session Authentication — Cross-App SSO Guide

This document explains the **shared single sign-on (SSO)** scheme used across our
family of apps (321Theater, Leash, FetchLog, …) so it can be added to a new
project quickly and correctly.

> **Audience / use case:** the apps in this family are **Flask** apps, so this
> guide is written **Flask-first** — the copy-paste code below is Flask. FetchLog
> is the one exception (it is FastAPI); see [Appendix A](#appendix-a--non-flask-apps-fastapi)
> for how a non-Flask app participates.

---

## 1. The idea in one paragraph

Every app points at **one shared PostgreSQL database**. A schema named **`shared`**
holds two tables: **`users`** (accounts) and **`app_sessions`** (logged-in
sessions). Sessions are stored **server-side**: the browser cookie holds only an
**opaque random session id**, and the actual session data lives in
`shared.app_sessions`. Because the session lives in the shared database and the
cookie is just a lookup key, **logging into one app logs you into all of them**,
and **logging out of one logs you out of all of them**. There is **no shared
secret key** — the cookie is not signed, so apps don't need to agree on a
`SECRET_KEY` to share login state.

```
                       ┌─────────────────────────────────────┐
                       │        PostgreSQL (one database)      │
                       │                                       │
                       │  schema "shared"                      │
                       │    ├─ users          (owned by        │
                       │    │                  321Theater)     │
                       │    └─ app_sessions   (the SSO store)  │
                       │                                       │
                       │  schema "theater321"  (321Theater)    │
                       │  schema "leash"       (Leash)         │
                       │  schema "fetchlog"    (FetchLog logs) │
                       └───────────────▲───────────────────────┘
                                       │ search_path = "<app>","shared"
        ┌──────────────┬───────────────┼───────────────┬──────────────┐
        │ 321Theater   │     Leash      │   FetchLog    │  new app …   │
        │ (auth owner) │                │  (FastAPI)    │              │
        └──────┬───────┴───────┬────────┴──────┬────────┴──────────────┘
               │ Set-Cookie: session=<opaque sid>   (shared across all)
               ▼
        ┌─────────────┐   browser sends the same cookie to every app on the
        │   Browser   │   same host / parent domain → one login everywhere.
        └─────────────┘
```

---

## 2. The shared database (source of truth)

### 2.1 Ownership rules

- **321Theater owns `shared.users`.** It is the only app that **creates and
  writes** users (registration, admin user management, password resets). Every
  other app treats `shared.users` as **READ-ONLY**.
- **`shared.app_sessions` is created idempotently** by whichever app boots first
  (`CREATE TABLE IF NOT EXISTS …`). All apps read and write it (that's how they
  share sessions and how logout works).
- Each app keeps its **own** tables in its **own** schema (e.g. `leash`,
  `theater321`, `fetchlog`). Only `users` and `app_sessions` live in `shared`.

### 2.2 Schema (PostgreSQL — canonical)

```sql
-- Owned and created by 321Theater. Other apps READ ONLY.
CREATE TABLE IF NOT EXISTS shared.users (
    id                   SERIAL PRIMARY KEY,
    username             TEXT UNIQUE NOT NULL,
    password_hash        TEXT NOT NULL,        -- Werkzeug scrypt (see §2.4)
    display_name         TEXT,
    role                 TEXT DEFAULT 'user',  -- 'admin' | 'staff' | 'user'
    theme                TEXT DEFAULT 'dark',
    last_login           TIMESTAMP,
    must_change_password INTEGER DEFAULT 0,
    email                TEXT DEFAULT '',
    is_readonly          INTEGER DEFAULT 0,
    email_confirmed      INTEGER DEFAULT 1,
    pending_approval     INTEGER DEFAULT 0,
    is_scheduler         INTEGER DEFAULT 0,
    is_asset_manager     INTEGER DEFAULT 0,
    is_document_viewer   INTEGER DEFAULT 0,
    -- Cross-app access flags (set in 321Theater; each app decides what to require)
    is_app_user          INTEGER DEFAULT 0,   -- "user of the shared apps"
    is_app_admin         INTEGER DEFAULT 0,   -- "admin of the shared apps"
    viewer_venues        TEXT DEFAULT NULL,
    viewer_doc_types     TEXT DEFAULT NULL,
    home_layout          TEXT DEFAULT 'columns',
    home_density         TEXT DEFAULT 'normal',
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- The server-side session store. Created by whichever app boots first.
CREATE TABLE IF NOT EXISTS shared.app_sessions (
    sid         TEXT PRIMARY KEY,
    user_id     INTEGER REFERENCES shared.users(id) ON DELETE CASCADE,
    data        TEXT NOT NULL DEFAULT '{}',     -- JSON session payload
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_sessions_expires ON shared.app_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_app_sessions_user    ON shared.app_sessions(user_id);
```

321Theater also maintains `user_pending_registration` and
`password_reset_tokens` in the shared schema for its self-service flows. A
consuming app does not need them.

### 2.3 Roles — how "Admin" is represented

Authorization is driven by the single **`role` text column**, plus a few
per-user boolean flags. **There is no `is_admin` column and no roles/tags join
table.**

| Role value | Meaning |
|------------|---------|
| `admin`    | Full administrator. "Tagged with Admin" == `role = 'admin'`. |
| `staff`    | Elevated/content user. |
| `user`     | Ordinary user. |

Each app decides **who may sign in to it**, by either of two signals:

- **By role** (`role` string) — e.g. Leash admits `admin` + `staff`.
- **By cross-app flag** (recommended for new ancillary apps) — two independent
  per-user flags in the shared directory let an app gate access without touching
  `role`:

  | Flag | Meaning |
  |------|---------|
  | `is_app_user`  | "this account is a user of the shared apps" |
  | `is_app_admin` | "this account is an administrator of the shared apps" |

  The flags are **fully independent** — all four combinations `(0,0) (1,0) (0,1)
  (1,1)` are valid; admin does **not** imply user. They are `INTEGER` `0/1`
  (default `0`), set by an admin in 321Theater, which applies no behavior of its
  own — **the consuming app owns their meaning**. FetchLog, for instance, gates
  **login on `is_app_user`** and reads `is_app_admin` for future admin-only
  features. Check whichever flag(s) your app needs.

### 2.4 Password hashing

Passwords are hashed with **Werkzeug** (`werkzeug.security`), default algorithm
**scrypt** on Werkzeug 3.x — the stored format looks like
`scrypt:32768:8:1$<salt>$<hash>`.

- To **verify**: `check_password_hash(user['password_hash'], password)`.
- To **create** (321Theater only): `generate_password_hash(plaintext)`.

Any app that *creates* users **must** use Werkzeug so the others can verify.
Consuming apps only ever call `check_password_hash`.

The default seeded admin is **`admin` / `admin123`** with
`must_change_password = 1`. Change it (in 321Theater) before relying on it.

---

## 3. How the shared session works

1. On login, the owning app validates the password, then **mints a random
   session id** (`secrets.token_urlsafe(32)` → ~43 url-safe chars), writes a row
   into `shared.app_sessions`, and sets a cookie: `session=<sid>`.
2. On every request, the app reads the `session` cookie, looks up the `sid` in
   `shared.app_sessions`, checks `expires_at`, and loads the JSON `data`.
3. Because all apps read/write the **same** `app_sessions` rows keyed by the same
   cookie, a session created by one app is immediately valid in the others.
4. On logout, the app **deletes the row** and clears the cookie → the user is
   logged out of **every** app at once.

**Why there is no shared `SECRET_KEY`:** the cookie value is a raw random token,
not a signed/serialized payload, so nothing cryptographic needs to match between
apps. (Each Flask app should still set its *own* stable `SECRET_KEY` for
`flash()` messages and CSRF — it just doesn't need to be the *same* key.)

> **Same-origin requirement.** The browser only sends the cookie to apps on the
> same hostname or a shared parent domain. Serve the apps under one host, or set
> `SESSION_COOKIE_DOMAIN` to a common parent (e.g. `.example.com`) in **every**
> app, and keep the cookie name/path/SameSite identical everywhere (see §4.3).

---

## 4. Adding a NEW Flask app to the family

### 4.0 Dependencies

```text
Flask>=3.0.0
Werkzeug>=3.0.0          # scrypt password verification
psycopg2-binary>=2.9.0   # PostgreSQL driver
gunicorn>=22.0.0         # production server (uvicorn worker not needed for Flask)
# optional:
Flask-Limiter>=3.5.0     # rate-limit /login
```

### 4.1 Connect to the shared database + set the search path

Point `SQLALCHEMY_DATABASE_URI` / your DSN at the **same** database the family
uses, and put your own schema **and** `shared` on the `search_path` so unqualified
`users` / `app_sessions` resolve to the shared schema:

```python
# SQLAlchemy engine options
SQLALCHEMY_ENGINE_OPTIONS = {
    "connect_args": {"options": "-csearch_path=myapp,shared"}
}
# or, with raw psycopg2, after connect:
#   cur.execute('SET search_path TO "myapp", "shared"')
```

> Postgres identifiers can't be bound parameters. If you ever interpolate a
> schema name into SQL, validate it first against `^[A-Za-z_][A-Za-z0-9_]*$`.

### 4.2 Install the server-side session backend

This is the heart of the integration. Drop this in and register it. It stores
the session in `shared.app_sessions`; the cookie carries only the `sid`.

```python
import json, re, secrets
from datetime import datetime, timedelta
from flask.sessions import SessionInterface, SessionMixin
from werkzeug.datastructures import CallbackDict

_SID_RE = re.compile(r'^[A-Za-z0-9_-]{20,128}$')


class _DBSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None, new=False):
        def _on_update(s):
            s.modified = True
        CallbackDict.__init__(self, initial, _on_update)
        self.sid = sid
        self.new = new
        self.modified = False


class DBSessionInterface(SessionInterface):
    """Server-side sessions stored in shared.app_sessions.

    `get_db()` must return a DB-API connection whose rows are dict-like
    (row['data'], row['expires_at']). The SQL below uses %s placeholders and a
    Postgres ON CONFLICT upsert; adapt if your helper uses ? placeholders.
    """

    def _new_sid(self):
        return secrets.token_urlsafe(32)

    def _load(self, sid):
        db = get_db()
        try:
            row = db.execute(
                "SELECT data, expires_at FROM app_sessions WHERE sid = %s", (sid,)
            ).fetchone()
            if not row:
                return {}, False
            expires = row['expires_at']
            if isinstance(expires, str):
                expires = datetime.fromisoformat(expires.split('.')[0].replace('Z', ''))
            if expires < datetime.utcnow():
                db.execute("DELETE FROM app_sessions WHERE sid = %s", (sid,))
                db.commit()
                return {}, False
            data = json.loads(row['data']) if row['data'] else {}
            return (data if isinstance(data, dict) else {}), True
        finally:
            db.close()

    def open_session(self, app, request):
        cookie_name = app.config.get('SESSION_COOKIE_NAME', 'session')
        sid = request.cookies.get(cookie_name)
        if not sid or not _SID_RE.match(sid):
            return _DBSession(sid=self._new_sid(), new=True)
        data, ok = self._load(sid)
        if not ok:
            # Never adopt an unknown client-supplied sid (session-fixation defense)
            return _DBSession(sid=self._new_sid(), new=True)
        return _DBSession(data, sid=sid, new=False)

    def save_session(self, app, session, response):
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        cookie_name = app.config.get('SESSION_COOKIE_NAME', 'session')

        if not session:                      # cleared (logout) -> drop row + cookie
            if session.modified:
                db = get_db()
                try:
                    db.execute("DELETE FROM app_sessions WHERE sid = %s", (session.sid,))
                    db.commit()
                finally:
                    db.close()
                response.delete_cookie(cookie_name, domain=domain, path=path)
            return

        if not session.modified and not session.new:
            return

        expires = datetime.utcnow() + app.permanent_session_lifetime
        data_json = json.dumps(dict(session), default=str)
        db = get_db()
        try:
            db.execute(
                "INSERT INTO app_sessions (sid, user_id, data, last_seen, expires_at) "
                "VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s) "
                "ON CONFLICT (sid) DO UPDATE SET "
                "  user_id = EXCLUDED.user_id, data = EXCLUDED.data, "
                "  last_seen = CURRENT_TIMESTAMP, expires_at = EXCLUDED.expires_at",
                (session.sid, session.get('user_id'), data_json, expires),
            )
            db.commit()
        finally:
            db.close()

        response.set_cookie(
            cookie_name, session.sid, expires=expires,
            httponly=self.get_cookie_httponly(app),
            domain=domain, path=path,
            secure=self.get_cookie_secure(app),
            samesite=self.get_cookie_samesite(app),
        )


# Register it (after the app + DB are configured):
app.session_interface = DBSessionInterface()
```

> 321Theater's in-repo version uses `INSERT OR REPLACE` because its `db_adapter`
> rewrites SQLite SQL to Postgres at runtime (it maps `app_sessions`'s conflict
> key to `sid`). For a fresh app talking straight to Postgres, the explicit
> `ON CONFLICT (sid) DO UPDATE` form above is clearer.

### 4.3 Cookie configuration — must match across apps

```python
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
# HTTPS deployments: set this true (the sid is a bearer token now).
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '') in ('1', 'true', 'yes')
# Cross-subdomain SSO: set the SAME parent domain in every app; else leave unset.
app.config['SESSION_COOKIE_DOMAIN'] = os.environ.get('SESSION_COOKIE_DOMAIN') or None
```

| Setting | Required value |
|---|---|
| `SESSION_COOKIE_NAME` | `session` (Flask default) — **same in all apps** |
| `SESSION_COOKIE_DOMAIN` | same value (or all unset) |
| `SESSION_COOKIE_PATH` | `/` (default) |
| `SESSION_COOKIE_SAMESITE` | `Lax` |
| `SESSION_COOKIE_HTTPONLY` | `True` |

### 4.4 Read the user from `shared.users`

```python
from werkzeug.security import check_password_hash

def get_user_by_username(username):
    db = get_db()
    try:
        return db.execute(
            "SELECT id, username, password_hash, role, display_name, "
            "       must_change_password, is_readonly, is_app_user, is_app_admin "
            "FROM users WHERE username = %s LIMIT 1", (username,)
        ).fetchone()
    finally:
        db.close()
```

### 4.5 Login & logout routes

```python
# How THIS app gates access (customize per app): a cross-app flag (recommended)
# or a role. FetchLog uses the 'is_app_user' flag.
_REQUIRE_FLAG = 'is_app_user'                    # or 'is_app_admin', or gate on role
_DUMMY_HASH = 'scrypt:32768:8:1$dummy$' + '0' * 64  # anti-enumeration timing

def _populate_session(session, user):
    """Write the session keys the family relies on."""
    session.clear()                              # session-fixation defense
    session['user_id']       = user['id']
    session['username']      = user['username']
    session['display_name']  = user['display_name'] or user['username']
    session['user_role']     = user['role']      # 321Theater reads this key
    session['role']          = user['role']      # Leash reads this key — set BOTH
    session['is_readonly']   = bool(user.get('is_readonly', 0))
    session['is_app_user']   = bool(user.get('is_app_user', 0))
    session['is_app_admin']  = bool(user.get('is_app_admin', 0))
    session['_role_checked_at'] = datetime.utcnow().timestamp()

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = get_user_by_username(username)
        if not user:
            check_password_hash(_DUMMY_HASH, password)        # constant-ish time
            flash('Invalid username or password.', 'error')
        elif not check_password_hash(user['password_hash'], password):
            flash('Invalid username or password.', 'error')
        elif not user.get(_REQUIRE_FLAG):        # gate on the cross-app flag
            flash('Your account does not have access to this app.', 'error')
        else:
            # Mint a fresh sid so the post-login cookie differs from any pre-login one
            session.sid = secrets.token_urlsafe(32)
            session.new = True
            _populate_session(session, user)
            session.permanent = True
            return redirect(request.form.get('next') or url_for('index'))
    return render_template('login.html', next=request.args.get('next', ''))

@app.route('/logout')
def logout():
    session.clear()        # deletes the shared row -> logs out of ALL apps
    return redirect(url_for('login'))
```

### 4.6 Authorization decorators

```python
from functools import wraps

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('user_role') != 'admin':   # admin == role 'admin'
            abort(403)
        return f(*a, **k)
    return w
```

### 4.7 `before_request`: periodic role re-check + forced password change

Re-read the role from `shared.users` every few minutes so a demotion/deletion in
321Theater takes effect mid-session:

```python
@app.before_request
def _refresh_session_roles():
    if 'user_id' not in session:
        return
    if datetime.utcnow().timestamp() - session.get('_role_checked_at', 0) < 300:
        return
    user = get_user_by_id(session['user_id'])    # must also SELECT the flags you gate on
    if not user or not user.get(_REQUIRE_FLAG):  # deleted or access revoked -> re-login
        session.clear()
        return redirect(url_for('login'))
    session['user_role']    = user['role']
    session['role']         = user['role']
    session['is_app_user']  = bool(user.get('is_app_user', 0))
    session['is_app_admin'] = bool(user.get('is_app_admin', 0))
    session['_role_checked_at'] = datetime.utcnow().timestamp()
```

321Theater additionally blocks all routes (except logout/change-password) when
`session['must_change_password']` is set. A read-only consuming app cannot change
passwords, so it should either ignore the flag or refuse login and tell the user
to set their password in 321Theater first.

### 4.8 `SECRET_KEY` (per app, need not match)

```python
app.secret_key = os.environ.get('SECRET_KEY') or _read_from_dotenv() or secrets.token_hex(32)
```

Used only for `flash()`/CSRF internals. It does **not** participate in the shared
session and **does not** need to be the same across apps.

---

## 5. Configuration reference (typical env vars)

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres DSN for the shared database (`postgresql://…`). |
| `DATABASE_SCHEMA` | This app's *own* schema (e.g. `myapp`). |
| `AUTH_DB_SCHEMA` | The shared schema name (`shared`). Empty = disable auth (dev/SQLite). |
| `SECRET_KEY` | Per-app Flask secret (flash/CSRF). |
| `SESSION_COOKIE_DOMAIN` | Shared parent domain for cross-subdomain SSO. |
| `SESSION_COOKIE_SECURE` | `1` on HTTPS deployments. |

---

## 6. Operational gotchas

- **Same host / parent domain** or the browser won't send the cookie to all apps.
- **Keep clocks in NTP sync** — session expiry uses `datetime.utcnow()`.
- **Logout is global** — `session.clear()` deletes the shared row, ending the
  session in every app. This is intended.
- **Two role keys exist:** 321Theater writes `user_role`, Leash writes `role`.
  New apps should **set both** on login and, ideally, **re-read the role from
  `shared.users` each request** rather than trusting the session copy.
- **Don't keep a private `users` table.** Read from `shared.users`; let 321Theater
  own writes. (If an app shipped with its own users table, repoint it.)
- **Fail closed:** if the shared DB is unreachable while auth is enabled, deny
  requests rather than letting users through.
- **`app_sessions` is auto-created** by whichever app starts first; the FK to
  `shared.users(id)` means `users` must already exist.

---

## 7. Security notes

- The `sid` is a 256-bit unguessable token; security rests on it staying secret.
  Use `SESSION_COOKIE_SECURE=1` on HTTPS so it isn't sent in cleartext.
- Cookie is `HttpOnly` (no JS access) and `SameSite=Lax`.
- Login should be **rate-limited** (the family uses 15/min) and should run a
  dummy hash on unknown usernames to avoid timing-based user enumeration.
- Always regenerate the `sid` on login (session-fixation defense).

---

## Appendix A — Non-Flask apps (FastAPI: how FetchLog does it)

FetchLog is FastAPI, not Flask, so it does **not** use Flask's session machinery.
It participates in the exact same SSO by talking to the shared tables directly —
the scheme is framework-agnostic precisely because the cookie is an opaque id and
the session lives in the database:

1. Read the `session` cookie from the request.
2. Validate the `sid` format, look it up in `shared.app_sessions`, check expiry.
3. Take `user_id` from the session row, re-read the user from `shared.users`,
   and check `role` is allowed (FetchLog: `admin` only).
4. To create a session (its own `/login`), mint a `sid`, insert into
   `shared.app_sessions` with the same JSON keys (`user_id`, `username`,
   `role` **and** `user_role`, …), and set the same `session` cookie.

The implementation lives in `auth.py` (`AuthManager`) and is wired into the
ASGI app via middleware in `web_server.py`. Verifying passwords still uses
Werkzeug (`check_password_hash`) so the scrypt hashes match. Because the session
is just a DB row keyed by an opaque cookie, a session minted by FetchLog is
accepted by the Flask apps and vice-versa.

Key difference to remember: **a non-Flask app must reproduce the cookie contract
exactly** (name `session`, same domain/path/SameSite, opaque sid value) and the
session-data keys — it cannot rely on Flask's `SessionInterface` to do it.

---

## Appendix B — New-app porting checklist

- [ ] App connects to the **same Postgres DB**; `search_path` includes `shared`.
- [ ] `DBSessionInterface` installed (`app.session_interface = …`) — Flask apps.
- [ ] Cookie config matches the family (name `session`, SameSite `Lax`, domain).
- [ ] Users read from `shared.users` (no private users table).
- [ ] Login sets `user_id`, `username`, `role` **and** `user_role`, …; regenerates `sid`.
- [ ] App gates access on the intended signal (`is_app_user` / `is_app_admin` flag, or `role`), re-checked in `before_request`.
- [ ] `login_required` / `admin_required` (or app-specific role gate) applied.
- [ ] `before_request` re-reads role from DB periodically.
- [ ] Passwords verified with Werkzeug; new users (321Theater only) hashed with Werkzeug.
- [ ] Per-app `SECRET_KEY` set (need not match other apps).
- [ ] Login rate-limited; dummy hash on unknown user; fail-closed if DB down.
- [ ] All apps served on one host / shared parent domain; clocks NTP-synced.
