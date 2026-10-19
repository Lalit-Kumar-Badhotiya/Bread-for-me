"""Site-level helpers: form-link detection, chat rejection handling,
circuit-breaker classification."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.sites.base import BaseSiteBot
from bot.sites.naukri import (CHAT_INVALID_RX, FORM_LINK_RX, NaukriBot)


class FormLinkTests(unittest.TestCase):
    def test_known_form_hosts_match(self):
        samples = (
            "<p>Apply link:</strong> https://forms.gle/AbCd1234 </p>",
            "Apply Here: https://forms.gle/XyZ98765",
            "https://docs.google.com/forms/d/e/1FAIpQL/viewform?usp=sf_link",
            "https://forms.office.com/r/abc123",
            "https://mybrand.typeform.com/to/AbC",
            "https://form.jotform.com/123456789",
            "https://tally.so/r/xyzabc",
            "https://airtable.com/shrAbCdEfG",
        )
        for s in samples:
            self.assertTrue(FORM_LINK_RX.search(s), s)

    def test_normal_text_and_urls_ignored(self):
        samples = (
            "we value internet and network skills",
            "check our site https://example.com/jobs for updates",
            "https://docs.google.com/document/d/abc123/edit",  # docs, not forms
            "forms.gl is not a form host",
            "contact hr@typeform.co.in for queries",
        )
        for s in samples:
            self.assertFalse(FORM_LINK_RX.search(s), s)


class ChatInvalidTests(unittest.TestCase):
    def test_invalid_phrases_match(self):
        self.assertTrue(CHAT_INVALID_RX.search(
            "The input seems invalid. What is your current CTC in Lacs "
            "per annum?"))
        self.assertTrue(CHAT_INVALID_RX.search("Invalid format, please retry"))

    def test_normal_questions_dont_match(self):
        self.assertFalse(CHAT_INVALID_RX.search(
            "What is your current CTC in Lacs per annum?"))
        self.assertFalse(CHAT_INVALID_RX.search("Do you have a valid passport?"))


class ChatVariantTests(unittest.TestCase):
    def test_numeric_answers_get_fallbacks(self):
        self.assertEqual(NaukriBot._chat_answer_variants("0"),
                         ["0.0", "Fresher"])
        self.assertEqual(NaukriBot._chat_answer_variants("5"),
                         ["5.0", "Fresher"])

    def test_decimal_and_text_answers_have_none(self):
        self.assertEqual(NaukriBot._chat_answer_variants("2.5"), ["Fresher"])
        self.assertEqual(NaukriBot._chat_answer_variants("Fresher"), [])
        self.assertEqual(NaukriBot._chat_answer_variants(""), [])


class BreakerTests(unittest.TestCase):
    def test_expected_parks_reset_the_streak(self):
        f = BaseSiteBot._breaker_stepped
        self.assertEqual(f("needs_you", 2, "external apply flow (cisco)"), 0)
        self.assertEqual(
            f("needs_you", 2, "needs your input: chat rejected 'What is...'"), 0)
        self.assertEqual(
            f("needs_you", 2, "Cloudflare verification not solved (manual)"), 0)
        self.assertEqual(
            f("needs_you", 2, "job page did not render (details unverifiable)"), 0)
        self.assertEqual(f("needs_you", 2, "manual login required (timed out)"), 0)

    def test_real_breakage_still_counts(self):
        f = BaseSiteBot._breaker_stepped
        self.assertEqual(f("needs_you", 0, "no next/submit button found"), 1)
        self.assertEqual(f("needs_you", 1, "no apply button"), 2)
        self.assertEqual(f("failed", 0, "exception: TimeoutError: x"), 1)

    def test_non_park_statuses_reset(self):
        f = BaseSiteBot._breaker_stepped
        self.assertEqual(f("applied", 5, "confirmed: application has been"), 0)
        self.assertEqual(f("skipped", 5, "experience: needs 3+ yrs"), 0)


if __name__ == "__main__":
    unittest.main()
