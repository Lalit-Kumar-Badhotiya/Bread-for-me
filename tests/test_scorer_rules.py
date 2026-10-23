"""Regression gate for the scoring / experience rules.

config/targets.yaml is the source of truth: these tests assert what the bot
must never do (apply to ".NET" postings, above-band experience, skill-less
matches) against the REAL current targets.

Run:  .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml

from bot.scorer import (_min_years_required, detail_verdict,
                        experience_verdict, score_job)

TARGETS = yaml.safe_load(
    (ROOT / "config" / "targets.yaml").read_text(encoding="utf-8"))


def _score(title: str, desc: str = "", posted: str = "2026-10-18"):
    return score_job(title, "Acme Pvt Ltd", "Bangalore", desc, posted, TARGETS)


class ExcludeKeywordTests(unittest.TestCase):
    def test_dotnet_all_spellings_rejected(self):
        for title in (".NET Developer", "ASP.NET Core Engineer",
                      "dotnet developer", "Dot Net Developer",
                      "Backend Developer (.net)", "Senior .NET Engineer"):
            _, reasons, reject = _score(title, "c# backend services",
                                        "2026-10-18")
            self.assertTrue(reject, f"{title!r} should be excluded, got {reasons}")
            self.assertTrue(any("exclude" in r for r in reasons), reasons)

    def test_dotnet_lookarounds_keep_normal_words(self):
        # ".net" must never fire inside "internet" / "network"
        for title in ("Internet of Things Engineer",
                      "Network Software Engineer"):
            _, reasons, reject = _score(
                title, "entry level, javascript and node work", "2026-10-18")
            self.assertFalse(reject, f"{title!r} wrongly rejected: {reasons}")

    def test_senior_roles_rejected(self):
        _, reasons, reject = _score("Senior Software Engineer", "javascript",
                                    "2026-10-18")
        self.assertTrue(reject)
        self.assertTrue(any("exclude" in r for r in reasons))


class ExperienceParserTests(unittest.TestCase):
    def test_low_end_of_range_is_the_minimum(self):
        self.assertEqual(_min_years_required("0-1 years"), [0.0])
        self.assertEqual(_min_years_required("2-3 yrs"), [2.0])
        self.assertEqual(_min_years_required("3 yrs to 6 yrs"), [3.0])
        self.assertEqual(_min_years_required("3 - 8 years"), [3.0])
        self.assertEqual(_min_years_required("5+ years"), [5.0])
        self.assertEqual(_min_years_required("2.5 years"), [2.5])

    def test_non_experience_numbers_ignored(self):
        self.assertEqual(_min_years_required("40 hours per week"), [])
        self.assertEqual(_min_years_required("established 20 years ago"), [])
        self.assertEqual(_min_years_required("no formal requirement stated"), [])


class VerdictTests(unittest.TestCase):
    def test_experience_verdict_zero_experience_policy(self):
        ok, why = experience_verdict("React and Node. 2-3 years experience.",
                                     TARGETS)
        self.assertFalse(ok)
        self.assertIn("2", why)

        ok, _ = experience_verdict("Freshers welcome. 0-1 years.", TARGETS)
        self.assertTrue(ok)

        ok, _ = experience_verdict("No prior experience needed, entry level",
                                   TARGETS)
        self.assertTrue(ok)

    def test_detail_verdict_skills_gate(self):
        # 2026-10-20 calibration: must-have-ANY gained python/java/sql from
        # the resume, so a "Python developer" detail page passes the gate
        # now — but a page with NONE of the must-have skills still fails.
        ok, _ = detail_verdict("Python developer, 0-1 years.", TARGETS)
        self.assertTrue(ok)

        ok, why = detail_verdict("Frontend developer, 0-1 years, cta html.", TARGETS)
        self.assertFalse(ok)
        self.assertIn("none of", why)

        ok, _ = detail_verdict("React developer, fresher, javascript", TARGETS)
        self.assertTrue(ok)

    def test_detail_verdict_experience_wins_first(self):
        ok, why = detail_verdict("React, javascript. 3+ years.", TARGETS)
        self.assertFalse(ok)
        self.assertIn("band", why)


if __name__ == "__main__":
    unittest.main()
