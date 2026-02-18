"""
Syslog message parser supporting RFC 3164 (BSD) and RFC 5424 formats.

Also handles raw/plain-text messages from non-syslog sources.
"""

import re
from datetime import datetime
from typing import Optional

# Syslog facility names
FACILITIES = {
    0: "kern", 1: "user", 2: "mail", 3: "daemon",
    4: "auth", 5: "syslog", 6: "lpr", 7: "news",
    8: "uucp", 9: "cron", 10: "authpriv", 11: "ftp",
    12: "ntp", 13: "security", 14: "console", 15: "solaris-cron",
    16: "local0", 17: "local1", 18: "local2", 19: "local3",
    20: "local4", 21: "local5", 22: "local6", 23: "local7",
}

# Syslog severity names
SEVERITIES = {
    0: "Emergency", 1: "Alert", 2: "Critical", 3: "Error",
    4: "Warning", 5: "Notice", 6: "Informational", 7: "Debug",
}

# RFC 3164 pattern: <PRI>TIMESTAMP HOSTNAME APP[PID]: MSG
RFC3164_PATTERN = re.compile(
    r'^<(\d{1,3})>'
    r'(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+'
    r'(\S+)\s+'
    r'(.*)$'
)

# RFC 5424 pattern: <PRI>VERSION TIMESTAMP HOSTNAME APP PROCID MSGID STRUCTURED MSG
RFC5424_PATTERN = re.compile(
    r'^<(\d{1,3})>(\d+)\s+'
    r'(\S+)\s+'
    r'(\S+)\s+'
    r'(\S+)\s+'
    r'(\S+)\s+'
    r'(\S+)\s+'
    r'(?:\[.*?\]|-)\s*'
    r'(.*)$'
)

# Simple priority-only: <PRI>message
SIMPLE_PRI_PATTERN = re.compile(r'^<(\d{1,3})>(.*)$')


def decode_priority(pri: int) -> tuple[int, int]:
    """Decode PRI value into facility and severity."""
    facility = pri >> 3
    severity = pri & 0x07
    return facility, severity


def facility_name(code: int) -> str:
    return FACILITIES.get(code, f"unknown({code})")


def severity_name(code: int) -> str:
    return SEVERITIES.get(code, f"unknown({code})")


def parse_rfc3164_timestamp(ts_str: str) -> Optional[str]:
    """Parse RFC 3164 timestamp (e.g., 'Jan  5 14:30:00') into ISO format."""
    try:
        now = datetime.utcnow()
        parsed = datetime.strptime(ts_str, "%b %d %H:%M:%S")
        parsed = parsed.replace(year=now.year)
        # If the parsed date is in the future by more than a day, assume previous year
        if parsed > now and (parsed - now).days > 1:
            parsed = parsed.replace(year=now.year - 1)
        return parsed.isoformat() + "Z"
    except ValueError:
        return None


def parse_message(data: bytes, source_ip: str, source_port: int) -> dict:
    """
    Parse an incoming UDP message. Tries syslog formats first,
    falls back to treating it as a raw string.
    """
    now = datetime.utcnow().isoformat() + "Z"

    # Try to decode as UTF-8, fall back to latin-1
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        text = data.decode("latin-1").strip()

    if not text:
        text = "(empty message)"

    result = {
        "source_ip": source_ip,
        "source_port": source_port,
        "raw_message": text,
        "timestamp": now,
        "is_syslog": False,
    }

    # Try RFC 5424 first (more specific)
    match = RFC5424_PATTERN.match(text)
    if match:
        pri = int(match.group(1))
        facility, severity = decode_priority(pri)
        ts_str = match.group(3)
        hostname = (match.group(4).strip() or None) if match.group(4) != "-" else None
        app_name = match.group(5) if match.group(5) != "-" else None
        proc_id = match.group(6) if match.group(6) != "-" else None
        msg_id = match.group(7) if match.group(7) != "-" else None
        message = match.group(8)

        # Parse ISO timestamp
        timestamp = now
        if ts_str and ts_str != "-":
            try:
                # Handle various ISO formats
                ts_str_clean = ts_str.replace("Z", "+00:00")
                timestamp = ts_str_clean
            except Exception:
                pass

        result.update({
            "is_syslog": True,
            "priority": pri,
            "facility": facility,
            "severity": severity,
            "timestamp": timestamp,
            "hostname": hostname,
            "app_name": app_name,
            "proc_id": proc_id,
            "msg_id": msg_id,
            "message": message,
        })
        return result

    # Try RFC 3164
    match = RFC3164_PATTERN.match(text)
    if match:
        pri = int(match.group(1))
        facility, severity = decode_priority(pri)
        ts_str = match.group(2)
        hostname = match.group(3).strip()
        remaining = match.group(4)

        timestamp = parse_rfc3164_timestamp(ts_str) or now

        # Try to extract app_name[pid]: message
        app_match = re.match(r'^(\S+?)(?:\[(\d+)\])?:\s*(.*)', remaining, re.DOTALL)
        if app_match:
            app_name = app_match.group(1)
            proc_id = app_match.group(2)
            message = app_match.group(3)
        else:
            app_name = None
            proc_id = None
            message = remaining

        result.update({
            "is_syslog": True,
            "priority": pri,
            "facility": facility,
            "severity": severity,
            "timestamp": timestamp,
            "hostname": hostname,
            "app_name": app_name,
            "proc_id": proc_id,
            "message": message,
        })
        return result

    # Try simple <PRI>message
    match = SIMPLE_PRI_PATTERN.match(text)
    if match:
        pri = int(match.group(1))
        if 0 <= pri <= 191:  # Valid syslog priority range
            facility, severity = decode_priority(pri)
            message = match.group(2).strip()
            result.update({
                "is_syslog": True,
                "priority": pri,
                "facility": facility,
                "severity": severity,
                "message": message or "(empty)",
            })
            return result

    # Raw / plain text message - not syslog
    result["message"] = text
    return result
