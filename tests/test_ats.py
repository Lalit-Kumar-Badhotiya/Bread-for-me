"""External-ATS autofill: family classification, safe-form gate, routing."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.ats import ats_family, safe_from_probe
from bot.sites.base import BaseSiteBot


# ------------------------------------------------------------------ stubs
class _Page:
    """Bare page: url + a canned evaluate() result (SAFE_FORM_JS probe)."""
    def __init__(self, url="", evaluate_result=None):
        self.url = url
        self._eval = evaluate_result

    def evaluate(self, js):
        return self._eval


class _Notify:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(str(msg))


class _Bot(BaseSiteBot):
    """Minimal bot — swaps __init__ for plain attributes (no browser/DB)."""
    name = "testsite"
    home = "https://www.testsite.com"
    EXTERNAL_APPLY_GUARD = True
    ALLOWED_APPLY_FAMILIES = ("shared-infra.com",)

    def __init__(self, settings=None):
        self.settings = settings or {}
        self.notify = _Notify()

    def safe_shot(self, page, tag):        # parks would screenshot — stub it
        return f"shots/{tag}.png"

    def safe_html(self, page, tag):
        return f"html/{tag}.html"


# ------------------------------------------------------------------ tests
class AtsFamilyTests(unittest.TestCase):
    def test_known_hosts(self):
        cases = [
            ("tas.zohorecruit.com", "zoho"),
            ("frestonanalytics.zohorecruit.in", "zoho"),
            ("jobs.lever.co", "lever"),
            ("f.lever.co", "lever"),
            ("boards.greenhouse.io", "greenhouse"),
            ("jobs.greenhouse.io", "greenhouse"),
            ("hillenbrand.wd3.myworkdayjobs.com", "workday"),
            ("godigit.darwinbox.in", "darwinbox"),
            ("forms.gle", "google-form"),
            ("forms.google.com", "google-form"),
            ("jobs.smartrecruiters.com", "smartrecruiters"),
            ("acme.recruitee.com", "recruitee"),
            ("jobs.jobvite.com", "jobvite"),
            ("acme.bamboohr.com", "bamboohr"),
            ("acme.ashbyhq.com", "ashby"),
            ("https://jobs.lever.co/acme/senior-dev/apply", "lever"),
            ("https://forms.gle/abc123", "google-form"),
        ]
        for host, fam in cases:
            self.assertEqual(ats_family(host), fam, host)

    def test_unknown_hosts(self):
        for host in ("kesdee.com", "www.youbroadband.in", "careers.mastercard.com",
                     "docs.google.com", "www.google.com", ""):
            self.assertIsNone(ats_family(host), host)


class SafeFormTests(unittest.TestCase):
    def test_none(self):
        self.assertFalse(safe_from_probe(None))
        self.assertFalse(safe_from_probe({}))

    def test_resume_slot_with_button(self):
        self.assertTrue(safe_from_probe({"file": 1, "total": 1, "submitBtn": True}))

    def test_resume_slot_without_button(self):
        self.assertFalse(safe_from_probe({"file": 1, "total": 1, "submitBtn": False}))

    def test_contact_form_lookalike_rejected(self):
        res = {"text": 2, "email": 1, "file": 0, "radios": 0,
               "total": 3, "submitBtn": True}
        self.assertFalse(safe_from_probe(res))

    def test_full_application_form(self):
        res = {"text": 4, "email": 1, "file": 0, "radios": 0,
               "total": 5, "submitBtn": True}
        self.assertTrue(safe_from_probe(res))

    def test_screeners_only(self):
        res = {"text": 0, "email": 0, "file": 0, "radios": 2,
               "total": 0, "submitBtn": True}
        self.assertTrue(safe_from_probe(res))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.bot = _Bot()

    def test_guard_parks_unknown_plain_page(self):
        page = _Page("https://some-unknown-careerstone.com/job/1",
                     evaluate_result={"safe": False, "submitBtn": False})
        res = self.bot._external_guard_block(page, 7)
        self.assertEqual(res[0], "needs_you")
        self.assertTrue(res[1].startswith("external apply flow (some-unknown-careerstone.com)"))

    def test_guard_proceeds_on_known_ats(self):
        page = _Page("https://frestonanalytics.zohorecruit.in/jobs/Careers/123",
                     evaluate_result={"safe": False})
        self.assertIsNone(self.bot._external_guard_block(page, 7))
        self.assertIn("external ATS autofill", self.bot.notify.sent[0])

    def test_guard_proceeds_unknown_with_real_form(self):
        page = _Page("https://www.somejobportal.com/apply",
                     evaluate_result={"safe": True, "file": 1, "submitBtn": True})
        self.assertIsNone(self.bot._external_guard_block(page, 7))

    def test_guard_parks_when_autofill_disabled(self):
        bot = _Bot({"run": {"external_autofill": False}})
        page = _Page("https://jobs.lever.co/acme/x", evaluate_result={"safe": True})
        res = bot._external_guard_block(page, 7)
        self.assertEqual(res[0], "needs_you")
        self.assertEqual(res[1], "external apply flow (jobs.lever.co)")

    def test_allowlist_still_wins(self):
        page = _Page("https://www.shared-infra.com/form",
                     evaluate_result={"safe": True})
        self.assertIsNone(self.bot._external_guard_block(page, 7))

    def test_same_home_domain_allowed(self):
        page = _Page("https://jobs.testsite.com/x", evaluate_result={"safe": False})
        self.assertIsNone(self.bot._external_guard_block(page, 7))

    def test_guard_noop_without_guard_flag(self):
        self.bot.EXTERNAL_APPLY_GUARD = False
        page = _Page("https://jobs.lever.co/acme/x", evaluate_result={"safe": False})
        self.assertIsNone(self.bot._external_guard_block(page, 7))


class WrapNoteTests(unittest.TestCase):
    def test_failed_note_gets_benign_prefix(self):
        out = BaseSiteBot._wrap_external_note(
            "frestonanalytics.zohorecruit.in", "zoho",
            ("needs_you", "no next/submit button found", "s.png"))
        self.assertTrue(out[1].startswith("external apply flow"))
        self.assertIn("no next/submit button found", out[1])
        self.assertIn("zoho", out[1])
        self.assertEqual(out[2], "s.png")

    def test_success_untouched(self):
        res = ("applied", "confirmed: thank you", "s.png")
        self.assertEqual(BaseSiteBot._wrap_external_note(
            "jobs.lever.co", "lever", res), res)

    def test_already_prefixed_not_double_wrapped(self):
        res = ("needs_you", "external apply flow (jobs.lever.co) — login required", "s.png")
        self.assertEqual(BaseSiteBot._wrap_external_note(
            "jobs.lever.co", "lever", res), res)


class PatternTests(unittest.TestCase):
    def test_next_patterns_cover_smartapply_review(self):
        for pat in (r"^review your application$", r"^review your answers$",
                    r"^review application$"):
            self.assertIn(pat, BaseSiteBot.NEXT_PATTERNS, pat)

    def test_base_declaration_default_is_none(self):
        # only sites that opt in auto-tick consent boxes
        self.assertIsNone(BaseSiteBot.DECLARATION_RX)


class QuietWindowTests(unittest.TestCase):
    """The bot must never pop the window for hands-needed steps by default.
    run.pop_on_action: true is the explicit escape hatch."""

    def setUp(self):
        import bot.sites.base as base_mod
        self.base_mod = base_mod
        self._orig = base_mod.set_window_state
        self.calls = []
        base_mod.set_window_state = lambda page, state: self.calls.append(state)

    def tearDown(self):
        self.base_mod.set_window_state = self._orig

    def test_never_pops_by_default(self):
        bot = _Bot({})                      # browser_window defaults to minimized
        bot._quiet_window(None, "normal")   # would pop up — must do NOTHING
        self.assertEqual(self.calls, [])
        bot._quiet_window(None, "minimized")
        self.assertEqual(self.calls, ["minimized"])

    def test_pop_on_action_true_restores_old_behavior(self):
        bot = _Bot({"run": {"pop_on_action": True}})
        bot._quiet_window(None, "normal")
        self.assertEqual(self.calls, ["normal"])

    def test_noop_when_window_mode_is_normal(self):
        bot = _Bot({"run": {"browser_window": "normal"}})
        bot._quiet_window(None, "minimized")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
