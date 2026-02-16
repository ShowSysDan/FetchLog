#!/usr/bin/env python3
"""
EverythingLogger - Universal Syslog Server & Log Viewer

A UDP syslog server that accepts messages from any source (syslog or raw strings),
stores them in SQLite, and provides a real-time web UI for viewing, filtering,
marking, and exporting logs.

Usage:
    python app.py [--udp-port 5514] [--web-port 8080] [--db logs.db]

Default ports:
    UDP syslog: 5514  (use 514 if running as root for standard syslog)
    Web UI:     8080  (open http://localhost:8080 in your browser)
"""

import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from database import LogDatabase
from syslog_server import start_syslog_server
from web_server import app, set_database, broadcast_log

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("everythinglogger")


def parse_args():
    parser = argparse.ArgumentParser(
        description="EverythingLogger - Universal Syslog Server & Log Viewer"
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
        "--db", type=str, default="logs.db",
        help="SQLite database file path (default: logs.db)"
    )
    return parser.parse_args()


class LogRouter:
    """Routes incoming UDP messages to database and WebSocket clients."""

    def __init__(self, db: LogDatabase, loop: asyncio.AbstractEventLoop):
        self.db = db
        self.loop = loop
        self._count = 0

    def on_message(self, entry: dict):
        """Called by the UDP server for each received message."""
        try:
            row_id = self.db.insert_log(entry)
            entry["id"] = row_id
            self._count += 1

            if self._count % 1000 == 0:
                logger.info("Processed %d messages total", self._count)

            # Schedule WebSocket broadcast on the event loop
            asyncio.run_coroutine_threadsafe(broadcast_log(entry), self.loop)

        except Exception:
            logger.exception("Error routing message")


async def run_app(args):
    """Main async entry point - starts UDP server and web server together."""
    # Initialize database
    db = LogDatabase(args.db)
    set_database(db)
    logger.info("Database initialized: %s", args.db)

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
        app,
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
║              EverythingLogger v1.0                   ║
╠══════════════════════════════════════════════════════╣
║  UDP Syslog:  {args.host}:{args.udp_port:<30}║
║  Web UI:      http://localhost:{args.web_port:<21}║
║  Database:    {args.db:<38}║
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
    try:
        asyncio.run(run_app(args))
    except KeyboardInterrupt:
        print("\nShutdown requested. Goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
