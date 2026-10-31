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
        self.assertIn("'a[target], form[target], base[target]'", js)  # sweep
        self.assertIn("closest('a[href]')", js)          # click-time strip
        self.assertIn("'_self'", js)                     # forced same-tab
        self.assertIn("tagName === 'FORM'", js)          # form target strip
        self.assertIn("location.href = u.href", js)      # same-tab navigation
        self.assertIn("window.focus = function", js)     # can't be summoned
        self.assertIn("window.moveTo = function", js)    # can't be dragged out
        self.assertIn("}, true)", js)                    # capture-phase


class LaunchPositionTests(unittest.TestCase):
    """The browser is born OFF-SCREEN (its first paint never appears), and
    the minimize call points the restore bounds back on-screen so taskbar
    clicks still surface a normal window."""

    def test_offscreen_birth_args(self):
        from bot.window import OFFSCREEN_ARGS
        self.assertTrue(
            any(a.startswith("--window-position=") for a in OFFSCREEN_ARGS))

    def test_restore_position_is_supported(self):
        import inspect
        import bot.window as wmod
        self.assertIn("restore_at",
                      inspect.signature(wmod.set_window_state).parameters)


class OffscreenRestoreTests(unittest.TestCase):
    """fix_offscreen_restore completes the USER'S own taskbar click: a
    window restored off-screen is pulled on-screen; minimized windows are
    never touched, and CDP failures never raise."""

    def _page(self, bounds):
        import types
        sent = []

        class _Cdp:
            def send(self, msg, arg=None):
                sent.append((msg, arg))
                if msg == "Browser.getWindowForTarget":
                    return {"windowId": 7}
                if msg == "Browser.getWindowBounds":
                    return bounds
                return {}

        ctx = types.SimpleNamespace(new_cdp_session=lambda p: _Cdp())
        page = types.SimpleNamespace(context=ctx)
        return page, sent

    def test_pulls_a_user_restored_window_onscreen(self):
        from bot.window import fix_offscreen_restore
        page, sent = self._page({"windowState": "normal",
                                 "left": -32000, "top": -32000})
        self.assertTrue(fix_offscreen_restore(page))
        self.assertIn(("Browser.setWindowBounds",
                       {"windowId": 7, "bounds": {"left": 100, "top": 80}}),
                      sent)

    def test_leaves_a_minimized_window_alone(self):
        from bot.window import fix_offscreen_restore
        page, sent = self._page({"windowState": "minimized",
                                 "left": -32000, "top": -32000})
        self.assertFalse(fix_offscreen_restore(page))
        self.assertNotIn("Browser.setWindowBounds",
                         [m for m, _ in sent if m == "Browser.setWindowBounds"])

    def test_leaves_an_onscreen_window_alone(self):
        from bot.window import fix_offscreen_restore
        page, sent = self._page({"windowState": "normal",
                                 "left": 100, "top": 80})
        self.assertFalse(fix_offscreen_restore(page))
        self.assertNotIn("Browser.setWindowBounds",
                         [m for m, _ in sent if m == "Browser.setWindowBounds"])

    def test_swallows_cdp_failures(self):
        import types
        from bot.window import fix_offscreen_restore

        class _Boom:
            def new_cdp_session(self, page):
                raise RuntimeError("no cdp")

        page = types.SimpleNamespace(context=_Boom())
        self.assertFalse(fix_offscreen_restore(page))   # must not raise


if __name__ == "__main__":
    unittest.main()
