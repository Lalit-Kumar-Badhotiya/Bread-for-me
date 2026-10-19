"""Answer-library matching + placeholder resolution (no browser)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.answers import AnswerLibrary

CFG = {
    "pairs": [
        {"q": "Are you willing to relocate?", "a": "Yes"},
        {"q": "What is your current CTC", "a": "0"},
        {"q": "How many years of experience do you have", "a": "0"},
    ],
    "blobs": {"cover_letter": "I am {{first_name}} {{last_name}}."},
}
PROFILE = {"personal": {"first_name": "Lalit", "last_name": "Badhotiya"}}


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.lib = AnswerLibrary(CFG, PROFILE, ROOT / "config" / "answers.yaml")

    def test_exact_match(self):
        self.assertEqual(self.lib.match("Are you willing to relocate?"), "Yes")

    def test_substring_match(self):
        q = "How many years of experience do you have in Software Engineering?"
        self.assertEqual(self.lib.match(q), "0")

    def test_fuzzy_match(self):
        self.assertEqual(
            self.lib.match("What is your current CTC in Lacs per annum?"), "0")

    def test_long_unrelated_question_does_not_match(self):
        q = ("Describe a time you led a team without any guidance and how you "
             "handled ambiguity in a fast paced environment")
        self.assertIsNone(self.lib.match(q))

    def test_resolve_placeholders(self):
        self.assertEqual(self.lib.resolve("I am {{first_name}} {{last_name}}."),
                         "I am Lalit Badhotiya.")

    def test_unknown_placeholder_kept_literal(self):
        self.assertEqual(self.lib.resolve("x {{nope_y}} z"), "x {{nope_y}} z")


if __name__ == "__main__":
    unittest.main()
