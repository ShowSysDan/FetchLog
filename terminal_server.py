"""
FetchLog terminal live view — embedded SSH and telnet servers.

Point any SSH or telnet client at FetchLog and get an htop-style, full-color
live view of the log stream: a stats bar up top, color-coded entries with the
newest at the top, older lines rolling off the bottom, and a key-hint bar at
the bottom of the screen.

The SSH server is embedded in the app (asyncssh) — no system sshd involved.
When FetchLog auth is enabled, SSH/telnet logins are validated against the
same shared user store as the web UI, unless "terminal.require_auth" is set
to false in db_config.json (then any client connects unauthenticated).

Keys:
    q / Q       Disconnect
    SPACE       Pause / resume (entries buffer while paused)
    c / C       Clear the screen buffer

Enabled via db_config.json:
    "terminal": {"ssh_port": 2222, "telnet_port": 2323}
or CLI: python app.py --ssh-port 2222 --telnet-port 2323
"""

import asyncio
import collections
import datetime
import itertools
import logging
import os
import re
from typing import Callable, Optional

logger = logging.getLogger("fetchlog.term")

# ---------------------------------------------------------------------------
# Severity / column configuration (mirrors tui.py's layout)
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

COL_TIME   = 14  # MM-DD HH:MM:SS
COL_SOURCE = 15
COL_HOST   = 14
COL_SEV    = 5
COL_APP    = 12
FIXED_COLS = COL_TIME + 1 + COL_SOURCE + 1 + COL_HOST + 1 + COL_SEV + 1 + COL_APP + 1  # = 65

MAX_BUFFER = 500          # entries kept per session for redraws
MIN_COLS, MIN_ROWS = 40, 8

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

CSI = "\x1b["
RESET = CSI + "0m"

# SGR codes per severity — same palette as the curses TUI
_SEV_SGR = {
    0: "1;91",   # EMERG  bright red bold
    1: "1;91",   # ALERT  bright red bold
    2: "31",     # CRIT   red
    3: "31",     # ERROR  red
    4: "33",     # WARN   yellow
    5: "32",     # NOTICE green
    6: "36",     # INFO   cyan
    7: "2;37",   # DEBUG  dim white
}
_RAW_SGR = "37"
_MARKER_SGR = "1;35"
_COLHDR_SGR = "30;42"    # black on green, htop-style column header
_KEYLBL_SGR = "30;46"    # black on cyan, htop-style key bar labels

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _cup(row: int, col: int) -> str:
    return f"{CSI}{row};{col}H"


def _fit(s, width: int) -> str:
    """Sanitized string exactly `width` chars wide (control chars stripped so
    log content can't inject escape sequences into the client terminal)."""
    s = _CTRL_RE.sub(".", str(s) if s is not None else "")
    if len(s) > width:
        return s[:width - 1] + "~"
    return s.ljust(width)


def _time(entry: dict) -> str:
    """Parse received_at (UTC) and display in local time as MM-DD HH:MM:SS."""
    ts = entry.get("received_at") or entry.get("timestamp") or ""
    if "T" in ts:
        try:
            s = ts.rstrip("Z").split("+")[0][:19]
            dt = datetime.datetime.fromisoformat(s).replace(
                tzinfo=datetime.timezone.utc
            ).astimezone()
            return dt.strftime("%m-%d %H:%M:%S")
        except ValueError:
            date_part, time_part = ts.split("T", 1)
            month_day = date_part[5:10] if len(date_part) >= 10 else date_part
            return f"{month_day} {time_part[:8]}"
    return "?"


def format_row(entry: dict, cols: int) -> str:
    """One log entry as a colored, width-clamped terminal line."""
    width = cols - 1
    if entry.get("is_marker"):
        label = _CTRL_RE.sub(".", str(entry.get("message") or "Marker"))
        center = f"--- {label} ---"
        pad = width - len(center)
        if pad > 0:
            left = pad // 2
            line = "-" * left + center + "-" * (pad - left)
        else:
            line = center[:width]
        return f"{CSI}{_MARKER_SGR}m{line}{RESET}"

    sev = entry.get("severity")
    sgr = _SEV_SGR.get(sev, _RAW_SGR) if sev is not None else _RAW_SGR
    sev_str = SEVERITY_SHORT.get(sev, "RAW  ") if sev is not None else "RAW  "
    app = entry.get("app_name") or ("syslog" if entry.get("is_syslog") else "raw")
    mw = max(cols - FIXED_COLS, 10)
    line = (
        f"{_fit(_time(entry),              COL_TIME  )} "
        f"{_fit(entry.get('source_ip'),    COL_SOURCE)} "
        f"{_fit(entry.get('hostname') or entry.get('source_ip'), COL_HOST)} "
        f"{_fit(sev_str,                   COL_SEV   )} "
        f"{_fit(app,                       COL_APP   )} "
        f"{_fit(entry.get('message'),      mw        )}"
    )
    return f"{CSI}{sgr}m{line[:width]}{RESET}"


def _colored_line(segments: list, width: int) -> str:
    """Join (sgr, text) segments, truncated to `width` visible chars, then
    erase to end of line so stale content never lingers."""
    out = []
    used = 0
    for sgr, text in segments:
        if used >= width:
            break
        t = text[: width - used]
        out.append(f"{CSI}{sgr}m{t}")
        used += len(t)
    out.append(RESET + CSI + "K")
    return "".join(out)


# ---------------------------------------------------------------------------
# Terminal session — transport-agnostic screen state machine
# ---------------------------------------------------------------------------

class TerminalSession:
    """One connected SSH/telnet client's screen.

    Layout:  row 1 = stats bar, row 2 = column header,
             rows 3..N-1 = scroll region (newest entry on top),
             row N = key-hint bar.

    New entries are inserted at the top of the scroll region with ESC[L, which
    pushes older lines down and drops the bottom line — no full redraw needed.
    """

    def __init__(self, write: Callable[[str], None], database, label: str,
                 cols: int = 80, rows: int = 24,
                 transport_name: str = "ssh", username: Optional[str] = None):
        self._write = write
        self.db = database
        self.label = label
        self.transport_name = transport_name
        self.username = username
        self.cols = max(int(cols) if cols else 80, MIN_COLS)
        self.rows = max(int(rows) if rows else 24, MIN_ROWS)
        self.paused = False
        self.pending: list = []
        self.entries: collections.deque = collections.deque(maxlen=MAX_BUFFER)  # newest first
        self.total = 0
        self.err_count = 0
        self.warn_count = 0
        self.closed = False

    # ---- layout -----------------------------------------------------------

    @property
    def log_top(self) -> int:
        return 3

    @property
    def log_bottom(self) -> int:
        return self.rows - 1

    @property
    def log_rows(self) -> int:
        return max(self.log_bottom - self.log_top + 1, 1)

    # ---- output -----------------------------------------------------------

    def _send(self, data: str):
        if self.closed:
            return
        try:
            self._write(data)
        except Exception:
            self.closed = True

    # ---- lifecycle --------------------------------------------------------

    async def start(self):
        """Backfill history (newest first) and paint the initial screen."""
        history = await asyncio.to_thread(self._fetch_history)
        for e in history:
            self.entries.append(e)
            self._tally(e)
        self.total = len(history)
        self.redraw()

    def _fetch_history(self) -> list:
        try:
            return self.db.query_logs(
                limit=MAX_BUFFER, offset=0,
                sort_by="received_at", sort_order="DESC")
        except Exception:
            logger.exception("history fetch failed for %s", self.label)
            return []

    def close(self):
        if self.closed:
            return
        try:
            self._write(CSI + "r" + CSI + "2J" + _cup(1, 1) + CSI + "?25h" +
                        RESET + "Disconnected from FetchLog.\r\n")
        except Exception:
            pass
        self.closed = True

    # ---- events -----------------------------------------------------------

    def _tally(self, entry: dict):
        sev = entry.get("severity")
        if sev is None or entry.get("is_marker"):
            return
        if sev <= 3:
            self.err_count += 1
        elif sev == 4:
            self.warn_count += 1

    def push(self, entry: dict):
        """A new live entry arrived: insert at top, roll oldest off the bottom."""
        if self.closed:
            return
        self.total += 1
        self._tally(entry)
        if self.paused:
            self.pending.append(entry)
            self._send(self._stats_bar())
            return
        self.entries.appendleft(entry)
        self._send(
            _cup(self.log_top, 1) + CSI + "L" +
            format_row(entry, self.cols) +
            self._stats_bar()
        )

    def handle_key(self, key: str):
        if not key:
            return
        k = key.lower()
        if k == "q" or key in ("\x03", "\x04"):   # q, Ctrl-C, Ctrl-D
            self.close()
        elif key == " ":
            if self.paused:
                self.paused = False
                for e in self.pending:
                    self.entries.appendleft(e)
                self.pending = []
                self.redraw()
            else:
                self.paused = True
                self._send(self._stats_bar() + self._key_bar())
        elif k == "c":
            self.entries.clear()
            self.pending.clear()
            self.redraw()

    def resize(self, cols: int, rows: int):
        self.cols = max(int(cols) if cols else 80, MIN_COLS)
        self.rows = max(int(rows) if rows else 24, MIN_ROWS)
        self.redraw()

    # ---- drawing ----------------------------------------------------------

    def redraw(self):
        out = [CSI + "?25l", CSI + "r", CSI + "2J"]
        out.append(self._stats_bar())
        out.append(self._column_header())
        for i, e in enumerate(itertools.islice(self.entries, self.log_rows)):
            out.append(_cup(self.log_top + i, 1) + format_row(e, self.cols))
        out.append(self._key_bar())
        out.append(CSI + f"{self.log_top};{self.log_bottom}r")
        out.append(_cup(self.log_top, 1))
        self._send("".join(out))

    def _stats_bar(self) -> str:
        segs = [
            ("1;96", " FetchLog "),
            ("2;37", f"{self.transport_name} "),
        ]
        if self.paused:
            segs.append(("1;93", "‖ PAUSED"))
            segs.append(("2;37", f" ({len(self.pending)} buffered)"))
        else:
            segs.append(("1;92", "● LIVE"))
        segs.append(("0", f"   Entries: {self.total}"))
        segs.append(("1;31", f"   Err: {self.err_count}"))
        segs.append(("1;33", f"   Warn: {self.warn_count}"))
        if self.username:
            segs.append(("2;37", f"   user: {self.username}"))
        return _cup(1, 1) + _colored_line(segs, self.cols - 1)

    def _column_header(self) -> str:
        mw = max(self.cols - FIXED_COLS, 10)
        text = (
            f"{'TIME':<{COL_TIME}} "
            f"{'SOURCE':<{COL_SOURCE}} "
            f"{'HOST':<{COL_HOST}} "
            f"{'SEV':<{COL_SEV}} "
            f"{'APP':<{COL_APP}} "
            f"{'MESSAGE':<{mw}}"
        )
        return (_cup(2, 1) + f"{CSI}{_COLHDR_SGR}m" +
                text[: self.cols - 1].ljust(self.cols - 1) + RESET)

    def _key_bar(self) -> str:
        keys = [
            ("Q", "Quit"),
            ("SPACE", "Resume" if self.paused else "Pause"),
            ("C", "Clear"),
        ]
        segs = []
        for key, action in keys:
            segs.append(("0", key))
            segs.append((_KEYLBL_SGR, action))
            segs.append(("0", " "))
        return _cup(self.rows, 1) + _colored_line(segs, self.cols - 1)


# ---------------------------------------------------------------------------
# Session registry + broadcast (called by web_server for every new entry)
# ---------------------------------------------------------------------------

_sessions: "set[TerminalSession]" = set()
_db = None
_auth = None
_require_auth = True


def _auth_required() -> bool:
    return bool(_require_auth and _auth is not None and _auth.enabled)


async def broadcast_entry(entry: dict):
    """Push a new log entry to every connected SSH/telnet session."""
    for session in list(_sessions):
        session.push(entry)
        if session.closed:
            _sessions.discard(session)


# ---------------------------------------------------------------------------
# Telnet server — minimal option negotiation (ECHO, SGA, NAWS), no deps
# ---------------------------------------------------------------------------

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA, OPT_NAWS = 1, 3, 31


class _TelnetParser:
    """Incremental telnet stream parser: separates IAC commands from data.

    feed() returns a list of events: ("data", bytes) and ("naws", (cols, rows)).
    """

    def __init__(self):
        self._state = "data"
        self._sb_opt = None
        self._sb = bytearray()

    def feed(self, data: bytes) -> list:
        events = []
        buf = bytearray()

        def flush():
            nonlocal buf
            if buf:
                events.append(("data", bytes(buf)))
                buf = bytearray()

        for b in data:
            if self._state == "data":
                if b == IAC:
                    self._state = "iac"
                else:
                    buf.append(b)
            elif self._state == "iac":
                if b == IAC:
                    buf.append(IAC)
                    self._state = "data"
                elif b in (DO, DONT, WILL, WONT):
                    self._state = "opt"
                elif b == SB:
                    self._state = "sb_opt"
                else:
                    self._state = "data"
            elif self._state == "opt":
                self._state = "data"
            elif self._state == "sb_opt":
                self._sb_opt = b
                self._sb = bytearray()
                self._state = "sb"
            elif self._state == "sb":
                if b == IAC:
                    self._state = "sb_iac"
                else:
                    self._sb.append(b)
            elif self._state == "sb_iac":
                if b == SE:
                    if self._sb_opt == OPT_NAWS and len(self._sb) >= 4:
                        flush()
                        cols = (self._sb[0] << 8) | self._sb[1]
                        rows = (self._sb[2] << 8) | self._sb[3]
                        events.append(("naws", (cols, rows)))
                    self._state = "data"
                elif b == IAC:
                    self._sb.append(IAC)
                    self._state = "sb"
                else:
                    self._state = "data"
        flush()
        return events


async def _telnet_read_line(reader, writer, parser: _TelnetParser,
                            dims: dict, echo: bool) -> Optional[str]:
    """Read one line during the telnet login prompt (we run with server-side
    echo, so echo printable chars back ourselves; never echo passwords)."""
    line = []
    while True:
        data = await reader.read(256)
        if not data:
            return None
        for kind, payload in parser.feed(data):
            if kind == "naws":
                dims["cols"], dims["rows"] = payload
                continue
            for b in payload:
                ch = chr(b)
                if ch in ("\r", "\n"):
                    writer.write(b"\r\n")
                    return "".join(line)
                if ch == "\x03":          # Ctrl-C
                    return None
                if b in (0x08, 0x7F):     # backspace / delete
                    if line:
                        line.pop()
                        if echo:
                            writer.write(b"\b \b")
                    continue
                if b == 0x00 or b < 0x20:
                    continue
                line.append(ch)
                if echo:
                    writer.write(ch.encode("utf-8", "replace"))


async def _telnet_login(reader, writer, parser: _TelnetParser,
                        dims: dict) -> Optional[str]:
    writer.write(b"\r\nFetchLog live view - authentication required.\r\n")
    for _ in range(3):
        writer.write(b"Username: ")
        username = await _telnet_read_line(reader, writer, parser, dims, echo=True)
        if username is None:
            return None
        writer.write(b"Password: ")
        password = await _telnet_read_line(reader, writer, parser, dims, echo=False)
        if password is None:
            return None
        try:
            result = await asyncio.to_thread(_auth.login, username.strip(), password)
        except Exception:
            logger.exception("telnet auth error")
            result = {"ok": False}
        if result.get("ok"):
            return username.strip()
        writer.write(b"\r\nLogin incorrect.\r\n\r\n")
    return None


async def _handle_telnet(reader, writer):
    peer = writer.get_extra_info("peername")
    label = f"{peer[0]}:{peer[1]}" if peer else "?"
    logger.info("Telnet client connected: %s", label)

    parser = _TelnetParser()
    dims = {"cols": 80, "rows": 24}
    session = None
    try:
        writer.write(bytes([IAC, WILL, OPT_SGA,
                            IAC, WILL, OPT_ECHO,
                            IAC, DO, OPT_NAWS]))

        username = None
        if _auth_required():
            username = await _telnet_login(reader, writer, parser, dims)
            if username is None:
                writer.write(b"Too many failures. Bye.\r\n")
                return
        else:
            # Give the client a moment to answer DO NAWS so the first paint
            # already uses the real window size.
            try:
                data = await asyncio.wait_for(reader.read(256), timeout=0.4)
                for kind, payload in parser.feed(data or b""):
                    if kind == "naws":
                        dims["cols"], dims["rows"] = payload
            except (asyncio.TimeoutError, ConnectionError):
                pass

        def write_str(s: str):
            transport = writer.transport
            if transport.is_closing():
                raise ConnectionError("telnet client gone")
            if transport.get_write_buffer_size() > 1_000_000:
                raise ConnectionError("telnet client too slow, dropping")
            writer.write(s.encode("utf-8", "replace"))

        session = TerminalSession(
            write=write_str, database=_db, label=label,
            cols=dims["cols"], rows=dims["rows"],
            transport_name="telnet", username=username)
        _sessions.add(session)
        await session.start()

        while not session.closed:
            data = await reader.read(1024)
            if not data:
                break
            for kind, payload in parser.feed(data):
                if kind == "naws":
                    session.resize(*payload)
                elif kind == "data":
                    for ch in payload.decode("utf-8", "ignore"):
                        session.handle_key(ch)
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    except Exception:
        logger.exception("telnet session error (%s)", label)
    finally:
        if session is not None:
            _sessions.discard(session)
            session.close()
        try:
            writer.close()
        except Exception:
            pass
        logger.info("Telnet client disconnected: %s", label)


# ---------------------------------------------------------------------------
# SSH server — embedded via asyncssh (imported lazily; only needed if enabled)
# ---------------------------------------------------------------------------

def _make_ssh_server_class(asyncssh):
    class _FetchLogSSHServer(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            # False = no authentication required (open access)
            return _auth_required()

        def password_auth_supported(self) -> bool:
            return True

        async def validate_password(self, username: str, password: str) -> bool:
            try:
                result = await asyncio.to_thread(_auth.login, username, password)
                return bool(result.get("ok"))
            except Exception:
                logger.exception("SSH auth error")
                return False

    return _FetchLogSSHServer


async def _handle_ssh_process(process):
    import asyncssh

    conn = process.channel.get_connection()
    peer = conn.get_extra_info("peername")
    username = conn.get_extra_info("username")
    label = f"{peer[0]}:{peer[1]}" if peer else "?"
    logger.info("SSH client connected: %s (user=%s)", label, username)

    cols, rows = 80, 24
    try:
        size = process.get_terminal_size()
        if size:
            cols = size[0] or 80
            rows = size[1] or 24
    except Exception:
        pass

    session = TerminalSession(
        write=process.stdout.write, database=_db, label=label,
        cols=cols, rows=rows, transport_name="ssh",
        username=username if _auth_required() else None)
    _sessions.add(session)
    try:
        await session.start()
        while not session.closed:
            try:
                data = await process.stdin.read(1)
            except asyncssh.TerminalSizeChanged as exc:
                session.resize(exc.width, exc.height)
                continue
            except (asyncssh.BreakReceived, asyncssh.SignalReceived):
                break
            if not data:
                break
            session.handle_key(data)
    except (ConnectionError, asyncssh.ConnectionLost):
        pass
    except Exception:
        logger.exception("SSH session error (%s)", label)
    finally:
        _sessions.discard(session)
        session.close()
        try:
            # Flush the goodbye/reset sequence before closing the channel.
            await process.stdout.drain()
            process.exit(0)
        except Exception:
            pass
        logger.info("SSH client disconnected: %s", label)


def _ensure_host_key(asyncssh, path: str):
    """Load the persistent SSH host key, generating one on first run so
    clients don't see a changed-host-key warning on every restart."""
    if os.path.isfile(path):
        return asyncssh.read_private_key(path)
    key = asyncssh.generate_private_key("ssh-ed25519", comment="fetchlog-host-key")
    with open(path, "wb") as f:
        f.write(key.export_private_key())
    os.chmod(path, 0o600)
    logger.info("Generated SSH host key: %s", path)
    return key


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def start_servers(*, host: str, ssh_port: int, telnet_port: int,
                        database, auth_manager,
                        require_auth: bool = True,
                        ssh_host_key: str = "ssh_host_key") -> list:
    """Start the enabled terminal servers. Returns handles with .close()."""
    global _db, _auth, _require_auth
    _db = database
    _auth = auth_manager
    _require_auth = require_auth

    handles = []
    auth_note = "auth required" if _auth_required() else "UNAUTHENTICATED"

    if telnet_port:
        server = await asyncio.start_server(_handle_telnet, host, telnet_port)
        handles.append(server)
        logger.info("Telnet live view on %s:%d (%s) - telnet is unencrypted, "
                    "use only on trusted networks", host, telnet_port, auth_note)

    if ssh_port:
        try:
            import asyncssh
        except ImportError:
            logger.error("asyncssh is not installed - SSH live view disabled. "
                         "Install it with: pip install asyncssh")
        else:
            key = _ensure_host_key(asyncssh, ssh_host_key)
            server = await asyncssh.listen(
                host, ssh_port,
                server_host_keys=[key],
                server_factory=_make_ssh_server_class(asyncssh),
                process_factory=_handle_ssh_process,
                encoding="utf-8",
                line_editor=False,   # deliver keypresses immediately, unbuffered
            )
            handles.append(server)
            logger.info("SSH live view on %s:%d (%s) - connect with: "
                        "ssh -p %d <this-host>", host, ssh_port, auth_note, ssh_port)

    return handles
