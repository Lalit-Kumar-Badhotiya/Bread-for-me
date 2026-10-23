"""SQLite application tracker — dedupe, queue, and audit log.

Every job ever seen lives in `jobs`. `applications` records each apply
attempt. `events` is a running audit trail for debugging and reports.
"""
import datetime as dt
import difflib
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
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at);
"""

STATUSES = ("discovered", "queued", "skipped", "applied", "needs_you", "failed")

# Re-fetching a search page re-surfaces jobs already handled. These two must
# never be re-queued by a fetch: applied = never apply twice, needs_you = the
# human's turn (not the bot's).
PROTECTED_STATUSES = ("applied", "needs_you")


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
    def _fuzzy_id(self, company: str, title: str, location: str, days: int = 90):
        """Cross-site tolerance pass: per-site parses differ slightly
        (punctuation, extra words) but it is the SAME job. Merge those.

        Bounded for speed: only rows updated in the last `days` days are
        compared (a cross-post twin is always re-fetched around the same
        time), and length-mismatched titles are skipped before the expensive
        similarity check (`ratio >= 0.90` is impossible beyond ~11% length
        difference).
        """
        nt, nc = norm_text(title), norm_text(company)
        nloc = norm_text(location)
        loc_tok = nloc.split()[0] if nloc.split() else ""
        if not nt:
            return None
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
        for r in self.con.execute(
                "SELECT id,title,company,location FROM jobs WHERE updated_at >= ?",
                (cutoff,)).fetchall():
            rt, rc = norm_text(r["title"]), norm_text(r["company"])
            if rt and abs(len(rt) - len(nt)) > max(3, len(nt) // 4):
                continue
            same_t = rt == nt \
                or difflib.SequenceMatcher(None, rt, nt).ratio() >= 0.90
            if not same_t:
                continue
            same_c = bool(
                nc and rc
                and (rc == nc or (nc.split() and rc.split()
                                  and nc.split()[0] == rc.split()[0])))
            if not same_c:
                continue
            tl = norm_text(r["location"])
            r_tok = tl.split()[0] if tl.split() else ""
            if not loc_tok or not r_tok or loc_tok == r_tok:
                return r["id"]
        return None

    def _write_update(self, jid, *, source_job_id, url, description, posted_date,
                      score, score_reasons):
        """Non-destructive update: never overwrite a good value with a worse
        or empty one (harvest variance can blank posted_date/description)."""
        self.con.execute(
            """UPDATE jobs SET
                   source_job_id=COALESCE(?,source_job_id),
                   url=COALESCE(?,url),
                   description=CASE WHEN ?<>'' THEN ? ELSE description END,
                   posted_date=CASE WHEN ?<>'' THEN ? ELSE posted_date END,
                   score=COALESCE(NULLIF(?,0),score),
                   score_reasons=CASE WHEN ?<>'' THEN ? ELSE score_reasons END,
                   updated_at=?
               WHERE id=?""",
            (source_job_id, url, description, description,
             posted_date, posted_date, score, score_reasons, score_reasons,
             now(), jid))
        self.con.commit()

    def upsert_job(self, *, source, source_job_id, url, title, company, location,
                   description, posted_date, score=0, score_reasons="", status=None):
        """Returns (job_id, is_new). Cross-post duplicates get the existing id."""
        fp = fingerprint(company, title, location)
        created = now()
        existing = self.con.execute(
            "SELECT id FROM jobs WHERE fingerprint = ?", (fp,)).fetchone()
        if existing:
            jid = existing["id"]
        else:
            fuzzy = self._fuzzy_id(company, title, location)
            if fuzzy:
                jid, existing = fuzzy, True
        if existing:
            self._write_update(jid, source_job_id=source_job_id, url=url,
                               description=description, posted_date=posted_date,
                               score=score, score_reasons=score_reasons)
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

    def reclassify(self, job_id: int, status: str, note: str = "") -> str | None:
        """Apply a fetch-time re-classification to an existing row, except for
        protected statuses (applied/needs_you), which a fetch must never
        resurrect into the queue — and except apply-time detail-gate skips
        (their note says "... detail page ..."), which a re-fetch must not
        undo or the same rejected job would eat an apply slot every run.

        Returns the PREVIOUS status when written, or None when protected.
        """
        row = self.con.execute(
            "SELECT status, status_note FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is not None:
            if row["status"] in PROTECTED_STATUSES:
                return None
            if (row["status"] == "skipped" and status == "queued"
                    and "detail page" in (row["status_note"] or "")):
                return None
        prior = row["status"] if row is not None else "discovered"
        self.set_status(job_id, status, note)
        return prior

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

    def retry_rows(self, source: str, *, reason: str | None = None,
                   ids: list[int] | None = None,
                   exclude: tuple[str, ...] = ("external apply flow",),
                   include_external: bool = False) -> list:
        """Requeue parked rows (needs_you/failed) for another attempt.

        Safeguards: never touches applied/skipped rows; without explicit ids
        it skips handoff rows ("external apply flow") unless the caller asks
        for them — external_autofill can now chase those, pass
        include_external=True (CLI: retry <site> --include-external)."""
        rows = self.con.execute(
            "SELECT id, title, status_note FROM jobs WHERE source=? "
            "AND status IN ('needs_you','failed')", (source,)).fetchall()
        picked = []
        for r in rows:
            note = (r["status_note"] or "")
            if ids and r["id"] not in ids:
                continue
            if reason and reason.lower() not in note.lower():
                continue
            if not ids and not include_external:
                if any(e.lower() in note.lower() for e in exclude):
                    continue
            picked.append(r)
        for r in picked:
            self.con.execute("UPDATE jobs SET status='queued', status_note='' "
                             "WHERE id=?", (r["id"],))
        self.con.commit()
        return picked

    def add_note(self, job_id: int, text: str) -> None:
        """Append an audit note to a row without changing its status."""
        self.con.execute(
            "UPDATE jobs SET status_note=TRIM(COALESCE(status_note,'') "
            "|| ?) WHERE id=?", (f" | {text}", job_id))
        self.con.commit()

    @staticmethod
    def _norm_key(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

    def find_cross_site_dup(self, *, source: str, title: str, company: str,
                            score: int, job_id: int):
        """A copy of this same posting on ANOTHER site: either already applied
        there (=> never re-apply) or a stronger queued copy we will apply to
        instead. Returns the winning row or None. Never matches empty
        company/title (too risky)."""
        t = self._norm_key(title)
        c = self._norm_key(company)
        if not t or not c:
            return None
        rows = self.con.execute(
            "SELECT id, source, title, company, score, status FROM jobs "
            "WHERE source != ? AND status IN ('applied','queued')",
            (source,)).fetchall()
        for r in rows:
            if self._norm_key(r["title"]) != t or self._norm_key(r["company"]) != c:
                continue
            if r["status"] == "applied":
                return r
            if r["score"] > score or (r["score"] == score and r["id"] < job_id):
                return r
        return None

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

    def rescore(self, job_id: int, score: int, reasons: str, status: str, note: str = ""):
        self.con.execute(
            """UPDATE jobs SET score=?, score_reasons=?, status=?, status_note=?,
                   updated_at=? WHERE id=?""",
            (score, reasons, status, note, now(), job_id))
        self.con.commit()

    def rescorable_rows(self):
        return self.con.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','skipped')").fetchall()

    def close(self):
        self.con.close()
