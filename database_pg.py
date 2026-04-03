"""
PostgreSQL database layer for FetchLog.

Drop-in replacement for the SQLite LogDatabase class, using PostgreSQL
with the 'fetchlog' schema.
"""

import json
import threading
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras


DEFAULT_CONFIG_PATH = "db_config.json"


def load_pg_config(config: dict) -> dict:
    """Extract PostgreSQL connection settings from a config dict."""
    return {
        "host": config.get("host", "localhost"),
        "port": config.get("port", 5432),
        "dbname": config.get("dbname", "fetchlog"),
        "user": config.get("user", "fetchlog"),
        "password": config.get("password", ""),
        "schema": config.get("schema", "fetchlog"),
    }


class LogDatabase:
    def __init__(self, config: dict):
        self.pg_config = load_pg_config(config)
        self.schema = self.pg_config["schema"]
        self._local = threading.local()
        self._init_db()

    def _get_conn(self):
        if not hasattr(self._local, "conn") or self._local.conn is None or self._local.conn.closed:
            conn = psycopg2.connect(
                host=self.pg_config["host"],
                port=self.pg_config["port"],
                dbname=self.pg_config["dbname"],
                user=self.pg_config["user"],
                password=self.pg_config["password"],
            )
            conn.autocommit = False
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        cur = conn.cursor()
        s = self.schema
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
        cur.execute(f"SET search_path TO {s}")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {s}.log_entries (
                id BIGSERIAL PRIMARY KEY,
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
            )
        """)
        # Create indexes
        for idx_name, col in [
            ("idx_timestamp", "timestamp"),
            ("idx_received_at", "received_at"),
            ("idx_source_ip", "source_ip"),
            ("idx_hostname", "hostname"),
            ("idx_severity", "severity"),
            ("idx_is_marker", "is_marker"),
        ]:
            cur.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {s}.log_entries({col})")

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {s}.known_hosts (
                ip TEXT PRIMARY KEY,
                hostname TEXT,
                display_name TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                message_count INTEGER DEFAULT 0
            )
        """)
        conn.commit()

    def _set_search_path(self, cur):
        cur.execute(f"SET search_path TO {self.schema}")

    def insert_log(self, entry: dict) -> int:
        conn = self._get_conn()
        cur = conn.cursor()
        self._set_search_path(cur)
        now = datetime.utcnow().isoformat() + "Z"
        cur.execute(f"""
            INSERT INTO {self.schema}.log_entries
                (timestamp, received_at, source_ip, source_port, hostname,
                 facility, severity, priority, app_name, proc_id, msg_id,
                 message, raw_message, is_syslog, is_marker, marker_style)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            entry.get("timestamp", now),
            now,
            entry.get("source_ip", "unknown"),
            entry.get("source_port"),
            (entry.get("hostname") or "").strip() or None,
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
        row_id = cur.fetchone()[0]
        conn.commit()

        # Update known_hosts
        ip = entry.get("source_ip", "unknown")
        hostname = (entry.get("hostname") or "").strip() or None
        if ip != "marker" and ip != "unknown":
            cur = conn.cursor()
            self._set_search_path(cur)
            cur.execute(f"""
                INSERT INTO {self.schema}.known_hosts (ip, hostname, display_name, first_seen, last_seen, message_count)
                VALUES (%s, %s, %s, %s, %s, 1)
                ON CONFLICT(ip) DO UPDATE SET
                    hostname = COALESCE(EXCLUDED.hostname, {self.schema}.known_hosts.hostname),
                    last_seen = EXCLUDED.last_seen,
                    message_count = {self.schema}.known_hosts.message_count + 1
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

    def _build_where(self, source_ip, hostname, severity, search,
                     start_time, end_time, include_markers):
        conditions = []
        params = []

        if source_ip:
            conditions.append("source_ip = %s")
            params.append(source_ip)
        if hostname:
            conditions.append(f"(hostname LIKE %s OR source_ip IN "
                              f"(SELECT ip FROM {self.schema}.known_hosts WHERE display_name LIKE %s))")
            params.extend([f"%{hostname}%", f"%{hostname}%"])
        if severity is not None:
            conditions.append("severity <= %s")
            params.append(severity)
        if search:
            conditions.append("message LIKE %s")
            params.append(f"%{search}%")
        if start_time:
            conditions.append("timestamp >= %s")
            params.append(start_time)
        if end_time:
            conditions.append("timestamp <= %s")
            params.append(end_time)
        if not include_markers:
            conditions.append("is_marker = 0")

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)
        return where, params

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
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self._set_search_path(cur)

        where, params = self._build_where(
            source_ip, hostname, severity, search,
            start_time, end_time, include_markers)

        allowed_sort = {"received_at", "timestamp", "severity", "source_ip", "hostname"}
        if sort_by not in allowed_sort:
            sort_by = "received_at"
        if sort_order.upper() not in ("ASC", "DESC"):
            sort_order = "DESC"

        query = f"""
            SELECT * FROM {self.schema}.log_entries
            {where}
            ORDER BY {sort_by} {sort_order}, id {sort_order}
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])

        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]

    def count_logs(self, source_ip: Optional[str] = None,
                   hostname: Optional[str] = None,
                   severity: Optional[int] = None,
                   search: Optional[str] = None,
                   start_time: Optional[str] = None,
                   end_time: Optional[str] = None,
                   include_markers: bool = True) -> int:
        conn = self._get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self._set_search_path(cur)

        where, params = self._build_where(
            source_ip, hostname, severity, search,
            start_time, end_time, include_markers)

        cur.execute(f"SELECT COUNT(*) as cnt FROM {self.schema}.log_entries {where}", params)
        return cur.fetchone()["cnt"]

    def get_known_hosts(self) -> list[dict]:
        conn = self._get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self._set_search_path(cur)
        cur.execute(f"SELECT * FROM {self.schema}.known_hosts ORDER BY last_seen DESC")
        return [dict(r) for r in cur.fetchall()]

    def update_host_display_name(self, ip: str, display_name: str):
        conn = self._get_conn()
        cur = conn.cursor()
        self._set_search_path(cur)
        cur.execute(
            f"UPDATE {self.schema}.known_hosts SET display_name = %s WHERE ip = %s",
            (display_name, ip)
        )
        conn.commit()

    def get_latest_id(self) -> int:
        conn = self._get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self._set_search_path(cur)
        cur.execute(f"SELECT MAX(id) as max_id FROM {self.schema}.log_entries")
        row = cur.fetchone()
        return row["max_id"] or 0

    def get_entries_after(self, after_id: int, limit: int = 100) -> list[dict]:
        conn = self._get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self._set_search_path(cur)
        cur.execute(
            f"SELECT * FROM {self.schema}.log_entries WHERE id > %s ORDER BY id ASC LIMIT %s",
            (after_id, limit)
        )
        return [dict(r) for r in cur.fetchall()]
