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
    """The browser is born OFF-SCREEN (its first paint never appears) and is
    minimized through a single CDP call. A restore placement cannot be set
    on a minimized window (position-only calls un-minimize it — verified
    live 2026-10-31), so taskbar-click visibility belongs to the restore
    watchdog, not to the minimize call."""

    def test_offscreen_birth_args(self):
        from bot.window import OFFSCREEN_ARGS
        self.assertTrue(
            any(a.startswith("--window-position=") for a in OFFSCREEN_ARGS))

    def test_minimize_sends_exactly_one_state_only_call(self):
        import types
        from bot.window import set_window_state
        sent = []

        class _Cdp:
            def send(self, msg, arg=None):
                sent.append((msg, dict(arg or {})))
                if msg == "Browser.getWindowForTarget":
                    return {"windowId": 3}
                return {}

        ctx = types.SimpleNamespace(new_cdp_session=lambda p: _Cdp())
        page = types.SimpleNamespace(context=ctx)
        self.assertTrue(set_window_state(page, "minimized",
                                         restore_at=(100, 80)))
        sets = [a for m, a in sent if m == "Browser.setWindowBounds"]
        self.assertEqual(len(sets), 1)              # one call only…
        # …carrying ONLY the state: a position would un-minimize the window
        self.assertEqual(list(sets[0]["bounds"].keys()), ["windowState"])
        self.assertEqual(sets[0]["bounds"]["windowState"], "minimized")


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
        page, sent = self._page(
            {"bounds": {"windowState": "normal",
                        "left": -32000, "top": -32000}})
        self.assertTrue(fix_offscreen_restore(page))
        self.assertIn(("Browser.setWindowBounds",
                       {"windowId": 7, "bounds": {"left": 100, "top": 80}}),
                      sent)

    def test_leaves_a_minimized_window_alone(self):
        from bot.window import fix_offscreen_restore
        page, sent = self._page(
            {"bounds": {"windowState": "minimized",
                        "left": -32000, "top": -32000}})
        self.assertFalse(fix_offscreen_restore(page))
        self.assertNotIn("Browser.setWindowBounds",
                         [m for m, _ in sent if m == "Browser.setWindowBounds"])

    def test_leaves_an_onscreen_window_alone(self):
        from bot.window import fix_offscreen_restore
        page, sent = self._page(
            {"bounds": {"windowState": "normal",
                        "left": 100, "top": 80}})
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

    def test_tolerates_a_bounds_less_response(self):
        # CDP hiccups can return a result with no "bounds": the shepherd
        # must neither raise nor reposition (it used to read the flat dict
        # and misjudge every window, 2026-10-31).
        from bot.window import fix_offscreen_restore
        page, sent = self._page({})
        self.assertFalse(fix_offscreen_restore(page))
        self.assertNotIn("Browser.setWindowBounds",
                         [m for m, _ in sent if m == "Browser.setWindowBounds"])


class RunGuardTests(unittest.TestCase):
    """set_window_state binds its CDP session to the page it is given: the
    window guard must minimize the POPUP'S own window. The old shared
    root-page session made Browser.getWindowForTarget answer with the ROOT
    window id, so the guard minimized the root window on every popup and
    real popup windows stayed visible on screen."""

    def test_sessions_are_page_bound(self):
        import types
        from bot.window import set_window_state

        class _Cdp:
            def __init__(self):
                self.sent = []

            def send(self, msg, arg=None):
                self.sent.append((msg, dict(arg or {})))
                if msg == "Browser.getWindowForTarget":
                    return {"windowId": 9}
                return {}

        sessions = []

        class _Ctx:
            def new_cdp_session(self, p):
                c = _Cdp()
                sessions.append(c)
                return c

        ctx = _Ctx()
        p1 = types.SimpleNamespace(context=ctx)
        p2 = types.SimpleNamespace(context=ctx)
        self.assertTrue(set_window_state(p1, "minimized"))
        self.assertTrue(set_window_state(p2, "minimized"))
        self.assertEqual(len(sessions), 2)          # one session PER page
        for sess in sessions:
            st = [a for m, a in sess.sent if m == "Browser.setWindowBounds"]
            self.assertEqual(len(st), 1)
            self.assertEqual(st[0]["bounds"], {"windowState": "minimized"})

    def test_form_submit_bypass_is_patched(self):
        from bot.window import POPUP_KILLER_JS
        self.assertIn("HTMLFormElement.prototype.submit", POPUP_KILLER_JS)
        self.assertIn("this.setAttribute('target', '_self')", POPUP_KILLER_JS)

    def test_window_open_override_cannot_be_deleted_by_sites(self):
        from bot.window import POPUP_KILLER_JS
        self.assertIn("Object.defineProperty(window, 'open'", POPUP_KILLER_JS)


class RestoreShepherdTests(unittest.TestCase):
    """The per-session watchdog pulls a window the USER restored off-screen
    back to RESTORE_AT within ~1s; minimized/on-screen windows are untouched
    and every failure path is silent."""

    def test_watchdog_decision(self):
        from bot.window import _window_action
        # minimized windows are remembered (parked candidates), never moved
        self.assertEqual(_window_action(True, True, False), "parked")
        self.assertEqual(_window_action(True, False, False), "parked")
        # the user's own taskbar click: was parked before, now out off-screen
        self.assertEqual(_window_action(False, True, True), "rescue")
        # a guard escape (off-screen, never minimized): hide, don't honor
        self.assertEqual(_window_action(False, True, False), "hide")
        # on-screen windows: nothing
        self.assertIsNone(_window_action(False, False, True))
        self.assertIsNone(_window_action(False, False, False))

    def test_scan_once_reacts_and_reports_parked(self):
        """_scan_once must park minimized SESSION windows, pull a PARKED
        window's restore back on-screen, HIDE guard escapes, keep the
        shepherd alive while a session window the USER is actively using
        sits on-screen, and ignore foreign (user's own) browser windows —
        the close-down counter relies on the return value. The previous
        version had both kill-bugs: liveness from a callback-local closure
        assignment (shepherd self-terminated after ~3 min, 'can't open the
        chrome'), and liveness keyed on 'something is minimized' (same
        self-kill while the user had the window open mid-use)."""
        import ctypes
        from ctypes import wintypes

        from bot.window import _scan_once, RESTORE_AT

        class _User32:
            """Minimal user32: windows map hwnd -> [cls, x, y, w, h,
            iconic, pid]; actions are recorded, never real."""

            def __init__(self, windows):
                self.windows = windows
                self.moves = []
                self.syscommands = []

            def _key(self, hwnd):
                return int(getattr(hwnd, "value", hwnd) or 0)

            def EnumWindows(self, cb, lp):
                for hwnd in list(self.windows):
                    if not cb(wintypes.HWND(hwnd), 0):
                        break

            def GetClassNameW(self, hwnd, buf, n):
                rec = self.windows.get(self._key(hwnd))
                if rec is None:
                    return 0
                buf.value = rec[0]
                return len(buf.value)

            def GetWindowRect(self, hwnd, r):
                rec = self.windows.get(self._key(hwnd))
                if rec is None:
                    return 0
                # byref() hands us an opaque wrapper: reach the RECT inside
                rect = getattr(r, "_obj", r)
                x, y, w, h = rec[1], rec[2], rec[3], rec[4]
                rect.left, rect.top = x, y
                rect.right, rect.bottom = x + w, y + h
                return 1

            def IsIconic(self, hwnd):
                rec = self.windows.get(self._key(hwnd))
                return 1 if rec and rec[5] else 0

            def GetWindowThreadProcessId(self, hwnd, pid):
                rec = self.windows.get(self._key(hwnd))
                # byref() wrapper: write through the DWORD inside it
                getattr(pid, "_obj", pid).value = 0 if rec is None else rec[6]
                return 1

            def MoveWindow(self, hwnd, x, y, w, h, repaint):
                h_int = self._key(hwnd)
                self.moves.append((h_int, x, y, w, h))
                rec = self.windows.get(h_int)   # emulate the real move, or
                if rec is not None:
                    rec[1], rec[2] = x, y       # the rescues never converge
                    rec[5] = False
                return 1

            def PostMessageW(self, hwnd, msg, wp, lp):
                h_int = self._key(hwnd)
                self.syscommands.append((h_int, wp))
                if wp == 0xF020:                # SC_MINIMIZE: mark minimal
                    rec = self.windows.get(h_int)
                    if rec is not None:
                        rec[5] = True
                return 1

        OURS, FOREIGN = 1001, 7777

        def win(cls="Chrome_WidgetWin_1", x=100, y=80, w=800, h=600,
                iconic=False, pid=OURS):
            return [cls, x, y, w, h, iconic, pid]

        u = _User32({111: win(), 222: win(iconic=True),
                     999: win(iconic=True, pid=FOREIGN)})  # user's own chrome
        parked = set()
        mine = frozenset({OURS})
        self.assertTrue(_scan_once(u, parked, mine))
        self.assertEqual(parked, {222})      # OUR minimized window remembered
        self.assertNotIn(999, parked)        # foreign window: ignored
        self.assertEqual(u.moves, [])        # on-screen window: untouched

        # the user clicks the taskbar icon of the bot chrome: the window
        # restores at its off-screen birth placement -> pulled to RESTORE_AT
        u.windows[222] = win(x=-26214, y=-26214)
        self.assertTrue(_scan_once(u, parked, mine))  # session still alive
        self.assertEqual(u.moves,
                         [(222, RESTORE_AT[0], RESTORE_AT[1], 800, 600)])
        self.assertNotIn(222, parked)        # now on-screen

        # a session window the user actively uses on-screen keeps the
        # shepherd alive (old liveness keyed on "something is minimized"
        # killed it mid-use, so a LATER click would have failed)
        self.assertTrue(_scan_once(u, parked, mine))
        self.assertFalse(parked)
        self.assertEqual(len(u.moves), 1)

        # a session window that escaped the guard off-screen (never
        # minimized): sent to the taskbar, not honored
        u.windows[333] = win(x=-26000, y=-25000)
        self.assertTrue(_scan_once(u, parked, mine))
        self.assertEqual(u.syscommands, [(333, 0xF020)])

        # once every session window is gone the pass reports dead…
        del u.windows[111], u.windows[222], u.windows[333]
        parked.add(111)
        self.assertFalse(_scan_once(u, parked, mine))
        self.assertEqual(parked, set())              # …and parked is pruned

        # …and foreign windows are never acted on even when off-screen
        u.windows[888] = win(x=-26000, y=-25000, pid=FOREIGN)
        u.syscommands.clear()
        self.assertFalse(_scan_once(u, parked, mine))
        self.assertEqual(u.syscommands, [])

    def test_start_and_stop_roundtrip(self):
        import threading
        import types
        from bot.window import start_restore_shepherd, stop_restore_shepherd
        ctx = types.SimpleNamespace()
        page = types.SimpleNamespace(context=ctx)
        stop = start_restore_shepherd(page)
        try:
            self.assertIsInstance(stop, threading.Event)
            self.assertIs(ctx._shepherd_stop, stop)
        finally:
            stop_restore_shepherd(ctx)
        self.assertTrue(stop.is_set())
        self.assertIsNone(ctx._shepherd_stop)
        stop_restore_shepherd(ctx)      # second call stays silent


if __name__ == "__main__":
    unittest.main()
