"""
Storage for the list of databases DB Pulse watches.

Why this exists as its own module: on Render's free tier, a web service's
local disk is wiped every time the container spins down from inactivity and
wakes back up - not just on redeploys. SQLite on local disk works fine for
local development, but on a free Render instance it will silently lose its
data between requests that are more than ~15 minutes apart.

To get real persistence, set METADATA_DATABASE_URL to a connection string for
any database you already have - Postgres or MongoDB both work, auto-detected
from the URL scheme. This module creates its own table/collection
(`dbpulse_targets`) and never touches your other data. If the env var isn't
set, this falls back to local SQLite, which is fine for local dev but NOT
recommended once deployed.
"""

import os
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

METADATA_DATABASE_URL = os.environ.get("METADATA_DATABASE_URL")
SQLITE_PATH = os.environ.get("STORAGE_DB_PATH", os.path.join(os.path.dirname(__file__), "storage.db"))

_scheme = urlparse(METADATA_DATABASE_URL).scheme.lower() if METADATA_DATABASE_URL else ""

USING_POSTGRES = _scheme.startswith("postgres")
USING_MONGO = _scheme.startswith("mongodb")
BACKEND_NAME = "postgres" if USING_POSTGRES else ("mongodb" if USING_MONGO else "sqlite")

if USING_POSTGRES:
    import psycopg2
    import psycopg2.extras

if USING_MONGO:
    from bson import ObjectId
    from pymongo import MongoClient

    _mongo_client = None

    def _mongo_db():
        global _mongo_client
        if _mongo_client is None:
            _mongo_client = MongoClient(METADATA_DATABASE_URL, serverSelectionTimeoutMS=8000)
        # Use the database named in the URL path if present, otherwise a fixed default.
        db_name = urlparse(METADATA_DATABASE_URL).path.lstrip("/") or "dbpulse"
        return _mongo_client[db_name]


# ---------------------------------------------------------------------------
# Postgres backend
# ---------------------------------------------------------------------------
def _pg_conn():
    return psycopg2.connect(METADATA_DATABASE_URL)


def _pg_init():
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS dbpulse_targets (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


def _pg_list_targets():
    conn = _pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, url, created_at FROM dbpulse_targets ORDER BY created_at DESC")
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _pg_add_target(name, url):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO dbpulse_targets (name, url) VALUES (%s, %s)", (name, url))
        conn.commit()
    finally:
        conn.close()


def _pg_delete_target(target_id):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dbpulse_targets WHERE id = %s", (target_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MongoDB backend
# ---------------------------------------------------------------------------
def _mongo_collection():
    return _mongo_db()["dbpulse_targets"]


def _mongo_init():
    # Nothing to create ahead of time - Mongo creates the collection on first
    # insert. This just verifies the connection works so failures surface at
    # startup instead of on the first click.
    _mongo_db().command("ping")


def _mongo_list_targets():
    docs = _mongo_collection().find().sort("created_at", -1)
    results = []
    for doc in docs:
        created_at = doc["created_at"]
        results.append(
            {
                "id": str(doc["_id"]),
                "name": doc["name"],
                "url": doc["url"],
                "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            }
        )
    return results


def _mongo_add_target(name, url):
    _mongo_collection().insert_one({"name": name, "url": url, "created_at": datetime.now(timezone.utc)})


def _mongo_delete_target(target_id):
    try:
        _mongo_collection().delete_one({"_id": ObjectId(target_id)})
    except Exception:
        pass  # invalid/unknown id - nothing to delete


# ---------------------------------------------------------------------------
# SQLite backend (local development fallback only - NOT persistent on
# Render's free tier)
# ---------------------------------------------------------------------------
def _sqlite_conn():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_init():
    conn = _sqlite_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS db_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _sqlite_list_targets():
    conn = _sqlite_conn()
    try:
        rows = conn.execute("SELECT id, name, url, created_at FROM db_targets ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _sqlite_add_target(name, url):
    conn = _sqlite_conn()
    try:
        conn.execute(
            "INSERT INTO db_targets (name, url, created_at) VALUES (?, ?, ?)",
            (name, url, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _sqlite_delete_target(target_id):
    conn = _sqlite_conn()
    try:
        conn.execute("DELETE FROM db_targets WHERE id = ?", (target_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public interface used by app.py - backend-agnostic
# ---------------------------------------------------------------------------
def init_storage():
    try:
        if USING_POSTGRES:
            _pg_init()
        elif USING_MONGO:
            _mongo_init()
        else:
            _sqlite_init()
    except Exception as exc:
        # Don't let a transient connection hiccup at boot crash the whole app -
        # individual requests will still surface the real error if it persists.
        print(f"[storage] warning: could not verify {BACKEND_NAME} storage at startup: {exc}")


def list_targets():
    if USING_POSTGRES:
        return _pg_list_targets()
    if USING_MONGO:
        return _mongo_list_targets()
    return _sqlite_list_targets()


def add_target(name, url):
    if USING_POSTGRES:
        _pg_add_target(name, url)
    elif USING_MONGO:
        _mongo_add_target(name, url)
    else:
        _sqlite_add_target(name, url)


def delete_target(target_id):
    if USING_POSTGRES:
        _pg_delete_target(target_id)
    elif USING_MONGO:
        _mongo_delete_target(target_id)
    else:
        _sqlite_delete_target(target_id)
