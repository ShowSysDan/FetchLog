"""
Async UDP syslog server.

Listens on a configurable UDP port and receives syslog messages
as well as raw strings from any device.
"""

import asyncio
import logging
from typing import Callable, Optional

from syslog_parser import parse_message

logger = logging.getLogger("everythinglogger.udp")


class SyslogProtocol(asyncio.DatagramProtocol):
    """asyncio UDP protocol handler for incoming log messages."""

    def __init__(self, on_message: Callable[[dict], None]):
        self.on_message = on_message
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        logger.info("UDP syslog server is listening")

    def datagram_received(self, data: bytes, addr: tuple):
        source_ip, source_port = addr
        try:
            entry = parse_message(data, source_ip, source_port)
            self.on_message(entry)
        except Exception:
            logger.exception("Error processing message from %s:%s", source_ip, source_port)

    def error_received(self, exc):
        logger.error("UDP error: %s", exc)

    def connection_lost(self, exc):
        if exc:
            logger.error("UDP connection lost: %s", exc)


async def start_syslog_server(
    on_message: Callable[[dict], None],
    host: str = "0.0.0.0",
    port: int = 5514,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> tuple[asyncio.DatagramTransport, SyslogProtocol]:
    """
    Start the UDP syslog server.

    Args:
        on_message: Callback invoked with each parsed log entry dict
        host: Bind address (default 0.0.0.0 for all interfaces)
        port: UDP port to listen on (default 5514; use 514 if running as root)
        loop: Event loop (uses current if None)

    Returns:
        Tuple of (transport, protocol)
    """
    if loop is None:
        loop = asyncio.get_running_loop()

    transport, protocol = await loop.create_datagram_endpoint(
        lambda: SyslogProtocol(on_message),
        local_addr=(host, port),
    )

    logger.info("Syslog UDP server started on %s:%d", host, port)
    return transport, protocol
