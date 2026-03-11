#!/usr/bin/env python3
"""
FetchLog TUI — Real-time terminal log viewer.

Connects to a running FetchLog server via WebSocket and REST API, displaying
incoming log entries with color-coding by severity in a columnar layout.

Usage:
    python tui.py [--host HOST] [--port PORT] [--tail N]

Keys:
    q / Q       Quit
    SPACE       Pause / resume live scrolling
"""

import argparse
import asyncio
import curses
import json
import queue
import threading
import time
import urllib.request


# ---------------------------------------------------------------------------
# Severity configuration
# ---------------------------------------------------------------------------

SEVERITY_SHORT = {
    0: "EMERG",
    1: "ALERT",
    2: "CRIT ",
    3: "ERROR",
    4: "WARN ",
    5: "NOTIC",
    6: "INFO ",
    7: "DEBUG",
}

# Curses color pair numbers
PAIR_EMERG  = 1   # RED + BOLD  — severity 0, 1
PAIR_ERROR  = 2   # RED         — severity 2, 3
PAIR_WARN   = 3   # YELLOW      — severity 4
PAIR_NOTICE = 4   # GREEN       — severity 5
PAIR_INFO   = 5   # CYAN        — severity 6
PAIR_DEBUG  = 6   # WHITE + DIM — severity 7
PAIR_RAW    = 7   # WHITE       — raw / no severity
PAIR_MARKER = 8   # MAGENTA + BOLD — markers
PAIR_HEADER = 9   # WHITE on BLUE  — header and status bar

# ---------------------------------------------------------------------------
# Column widths (characters)
# ---------------------------------------------------------------------------

COL_TIME   = 14  # MM-DD HH:MM:SS
COL_SOURCE = 15  # source IP
COL_HOST   = 14  # hostname
COL_SEV    = 5   # EMERG / WARN / INFO …
COL_APP    = 12  # app_name
# MESSAGE width = terminal_width - FIXED_COLS
# Spacing: one space between each column = 5 separators
FIXED_COLS = COL_TIME + 1 + COL_SOURCE + 1 + COL_HOST + 1 + COL_SEV + 1 + COL_APP + 1  # = 65

MAX_BUFFER = 2000   # maximum log entries kept in memory


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="FetchLog TUI — real-time terminal log viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Keys:  [Q] quit    [SPACE] pause/resume",
    )
    p.add_argument("--host", default="localhost",
                   help="FetchLog server host (default: localhost)")
    p.add_argument("--port", type=int, default=8080,
                   help="FetchLog web port (default: 8080)")
    p.add_argument("--tail", type=int, default=50, metavar="N",
                   help="Historical lines to show on startup (default: 50)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# History fetch — stdlib urllib only, no extra deps
# ---------------------------------------------------------------------------

def fetch_history(host: str, port: int, tail: int) -> list:
    """Fetch the last `tail` log entries from the FetchLog REST API."""
    url = (
        f"http://{host}:{port}/api/logs"
        f"?limit={tail}&sort_by=received_at&sort_order=ASC"
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("entries", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# WebSocket reader — runs in a background daemon thread
# ---------------------------------------------------------------------------

async def _ws_loop(host: str, port: int, q: queue.Queue, stop: threading.Event):
    """Connect to FetchLog WebSocket, push entries into the queue."""
    import websockets

    uri = f"ws://{host}:{port}/ws"
    delay = 2.0

    while not stop.is_set():
        try:
            async with websockets.connect(
                uri, ping_interval=20, ping_timeout=10
            ) as ws:
                delay = 2.0  # reset backoff on successful connection
                q.put({"_status": "connected"})
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        entry = json.loads(raw)
                        if isinstance(entry, dict) and "_status" not in entry:
                            q.put(entry)
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break  # connection dropped — fall through to reconnect
        except Exception:
            pass

        if stop.is_set():
            break

        q.put({"_status": "disconnected"})

        # Exponential backoff: 2 → 4 → 8 → … → 30 seconds max
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not stop.is_set():
            await asyncio.sleep(0.25)
        delay = min(delay * 2, 30.0)


def ws_reader(host: str, port: int, q: queue.Queue, stop: threading.Event):
    """Thread target: run the async WebSocket loop."""
    asyncio.run(_ws_loop(host, port, q, stop))


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def init_colors():
    """Set up curses color pairs."""
    curses.start_color()
    curses.use_default_colors()  # -1 = terminal default background
    curses.init_pair(PAIR_EMERG,  curses.COLOR_RED,     -1)
    curses.init_pair(PAIR_ERROR,  curses.COLOR_RED,     -1)
    curses.init_pair(PAIR_WARN,   curses.COLOR_YELLOW,  -1)
    curses.init_pair(PAIR_NOTICE, curses.COLOR_GREEN,   -1)
    curses.init_pair(PAIR_INFO,   curses.COLOR_CYAN,    -1)
    curses.init_pair(PAIR_DEBUG,  curses.COLOR_WHITE,   -1)
    curses.init_pair(PAIR_RAW,    curses.COLOR_WHITE,   -1)
    curses.init_pair(PAIR_MARKER, curses.COLOR_MAGENTA, -1)
    curses.init_pair(PAIR_HEADER, curses.COLOR_WHITE,   curses.COLOR_BLUE)


def get_attr(entry: dict, has_colors: bool) -> int:
    """Return curses attribute (color + bold/dim) for a log entry."""
    if not has_colors:
        return curses.A_REVERSE if entry.get("is_marker") else curses.A_NORMAL

    if entry.get("is_marker"):
        return curses.color_pair(PAIR_MARKER) | curses.A_BOLD

    sev = entry.get("severity")
    if sev is None:
        return curses.color_pair(PAIR_RAW)
    if sev <= 1:
        return curses.color_pair(PAIR_EMERG) | curses.A_BOLD
    if sev <= 3:
        return curses.color_pair(PAIR_ERROR)
    if sev == 4:
        return curses.color_pair(PAIR_WARN)
    if sev == 5:
        return curses.color_pair(PAIR_NOTICE)
    if sev == 6:
        return curses.color_pair(PAIR_INFO)
    # sev == 7, debug
    return curses.color_pair(PAIR_DEBUG) | curses.A_DIM


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fit(s, width: int) -> str:
    """Return string exactly `width` chars wide, truncating with ellipsis if needed."""
    s = str(s) if s is not None else ""
    if len(s) > width:
        return s[:width - 1] + "~"
    return s.ljust(width)


def _time(entry: dict) -> str:
    """Extract MM-DD HH:MM:SS from received_at (or timestamp)."""
    ts = entry.get("received_at") or entry.get("timestamp") or ""
    if "T" in ts:
        date_part, time_part = ts.split("T", 1)
        # date_part is YYYY-MM-DD; take MM-DD
        month_day = date_part[5:10] if len(date_part) >= 10 else date_part
        return f"{month_day} {time_part[:8]}"
    return "?             "


def format_row(entry: dict, msg_width: int) -> str:
    """Format a single log entry into a fixed-width display row."""
    if entry.get("is_marker"):
        label = entry.get("message", "Marker")
        total = FIXED_COLS + msg_width
        center = f"--- {label} ---"
        padding = total - len(center)
        if padding > 0:
            left = padding // 2
            right = padding - left
            return "-" * left + center + "-" * right
        return center[:total]

    sev = entry.get("severity")
    sev_str = SEVERITY_SHORT.get(sev, "RAW  ") if sev is not None else "RAW  "
    app = entry.get("app_name") or ("syslog" if entry.get("is_syslog") else "raw")

    return (
        f"{_fit(_time(entry),              COL_TIME  )} "
        f"{_fit(entry.get('source_ip'),    COL_SOURCE)} "
        f"{_fit(entry.get('hostname') or entry.get('source_ip'), COL_HOST)} "
        f"{_fit(sev_str,                   COL_SEV   )} "
        f"{_fit(app,                       COL_APP   )} "
        f"{_fit(entry.get('message'),      msg_width )}"
    )


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _msg_width(max_x: int) -> int:
    return max(max_x - FIXED_COLS, 10)


def draw_header(stdscr, max_x: int, has_colors: bool):
    attr = (curses.color_pair(PAIR_HEADER) | curses.A_BOLD) if has_colors else (curses.A_REVERSE | curses.A_BOLD)
    mw = _msg_width(max_x)
    line = (
        f"{'TIME':<{COL_TIME}} "
        f"{'SOURCE':<{COL_SOURCE}} "
        f"{'HOST':<{COL_HOST}} "
        f"{'SEV':<{COL_SEV}} "
        f"{'APP':<{COL_APP}} "
        f"{'MESSAGE':<{mw}}"
    )
    try:
        stdscr.addstr(0, 0, line[:max_x - 1].ljust(max_x - 1), attr)
    except curses.error:
        pass


def draw_status(stdscr, max_y: int, max_x: int,
                count: int, connected: bool, paused: bool, has_colors: bool):
    attr = (curses.color_pair(PAIR_HEADER) | curses.A_BOLD) if has_colors else (curses.A_REVERSE | curses.A_BOLD)
    conn = "● Connected   " if connected else "✗ Connecting.."
    pause_hint = "[SPACE] Resume" if paused else "[SPACE] Pause "
    paused_tag = "  PAUSED" if paused else ""
    line = f" FetchLog TUI  {conn}  {count} entries{paused_tag}  [Q] Quit  {pause_hint} "
    try:
        stdscr.addstr(max_y - 1, 0, line[:max_x - 1].ljust(max_x - 1), attr)
    except curses.error:
        pass


def draw_logs(stdscr, entries: list, log_rows: int, max_x: int, has_colors: bool):
    """Draw the most recent log entries in the available rows."""
    mw = _msg_width(max_x)
    visible = entries[-log_rows:]
    for i, entry in enumerate(visible):
        row = i + 1  # row 0 is the header
        attr = get_attr(entry, has_colors)
        line = format_row(entry, mw)
        try:
            stdscr.addstr(row, 0, line[:max_x - 1].replace('\x00', '.'), attr)
            stdscr.clrtoeol()
        except curses.error:
            pass
    # Clear any leftover rows below current entries
    for i in range(len(visible), log_rows):
        try:
            stdscr.move(i + 1, 0)
            stdscr.clrtoeol()
        except curses.error:
            pass


# ---------------------------------------------------------------------------
# Main curses loop
# ---------------------------------------------------------------------------

def main_loop(stdscr, args: argparse.Namespace, history: list):
    curses.curs_set(0)        # hide cursor
    stdscr.timeout(100)       # non-blocking getch with 100 ms timeout

    has_colors = curses.has_colors()
    if has_colors:
        init_colors()

    entries: list = list(history)
    frozen_at: int = len(entries)   # display anchor when paused
    paused: bool = False
    connected: bool = False

    q: queue.Queue = queue.Queue()
    stop = threading.Event()

    ws_thread = threading.Thread(
        target=ws_reader,
        args=(args.host, args.port, q, stop),
        daemon=True,
    )
    ws_thread.start()

    try:
        while True:
            # ── Drain the message queue ─────────────────────────────────
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break

                if "_status" in item:
                    connected = (item["_status"] == "connected")
                else:
                    entries.append(item)
                    if len(entries) > MAX_BUFFER:
                        trim = len(entries) - MAX_BUFFER
                        entries = entries[trim:]
                        if paused:
                            frozen_at = max(0, frozen_at - trim)

            # ── Determine what to show ──────────────────────────────────
            max_y, max_x = stdscr.getmaxyx()
            log_rows = max(1, max_y - 2)   # rows available between header and status
            display = entries[:frozen_at] if paused else entries

            # ── Redraw ──────────────────────────────────────────────────
            stdscr.erase()
            draw_header(stdscr, max_x, has_colors)
            draw_logs(stdscr, display, log_rows, max_x, has_colors)
            draw_status(stdscr, max_y, max_x,
                        len(display), connected, paused, has_colors)
            stdscr.refresh()

            # ── Keyboard ────────────────────────────────────────────────
            key = stdscr.getch()
            if key in (ord('q'), ord('Q')):
                break
            elif key == ord(' '):
                if paused:
                    paused = False        # resume: jump to newest
                else:
                    paused = True
                    frozen_at = len(entries)
            elif key == curses.KEY_RESIZE:
                stdscr.clear()

    finally:
        stop.set()
        ws_thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    # Load history before entering curses so errors print normally
    history = fetch_history(args.host, args.port, args.tail)

    curses.wrapper(main_loop, args, history)


if __name__ == "__main__":
    main()
