"""Tracker basics: fingerprinting and the retry-park conventions."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.tracker import Tracker, fingerprint


class FingerprintTests(unittest.TestCase):
    def test_case_and_punctuation_insensitive(self):
        a = fingerprint("NxtWave Disruptive Technologies",
                        "Software Developer", "Chennai")
        b = fingerprint("nxtwave disruptive technologies",
                        "software  developer", "chennai")
        self.assertEqual(a, b)

    def test_different_jobs_differ(self):
        a = fingerprint("Acme", "Software Engineer", "Pune")
        b = fingerprint("Acme", "Backend Developer", "Pune")
        self.assertNotEqual(a, b)


class AppliedTodayTests(unittest.TestCase):
    """The day meter counts ONLY new real submissions (jobs ledger,
    'confirmed:' notes) — pre-check detections and date drift must not
    shift the cap (2026-11-02 drift let indeed appplly 34/25)."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Tracker(Path(tmp.name) / "t.db")
        self.addCleanup(self.t.close)

    def _row(self, title, note, source="naukri"):
        jid, _ = self.t.upsert_job(
            source=source, source_job_id=title, url="https://x/" + title,
            title=title, company="Co " + title, location="Pune",
            description="d", posted_date="2026-11-03")
        self.t.set_status(jid, "applied", note)
        return jid

    def test_counts_only_real_submissions_today(self):
        self._row("auk1", "confirmed: successfully applied")
        self._row("ind1", "confirmed: you've applied", source="indeed")
        self._row("sh1", "confirmed: application has been", source="simplyhired")
        self._row("pc1", "pre-check: site already shows applied state")
        self._row("pc2", "already applied on site (detected on job page)")
        self.assertEqual(self.t.applied_today("naukri"), 1)
        self.assertEqual(self.t.applied_today("indeed"), 1)
        self.assertEqual(self.t.applied_today("simplyhired"), 1)
        self.assertEqual(self.t.applied_today(None), 3)

    def test_yesterday_does_not_count_and_no_notes_never_count(self):
        jid = self._row("old1", "confirmed: successfully applied")
        self.t.con.execute("UPDATE jobs SET updated_at='2026-11-02 10:00' "
                           "WHERE id=?", (jid,))
        self.t.con.commit()
        self._row("bare1", "")                 # no note: never counted
        self.assertEqual(self.t.applied_today("naukri"), 0)
        self.assertEqual(self.t.applied_today(None), 0)


class RetryRowsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Tracker(Path(tmp.name) / "t.db")
        self.addCleanup(self.t.close)  # LIFO: close before the dir is removed

    def _row(self, title, status, note):
        jid, _ = self.t.upsert_job(
            source="naukri", source_job_id=title, url="https://x/" + title,
            title=title, company="Co " + title, location="Pune",
            description="d", posted_date="2026-10-18")
        self.t.set_status(jid, status, note)
        return jid

    def test_retry_skips_external_flows_by_default(self):
        self._row("extjob", "needs_you",
                  "external apply flow (cisco.wd5.myworkdayjobs.com)")
        self._row("qjob", "needs_you",
                  "needs your input: What is your current CTC")
        self._row("okjob", "failed", "exception: TimeoutError")
        picked = self.t.retry_rows("naukri")
        notes = " ".join(r["status_note"] for r in picked)
        self.assertIn("needs your input", notes)
        self.assertIn("TimeoutError", notes)
        self.assertNotIn("external apply flow", notes)

    def test_explicit_id_overrides_the_external_skip(self):
        jid = self._row("extonly", "needs_you",
                        "external apply flow (careers.example.com)")
        picked = self.t.retry_rows("naukri", ids=[jid])
        self.assertEqual([r["id"] for r in picked], [jid])


if __name__ == "__main__":
    unittest.main()
