"""Quiet-run window control: keep the browser minimized to the taskbar while
the bot works, and pop it back up only when something needs the user's hands
(login, Cloudflare "verify you are human").

Why not --start-minimized? Chrome ignores that flag reliably when it is
spawned by another process, so the state is set through CDP right after
launch (~1s flash on screen first — unavoidable; Playwright cannot spawn a
process pre-minimized). Headless is NOT an alternative: a headless window
never exists in the taskbar and the user can never watch or repair it.

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


def set_window_state(page: Page, state: str) -> bool:
    """Minimize ("minimized") or restore ("normal") the browser window that
    hosts `page` — the persistent context runs in a single window, so this
    covers every tab the bot opens. Best-effort: returns False instead of
    raising, so a closed window or a CDP hiccup never breaks a run."""
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
        return True
    except Exception:
        return False
