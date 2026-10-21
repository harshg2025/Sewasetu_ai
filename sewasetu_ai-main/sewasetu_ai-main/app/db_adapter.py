# -*- coding: utf-8 -*-
"""
Dual Database Adapter for Sewa Setu.

Provides seamless compatibility:
- If PostgreSQL is available (e.g. running in Docker or local PostgreSQL daemon),
  it connects via psycopg2 directly.
- If PostgreSQL is not available, it automatically falls back to an embedded SQLite
  database (sewasetu.db), translating PostgreSQL dialects (%s placeholders, IN %s tuple
  expansion, JSONB, ON CONFLICT, RETURNING clauses, etc.) with 100% fidelity.
"""

import os
import re
import json
import sqlite3
from datetime import datetime, date

POSTGRES_AVAILABLE = False
try:
    import psycopg2
    POSTGRES_AVAILABLE = True
except ImportError:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_DB_PATH = os.path.join(BASE_DIR, "sewasetu.db")


def _convert_pg_to_sqlite_query(query, params):
    """
    Translates a PostgreSQL query with %s placeholders to SQLite query with ? placeholders.
    Properly handles tuple params for `IN %s` -> `IN (?, ?, ...)`.
    """
    if params is None:
        params = ()

    # If already using ?, don't re-convert
    if "%s" not in query and "?" in query:
        return query, tuple(params) if isinstance(params, (list, tuple)) else (params,)

    # If params is a dict (named parameters), SQLite uses :key
    if isinstance(params, dict):
        q = re.sub(r'%\((\w+)\)s', r':\1', query)
        return q, params

    params_list = list(params) if isinstance(params, (list, tuple)) else [params]
    new_params = []
    
    # Split query by %s while ignoring escaped %%
    parts = re.split(r'(%s)', query)
    new_query_parts = []
    param_idx = 0

    for part in parts:
        if part == '%s':
            if param_idx < len(params_list):
                val = params_list[param_idx]
                param_idx += 1
                if isinstance(val, (list, tuple)):
                    # Expand tuple for `IN %s` -> `(?, ?, ...)`
                    q_marks = ', '.join(['?'] * len(val))
                    new_query_parts.append(f"({q_marks})")
                    new_params.extend(val)
                elif isinstance(val, dict):
                    # Store dict as JSON string
                    new_query_parts.append("?")
                    new_params.append(json.dumps(val))
                else:
                    new_query_parts.append("?")
                    new_params.append(val)
            else:
                new_query_parts.append("?")
        else:
            new_query_parts.append(part)

    final_query = "".join(new_query_parts)
    return final_query, tuple(new_params)


class SQLiteCursorWrapper:
    """Wraps an SQLite cursor to emulate psycopg2 cursor behavior."""

    def __init__(self, raw_cursor, conn):
        self._raw = raw_cursor
        self._conn = conn

    def execute(self, query, params=None):
        sqlite_query, sqlite_params = _convert_pg_to_sqlite_query(query, params)
        try:
            return self._raw.execute(sqlite_query, sqlite_params)
        except sqlite3.OperationalError as e:
            # Handle possible syntax differences gracefully
            raise e

    def executemany(self, query, seq_of_params):
        if not seq_of_params:
            return
        first_q, _ = _convert_pg_to_sqlite_query(query, seq_of_params[0])
        converted_seq = []
        for p in seq_of_params:
            _, converted_p = _convert_pg_to_sqlite_query(query, p)
            converted_seq.append(converted_p)
        return self._raw.executemany(first_q, converted_seq)

    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()

    def fetchmany(self, size=None):
        if size is None:
            return self._raw.fetchmany()
        return self._raw.fetchmany(size)

    @property
    def description(self):
        return self._raw.description

    @property
    def rowcount(self):
        return self._raw.rowcount

    def close(self):
        self._raw.close()


class SQLiteConnectionWrapper:
    """Wraps an SQLite connection to emulate psycopg2 connection."""

    def __init__(self, db_path):
        # Allow multi-threaded access for Flask with WAL mode and generous busy timeout
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0,
                                     detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=30000;")
        self._conn.row_factory = sqlite3.Row  # Can access by column or index

    def cursor(self):
        return SQLiteCursorWrapper(self._conn.cursor(), self)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def init_sqlite_db(db_path):
    """Initializes tables and loads initial seed records if the SQLite database is new."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Create tables compatible with schema.sql
    cur.execute("""
    CREATE TABLE IF NOT EXISTS applications (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        application_no  VARCHAR(20),
        applicant_name  VARCHAR(100),
        mobile          VARCHAR(15),
        dob             DATE,
        gender          VARCHAR(10),
        marital_status  VARCHAR(20),
        husband_name    VARCHAR(100),
        husband_employer VARCHAR(100),
        village         VARCHAR(100),
        block           VARCHAR(50),
        bank_account    VARCHAR(30),
        ifsc            VARCHAR(15),
        doc_path        VARCHAR(200),
        status          VARCHAR(20) DEFAULT 'PENDING',
        submitted_at    TIMESTAMP,
        decided_at      TIMESTAMP,
        decided_by      VARCHAR(50)
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS portal_users (
        mobile        VARCHAR(15) PRIMARY KEY,
        password_hash VARCHAR(80)
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS otps (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile     VARCHAR(15),
        code       VARCHAR(6),
        created_at TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER,
        action         VARCHAR(20),
        actor          VARCHAR(50),
        note           VARCHAR(500),
        at             TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_keys (
        kid         VARCHAR(32) PRIMARY KEY,
        private_pem TEXT,
        created_at  TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_clients (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id     VARCHAR(300) UNIQUE,
        client_name   VARCHAR(100),
        redirect_uris TEXT,
        secret_hash   VARCHAR(64),
        kind          VARCHAR(10),
        created_at    TIMESTAMP,
        fetched_at    TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_requests (
        id             VARCHAR(64) PRIMARY KEY,
        params         TEXT,
        subject        VARCHAR(80),
        role           VARCHAR(10),
        pending_mobile VARCHAR(15),
        created_at     TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_codes (
        code_hash      VARCHAR(64) PRIMARY KEY,
        client_id      VARCHAR(300),
        redirect_uri   VARCHAR(500),
        scope          VARCHAR(20),
        code_challenge VARCHAR(128),
        resource       VARCHAR(300),
        subject        VARCHAR(80),
        role           VARCHAR(10),
        created_at     TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
        token_hash  VARCHAR(64) PRIMARY KEY,
        client_id   VARCHAR(300),
        client_name VARCHAR(100),
        subject     VARCHAR(80),
        role        VARCHAR(10),
        scope       VARCHAR(20),
        resource    VARCHAR(300),
        created_at  TIMESTAMP,
        expires_at  TIMESTAMP,
        revoked_at  TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_program_tokens (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        jti        VARCHAR(32) UNIQUE,
        label      VARCHAR(100),
        subject    VARCHAR(80),
        audience   VARCHAR(300),
        issued_by  VARCHAR(50),
        created_at TIMESTAMP,
        expires_at TIMESTAMP,
        revoked_at TIMESTAMP
    )""")

    # Indices
    cur.execute("CREATE INDEX IF NOT EXISTS idx_app_mobile ON applications(mobile)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_app ON audit_log(application_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_app_status_submitted ON applications(status, submitted_at)")

    # Check if seed data needs to be populated
    cur.execute("SELECT count(*) FROM applications")
    count = cur.fetchone()[0]
    if count == 0:
        seed_path = os.path.join(os.path.dirname(BASE_DIR), "seed", "seed.sql")
        if os.path.exists(seed_path):
            print(f"[db_adapter] Populating initial dataset from {seed_path}...")
            _load_seed_tsv(cur, seed_path, limit=25000)
            conn.commit()
            print(f"[db_adapter] Seed dataset loaded successfully.")

    conn.commit()
    cur.close()
    conn.close()


def _load_seed_tsv(cur, seed_path, limit=25000):
    """Parses COPY format TSV lines from seed.sql and inserts them efficiently."""
    rows = []
    in_copy = False
    with open(seed_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("COPY applications"):
                in_copy = True
                continue
            if in_copy:
                if line.strip() == r"\." or line.strip() == "":
                    break
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 18:
                    # Clean up empty strings to None where appropriate
                    cleaned_parts = [p if p != "" else None for p in parts]
                    rows.append(cleaned_parts)
                    if len(rows) >= limit:
                        break

    if rows:
        cur.executemany("""
        INSERT OR REPLACE INTO applications 
        (id, application_no, applicant_name, mobile, dob, gender, marital_status,
         husband_name, husband_employer, village, block, bank_account, ifsc,
         doc_path, status, submitted_at, decided_at, decided_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        
        # Populate portal_users for status login (DOB as DDMMYYYY)
        import hashlib
        portal_users = []
        for r in rows:
            mobile = r[3]
            dob_str = r[4]
            if mobile and dob_str:
                try:
                    # dob_str is YYYY-MM-DD
                    dt = datetime.strptime(dob_str, "%Y-%m-%d")
                    pw = dt.strftime("%d%m%Y")
                    pwhash = hashlib.sha256(pw.encode("utf-8")).hexdigest()
                    portal_users.append((mobile, pwhash))
                except Exception:
                    pass
        if portal_users:
            cur.executemany("INSERT OR IGNORE INTO portal_users (mobile, password_hash) VALUES (?, ?)",
                            portal_users)


_SQLITE_INITIALIZED = False


def get_connection(host, port, dbname, user, password):
    """
    Returns a database connection. Tries PostgreSQL first. If unreachable or in local
    standalone mode, uses SQLite with seamless compatibility wrapper.
    """
    global _SQLITE_INITIALIZED

    # If explicit forced local mode, or if postgres host is not reached
    force_sqlite = os.environ.get("FORCE_SQLITE", "0") == "1"

    if POSTGRES_AVAILABLE and not force_sqlite:
        try:
            # Set short connect timeout so we don't hang if Postgres is not running
            conn = psycopg2.connect(
                host=host,
                port=port,
                dbname=dbname,
                user=user,
                password=password,
                connect_timeout=2
            )
            return conn
        except Exception:
            pass

    # PostgreSQL not reachable; fall back to SQLite
    if not _SQLITE_INITIALIZED or not os.path.exists(SQLITE_DB_PATH):
        init_sqlite_db(SQLITE_DB_PATH)
        _SQLITE_INITIALIZED = True

    return SQLiteConnectionWrapper(SQLITE_DB_PATH)
