# EverythingLogger

A universal syslog server and real-time log viewer. It opens a UDP port that accepts messages from **anything** — standard syslog (RFC 3164/5424) or plain-text strings — stores everything in SQLite, and serves a live web dashboard for viewing, filtering, marking, and exporting logs.

Built to handle **300+ devices** simultaneously with no performance issues.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Usage](#usage)
  - [Command-Line Options](#command-line-options)
  - [Sending Test Messages](#sending-test-messages)
  - [Configuring Devices to Send Logs](#configuring-devices-to-send-logs)
- [Web Interface](#web-interface)
  - [Live Log Stream](#live-log-stream)
  - [Filtering](#filtering)
  - [Sorting](#sorting)
  - [Markers](#markers)
  - [CSV Export](#csv-export)
  - [Host Management](#host-management)
- [Architecture](#architecture)
- [Syslog Format Support](#syslog-format-support)
  - [Severity Levels](#severity-levels)
  - [Facility Codes](#facility-codes)
- [REST API Reference](#rest-api-reference)
- [Database](#database)
- [FAQ](#faq)

---

## Features

- **Universal UDP Listener** — Receives RFC 3164, RFC 5424, and raw plain-text messages on a single port
- **Real-Time Web UI** — Dark terminal-style dashboard with WebSocket-powered live log scrolling
- **Filtering** — Filter by source IP, hostname, severity level, message text, and date range (all combinable)
- **Sortable Columns** — Click column headers to sort by time, source IP, hostname, or severity
- **Markers** — Insert custom labeled dividers into the log stream (e.g., "Maintenance Start") with timestamps and color styles
- **CSV Export** — Export filtered results to CSV for reporting or analysis
- **SQLite + WAL Mode** — High-throughput storage that handles hundreds of concurrent writers without breaking a sweat
- **Auto Host Tracking** — Automatically detects and tracks all devices that send messages, with renameable display names
- **Severity Color Coding** — Syslog messages are color-coded by severity (red for errors, yellow for warnings, etc.). Non-syslog messages display in neutral gray

---

## Quick Start

```bash
# Clone and install
git clone https://github.com/ShowSysDan/EverythingLogger.git
cd EverythingLogger
pip install -r requirements.txt

# Run it
python app.py

# Open http://localhost:8080 in your browser

# In another terminal, send a test message
echo "Hello from a random device" | nc -u 127.0.0.1 5514
```

---

## Installation

**Requirements:** Python 3.10+

```bash
pip install -r requirements.txt
```

**Dependencies:**

| Package | Purpose |
|---------|---------|
| `fastapi` | Web framework and REST API |
| `uvicorn[standard]` | ASGI server with WebSocket support |
| `websockets` | WebSocket protocol handling |
| `jinja2` | HTML template rendering |
| `aiofiles` | Async static file serving |
| `python-dateutil` | Date/time parsing utilities |

No external database server needed — SQLite is built into Python.

---

## Usage

### Command-Line Options

```
python app.py [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--udp-port` | `5514` | UDP port for receiving log messages |
| `--web-port` | `8080` | HTTP port for the web UI |
| `--host` | `0.0.0.0` | Bind address (all interfaces by default) |
| `--db` | `logs.db` | Path to the SQLite database file |

**Examples:**

```bash
# Default (UDP 5514, Web 8080)
python app.py

# Standard syslog port (requires root/sudo)
sudo python app.py --udp-port 514

# Custom everything
python app.py --udp-port 1514 --web-port 9090 --db /var/log/everything.db

# Bind to specific interface
python app.py --host 192.168.1.100
```

On startup you'll see:

```
╔══════════════════════════════════════════════════════╗
║              EverythingLogger v1.0                   ║
╠══════════════════════════════════════════════════════╣
║  UDP Syslog:  0.0.0.0:5514                           ║
║  Web UI:      http://localhost:8080                   ║
║  Database:    logs.db                                 ║
╚══════════════════════════════════════════════════════╝
```

### Sending Test Messages

```bash
# Standard syslog via logger command
logger -d -n 127.0.0.1 -P 5514 "test syslog message"

# Raw plain-text via netcat
echo "Hello from some device" | nc -u 127.0.0.1 5514

# Syslog with priority (facility=1/user, severity=6/info → PRI=14)
echo "<14>myapp[1234]: Application started successfully" | nc -u 127.0.0.1 5514

# Syslog with error severity (facility=1/user, severity=3/error → PRI=11)
echo "<11>myapp[1234]: Connection failed to database" | nc -u 127.0.0.1 5514

# Full RFC 3164 format
echo "<34>Jan 23 15:43:00 router1 sshd[5432]: Failed password for root" | nc -u 127.0.0.1 5514

# Simulate many devices with a loop
for i in $(seq 1 50); do
  echo "Device $i reporting status OK" | nc -u -w0 127.0.0.1 5514
done
```

### Configuring Devices to Send Logs

**Linux (rsyslog):**
Add to `/etc/rsyslog.conf`:
```
*.* @YOUR_SERVER_IP:5514
```
Then restart: `sudo systemctl restart rsyslog`

**Network equipment (Cisco, Ubiquiti, etc.):**
Set the syslog server to `YOUR_SERVER_IP` port `5514` (UDP) in the device's logging configuration.

**Custom applications:**
Send any UDP packet to the server's IP and port. No special formatting required — raw strings work fine.

```python
# Python example
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(b"My application event: user logged in", ("YOUR_SERVER_IP", 5514))
```

---

## Web Interface

Open `http://localhost:8080` (or your configured `--web-port`) in any browser.

### Live Log Stream

The main view shows log entries in a scrollable table with these columns:

| Column | Description |
|--------|-------------|
| **Time** | Timestamp of the log entry |
| **Source IP** | IP address of the device that sent the message |
| **Hostname** | Hostname from the syslog header, or the IP if not available |
| **Severity** | Syslog severity badge, or "raw" for non-syslog messages |
| **App** | Application name (from syslog header) |
| **Message** | The log message content |

**Live mode** is enabled by default. New messages appear at the bottom in real-time via WebSocket. The view auto-scrolls to show the latest entries. If you scroll up to read older logs, auto-scroll pauses and a **"Scroll to latest"** button appears.

Toggle the **Live: ON/OFF** button in the header to pause/resume the live stream.

The status indicator in the header shows connection state:
- Green dot = connected and receiving
- Red dot = disconnected (auto-reconnects with exponential backoff, up to 50 attempts)

### Filtering

All filters are combinable and update results immediately:

| Filter | Control | Behavior |
|--------|---------|----------|
| **Source IP** | Dropdown | Select a specific device from auto-detected hosts |
| **Hostname** | Text input | Substring search across hostnames and display names |
| **Severity** | Dropdown | Show messages at or above the selected severity (e.g., "Error" shows Emergency + Alert + Critical + Error) |
| **Search** | Text input | Substring match on message content |
| **From / To** | Date-time pickers | Filter to a specific time range |

Text inputs (hostname, search) are debounced at 400ms to avoid excessive requests.

Click **Clear Filters** to reset everything.

### Sorting

Click any column header to sort by that column. Click again to toggle between ascending and descending order. An arrow indicator (▲/▼) shows the current sort direction.

Sortable columns: **Time**, **Source IP**, **Hostname**, **Severity**

### Markers

Markers are custom labeled dividers that appear inline in the log stream — useful for noting events like "maintenance started" or "deployed v2.3".

Click the **+ Marker** button to open the marker dialog:

| Field | Description |
|-------|-------------|
| **Label** | The marker text (e.g., "Event Start", "Deploy v2.3") |
| **Timestamp** | When the event occurred (defaults to now, but you can backdate it) |
| **Style** | Visual color style |

**Marker styles:**

| Style | Color | Use case |
|-------|-------|----------|
| Default | Purple | General-purpose markers |
| Info | Blue | Informational notes |
| Success | Green | Successful events |
| Warning | Yellow | Caution points |
| Danger | Red | Errors or critical events |

Markers appear as full-width colored bars across the log table so they're easy to spot.

### CSV Export

Click **Export CSV** to download the currently filtered log entries as a CSV file. The export respects all active filters (IP, hostname, severity, search, date range), so you can narrow down to exactly the entries you need before exporting.

The CSV includes these columns:
`ID, Timestamp, Received At, Source IP, Hostname, Facility, Severity, App Name, Message, Is Syslog, Is Marker`

Files are named `logs_export_YYYYMMDD_HHMMSS.csv`. Maximum 10,000 entries per export by default (configurable via the API up to 100,000).

### Host Management

EverythingLogger automatically tracks every unique IP address that sends messages. The **Source** filter dropdown is populated with all known hosts.

To rename a host (so "192.168.1.50" shows up as "Main Router"), use the API:

```bash
curl -X POST http://localhost:8080/api/hosts/192.168.1.50/name \
  -H "Content-Type: application/json" \
  -d '{"display_name": "Main Router"}'
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        Devices (300+)                        │
│     Routers, Switches, Servers, IoT, Custom Apps, etc.       │
└──────────────────────┬───────────────────────────────────────┘
                       │ UDP packets (syslog or raw strings)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                   UDP Syslog Server                           │
│                  (asyncio DatagramProtocol)                   │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐    │
│  │              Syslog Parser                            │    │
│  │  RFC 5424 → RFC 3164 → Simple PRI → Raw fallback     │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────┬───────────────────────────────────────┘
                       │ Parsed entry dict
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                      Log Router                              │
│                                                              │
│  ┌─────────────┐          ┌─────────────────────────────┐    │
│  │   SQLite DB  │          │  WebSocket Broadcast         │    │
│  │  (WAL mode)  │          │  (to all connected browsers) │    │
│  └─────────────┘          └─────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                    FastAPI Web Server                         │
│                                                              │
│  /           → Web UI (HTML/CSS/JS)                          │
│  /ws         → WebSocket live feed                           │
│  /api/logs   → Query & filter logs                           │
│  /api/hosts  → Known host list                               │
│  /api/markers→ Create markers                                │
│  /api/export → CSV download                                  │
│  /api/stats  → Server statistics                             │
└──────────────────────────────────────────────────────────────┘
```

**File structure:**

```
EverythingLogger/
├── app.py              # Main entry point, CLI args, startup
├── syslog_server.py    # Async UDP listener
├── syslog_parser.py    # Message parser (RFC 3164/5424/raw)
├── database.py         # SQLite layer with WAL mode
├── web_server.py       # FastAPI REST API + WebSocket
├── templates/
│   └── index.html      # Web UI template
├── static/
│   ├── app.js          # Frontend application logic
│   └── style.css       # Dark terminal-style theme
├── requirements.txt    # Python dependencies
└── README.md           # This file
```

---

## Syslog Format Support

The parser tries formats in order of specificity and falls back gracefully:

1. **RFC 5424** (IETF Syslog) — `<PRI>VERSION TIMESTAMP HOSTNAME APP PROCID MSGID STRUCTURED-DATA MSG`
2. **RFC 3164** (BSD Syslog) — `<PRI>TIMESTAMP HOSTNAME APP[PID]: MSG`
3. **Simple Priority** — `<PRI>MSG` (priority value 0–191)
4. **Raw Text** — Anything that doesn't match the above is stored as-is

All formats are decoded from the same UDP port. UTF-8 is attempted first, with latin-1 as fallback.

### Severity Levels

Syslog severity determines the color coding in the web UI:

| Code | Name | Color | Description |
|------|------|-------|-------------|
| 0 | Emergency | Bright Red (bold) | System is unusable |
| 1 | Alert | Red (bold) | Immediate action needed |
| 2 | Critical | Red-Orange | Critical conditions |
| 3 | Error | Red | Error conditions |
| 4 | Warning | Orange | Warning conditions |
| 5 | Notice | Green | Normal but significant |
| 6 | Informational | Blue | Informational messages |
| 7 | Debug | Gray | Debug-level messages |

The severity filter uses a **"<=  threshold"** approach: selecting "Error" shows Emergency, Alert, Critical, *and* Error messages.

Non-syslog (raw) messages are displayed in neutral gray with a "raw" label instead of a severity badge.

### Facility Codes

| Code | Name | Code | Name |
|------|------|------|------|
| 0 | kern | 12 | ntp |
| 1 | user | 13 | security |
| 2 | mail | 14 | console |
| 3 | daemon | 15 | solaris-cron |
| 4 | auth | 16 | local0 |
| 5 | syslog | 17 | local1 |
| 6 | lpr | 18 | local2 |
| 7 | news | 19 | local3 |
| 8 | uucp | 20 | local4 |
| 9 | cron | 21 | local5 |
| 10 | authpriv | 22 | local6 |
| 11 | ftp | 23 | local7 |

---

## REST API Reference

All API endpoints are available at `http://localhost:8080/api/`.

### GET `/api/logs`

Query log entries with optional filtering, sorting, and pagination.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 200 | Entries per page (1–5000) |
| `offset` | int | 0 | Skip this many entries |
| `source_ip` | string | — | Exact match on source IP |
| `hostname` | string | — | Substring match on hostname or display name |
| `severity` | int | — | Show entries with severity ≤ this value (0–7) |
| `search` | string | — | Substring match on message content |
| `start_time` | string | — | ISO 8601 timestamp lower bound |
| `end_time` | string | — | ISO 8601 timestamp upper bound |
| `sort_by` | string | `received_at` | Column to sort by: `received_at`, `timestamp`, `severity`, `source_ip`, `hostname` |
| `sort_order` | string | `DESC` | `ASC` or `DESC` |
| `include_markers` | bool | true | Include marker entries in results |

**Response:**
```json
{
  "entries": [
    {
      "id": 1,
      "timestamp": "2025-01-23T15:43:00Z",
      "received_at": "2025-01-23T15:43:00.123Z",
      "source_ip": "192.168.1.1",
      "source_port": 45678,
      "hostname": "router1",
      "facility": 4,
      "severity": 2,
      "priority": 34,
      "app_name": "sshd",
      "proc_id": "5432",
      "msg_id": null,
      "message": "Failed password for root",
      "raw_message": "<34>Jan 23 15:43:00 router1 sshd[5432]: Failed password for root",
      "is_syslog": 1,
      "is_marker": 0,
      "marker_style": null,
      "severity_name": "Critical",
      "facility_name": "auth"
    }
  ],
  "total": 15432,
  "limit": 200,
  "offset": 0
}
```

### GET `/api/hosts`

List all known source devices.

**Response:**
```json
{
  "hosts": [
    {
      "ip": "192.168.1.1",
      "hostname": "router1",
      "display_name": "Main Router",
      "first_seen": "2025-01-20T10:00:00Z",
      "last_seen": "2025-01-23T15:43:00Z",
      "message_count": 4523
    }
  ]
}
```

### POST `/api/hosts/{ip}/name`

Set a display name for a known host.

**Request body:**
```json
{ "display_name": "Main Router" }
```

### POST `/api/markers`

Insert a marker into the log stream.

**Request body:**
```json
{
  "label": "Maintenance Window Start",
  "timestamp": "2025-01-23T15:43:00Z",
  "style": "warning"
}
```

| Field | Required | Default | Options |
|-------|----------|---------|---------|
| `label` | Yes | — | Any text |
| `timestamp` | No | Now | ISO 8601 string |
| `style` | No | `default` | `default`, `info`, `success`, `warning`, `danger` |

### GET `/api/export`

Download log entries as a CSV file. Accepts the same filter parameters as `/api/logs` plus:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | 10000 | Maximum entries (up to 100,000) |

### GET `/api/stats`

Server statistics.

**Response:**
```json
{
  "total_entries": 154320,
  "known_hosts": 47,
  "latest_id": 154320
}
```

---

## Database

EverythingLogger uses **SQLite** with **WAL (Write-Ahead Logging)** mode, which provides:

- **Concurrent reads during writes** — The web UI can query while messages are being inserted
- **High write throughput** — Easily handles thousands of inserts per second
- **Zero configuration** — No database server to install or manage
- **Single file** — The entire database is one `.db` file, easy to back up or move

**Performance settings:**
- `journal_mode=WAL` — Concurrent reads/writes
- `synchronous=NORMAL` — Batched disk flushes for speed
- `cache_size=64MB` — Large in-memory cache
- `busy_timeout=5000ms` — Wait up to 5 seconds for locks

**Indexes** are created on: `timestamp`, `received_at`, `source_ip`, `hostname`, `severity`, `is_marker`

The database file location defaults to `logs.db` in the current directory. Override with `--db /path/to/database.db`.

**Backup:** Simply copy the `.db` file while the server is running (WAL mode makes this safe).

---

## FAQ

**Q: Is SQLite really fast enough for 300 devices?**
Yes. With WAL mode, SQLite handles tens of thousands of writes per second. Typical syslog traffic from 300 devices is well within its capability. The bottleneck would be network I/O long before SQLite becomes an issue.

**Q: Why port 5514 instead of 514?**
Port 514 is the standard syslog port but requires root/sudo privileges. Port 5514 works without elevated permissions. Use `--udp-port 514` with `sudo` if you need the standard port.

**Q: What happens with non-syslog messages?**
They're stored as-is with `is_syslog=0`. In the web UI they appear in neutral gray with a "raw" label. No fake severity is assigned — the severity filter won't match them unless you leave it on "All".

**Q: Can I filter by severity if some messages aren't syslog?**
Yes. The severity filter only applies to syslog messages. When filtering by severity, non-syslog messages are excluded from results since they don't have a severity level.

**Q: How do markers work with the timestamp?**
When you create a marker, you can set the timestamp to any time — past, present, or future. This lets you retroactively annotate events. For example, you could add a marker for "Power outage started" at the exact time it happened, even if you're adding it hours later.

**Q: How do I run this as a background service?**
Use systemd, supervisor, or screen/tmux:

```bash
# With systemd (create /etc/systemd/system/everythinglogger.service)
[Unit]
Description=EverythingLogger Syslog Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/EverythingLogger/app.py --udp-port 514
WorkingDirectory=/path/to/EverythingLogger
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```

```bash
# Or simply with nohup
nohup python app.py &
```

**Q: How big will the database get?**
Depends on message volume. A rough estimate: ~200 bytes per entry average, so 1 million entries ≈ 200MB. SQLite handles databases up to 281 TB, so storage is effectively limited only by your disk.
