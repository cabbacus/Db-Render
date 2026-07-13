import os
import socket
import sqlite3
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for

load_dotenv()  # reads variables from a local .env file, if one exists

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin123")
HEALTH_API_KEY = os.environ.get("HEALTH_API_KEY")  # optional, for unauthenticated monitoring access

DB_PATH = os.environ.get("STORAGE_DB_PATH", os.path.join(os.path.dirname(__file__), "storage.db"))


# ---------------------------------------------------------------------------
# Storage (SQLite) - holds the list of database URLs the user wants to watch
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.close()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == APP_USERNAME and password == APP_PASSWORD:
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("dashboard"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard - add / remove database URLs
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    db = get_db()
    targets = db.execute("SELECT * FROM db_targets ORDER BY created_at DESC").fetchall()
    return render_template("dashboard.html", targets=targets, username=session.get("username"))


@app.route("/add", methods=["POST"])
@login_required
def add_target():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    if name and url:
        db = get_db()
        db.execute(
            "INSERT INTO db_targets (name, url, created_at) VALUES (?, ?, ?)",
            (name, url, datetime.utcnow().isoformat()),
        )
        db.commit()
    return redirect(url_for("dashboard"))


@app.route("/delete/<int:target_id>", methods=["POST"])
@login_required
def delete_target(target_id):
    db = get_db()
    db.execute("DELETE FROM db_targets WHERE id = ?", (target_id,))
    db.commit()
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Health check logic - supports postgres, mysql, mongodb, redis, and a
# generic TCP fallback for anything else.
# ---------------------------------------------------------------------------
def check_single_db(url, timeout=5):
    scheme = urlparse(url).scheme.lower()
    parsed = urlparse(url)

    try:
        if scheme.startswith("postgres"):
            import psycopg2

            conn = psycopg2.connect(url, connect_timeout=timeout)
            conn.close()

        elif scheme.startswith("mysql"):
            import pymysql

            conn = pymysql.connect(
                host=parsed.hostname,
                port=parsed.port or 3306,
                user=parsed.username,
                password=parsed.password,
                database=parsed.path.lstrip("/") or None,
                connect_timeout=timeout,
            )
            conn.close()

        elif scheme.startswith("mongodb"):
            import pymongo

            client = pymongo.MongoClient(url, serverSelectionTimeoutMS=timeout * 1000)
            client.admin.command("ping")
            client.close()

        elif scheme.startswith("redis"):
            import redis

            r = redis.from_url(url, socket_connect_timeout=timeout)
            r.ping()

        else:
            # Generic fallback: just check the host:port is reachable over TCP.
            host = parsed.hostname
            port = parsed.port
            if not host or not port:
                return "unknown", "Could not determine host/port from URL"
            with socket.create_connection((host, port), timeout=timeout):
                pass

        return "running", None

    except Exception as exc:
        return "stopped", str(exc)


def run_health_checks():
    db = get_db()
    targets = db.execute("SELECT * FROM db_targets ORDER BY created_at DESC").fetchall()
    results = []
    for t in targets:
        status, error = check_single_db(t["url"])
        results.append(
            {
                "id": t["id"],
                "name": t["name"],
                "url": mask_credentials(t["url"]),
                "status": status,
                "error": error,
                "checked_at": datetime.utcnow().isoformat() + "Z",
            }
        )
    return results


def mask_credentials(url):
    """Hide password when displaying/returning the URL."""
    parsed = urlparse(url)
    if parsed.password:
        netloc = parsed.netloc.replace(parsed.password, "****")
        return parsed._replace(netloc=netloc).geturl()
    return url


# ---------------------------------------------------------------------------
# Public-ish API: GET /api/health
# Accessible either via an active login session, or via X-API-KEY header
# (or ?key=) if HEALTH_API_KEY is configured - handy for uptime monitors.
# ---------------------------------------------------------------------------
@app.route("/api/health", methods=["GET"])
def api_health():
    authorized = bool(session.get("logged_in"))

    if not authorized and HEALTH_API_KEY:
        supplied = request.headers.get("X-API-KEY") or request.args.get("key")
        authorized = supplied == HEALTH_API_KEY

    if not authorized:
        return jsonify({"error": "Unauthorized"}), 401

    results = run_health_checks()
    overall = "running" if all(r["status"] == "running" for r in results) else (
        "stopped" if results else "no_targets"
    )
    return jsonify(
        {
            "overall_status": overall,
            "checked_at": datetime.utcnow().isoformat() + "Z",
            "total": len(results),
            "databases": results,
        }
    )


@app.route("/healthz")
def healthz():
    # Simple liveness probe for the app itself (used by Render, not the DBs)
    return jsonify({"status": "ok"})


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
