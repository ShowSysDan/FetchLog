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
    "python-multipart": "multipart",
    "werkzeug": "werkzeug",
    "gunicorn": "gunicorn",
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


def ensure_dependencies(db_type: str = "sqlite", auth_enabled: bool = False):
    """Check that all required packages are importable; pip-install missing ones."""
    req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    requirements = _parse_requirements(req_path)

    def _skip(import_name: str) -> bool:
        # gunicorn is a production process manager: it imports the app, the app
        # never imports it, so don't require it to be importable in dev.
        if import_name == "gunicorn":
            return True
        # psycopg2 is needed for PostgreSQL log storage OR for auth (the shared
        # session store is always PostgreSQL).
        if import_name == "psycopg2" and db_type != "postgresql" and not auth_enabled:
            return True
        return False

    missing = []
    for pip_spec, import_name in requirements:
        if _skip(import_name):
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
        logger.warning("Auto-install failed (exit code %d). Checking if packages are already available...", exc.returncode)
        # Auto-install may fail due to permissions (e.g. service user cannot
        # write to a venv owned by another user).  Re-check whether the
        # packages are actually importable -- they may have been installed
        # earlier by the venv owner.
        still_missing = []
        for pip_spec, import_name in requirements:
            if _skip(import_name):
                continue
            try:
                importlib.import_module(import_name)
            except ImportError:
                still_missing.append(pip_spec)
        if still_missing:
            logger.error("Missing packages that could not be auto-installed: %s", ", ".join(still_missing))
            logger.error(
                "Install them manually inside the virtualenv:\n"
                "  %s -m pip install %s",
                sys.executable, " ".join(still_missing),
            )
            sys.exit(1)
        logger.info("All required packages are already installed. Continuing.")


# ---- Imports that depend on installed packages ------------------------------
# These are deferred until after ensure_dependencies() runs in main().

def _load_app_modules():
    """Import application modules after dependencies are verified."""
    global uvicorn, fastapi_app
    import uvicorn as _uvicorn
    uvicorn = _uvicorn
    # web_server's lifespan owns DB init, auth, and the UDP syslog server.
    from web_server import app as _app
    fastapi_app = _app


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


async def run_app(args, db_config: dict):
    """Dev/simple entry point: run uvicorn against the FastAPI app.

    The web app's lifespan (web_server.py) initializes the database + auth and
    starts the UDP syslog server. Runtime settings are passed via the environment
    so the very same app object also runs under gunicorn:

        gunicorn web_server:app -k uvicorn.workers.UvicornWorker -w 1 -b HOST:PORT
    """
    db_type = db_config.get("db_type", "sqlite")
    if db_type == "postgresql":
        db_label = (f"PostgreSQL ({db_config.get('host', 'localhost')}:"
                    f"{db_config.get('port', 5432)}/{db_config.get('dbname', 'fetchlog')})")
    else:
        db_label = db_config.get("sqlite_path", "logs.db")

    # Hand runtime settings to the app's lifespan via the environment.
    os.environ["FETCHLOG_HOST"] = args.host
    os.environ["FETCHLOG_UDP_PORT"] = str(args.udp_port)
    os.environ["FETCHLOG_DB_CONFIG"] = args.db_config
    if args.db:
        os.environ["FETCHLOG_SQLITE_PATH"] = args.db

    auth_state = "enabled" if (db_config.get("auth") or {}).get("enabled") else "disabled"

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
    logger.info("Authentication: %s", auth_state)

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

    await server.serve()


def main():
    args = parse_args()
    db_config = load_db_config(args.db_config)
    # --db flag overrides sqlite_path for backwards compatibility
    if args.db:
        db_config["sqlite_path"] = args.db
    # Check and auto-install missing dependencies before importing app modules
    auth_enabled = bool((db_config.get("auth") or {}).get("enabled"))
    ensure_dependencies(db_type=db_config.get("db_type", "sqlite"),
                        auth_enabled=auth_enabled)
    _load_app_modules()
    try:
        asyncio.run(run_app(args, db_config))
    except KeyboardInterrupt:
        print("\nShutdown requested. Goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
