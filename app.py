import os
import socket
from datetime import datetime
from functools import wraps
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

import storage

load_dotenv()  # reads variables from a local .env file, if one exists

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin123")
HEALTH_API_KEY = os.environ.get("HEALTH_API_KEY")  # optional, for unauthenticated monitoring access


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
    targets = storage.list_targets()
    return render_template(
        "dashboard.html",
        targets=targets,
        username=session.get("username"),
    )


@app.route("/add", methods=["POST"])
@login_required
def add_target():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    if name and url:
        storage.add_target(name, url)
    return redirect(url_for("dashboard"))


@app.route("/delete/<target_id>", methods=["POST"])
@login_required
def delete_target(target_id):
    storage.delete_target(target_id)
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
            import ssl as ssl_lib

            import pymysql

            query = parse_qs(parsed.query)
            wants_ssl = any(
                query.get(k, [""])[0].upper() in ("REQUIRED", "TRUE", "1", "VERIFY_CA", "VERIFY_IDENTITY")
                for k in ("ssl-mode", "sslmode", "ssl")
            )

            connect_kwargs = dict(
                host=parsed.hostname,
                port=parsed.port or 3306,
                user=parsed.username,
                password=parsed.password,
                database=parsed.path.lstrip("/") or None,
                connect_timeout=timeout,
            )

            if wants_ssl:
                ctx = ssl_lib.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl_lib.CERT_NONE
                connect_kwargs["ssl"] = ctx

            conn = pymysql.connect(**connect_kwargs)
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
    targets = storage.list_targets()
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
    return jsonify({"status": "ok"})


storage.init_storage()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)