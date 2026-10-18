"""SQLite application tracker — dedupe, queue, and audit log.

Every job ever seen lives in `jobs`. `applications` records each apply
attempt. `events` is a running audit trail for debugging and reports.
"""
import datetime as dt
import hashlib
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "tracker.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT UNIQUE,
  source TEXT NOT NULL,
  source_job_id TEXT,
  url TEXT,
  title TEXT,
  company TEXT,
  location TEXT,
  description TEXT,
  posted_date TEXT,
  fetched_at TEXT,
  score INTEGER DEFAULT 0,
  score_reasons TEXT DEFAULT '',
  status TEXT DEFAULT 'discovered',
  status_note TEXT DEFAULT '',
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  note TEXT DEFAULT '',
  screenshot TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  kind TEXT,
  message TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
"""

STATUSES = ("discovered", "queued", "skipped", "applied", "needs_you", "failed")


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def norm_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fingerprint(company: str, title: str, location: str) -> str:
    """Cross-post dedupe key: same role at same company+city on any site = one entry."""
    raw = "|".join(norm_text(x) for x in (company, title, location))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class Tracker:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(db_path))
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_SCHEMA)
        self.con.commit()

    # ---------- jobs ----------
    def upsert_job(self, *, source, source_job_id, url, title, company, location,
                   description, posted_date, score=0, score_reasons="", status=None):
        """Returns (job_id, is_new). Cross-post duplicates get the existing id."""
        fp = fingerprint(company, title, location)
        existing = self.con.execute(
            "SELECT id FROM jobs WHERE fingerprint = ?", (fp,)).fetchone()
        created = now()
        if existing:
            jid = existing["id"]
            self.con.execute(
                """UPDATE jobs SET source_job_id=COALESCE(?,source_job_id),
                       url=COALESCE(?,url), description=COALESCE(?,description),
                       posted_date=COALESCE(?,posted_date),
                       score=COALESCE(NULLIF(?,0),score),
                       score_reasons=CASE WHEN ?<>'' THEN ? ELSE score_reasons END,
                       updated_at=?
                   WHERE id=?""",
                (source_job_id, url, description, posted_date, score, score_reasons,
                 score_reasons, created, jid))
            self.con.commit()
            return jid, False
        cur = self.con.execute(
            """INSERT INTO jobs (fingerprint, source, source_job_id, url, title, company,
                 location, description, posted_date, fetched_at, score, score_reasons,
                 status, status_note, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fp, source, source_job_id, url, title, company, location, description,
             posted_date, created, score, score_reasons,
             status or "discovered", "", created))
        self.con.commit()
        return cur.lastrowid, True

    def set_status(self, job_id: int, status: str, note: str = ""):
        assert status in STATUSES
        self.con.execute(
            "UPDATE jobs SET status=?, status_note=?, updated_at=? WHERE id=?",
            (status, note, now(), job_id))
        self.con.commit()

    def queued(self, source: str | None = None, limit: int | None = None):
        q = "SELECT * FROM jobs WHERE status='queued'"
        args: list = []
        if source:
            q += " AND source=?"
            args.append(source)
        q += " ORDER BY score DESC, fetched_at ASC"
        if limit:
            q += " LIMIT ?"
            args.append(limit)
        return self.con.execute(q, args).fetchall()

    def recent(self, days: int = 1, status: str | None = None):
        since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
        q, args = "SELECT * FROM jobs WHERE updated_at >= ?", [since]
        if status:
            q += " AND status=?"
            args.append(status)
        return self.con.execute(q + " ORDER BY updated_at DESC", args).fetchall()

    # ---------- applications ----------
    def start_application(self, job_id: int) -> int:
        cur = self.con.execute(
            "INSERT INTO applications (job_id, started_at, status) VALUES (?,?,?)",
            (job_id, now(), "running"))
        self.con.commit()
        return cur.lastrowid

    def finish_application(self, app_id: int, status: str, note: str = "",
                           screenshot: str = ""):
        self.con.execute(
            "UPDATE applications SET status=?, note=?, screenshot=?, finished_at=? WHERE id=?",
            (status, note, screenshot, now(), app_id))
        self.con.commit()

    def applied_today(self, source: str | None = None) -> int:
        today = dt.date.today().isoformat()
        q = """SELECT COUNT(*) c FROM applications
               WHERE status='applied' AND finished_at LIKE ?"""
        args: list = [f"{today}%"]
        if source:
            q += " AND job_id IN (SELECT id FROM jobs WHERE source=?)"
            args.append(source)
        return self.con.execute(q, args).fetchone()["c"]

    # ---------- events ----------
    def log_event(self, kind: str, message: str):
        self.con.execute("INSERT INTO events (ts, kind, message) VALUES (?,?,?)",
                         (now(), kind, message))
        self.con.commit()

    # ---------- reports ----------
    def stats(self) -> dict:
        rows = self.con.execute(
            "SELECT source, status, COUNT(*) c FROM jobs GROUP BY source, status").fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(r["source"], {})[r["status"]] = r["c"]
        return out

    def close(self):
        self.con.close()
