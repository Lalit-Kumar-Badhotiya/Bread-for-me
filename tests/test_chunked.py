"""Chunk-boundary session rescue: a rotated identity mid-run waits for one
login in the already-open recycled browser instead of ending the run
(2026-11-02: fresh indeed logins had lifetimes of 11-16 min once SmartApply
identity gates ran, forcing repeated full relaunches)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot.main as main_mod


# ------------------------------------------------------------------ stubs
class _Ctx:
    def __init__(self):
        self._shepherd_stop = None

    def close(self):
        pass


class _Pw:
    def stop(self):
        pass


class _Tracker:
    def __init__(self):
        self.events = []

    def log_event(self, kind, msg):
        self.events.append((kind, str(msg)))


class _Notify:
    def __init__(self):
        self.sent = []

    def send(self, msg, toast=False):
        self.sent.append(str(msg))


class _Bot:
    name = "testsite"

    def __init__(self, sig_seq=(), apply_seq=(), rescued=True, quota=10):
        self.settings = {"limits": {"apply_chunk_size": 1}}
        self.notify = _Notify()
        self.tracker = _Tracker()
        self._sig_seq = list(sig_seq)
        self._apply_seq = list(apply_seq)
        self.apply_calls = []
        self.login_waits = 0
        self._rescued = rescued
        self._quota = quota

    def apply(self, page, max_apps=8):
        self.apply_calls.append(max_apps)
        if self._apply_seq:
            got = self._apply_seq.pop(0)
            self._quota -= got or 0          # the real bot consumes quota
            return got
        return 0

    def quota_left(self):
        return self._quota

    def preflight(self, page):
        if self._sig_seq:
            return self._sig_seq.pop(0)
        return False

    def await_manual_login(self, page):
        self.login_waits += 1
        return self._rescued


# ------------------------------------------------------------------ tests
class ChunkBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._orig_open = main_mod.open_session
        calls = []

        def fake_open_session(site, headless):
            calls.append(site)
            return _Pw(), _Ctx(), object()          # page stub

        main_mod.open_session = fake_open_session
        self._open_calls = calls

    def tearDown(self):
        main_mod.open_session = self._orig_open

    def test_boundary_rescued_continues_run(self):
        bot = _Bot(sig_seq=[False, True], apply_seq=[1, 1], quota=2)
        pw, ctx, page = main_mod._apply_chunked(bot, bot.settings,
                                                "test", _Pw(), _Ctx(), None,
                                                max_apps=2)
        self.assertEqual(bot.apply_calls, [1, 1])
        self.assertEqual(bot.login_waits, 1)        # one bounded wait
        self.assertEqual(len(self._open_calls), 1)  # one recycled browser
        self.assertTrue(any("rescued" in m for _, m in bot.tracker.events))
        self.assertFalse(any("session expired" in s for s in bot.notify.sent))

    def test_boundary_unrescued_breaks_with_old_message(self):
        bot = _Bot(sig_seq=[False, True], apply_seq=[1], rescued=False)
        main_mod._apply_chunked(bot, bot.settings, "test",
                                _Pw(), _Ctx(), None, max_apps=2)
        self.assertEqual(bot.apply_calls, [1])
        self.assertEqual(bot.login_waits, 1)
        self.assertTrue(any("session expired at a chunk boundary"
                            in s for s in bot.notify.sent))
        self.assertFalse(any("rescued" in m for _, m in bot.tracker.events))

    def test_healthy_boundary_never_waits(self):
        bot = _Bot(sig_seq=[True], apply_seq=[1, 0], quota=2)
        main_mod._apply_chunked(bot, bot.settings, "test",
                                _Pw(), _Ctx(), None, max_apps=2)
        self.assertEqual(len(bot.apply_calls), 2)
        self.assertEqual(bot.login_waits, 0)
        self.assertFalse(any("session expired" in s for s in bot.notify.sent))


if __name__ == "__main__":
    unittest.main()
