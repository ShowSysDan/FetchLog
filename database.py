"""
SQLite database layer for EverythingLogger.

Uses WAL mode for high-throughput concurrent writes from many devices.
"""

import sqlite3
import threading
import time
from datetime import datetime
from typing import Optional


class LogDatabase:
    def __init__(self, db_path: str = "logs.db"):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS log_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                received_at TEXT NOT NULL,
                source_ip TEXT NOT NULL,
                source_port INTEGER,
                hostname TEXT,
                facility INTEGER,
                severity INTEGER,
                priority INTEGER,
                app_name TEXT,
                proc_id TEXT,
                msg_id TEXT,
                message TEXT NOT NULL,
                raw_message TEXT NOT NULL,
                is_syslog INTEGER NOT NULL DEFAULT 0,
                is_marker INTEGER NOT NULL DEFAULT 0,
                marker_style TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_timestamp ON log_entries(timestamp);
            CREATE INDEX IF NOT EXISTS idx_received_at ON log_entries(received_at);
            CREATE INDEX IF NOT EXISTS idx_source_ip ON log_entries(source_ip);
            CREATE INDEX IF NOT EXISTS idx_hostname ON log_entries(hostname);
            CREATE INDEX IF NOT EXISTS idx_severity ON log_entries(severity);
            CREATE INDEX IF NOT EXISTS idx_is_marker ON log_entries(is_marker);

            CREATE TABLE IF NOT EXISTS known_hosts (
                ip TEXT PRIMARY KEY,
                hostname TEXT,
                display_name TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                message_count INTEGER DEFAULT 0
            );
        """)
        conn.commit()

    def insert_log(self, entry: dict) -> int:
        conn = self._get_conn()
        now = datetime.utcnow().isoformat() + "Z"
        cur = conn.execute("""
            INSERT INTO log_entries
                (timestamp, received_at, source_ip, source_port, hostname,
                 facility, severity, priority, app_name, proc_id, msg_id,
                 message, raw_message, is_syslog, is_marker, marker_style)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.get("timestamp", now),
            now,
            entry.get("source_ip", "unknown"),
            entry.get("source_port"),
            entry.get("hostname"),
            entry.get("facility"),
            entry.get("severity"),
            entry.get("priority"),
            entry.get("app_name"),
            entry.get("proc_id"),
            entry.get("msg_id"),
            entry.get("message", ""),
            entry.get("raw_message", ""),
            1 if entry.get("is_syslog") else 0,
            1 if entry.get("is_marker") else 0,
            entry.get("marker_style"),
        ))
        conn.commit()
        row_id = cur.lastrowid

        # Update known_hosts
        ip = entry.get("source_ip", "unknown")
        hostname = entry.get("hostname")
        if ip != "marker" and ip != "unknown":
            conn.execute("""
                INSERT INTO known_hosts (ip, hostname, display_name, first_seen, last_seen, message_count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(ip) DO UPDATE SET
                    hostname = COALESCE(excluded.hostname, known_hosts.hostname),
                    last_seen = excluded.last_seen,
                    message_count = known_hosts.message_count + 1
            """, (ip, hostname, hostname, now, now))
            conn.commit()

        return row_id

    def insert_marker(self, label: str, timestamp: Optional[str] = None,
                      style: str = "default") -> int:
        now = datetime.utcnow().isoformat() + "Z"
        entry = {
            "timestamp": timestamp or now,
            "source_ip": "marker",
            "hostname": "MARKER",
            "message": label,
            "raw_message": f"[MARKER] {label}",
            "is_marker": True,
            "marker_style": style,
        }
        return self.insert_log(entry)

    def query_logs(self, limit: int = 200, offset: int = 0,
                   source_ip: Optional[str] = None,
                   hostname: Optional[str] = None,
                   severity: Optional[int] = None,
                   search: Optional[str] = None,
                   start_time: Optional[str] = None,
                   end_time: Optional[str] = None,
                   sort_by: str = "received_at",
                   sort_order: str = "DESC",
                   include_markers: bool = True) -> list[dict]:
        conn = self._get_conn()
        conditions = []
        params = []

        if source_ip:
            conditions.append("source_ip = ?")
            params.append(source_ip)
        if hostname:
            conditions.append("(hostname LIKE ? OR source_ip IN "
                              "(SELECT ip FROM known_hosts WHERE display_name LIKE ?))")
            params.extend([f"%{hostname}%", f"%{hostname}%"])
        if severity is not None:
            conditions.append("severity <= ?")
            params.append(severity)
        if search:
            conditions.append("message LIKE ?")
            params.append(f"%{search}%")
        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)
        if not include_markers:
            conditions.append("is_marker = 0")

        allowed_sort = {"received_at", "timestamp", "severity", "source_ip", "hostname"}
        if sort_by not in allowed_sort:
            sort_by = "received_at"
        if sort_order.upper() not in ("ASC", "DESC"):
            sort_order = "DESC"

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        query = f"""
            SELECT * FROM log_entries
            {where}
            ORDER BY {sort_by} {sort_order}, id {sort_order}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def count_logs(self, source_ip: Optional[str] = None,
                   hostname: Optional[str] = None,
                   severity: Optional[int] = None,
                   search: Optional[str] = None,
                   start_time: Optional[str] = None,
                   end_time: Optional[str] = None,
                   include_markers: bool = True) -> int:
        conn = self._get_conn()
        conditions = []
        params = []

        if source_ip:
            conditions.append("source_ip = ?")
            params.append(source_ip)
        if hostname:
            conditions.append("(hostname LIKE ? OR source_ip IN "
                              "(SELECT ip FROM known_hosts WHERE display_name LIKE ?))")
            params.extend([f"%{hostname}%", f"%{hostname}%"])
        if severity is not None:
            conditions.append("severity <= ?")
            params.append(severity)
        if search:
            conditions.append("message LIKE ?")
            params.append(f"%{search}%")
        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)
        if not include_markers:
            conditions.append("is_marker = 0")

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        row = conn.execute(f"SELECT COUNT(*) as cnt FROM log_entries {where}", params).fetchone()
        return row["cnt"]

    def get_known_hosts(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM known_hosts ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_host_display_name(self, ip: str, display_name: str):
        conn = self._get_conn()
        conn.execute(
            "UPDATE known_hosts SET display_name = ? WHERE ip = ?",
            (display_name, ip)
        )
        conn.commit()

    def get_latest_id(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT MAX(id) as max_id FROM log_entries").fetchone()
        return row["max_id"] or 0

    def get_entries_after(self, after_id: int, limit: int = 100) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM log_entries WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]
