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
from playwright.sync_api import Page

# Keep pages fully active even when their window is minimized/occluded.
STEADY_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]

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
    covers every tab the bot opens. restore_at=(left, top) moves the
    window's position in the same call — used after an off-screen
    launch to point the restore bounds at a visible spot, so a taskbar
    click surfaces a normal window. Best-effort: returns False instead
    of raising, so a closed window or a CDP hiccup never breaks a run."""
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
        cdp.send("Browser.setWindowBounds",
                 {"windowId": window_id, "bounds": {"windowState": state}})
        if restore_at is not None:
            # SECOND, position-only call: a single combined call left the
            # restore placement stuck at the off-screen birth position, so a
            # taskbar click surfaced an invisible window (2026-10-31, user
            # stranded mid-captcha). Position-while-minimized points the
            # placement the window restores to; fix_offscreen_restore below
            # still covers the case where Chromium ignores this call.
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id,
                      "bounds": {"left": restore_at[0], "top": restore_at[1]}})
        return True
    except Exception:
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
        b = cdp.send("Browser.getWindowBounds", {"windowId": window_id})
        if b.get("windowState") == "minimized":
            return False                     # still parked; user hasn't clicked
        if (b.get("left", 0) <= -20000 or b.get("top", 0) <= -20000):
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id,
                      "bounds": {"left": 100, "top": 80}})
            return True                      # user's click now shows the window
        return False
    except Exception:
        return False


def attach_window_guard(ctx) -> None:
    """Every page/window this context opens LATER is forced straight back
    into the taskbar at creation.

    target=_blank links and window.open() spawn separate OS windows —
    glassdoor/simplyhired "Easy Apply" -> smartapply is one — and a new
    window comes up in the FOREGROUND even while the root window sits
    minimized. This guard sends every such window to the taskbar the
    moment Playwright reports it, so nothing ever stays on screen.
    Best-effort: any failure is swallowed."""
    try:
        ctx.on("page", lambda p: set_window_state(p, "minimized"))
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
  try { window.open = function (url) {
      try {
        if (typeof url !== 'string') return null;
        const u = new URL(url, location.href);
        if (u.protocol === 'http:' || u.protocol === 'https:')
          location.href = u.href;
      } catch (e) {}
      return null;
  }; } catch (e) {}
  try { window.focus = function () {}; } catch (e) {}
  try { window.moveTo = function (x, y) {}; } catch (e) {}
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
