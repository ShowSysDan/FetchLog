"""
FastAPI web server for EverythingLogger.

Provides:
- Web UI for viewing live log stream
- REST API for querying, filtering, and exporting logs
- WebSocket endpoint for real-time log push
- Marker creation endpoint
"""

import asyncio
import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import LogDatabase
from syslog_parser import SEVERITIES, FACILITIES, facility_name, severity_name

app = FastAPI(title="EverythingLogger", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Database instance (set from app.py)
db: Optional[LogDatabase] = None

# Connected WebSocket clients
ws_clients: set[WebSocket] = set()


def set_database(database: LogDatabase):
    global db
    db = database


async def broadcast_log(entry: dict):
    """Send a new log entry to all connected WebSocket clients."""
    if not ws_clients:
        return
    # Enrich entry with human-readable fields
    enriched = enrich_entry(entry)
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_json(enriched)
        except Exception:
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
    return templates.TemplateResponse("index.html", {"request": request})


# ---------- WebSocket ----------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
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
