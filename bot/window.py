"""Quiet-run window control: keep the browser minimized to the taskbar while
the bot works, and pop it back up only when something needs the user's hands
(login, Cloudflare "verify you are human").

Why not --start-minimized? Chrome ignores that flag reliably when it is
spawned by another process, so the state is set through CDP right after
launch — and the window is BORN OFF-SCREEN (--window-position, see
OFFSCREEN_ARGS) so its first paint never appears on any monitor.
Headless is NOT an alternative: a headless window never exists in the
taskbar and the user can never watch or repair it.

A minimized/occluded window gets renderer throttling, which stalls
scroll-driven listing pages — STEADY_ARGS below disables that so pages run
exactly as they would in the foreground while sitting in the taskbar.
"""
import json
import threading
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page

# Keep pages fully active even when their window is minimized/occluded.
STEADY_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]

# Where user-completed taskbar clicks are placed (the on-screen spot the
# rescued window is pulled to), and the same spot set_window_state uses as
# its restore placement.
RESTORE_AT = (100, 80)

# Anything left at -32000 or beyond is off any sane monitor — real
# multi-monitor setups stay well above this (and minimized windows park
# exactly at -32000).

# A Chromium window paints at default bounds BEFORE any CDP command can
# minimize it — that was the ~1s flash at every launch/chunk-recycle.
# Launching at an off-screen position hides that first paint entirely;
# the immediate minimize call re-positions the restore bounds on-screen,
# so clicking the taskbar icon still surfaces a normal window.
OFFSCREEN_ARGS = ("--window-position=-32000,-32000",)

_MODES = {
    "minimized": "minimized", "minimal": "minimized", "taskbar": "minimized",
    "normal": "normal", "visible": "normal", "foreground": "normal",
}


def normalize_mode(value) -> str:
    """settings.run.browser_window parser. Missing/unknown -> minimized
    (that is the intended default for every run)."""
    key = str(value if value is not None else "minimized").strip().lower()
    return _MODES.get(key, "minimized")


def minimize_requested(settings: dict, headless: bool) -> bool:
    """True when this run should launch minimized — never for headless
    runs (headless windows do not live in a taskbar)."""
    if headless:
        return False
    run = (settings or {}).get("run") or {}
    return normalize_mode(run.get("browser_window")) == "minimized"


def set_window_state(page: Page, state: str,
                     restore_at: tuple | None = None) -> bool:
    """Minimize ("minimized") or restore ("normal") the browser window that
    hosts `page` — the persistent context runs in a single window, so this
    covers every tab the bot opens.

    IMPORTANT: this call binds its CDP session to the PAGE IT IS GIVEN —
    never to a shared cache. Browser.getWindowForTarget() without a target
    id answers with the SESSION's target's window: with the old root-page
    cache, the window guard "minimized" the root window on every popup and
    left real popup windows visible on screen ("pops up once in a while",
    2026-10-31). restore_at is accepted for compatibility but NOT sent — a
    position-only setWindowBounds call on a minimized window un-minimizes
    it (verified live 2026-10-31), so a taskbar click resurfaces the window
    at the off-screen birth placement and the restore watchdog
    (start_restore_shepherd) pulls it on-screen. Best-effort: returns False
    instead of raising, so a closed window or CDP hiccup never breaks a
    run."""
    try:
        cdp = page.context.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget").get("windowId")
        cdp.send("Browser.setWindowBounds",
                 {"windowId": window_id, "bounds": {"windowState": state}})
        return True
    except Exception:
        try:
            _note_popup(page, "minimize FAILED — window may stay visible")
        except Exception:
            pass
        return False


def fix_offscreen_restore(page: Page) -> bool:
    """Complete the USER'S own taskbar click: when they bring the window out
    of minimized state and it would surface off-screen (birth position
    -32000), pull it back on-screen. The bot never initiates a restore —
    this only makes a restore the user performs VISIBLE. Poll from the
    hands-needed wait loops (login / captcha). Best-effort: False on any
    failure, so a closed window or CDP hiccup never breaks a run."""
    try:
        ctx = page.context
        cdp = getattr(ctx, "_quiet_window_cdp", None)
        if cdp is None:
            cdp = ctx.new_cdp_session(page)
            try:  # one CDP session per browser session is enough
                ctx._quiet_window_cdp = cdp
            except Exception:
                pass
        window_id = cdp.send("Browser.getWindowForTarget").get("windowId")
        # CDP nests the values: {"bounds": {left, top, windowState, ...}} —
        # reading them flat (b.get("left", 0)) always saw the 0 default and
        # the window never got pulled on-screen (2026-10-31, "still not
        # opening" after the first shepherd attempt).
        bounds = (cdp.send("Browser.getWindowBounds",
                           {"windowId": window_id}).get("bounds") or {})
        if bounds.get("windowState") == "minimized":
            return False                     # still parked; user hasn't clicked
        if (bounds.get("left", 0) <= -20000
                or bounds.get("top", 0) <= -20000):
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id,
                      "bounds": {"left": RESTORE_AT[0], "top": RESTORE_AT[1]}})
            return True                      # user's click now shows the window
        return False
    except Exception:
        return False


# ------------------------------------------------------- restore watchdog
# fix_offscreen_restore above only runs from the dedicated hands-needed wait
# loops — anywhere else (fetch, sweeps, between jobs, mid-wizard pauses) a
# user's taskbar click resurfaced the window at the off-screen placement and
# nothing brought it in. This watchdog is a tiny thread born with EVERY
# quiet browser session: once a second it scans top-level windows and pulls
# any RESTORED Chrome-class window parked at an impossible position back to
# RESTORE_AT. It uses plain user32 calls (no playwright objects, so it is
# safe to run alongside the main thread) and it NEVER surfaces a window on
# its own — a minimized window is left alone; only a window the user whose
# own click brought back gets rescued.

def _is_offscreen(left: int, top: int) -> bool:
    """Same threshold everywhere: Chromium clamps the off-screen birth
    position (read back as -26214, 2026-10-31), and a parked/restored window
    sits far beyond any real multi-monitor coordinate."""
    return left <= -20000 or top <= -20000


def _window_action(iconic: bool, offscreen: bool,
                   parked_before: bool) -> str | None:
    """Watchdog decision for one top-level browser window:
    * "parked"  — minimized right now; remember it as user-clickable.
    * "rescue"  — it was in the taskbar before and came back out off-screen:
                  that is the USER's taskbar click; pull it to RESTORE_AT.
    * "hide"    — off-screen but NEVER minimized (a popup that escaped the
                  window guard): send it to the taskbar instead of honoring
                  it (pulling guard escapes on-screen was a real popup
                  source, 2026-10-31 live popup test).
    * None      — nothing to do (on-screen normal window)."""
    if iconic:                                   # minimize may re-fix soon
        return "parked"
    if not offscreen:
        return None
    return "rescue" if parked_before else "hide"


def _snapshot_session_pids() -> frozenset:
    """Pids of browser windows that are minimized OR off-screen right now —
    at shepherd spawn that is exactly this session's freshly born window(s)
    (birth-minimized, off-screen placement). The user's on-screen chrome is
    excluded, and every watchdog decision is pid-gated on this set so the
    watchdog never touches (and never dies because of) foreign browser
    windows. Popups the sites open later share the browser main-process pid,
    so they stay covered by the snapshot."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    pids = set()
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lp):
        try:
            hbox = int(getattr(hwnd, "value", hwnd) or 0)
            cls = ctypes.create_unicode_buffer(64)
            if user32.GetClassNameW(hbox, cls, 64) == 0:
                return True
            if cls.value != "Chrome_WidgetWin_1":
                return True
            r = wintypes.RECT()
            if not user32.GetWindowRect(hbox, ctypes.byref(r)):
                return True
            off = _is_offscreen(int(r.left), int(r.top))
            iconic = bool(user32.IsIconic(hbox))
            if not (off or iconic):
                return True            # a user's own on-screen browser
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hbox, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
        except Exception:
            pass
        return True

    c = proto(cb)
    try:
        user32.EnumWindows(c, 0)
    finally:
        c = None
    return frozenset(pids)


def start_restore_shepherd(page: Page):
    """Spawn the watchdog thread for this browser session. Returns the stop
    Event; the thread also self-terminates ~3 min after this session's
    browser windows are all gone (browser closed / process killed)."""
    stop = threading.Event()
    mine = _snapshot_session_pids()
    threading.Thread(target=_shepherd_loop, args=(stop, mine),
                     name="restore-shepherd", daemon=True).start()
    try:
        page.context._shepherd_stop = stop
    except Exception:
        pass
    return stop


def stop_restore_shepherd(ctx) -> None:
    """Signal the session's watchdog thread to stop (called from
    _safe_close). Absent/inert when the context has no watchdog."""
    try:
        ev = getattr(ctx, "_shepherd_stop", None)
        if ev is not None:
            ev.set()
        ctx._shepherd_stop = None
    except Exception:
        pass


def _scan_once(user32, parked: set, mine_pids: frozenset) -> bool:
    """One watchdog pass over all top-level windows, acting ONLY on windows
    whose pid belongs to this browser session (mine_pids, snapshotted at
    spawn). Returns True while any of this session's windows exists — the
    close-down counter in _shepherd_loop relies on that return value. The
    previous version had BOTH of these bugs (2026-10-31 'can't open the
    chrome'):
    * liveness came from a closure assignment (`any_parked = True` inside
      the EnumWindows callback) which Python made callback-local — it never
      updated the outer variable, so the shepherd killed itself after ~3
      minutes and the user's taskbar click stopped surfacing the window;
    * liveness keyed on "something is minimized", so a window the USER was
      actively using on-screen also counted as gone — same self-kill
      mid-use. Both are gone: liveness is "a session pid still owns a
      window"."""
    import ctypes
    from ctypes import wintypes
    recs = []       # (hwnd, pid, iconic, offscreen, width, height)

    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                               wintypes.LPARAM)

    def on_hwnd(hwnd, _lparam):
        try:
            # EnumWindows delivers the handle as a plain int through the
            # real FFI trampoline; normalize whatever form arrives so the
            # bookkeeping below can't crash on a ctypes instance.
            hbox = int(getattr(hwnd, "value", hwnd) or 0)
            cls = ctypes.create_unicode_buffer(64)
            if user32.GetClassNameW(hbox, cls, 64) == 0:
                return True
            if cls.value != "Chrome_WidgetWin_1":  # only browser windows
                return True
            r = wintypes.RECT()
            if not user32.GetWindowRect(hbox, ctypes.byref(r)):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hbox, ctypes.byref(pid))
            left, top = int(r.left), int(r.top)
            off = _is_offscreen(left, top)
            iconized = bool(user32.IsIconic(hbox))
            recs.append((hbox, pid.value, iconized, off,
                         int(r.right) - left, int(r.bottom) - top))
        except Exception:
            pass
        return True

    cb = proto(on_hwnd)
    try:
        user32.EnumWindows(cb, 0)
    finally:
        cb = None                       # release the callback ref

    alive = False
    live = set()
    for hbox, pid, iconic, off, w, h in recs:
        if pid not in mine_pids:
            continue                    # the user's own browser: never touch
        alive = True
        live.add(hbox)
        action = _window_action(iconic, off, hbox in parked)
        if action == "parked":
            parked.add(hbox)
        elif action == "rescue":
            parked.discard(hbox)
            user32.MoveWindow(wintypes.HWND(hbox), RESTORE_AT[0],
                              RESTORE_AT[1], w, h, True)
        elif action == "hide":
            # WM_SYSCOMMAND(0x112) / SC_MINIMIZE(0xF020) — the window
            # escaped the guard: send it to the taskbar. wintypes has no
            # WM_SYSCOMMAND name in Python 3.14 (would AttributeError and
            # kill the thread, caught by the fake-user32 test).
            user32.PostMessageW(wintypes.HWND(hbox), 0x0112, 0xF020, 0)
    parked &= live                      # drop hwnds of closed windows
    return alive


def _shepherd_loop(stop, mine_pids: frozenset) -> None:
    import ctypes
    user32 = ctypes.windll.user32
    parked = set()      # hwnds seen minimized (candidates for a user click)
    misses = 0
    while not stop.is_set():
        stop.wait(1.0)
        if _scan_once(user32, parked, mine_pids):
            misses = 0                  # a session window still exists
            continue
        misses += 1
        if misses >= 180:   # no session window for ~3 min -> session gone
            break


def _note_popup(page: Page, note: str) -> None:
    """Append an event line (console + data/logs/popup_windows.jsonl) when a
    context creates a window we send to the taskbar, or when a minimize
    fails — evidence for any future 'it popped up' report."""
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "note": note, "url": (getattr(page, "url", "") or "")[:200]}
        line = json.dumps(rec, ensure_ascii=False)
        path = (Path(__file__).resolve().parents[1] / "data" / "logs"
                / "popup_windows.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(f"[popup] {line}")
    except Exception:
        pass


def attach_window_guard(ctx) -> None:
    """Every page/window this context opens LATER is forced straight back
    into the taskbar at creation.

    target=_blank links and window.open() spawn separate OS windows —
    glassdoor/simplyhired "Easy Apply" -> smartapply is one — and a new
    window comes up in the FOREGROUND even while the root window sits
    minimized. This guard sends every such window to the taskbar the
    moment Playwright reports it, so nothing ever stays on screen.
    Best-effort: any failure is swallowed. Every triggering page is noted
    (console + popup_windows.jsonl) so guard escapes are visible."""
    def _quieten(page: Page) -> None:
        try:
            _note_popup(page, "new tab/window sent to taskbar by guard")
        except Exception:
            pass
        set_window_state(page, "minimized")
    try:
        ctx.on("page", _quieten)
    except Exception:
        pass


# Even the guard leaves a sub-second flash: Chromium SHOWS a new window
# before any CDP minimize can land. This init script removes the root
# cause — same-tab navigation instead of popup OS windows — so the window
# is never created at all. click/submit listeners run in the capture
# phase (before the site's own default action). It also covers
# base[target=_blank] (which plain anchor checks miss), strips already
# loaded _blank targets, and neuters window.focus()/moveTo() so site
# scripts cannot summon a minimized window back onto the screen.
POPUP_KILLER_JS = """
(() => {
  // Immutable overrides: plain assignment can be defeated by site scripts
  // (delete window.open restores the native popup opener) — defining them
  // non-writable/non-configurable makes the rewrite permanent.
  try {
    Object.defineProperty(window, 'open', {
      value: function (url) {
        try {
          if (typeof url !== 'string') return null;
          const u = new URL(url, location.href);
          if (u.protocol === 'http:' || u.protocol === 'https:')
            location.href = u.href;
        } catch (e) {}
        return null;
      },
      writable: false, configurable: false });
  } catch (e) {
    try { window.open = function (url) { return null; }; } catch (e2) {}
  }
  try {
    Object.defineProperty(window, 'focus', {
      value: function () {}, writable: false, configurable: false });
  } catch (e) { try { window.focus = function () {}; } catch (e2) {} }
  try {
    Object.defineProperty(window, 'moveTo', {
      value: function () {}, writable: false, configurable: false });
  } catch (e) { try { window.moveTo = function () {}; } catch (e2) {} }
  // Programmatic-submit bypass: form.target='_blank'; form.submit() does
  // NOT fire the 'submit' event, so the submit listener below never saw
  // it and a real OS window popped. The prototype itself now strips popup
  // targets before the real submit runs.
  try {
    const realSubmit = HTMLFormElement.prototype.submit;
    if (realSubmit) HTMLFormElement.prototype.submit = function () {
      try {
        const t = ((this.getAttribute('target') || this.target || '') + '')
          .toLowerCase();
        if (t === '_blank') {
          this.setAttribute('target', '_self');
          try { this.target = '_self'; } catch (e2) {}
        }
      } catch (e) {}
      return realSubmit.apply(this, arguments);
    };
  } catch (e) {}
  try {
    document.querySelectorAll('a[target], form[target], base[target]')
      .forEach(el => {
        if ((el.getAttribute('target') || '').toLowerCase() === '_blank')
          el.removeAttribute('target');
      });
  } catch (e) {}
  document.addEventListener('click', function (e) {
    try {
      const t = e.target;
      const a = t && t.closest ? t.closest('a[href]') : null;
      if (!a) return;
      let baseBlank = false;
      try {
        const bs = document.querySelectorAll('base[target]');
        for (const b of bs) {
          if ((b.getAttribute('target') || '').toLowerCase() === '_blank') {
            baseBlank = true;
            break;
          }
        }
      } catch (err) {}
      const own = (a.getAttribute('target') || '').toLowerCase();
      if (own === '_blank' || (!own && baseBlank))
        a.setAttribute('target', '_self');
    } catch (e) {}
  }, true);
  document.addEventListener('submit', function (e) {
    try {
      const f = e.target;
      if (f && f.tagName === 'FORM' && f.target === '_blank')
        f.removeAttribute('target');
    } catch (e) {}
  }, true);
})();
"""
