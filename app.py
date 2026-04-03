#!/usr/bin/env python3
"""
FetchLog - Universal Syslog Server & Log Viewer

A UDP syslog server that accepts messages from any source (syslog or raw strings),
stores them in a database (SQLite or PostgreSQL), and provides a real-time web UI
for viewing, filtering, marking, and exporting logs.

Usage:
    python app.py [--udp-port 5514] [--web-port 8080]
    python app.py --db-config /path/to/db_config.json

Default ports:
    UDP syslog: 5514  (use 514 if running as root for standard syslog)
    Web UI:     8080  (open http://localhost:8080 in your browser)
"""

import argparse
import asyncio
import importlib
import json
import logging
import os
import re
import subprocess
import sys

# Configure logging early so dependency checks can log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fetchlog")


# ---- Dependency auto-install ------------------------------------------------

# Map PyPI package names to their Python import names where they differ
_IMPORT_MAP = {
    "uvicorn[standard]": "uvicorn",
    "python-dateutil": "dateutil",
    "psycopg2-binary": "psycopg2",
    "aiofiles": "aiofiles",
    "jinja2": "jinja2",
    "fastapi": "fastapi",
    "websockets": "websockets",
}


def _parse_requirements(path: str) -> list[tuple[str, str]]:
    """Return list of (pip_package, import_name) from a requirements file."""
    entries = []
    if not os.path.isfile(path):
        return entries
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Strip version specifiers (>=, ==, ~=, etc.)
            pkg = re.split(r"[><=!~;]", line)[0].strip()
            import_name = _IMPORT_MAP.get(pkg, pkg.replace("-", "_"))
            entries.append((line, import_name))
    return entries


def load_db_config(config_path: str) -> dict:
    """Load the database config file. Returns defaults for SQLite if file not found."""
    if os.path.isfile(config_path):
        with open(config_path) as f:
            return json.load(f)
    # No config file = SQLite defaults
    return {"db_type": "sqlite", "sqlite_path": "logs.db"}


def ensure_dependencies(db_type: str = "sqlite"):
    """Check that all required packages are importable; pip-install missing ones."""
    req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    requirements = _parse_requirements(req_path)

    missing = []
    for pip_spec, import_name in requirements:
        # Skip psycopg2 when using sqlite - no need to install it
        if import_name == "psycopg2" and db_type != "postgresql":
            continue
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_spec)

    if not missing:
        return

    logger.info("Installing missing dependencies: %s", ", ".join(missing))
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
            stdout=subprocess.DEVNULL,
        )
        logger.info("Dependencies installed successfully.")
        # Clear the import cache so newly installed packages are found
        importlib.invalidate_caches()
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to install dependencies (exit code %d).", exc.returncode)
        logger.error("Install them manually with:  pip install -r %s", req_path)
        sys.exit(1)


# ---- Imports that depend on installed packages ------------------------------
# These are deferred until after ensure_dependencies() runs in main().

def _load_app_modules():
    """Import application modules after dependencies are verified."""
    global uvicorn, start_syslog_server, fastapi_app, set_database, broadcast_log
    import uvicorn as _uvicorn
    uvicorn = _uvicorn
    from syslog_server import start_syslog_server as _syslog
    start_syslog_server = _syslog
    from web_server import app as _app, set_database as _set_db, broadcast_log as _broadcast
    fastapi_app = _app
    set_database = _set_db
    broadcast_log = _broadcast


def parse_args():
    parser = argparse.ArgumentParser(
        description="FetchLog - Universal Syslog Server & Log Viewer"
    )
    parser.add_argument(
        "--udp-port", type=int, default=5514,
        help="UDP port for receiving syslog/raw messages (default: 5514)"
    )
    parser.add_argument(
        "--web-port", type=int, default=8080,
        help="HTTP port for the web UI (default: 8080)"
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--db-config", type=str, default="db_config.json",
        help="Path to database config file (default: db_config.json)"
    )
    parser.add_argument(
        "--db", type=str, default=None,
        help="SQLite database file path (overrides sqlite_path in config file)"
    )
    return parser.parse_args()


class LogRouter:
    """Routes incoming UDP messages to database and WebSocket clients."""

    def __init__(self, db, loop: asyncio.AbstractEventLoop):
        self.db = db
        self.loop = loop
        self._count = 0

    def on_message(self, entry: dict):
        """Called by the UDP server for each received message."""
        try:
            row_id = self.db.insert_log(entry)
            self._count += 1

            if self._count % 1000 == 0:
                logger.info("Processed %d messages total", self._count)

            # Fetch the full row from DB so the WebSocket message has
            # the same shape as /api/logs responses (all columns present)
            rows = self.db.get_entries_after(row_id - 1, limit=1)
            if rows:
                self.loop.create_task(broadcast_log(rows[0]))

        except Exception:
            logger.exception("Error routing message")


async def run_app(args, db_config: dict):
    """Main async entry point - starts UDP server and web server together."""
    # Initialize database
    db_type = db_config.get("db_type", "sqlite")
    if db_type == "postgresql":
        from database_pg import LogDatabase
        db = LogDatabase(db_config)
        db_label = f"PostgreSQL ({db_config.get('host', 'localhost')}:{db_config.get('port', 5432)}/{db_config.get('dbname', 'fetchlog')})"
    else:
        from database import LogDatabase
        sqlite_path = db_config.get("sqlite_path", "logs.db")
        db = LogDatabase(sqlite_path)
        db_label = sqlite_path
    set_database(db)
    logger.info("Database initialized: %s", db_label)

    loop = asyncio.get_running_loop()
    router = LogRouter(db, loop)

    # Start UDP syslog server
    transport, protocol = await start_syslog_server(
        on_message=router.on_message,
        host=args.host,
        port=args.udp_port,
        loop=loop,
    )
    logger.info("UDP syslog server listening on %s:%d", args.host, args.udp_port)

    # Start web server using uvicorn
    config = uvicorn.Config(
        fastapi_app,
        host=args.host,
        port=args.web_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info("Web UI available at http://%s:%d",
                "localhost" if args.host == "0.0.0.0" else args.host,
                args.web_port)

    print(f"""
╔══════════════════════════════════════════════════════╗
║                   FetchLog v1.0                      ║
╠══════════════════════════════════════════════════════╣
║  UDP Syslog:  {args.host}:{args.udp_port:<30}║
║  Web UI:      http://localhost:{args.web_port:<21}║
║  Database:    {db_label:<38}║
╠══════════════════════════════════════════════════════╣
║  Send syslog:                                        ║
║    logger -d -n 127.0.0.1 -P {args.udp_port:<5} "test message"     ║
║                                                      ║
║  Send raw UDP:                                       ║
║    echo "hello" | nc -u 127.0.0.1 {args.udp_port:<18}║
╚══════════════════════════════════════════════════════╝
""")

    try:
        await server.serve()
    finally:
        transport.close()
        logger.info("Shutting down...")


def main():
    args = parse_args()
    db_config = load_db_config(args.db_config)
    # --db flag overrides sqlite_path for backwards compatibility
    if args.db:
        db_config["sqlite_path"] = args.db
    # Check and auto-install missing dependencies before importing app modules
    ensure_dependencies(db_type=db_config.get("db_type", "sqlite"))
    _load_app_modules()
    try:
        asyncio.run(run_app(args, db_config))
    except KeyboardInterrupt:
        print("\nShutdown requested. Goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
