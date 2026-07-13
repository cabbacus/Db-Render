"""
Storage for the list of databases DB Pulse watches - MongoDB only.

Set METADATA_DATABASE_URL to a MongoDB connection string (Atlas SRV URL or
plain mongodb:// URL both work). This module creates its own collection,
`dbpulse_targets`, in that database and never touches anything else there.

This module intentionally has no local-disk fallback: on Render (and most
hosts), a web service's local disk is wiped whenever the app spins down from
inactivity and wakes back up, so anything stored there is not durable.
"""

import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from bson import ObjectId
from pymongo import MongoClient

METADATA_DATABASE_URL = os.environ.get("METADATA_DATABASE_URL")

if not METADATA_DATABASE_URL:
    raise RuntimeError(
        "METADATA_DATABASE_URL is not set. DB Pulse stores its list of watched "
        "databases in MongoDB - set METADATA_DATABASE_URL to a mongodb:// or "
        "mongodb+srv:// connection string, e.g.:\n"
        "  METADATA_DATABASE_URL=mongodb+srv://user:pass@cluster.mongodb.net/dbpulse"
    )

_scheme = urlparse(METADATA_DATABASE_URL).scheme.lower()
if not _scheme.startswith("mongodb"):
    raise RuntimeError(
        f"METADATA_DATABASE_URL must be a MongoDB connection string (mongodb:// or "
        f"mongodb+srv://), got scheme '{_scheme}'."
    )

BACKEND_NAME = "mongodb"

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = MongoClient(METADATA_DATABASE_URL, serverSelectionTimeoutMS=8000)
    return _client


def _get_db():
    # Use the database named in the URL path if present, otherwise a fixed default.
    db_name = urlparse(METADATA_DATABASE_URL).path.lstrip("/") or "dbpulse"
    return _get_client()[db_name]


def _get_collection():
    return _get_db()["dbpulse_targets"]


def init_storage():
    """Verify the connection works. Failure here is logged, not fatal, so a
    transient network hiccup at boot doesn't crash-loop the whole app."""
    try:
        _get_db().command("ping")
    except Exception as exc:
        print(f"[storage] warning: could not verify MongoDB connection at startup: {exc}")


def list_targets():
    docs = _get_collection().find().sort("created_at", -1)
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


def add_target(name, url):
    _get_collection().insert_one({"name": name, "url": url, "created_at": datetime.now(timezone.utc)})


def delete_target(target_id):
    try:
        _get_collection().delete_one({"_id": ObjectId(target_id)})
    except Exception:
        pass  # invalid/unknown id - nothing to delete