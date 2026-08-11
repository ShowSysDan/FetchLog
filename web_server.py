"""
FastAPI web server for FetchLog.

Provides:
- Web UI for viewing live log stream
- REST API for querying, filtering, and exporting logs
- WebSocket endpoint for real-time log push
- Marker creation endpoint
"""

import asyncio
import csv
import io
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

import terminal_server
from auth import AuthManager
from syslog_parser import SEVERITIES, FACILITIES, facility_name, severity_name
from syslog_server import start_syslog_server

logger = logging.getLogger("fetchlog.web")

# Database instance - works with either SQLite or PostgreSQL LogDatabase.
# Set during application startup (lifespan).
db = None

# Auth manager - set during startup. None or disabled means no auth gate.
auth_mgr: Optional[AuthManager] = None

# Connected WebSocket clients
ws_clients: "set[WebSocket]" = set()

# Paths that bypass the auth gate.
_PUBLIC_PATHS = {"/login", "/logout", "/favicon.ico"}


def set_database(database):
    global db
    db = database


def _load_config() -> dict:
    """Load the database/auth config (the same file app.py reads)."""
    path = os.environ.get("FETCHLOG_DB_CONFIG", "db_config.json")
    if os.path.isfile(path):
        with open(path) as f:
            cfg = json.load(f)
    else:
        cfg = {"db_type": "sqlite", "sqlite_path": "logs.db"}
    override = os.environ.get("FETCHLOG_SQLITE_PATH")
    if override:
        cfg["sqlite_path"] = override
    return cfg


def _build_auth_config(cfg: dict) -> dict:
    """Derive the auth connection settings, inheriting the top-level PostgreSQL
    connection fields for anything the 'auth' block leaves unset."""
    auth = dict(cfg.get("auth") or {})
    for key in ("host", "port", "user", "password", "dbname"):
        auth.setdefault(key, cfg.get(key))
    return auth


class LogRouter:
    """Routes incoming UDP messages to the database and WebSocket clients."""

    def __init__(self, database, loop: asyncio.AbstractEventLoop):
        self.db = database
        self.loop = loop
        self._count = 0

    def on_message(self, entry: dict):
        try:
            row_id = self.db.insert_log(entry)
            self._count += 1
            if self._count % 1000 == 0:
                logger.info("Processed %d messages total", self._count)
            # Fetch the full row so the WebSocket payload matches /api/logs.
            rows = self.db.get_entries_after(row_id - 1, limit=1)
            if rows:
                self.loop.create_task(broadcast_log(rows[0]))
        except Exception:
            logger.exception("Error routing message")


@asynccontextmanager
async def lifespan(app_: FastAPI):
    """Initialize the database + auth and start the UDP syslog server.

    Runs whether the app is launched via `python app.py` (programmatic uvicorn)
    or under gunicorn (`gunicorn web_server:app -k uvicorn.workers.UvicornWorker
    -w 1`). The UDP listener and the in-memory WebSocket fan-out require a single
    process, which is why gunicorn must run with exactly one worker.
    """
    global auth_mgr

    cfg = _load_config()

    # ----- logs database -----
    db_type = cfg.get("db_type", "sqlite")
    if db_type == "postgresql":
        from database_pg import LogDatabase
        database = LogDatabase(cfg)
    else:
        from database import LogDatabase
        database = LogDatabase(cfg.get("sqlite_path", "logs.db"))
    set_database(database)
    logger.info("Database initialized (%s)", db_type)

    # ----- auth -----
    auth_mgr = AuthManager(_build_auth_config(cfg))
    if auth_mgr.enabled:
        if auth_mgr.connect_and_init():
            logger.info(
                "Auth ENABLED - shared schema '%s' on %s:%s/%s (login gate: %s=1)",
                auth_mgr.shared_schema, auth_mgr.host, auth_mgr.port,
                auth_mgr.dbname, auth_mgr.require_flag)
        else:
            logger.error(
                "Auth is ENABLED but the shared session store could not be "
                "initialized. All web requests will be denied until this is "
                "resolved (see the error above).")
    else:
        logger.warning(
            'Auth is DISABLED (set "auth": {"enabled": true, ...} in '
            "db_config.json). The web UI and API are open to anyone who can "
            "reach this port.")

    # ----- UDP syslog server -----
    loop = asyncio.get_running_loop()
    router = LogRouter(database, loop)
    udp_host = os.environ.get("FETCHLOG_HOST", "0.0.0.0")
    udp_port = int(os.environ.get("FETCHLOG_UDP_PORT", "5514"))
    transport, _protocol = await start_syslog_server(
        on_message=router.on_message, host=udp_host, port=udp_port, loop=loop)
    logger.info("UDP syslog server listening on %s:%d", udp_host, udp_port)

    # ----- SSH / telnet live view -----
    term_cfg = cfg.get("terminal") or {}

    def _term_port(env_var: str, cfg_key: str) -> int:
        value = os.environ.get(env_var)
        if value:
            return int(value)
        return int(term_cfg.get(cfg_key) or 0)

    ssh_port = _term_port("FETCHLOG_SSH_PORT", "ssh_port")
    telnet_port = _term_port("FETCHLOG_TELNET_PORT", "telnet_port")
    term_handles = []
    if ssh_port or telnet_port:
        term_handles = await terminal_server.start_servers(
            host=udp_host, ssh_port=ssh_port, telnet_port=telnet_port,
            database=database,
            ssh_host_key=term_cfg.get("ssh_host_key") or "ssh_host_key")

    try:
        yield
    finally:
        transport.close()
        for handle in term_handles:
            handle.close()
        logger.info("Shutting down...")


app = FastAPI(title="FetchLog", version="1.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ---------- Authentication ----------

# Simple in-process per-IP rate limiter for the login form (mirrors the
# siblings' "15 per minute" on /login). Sufficient for a single-worker process.
_login_attempts: "dict[str, list[float]]" = {}


def _login_rate_ok(ip: str, limit: int = 15, window: float = 60.0) -> bool:
    now = time.time()
    bucket = [t for t in _login_attempts.get(ip, []) if now - t < window]
    bucket.append(now)
    _login_attempts[ip] = bucket
    return len(bucket) <= limit


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    mgr = auth_mgr
    if mgr is None or not mgr.enabled:
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    user = await run_in_threadpool(
        mgr.authenticate_request, request.cookies.get(mgr.cookie_name))
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=f"/login?next={quote(target, safe='')}", status_code=302)
    request.state.user = user
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    mgr = auth_mgr
    if mgr is not None and mgr.enabled:
        user = await run_in_threadpool(
            mgr.authenticate_request, request.cookies.get(mgr.cookie_name))
        if user is not None:
            return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request, "login.html",
        {"next": request.query_params.get("next", ""), "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request):
    mgr = auth_mgr
    if mgr is None or not mgr.enabled:
        return RedirectResponse(url="/", status_code=303)

    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_url = form.get("next") or "/"

    ip = request.client.host if request.client else "?"
    if not _login_rate_ok(ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"next": next_url,
             "error": "Too many attempts. Please wait a minute and try again."},
            status_code=429)

    result = await run_in_threadpool(mgr.login, username, password)
    if not result.get("ok"):
        return templates.TemplateResponse(
            request, "login.html",
            {"next": next_url, "error": result.get("error", "Login failed.")},
            status_code=401)

    sid = await run_in_threadpool(mgr.create_session, result["user"])
    # Only allow same-site relative redirect targets (no open redirect).
    if not (next_url.startswith("/") and not next_url.startswith("//")):
        next_url = "/"
    response = RedirectResponse(url=next_url, status_code=303)
    mgr.set_cookie(response, sid)
    return response


@app.api_route("/logout", methods=["GET", "POST"])
async def logout(request: Request):
    mgr = auth_mgr
    response = RedirectResponse(url="/login", status_code=303)
    if mgr is not None and mgr.enabled:
        sid = request.cookies.get(mgr.cookie_name)
        if sid:
            await run_in_threadpool(mgr.delete_session, sid)
        mgr.clear_cookie(response)
    return response


async def broadcast_log(entry: dict):
    """Send a new log entry to all connected WebSocket and SSH/telnet clients."""
    global ws_clients
    # Enrich entry with human-readable fields
    enriched = enrich_entry(entry)
    await terminal_server.broadcast_entry(enriched)
    if not ws_clients:
        return
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_json(enriched)
        except Exception as e:
            logging.getLogger("fetchlog.ws").debug(
                "WebSocket send failed: %s", e)
            dead.add(ws)
    ws_clients -= dead


def enrich_entry(entry: dict) -> dict:
    """Add human-readable severity/facility names to an entry."""
    result = dict(entry)
    if entry.get("severity") is not None:
        result["severity_name"] = severity_name(entry["severity"])
    else:
        result["severity_name"] = None
    if entry.get("facility") is not None:
        result["facility_name"] = facility_name(entry["facility"])
    else:
        result["facility_name"] = None
    return result


# ---------- Web UI ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ---------- WebSocket ----------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    mgr = auth_mgr
    if mgr is not None and mgr.enabled:
        user = await run_in_threadpool(
            mgr.authenticate_request, ws.cookies.get(mgr.cookie_name))
        if user is None:
            await ws.close(code=1008)  # policy violation
            return
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            # Keep connection alive; client can send ping/commands
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)


# ---------- REST API ----------

@app.get("/api/logs")
async def get_logs(
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    source_ip: Optional[str] = None,
    hostname: Optional[str] = None,
    severity: Optional[int] = Query(None, ge=0, le=7),
    search: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    sort_by: str = "received_at",
    sort_order: str = "DESC",
    include_markers: bool = True,
):
    entries = db.query_logs(
        limit=limit, offset=offset,
        source_ip=source_ip, hostname=hostname,
        severity=severity, search=search,
        start_time=start_time, end_time=end_time,
        sort_by=sort_by, sort_order=sort_order,
        include_markers=include_markers,
    )
    total = db.count_logs(
        source_ip=source_ip, hostname=hostname,
        severity=severity, search=search,
        start_time=start_time, end_time=end_time,
        include_markers=include_markers,
    )
    return {
        "entries": [enrich_entry(e) for e in entries],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/hosts")
async def get_hosts():
    hosts = db.get_known_hosts()
    return {"hosts": hosts}


@app.post("/api/hosts/{ip}/name")
async def set_host_name(ip: str, request: Request):
    body = await request.json()
    name = body.get("display_name", "")
    db.update_host_display_name(ip, name)
    return {"ok": True}


@app.post("/api/markers")
async def create_marker(request: Request):
    body = await request.json()
    label = body.get("label", "Marker")
    timestamp = body.get("timestamp")
    style = body.get("style", "default")

    row_id = db.insert_marker(label, timestamp=timestamp, style=style)

    # Fetch the inserted entry and broadcast it
    entries = db.get_entries_after(row_id - 1, limit=1)
    if entries:
        await broadcast_log(entries[0])

    return {"ok": True, "id": row_id}


@app.get("/api/export")
async def export_csv(
    source_ip: Optional[str] = None,
    hostname: Optional[str] = None,
    severity: Optional[int] = Query(None, ge=0, le=7),
    search: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    sort_by: str = "timestamp",
    sort_order: str = "ASC",
    include_markers: bool = True,
    limit: int = Query(10000, ge=1, le=100000),
):
    entries = db.query_logs(
        limit=limit, offset=0,
        source_ip=source_ip, hostname=hostname,
        severity=severity, search=search,
        start_time=start_time, end_time=end_time,
        sort_by=sort_by, sort_order=sort_order,
        include_markers=include_markers,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Timestamp", "Received At", "Source IP", "Hostname",
        "Facility", "Severity", "App Name", "Message", "Is Syslog", "Is Marker"
    ])
    for e in entries:
        fac = facility_name(e["facility"]) if e.get("facility") is not None else ""
        sev = severity_name(e["severity"]) if e.get("severity") is not None else ""
        writer.writerow([
            e["id"], e["timestamp"], e["received_at"], e["source_ip"],
            e.get("hostname", ""), fac, sev,
            e.get("app_name", ""), e["message"],
            "Yes" if e["is_syslog"] else "No",
            "Yes" if e["is_marker"] else "No",
        ])

    output.seek(0)
    timestamp_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=logs_export_{timestamp_str}.csv"},
    )


@app.get("/api/stats")
async def get_stats():
    total = db.count_logs()
    hosts = db.get_known_hosts()
    return {
        "total_entries": total,
        "known_hosts": len(hosts),
        "latest_id": db.get_latest_id(),
    }
