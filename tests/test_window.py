"""Quiet-run window mode parsing (no browser)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.window import STEADY_ARGS, minimize_requested, normalize_mode


class ModeTests(unittest.TestCase):
    def test_default_is_minimized(self):
        self.assertEqual(normalize_mode(None), "minimized")  # requested default

    def test_known_values(self):
        self.assertEqual(normalize_mode("minimized"), "minimized")
        self.assertEqual(normalize_mode("normal"), "normal")
        self.assertEqual(normalize_mode(" VISIBLE "), "normal")
        self.assertEqual(normalize_mode("Taskbar"), "minimized")

    def test_unknown_value_falls_back_to_minimized(self):
        self.assertEqual(normalize_mode("minemized"), "minimized")


class RequestTests(unittest.TestCase):
    def test_minimized_when_headed(self):
        self.assertTrue(
            minimize_requested({"run": {"browser_window": "minimized"}}, False))

    def test_minimized_by_default(self):
        self.assertTrue(minimize_requested({}, False))

    def test_never_when_headless(self):
        self.assertFalse(
            minimize_requested({"run": {"browser_window": "minimized"}}, True))

    def test_off_when_normal(self):
        self.assertFalse(
            minimize_requested({"run": {"browser_window": "normal"}}, False))


class SteadyArgsTests(unittest.TestCase):
    def test_flags_disable_throttling(self):
        joined = " ".join(STEADY_ARGS)
        for flag in ("--disable-background-timer-throttling",
                     "--disable-renderer-backgrounding",
                     "--disable-backgrounding-occluded-windows"):
            self.assertIn(flag, joined)


if __name__ == "__main__":
    unittest.main()
