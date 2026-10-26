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


class WindowGuardTests(unittest.TestCase):
    """Every window the context opens later (target=_blank popups) is sent
    straight to the taskbar — a popup must never surface on screen even
    while the root window sits minimized."""

    def test_guard_minimizes_new_pages(self):
        import bot.window as wmod
        from bot.window import attach_window_guard
        orig = wmod.set_window_state
        calls = []
        wmod.set_window_state = lambda page, state: calls.append(state)
        try:
            class _Ctx:
                def __init__(self):
                    self.handlers = {}

                def on(self, ev, fn):
                    self.handlers[ev] = fn

            ctx = _Ctx()
            attach_window_guard(ctx)
            self.assertIn("page", ctx.handlers)
            ctx.handlers["page"](object())          # a popup appears
            self.assertEqual(calls, ["minimized"])  # -> taskbar immediately
        finally:
            wmod.set_window_state = orig

    def test_guard_survives_a_broken_context(self):
        from bot.window import attach_window_guard

        class _Ctx:
            def on(self, ev, fn):
                raise RuntimeError("no events here")

        attach_window_guard(_Ctx())     # must not raise


class PopupKillerScriptTests(unittest.TestCase):
    """The init script must stop popup OS windows at the SOURCE (same-tab
    navigation) — no window is ever created, so nothing can flash."""

    def test_covers_all_popup_sources(self):
        from bot.window import POPUP_KILLER_JS as js
        self.assertIn("window.open = function", js)      # window.open override
        self.assertIn("a[target=\"_blank\"]", js)        # anchor target strip
        self.assertIn("tagName === 'FORM'", js)          # form target strip
        self.assertIn("location.href = u.href", js)      # same-tab navigation
        self.assertIn("}, true)", js)                    # capture-phase


if __name__ == "__main__":
    unittest.main()
