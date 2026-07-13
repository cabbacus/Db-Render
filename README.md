# DB Pulse

A tiny app with:
- A login page
- A dashboard where you add any number of database URLs
- A **"Check database health"** button that pings each one live
- One JSON endpoint, `GET /api/health`, that always reflects the current, dynamic status of every database you've added

Supports Postgres, MySQL, MongoDB, and Redis URLs out of the box, plus a generic TCP reachability check for anything else (e.g. `tcp://host:port`).

## Run it locally

```bash
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your own values
python app.py
```

`.env` is loaded automatically on startup (via `python-dotenv`) — no need to `export` anything by hand. It's already in `.gitignore` so it won't get committed.

Visit `http://localhost:5000`, log in, and start adding database URLs, e.g.:

```
postgresql://user:password@host:5432/dbname
mysql://user:password@host:3306/dbname
mongodb://user:password@host:27017/dbname
redis://:password@host:6379/0
```

Click **Check database health** to see live status. The same data is available anytime at `/api/health`.

## Deploy to Render

1. Push this folder to a GitHub repo.
2. In Render: **New → Blueprint**, point it at the repo (it will read `render.yaml` automatically).
   - Or **New → Web Service** manually with:
     - Build command: `pip install -r requirements.txt`
     - Start command: `gunicorn app:app`
3. Set these environment variables in the Render dashboard (`.env` is only for local development — Render doesn't read that file, it uses its own env var settings):
   | Variable | Purpose |
   |---|---|
   | `SECRET_KEY` | Random string for session security (Render can auto-generate) |
   | `APP_USERNAME` | Login username |
   | `APP_PASSWORD` | Login password |
   | `HEALTH_API_KEY` | *(optional)* lets external monitors call `/api/health?key=...` without logging in |
4. Deploy. Your login page will be at `https://<your-app>.onrender.com/login`.

## Notes

- The list of database URLs is stored in a local SQLite file (`storage.db`). On Render's free tier, disk is **ephemeral** — it resets on redeploys. If you need the list to survive redeploys, attach a [Render Disk](https://render.com/docs/disks) and set `STORAGE_DB_PATH` to a path on that disk.
- Passwords in database URLs are masked (`****`) before being shown or returned by the API.
- `/api/health` requires either an active login session or a matching `HEALTH_API_KEY` header/query param — it's never fully open unless you explicitly set that key and share it.
