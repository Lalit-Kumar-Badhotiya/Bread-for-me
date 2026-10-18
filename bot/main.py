"""CLI orchestrator for the apply bot.

Commands:
  init            sanity-check configs/print what to fill
  login SITE      open a persistent browser session to log in manually once
  fetch SITE      pull + score + queue new jobs (no browser needed)
  apply SITE      submit queued jobs in the browser (quota-capped)
  run SITE        fetch + apply in one go
  calibrate SITE  show the current queue so you can tune targets.yaml
  rescore         re-apply current targets.yaml to the existing queue (no browser)
  report          today's stats across sites
"""
import argparse
import copy
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:  # Windows console utf-8 safety
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import yaml

from bot.answers import AnswerLibrary
from bot.notify import Notifier
from bot.tracker import Tracker

CFG_DIR = ROOT / "config"
RESUME_PATH = ROOT / "resume" / "resume.pdf"

REGISTRY = {
    "naukri": ("bot.sites.naukri", "NaukriBot",
               "https://www.naukri.com"),
    "wellfound": ("bot.sites.wellfound", "WellfoundBot",
                  "https://wellfound.com"),
    # Indeed group: one parent company + shared anti-bot => shared budget
    "indeed": ("bot.sites.indeed_group", "IndeedBot", "https://in.indeed.com"),
    "glassdoor": ("bot.sites.indeed_group", "GlassdoorBot",
                  "https://www.glassdoor.co.in"),
    "simplyhired": ("bot.sites.indeed_group", "SimplyHiredBot",
                    "https://www.simplyhired.co.in"),
    # LinkedIn stays manual-by-design FOREVER and is never registered or
    # automated.
}


def load_yaml(path: Path) -> dict:
    if not path.exists():
        print(f"MISSING config: {path}")
        sys.exit(1)
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def shared():
    settings = load_yaml(CFG_DIR / "settings.yaml")
    profile = load_yaml(CFG_DIR / "profile.yaml")
    targets = load_yaml(CFG_DIR / "targets.yaml")
    answers_cfg = load_yaml(CFG_DIR / "answers.yaml")
    return (settings, profile, targets,
            AnswerLibrary(answers_cfg, profile, CFG_DIR / "answers.yaml"),
            Tracker(), Notifier(settings))


def get_bot(site: str, extra_titles=None, *, settings=None, profile=None,
            targets=None, answers_lib=None, tracker=None, notifier=None):
    if site not in REGISTRY:
        print(f"Unknown site '{site}'. Available: {', '.join(REGISTRY)}")
        print("(wellfound/indeed/glassdoor/simplyhired arrive in later phases;")
        print(" LinkedIn is manual-by-design and is never automated.)")
        sys.exit(2)
    settings = settings or load_yaml(CFG_DIR / "settings.yaml")
    profile = profile or load_yaml(CFG_DIR / "profile.yaml")
    targets = targets or load_yaml(CFG_DIR / "targets.yaml")
    answers_lib = answers_lib or AnswerLibrary(
        load_yaml(CFG_DIR / "answers.yaml"), profile, CFG_DIR / "answers.yaml")
    tracker = tracker or Tracker()
    notifier = notifier or Notifier(settings)
    if extra_titles:
        targets = copy.deepcopy(targets)
        targets.setdefault("titles", [])
        targets["titles"] += [t for t in extra_titles if t not in targets["titles"]]

    mod_path, cls_name, _home = REGISTRY[site]
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    return cls(settings["sites"].get(site, {}), tracker=tracker,
               settings=settings, profile=profile, targets=targets,
               answers=answers_lib, notifier=notifier), tracker, notifier


def open_session(site: str, headless: bool):
    """Launch persistent browser context; returns (playwright, context, page).

    settings.yaml may set sites.<site>.browser_channel ("chrome"/"msedge")
    to run that site in the installed browser instead of bundled Chromium —
    required for Cloudflare-protected sites like Indeed, which treat the
    bundled browser as a bot however human the user's actions are.
    """
    from playwright.sync_api import sync_playwright
    state = ROOT / "data" / "state" / site
    state.mkdir(parents=True, exist_ok=True)
    settings = load_yaml(CFG_DIR / "settings.yaml")
    channel = ((settings.get("sites") or {}).get(site) or {}).get("browser_channel")
    pw = sync_playwright().start()
    extra = {"channel": channel} if channel else {}
    ctx = pw.chromium.launch_persistent_context(
        str(state), headless=headless,
        args=["--disable-blink-features=AutomationControlled"], **extra)
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    ctx.set_default_timeout(20000)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return pw, ctx, page


# ---------------------------------------------------------------- commands
def cmd_init(args):
    for d in ("data/state", "data/logs", "resume"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    settings, profile, targets, answers, tracker, notifier = shared()
    missing_profile = [k for k, v in (profile.get("personal") or {}).items()
                       if k in {"first_name", "email", "phone"} and not str(v).strip()]
    print("== Setup checklist ==")
    print(f"[{'X' if missing_profile else ' '}] fill config/profile.yaml "
          f"(missing: {', '.join(missing_profile) or 'none'})")
    print(f"[{'X' if not targets.get('titles') else ' '}] fill config/targets.yaml titles/skills")
    print(f"[{'X' if not RESUME_PATH.exists() else ' '}] resume/resume.pdf present "
          f"({str(RESUME_PATH) if not RESUME_PATH.exists() else 'ok'})")
    print(f"[ ] one-time: python run_apply.py login naukri  (manual login in the window)")
    print(f"[ ] then:     python run_apply.py calibrate naukri -> tune targets.yaml")
    print(f"[ ] then:     python run_apply.py run naukri")
    print(f"\nrecords so far: {len(tracker.queued(limit=None))} queued jobs in tracker")


def _safe_close(ctx, pw):
    """Shut the browser down without crashing the CLI (the user may have
    closed the window/context mid-run — closing twice raises TargetClosed)."""
    try:
        ctx.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass


def _preflight_or_exit(bot, page, ctx, pw, site, notifier):
    """Stop before a run when the session is clearly logged out."""
    try:
        sig = bot.preflight(page)
    except Exception:
        sig = None
    if sig is False:
        print(f"[{site}] session looks logged OUT — run: "
              f".venv\\Scripts\\python.exe run_apply.py login {site}")
        notifier.send(f"[{site}] preflight: not logged in — stopping before "
                      "wasting a run", toast=True)
        _safe_close(ctx, pw)
        sys.exit(4)


def _apply_chunked(bot, settings, site, pw, ctx, page, max_apps):
    """Run bot.apply in chunks, reopening the browser between chunks so long
    sessions cannot degrade on one browser process. Returns the live
    (pw, ctx, page) tuple (it may have been recycled)."""
    chunk = int((settings.get("limits", {}) or {}).get("apply_chunk_size", 0) or 0)
    if chunk <= 0:
        bot.apply(page, max_apps=max_apps)
        return pw, ctx, page
    total = 0
    while True:
        left = None if max_apps is None else max_apps - total
        if left is not None and left <= 0:
            break
        n = bot.apply(page, max_apps=min(chunk, left) if left is not None else chunk)
        total += n
        if n == 0 or bot.quota_left() <= 0:
            break
        _safe_close(ctx, pw)
        headless = bool(settings.get("run", {}).get("headless", False))
        pw, ctx, page = open_session(site, headless)
        try:
            sig = bot.preflight(page)
        except Exception:
            sig = None
        if sig is False:
            bot.notify.send(f"[{site}] session expired at a chunk boundary — "
                            f"run: run_apply.py login {site}", toast=True)
            break
        bot.tracker.log_event("run", f"[{site}] browser recycled after "
                                      f"{total} processed job(s)")
    return pw, ctx, page


def cmd_login(args):
    settings = load_yaml(CFG_DIR / "settings.yaml")
    _bot, tracker, notifier = get_bot(args.site)
    headless = bool(settings.get("run", {}).get("headless", False))
    pw, ctx, page = open_session(args.site, headless=False)  # login always headed
    (_, _, home) = REGISTRY[args.site]
    try:
        page.goto(home, wait_until="domcontentloaded")
    except Exception:
        pass
    notifier.send(f"Browser open — log in to {args.site} manually now.", toast=True)
    input("When you are logged in (you can see your name/profile), press ENTER here... ")
    _safe_close(ctx, pw)
    print(f"Session saved to data/state/{args.site} — future runs reuse it.")


def cmd_fetch(args):
    settings = load_yaml(CFG_DIR / "settings.yaml")
    bot, tracker, notifier = get_bot(args.site, extra_titles=args.title, settings=settings)
    if getattr(args, "min_score", None) is not None:
        bot.targets["min_score"] = int(args.min_score)
    headless = bool(settings.get("run", {}).get("headless", False))
    pw, ctx, page = open_session(args.site, headless)
    try:
        n = bot.fetch_and_rescore(page)
    except Exception as e:
        n = 0
        notifier.send(f"[{args.site}] fetch failed: {type(e).__name__}: {e}", toast=True)
    finally:
        _safe_close(ctx, pw)
    queued = tracker.queued(source=args.site, limit=100000)
    notifier.send(f"[{args.site}] fetch done: {n} newly queued "
                  f"({len(queued)} in queue total)")


def cmd_apply(args):
    settings, profile, targets, answers, tracker, notifier = shared()
    bot, tracker, notifier = get_bot(args.site, settings=settings, profile=profile,
                                     targets=targets, answers_lib=answers,
                                     tracker=tracker, notifier=notifier)
    if getattr(args, "no_learn", False):
        bot.settings.setdefault("run", {})["interactive_learning"] = False
    if args.force:
        bot.force_hours = True
    if not bot.in_business_hours() and not args.force:
        print("Outside business hours (settings.run) — use --force to override.")
        sys.exit(3)
    headless = bool(settings.get("run", {}).get("headless", False))
    pw, ctx, page = open_session(args.site, headless)
    try:
        _preflight_or_exit(bot, page, ctx, pw, args.site, notifier)
        pw, ctx, page = _apply_chunked(bot, settings, args.site, pw, ctx, page,
                                       args.max)
    finally:
        _safe_close(ctx, pw)


def cmd_run(args):
    settings, profile, targets, answers, tracker, notifier = shared()
    bot, tracker, notifier2 = get_bot(args.site, extra_titles=args.title,
                                      settings=settings, profile=profile,
                                      targets=targets, answers_lib=answers,
                                      tracker=tracker, notifier=notifier)
    if getattr(args, "no_learn", False):
        bot.settings.setdefault("run", {})["interactive_learning"] = False
    if args.force:
        bot.force_hours = True
    if getattr(args, "min_score", None) is not None:
        bot.targets["min_score"] = int(args.min_score)
    if not bot.in_business_hours() and not args.force:
        print("Outside business hours (settings.run) — use --force to override.")
        sys.exit(3)
    headless = bool(settings.get("run", {}).get("headless", False))
    pw, ctx, page = open_session(args.site, headless)
    errors: list[str] = []
    try:
        _preflight_or_exit(bot, page, ctx, pw, args.site, notifier2)
        try:
            n = bot.fetch_and_rescore(page)
        except Exception as e:
            n = 0
            errors.append(f"fetch: {type(e).__name__}: {e}")
            notifier2.send(f"[{args.site}] fetch failed: {type(e).__name__}: {e}", toast=True)
        swept = 0
        vlimit = int((settings.get("limits", {}) or {}).get("validate_after_fetch", 0) or 0)
        if vlimit > 0:
            try:
                swept = bot.validate_dead_links(page, limit=vlimit)
            except Exception:
                swept = 0
        try:
            pw, ctx, page = _apply_chunked(bot, settings, args.site, pw, ctx, page,
                                           args.max)
        except Exception as e:
            errors.append(f"apply: {type(e).__name__}: {e}")
            notifier2.send(f"[{args.site}] apply crashed: {type(e).__name__}: {e}", toast=True)
            print(f"Resume any time with: .venv\\Scripts\\python.exe "
                  f"run_apply.py run {args.site}")
        queued = tracker.queued(source=args.site, limit=100000)
        notifier2.send(f"[{args.site}] run done: fetched {n}, "
                       f"applied today {tracker.applied_today(args.site)}, "
                       f"queue {len(queued)}"
                       + (f", swept {swept} dead" if vlimit > 0 else "")
                       + (f" | errors: {'; '.join(errors)}" if errors else ""))
    finally:
        _safe_close(ctx, pw)


def cmd_calibrate(args):
    bot, tracker, notifier = get_bot(args.site)
    rows = tracker.queued(source=args.site, limit=args.limit)
    if not rows:
        print("Queue empty — run: python run_apply.py fetch "
              f"{args.site} --title \"your job title\"")
        return
    print(f"== {args.site} queue ({len(rows)} shown) ==")
    for r in rows:
        print(f"  {r['score']:>3}  {r['title'][:52]:<52} @ {(r['company'] or '')[:18]:<18}"
              f"  {r['url']}")
        print(f"      why: {r['score_reasons'][:150]}")


def cmd_rescore(args):
    """Re-apply current targets.yaml to every queued/skipped row (no browser)."""
    settings = load_yaml(CFG_DIR / "settings.yaml")
    targets = load_yaml(CFG_DIR / "targets.yaml")
    tracker = Tracker()
    notifier = Notifier(settings)
    from bot.scorer import rescore_pending
    before = len(tracker.queued(limit=None))
    # explicit command = user wants everything re-judged (also revives
    # detail-gate skips after target edits)
    changed = rescore_pending(tracker, targets, settings,
                              revive_gate_skips=True)
    after = len(tracker.queued(limit=None))
    notifier.send(f"rescore: {changed} row(s) re-classified against current "
                  f"targets; queue {before} -> {after}")


def cmd_retry(args):
    """Requeue parked rows (needs_you/failed) so a fixed wizard can retry."""
    settings, profile, targets, answers, tracker, notifier = shared()
    picked = tracker.retry_rows(args.site, reason=args.reason,
                                ids=list(args.id) if args.id else None)
    if not picked:
        print("nothing to retry (no needs_you/failed rows match)")
        return
    for r in picked:
        print(f"  requeued [{r['id']}] {r['title'][:60]}")
    notifier.send(f"[{args.site}] retry: {len(picked)} row(s) re-queued")
    print(f"{len(picked)} row(s) back in the queue — run: "
          f".venv\\Scripts\\python.exe run_apply.py run {args.site}")


def cmd_validate(args):
    """Sweep the front of the queue for dead/expired listings."""
    settings, profile, targets, answers, tracker, notifier = shared()
    bot, tracker, notifier = get_bot(args.site, settings=settings, profile=profile,
                                     targets=targets, answers_lib=answers,
                                     tracker=tracker, notifier=notifier)
    headless = bool(settings.get("run", {}).get("headless", False))
    pw, ctx, page = open_session(args.site, headless)
    try:
        n = bot.validate_dead_links(page, limit=args.limit)
    finally:
        _safe_close(ctx, pw)
    print(f"checked {n} queued row(s) for {args.site} "
          f"(dead ones were marked skipped)")


def cmd_reconcile(args):
    """Audit 'applied' rows against the site's own applied state."""
    settings, profile, targets, answers, tracker, notifier = shared()
    sites = [args.site] if getattr(args, "site", None) else \
        ["naukri", "wellfound", "indeed", "glassdoor", "simplyhired"]
    tally = {"confirmed": 0, "reset": 0, "unknown": 0}
    for site in sites:
        rows = tracker.con.execute(
            "SELECT id, title, url FROM jobs WHERE source=? AND status='applied'",
            (site,)).fetchall()
        if not rows:
            continue
        bot, tracker, notifier = get_bot(site, settings=settings, profile=profile,
                                         targets=targets, answers_lib=answers,
                                         tracker=tracker, notifier=notifier)
        headless = bool(settings.get("run", {}).get("headless", False))
        pw, ctx, page = open_session(site, headless)
        try:
            for job in rows:
                try:
                    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(3000)
                    verdict = bot.reconcile_check(page, job)
                except Exception:
                    verdict = None
                if verdict is True:
                    tracker.add_note(job["id"], "reconciler: confirmed on site")
                    tally["confirmed"] += 1
                    print(f"  confirmed {site}: {job['title'][:52]}")
                elif verdict is False:
                    tracker.set_status(job["id"], "needs_you",
                                       "reconciler: site shows NO applied state — verify")
                    tally["reset"] += 1
                    print(f"  RESET {site}: {job['title'][:52]} -> needs_you")
                else:
                    tally["unknown"] += 1
                    print(f"  unclear {site}: {job['title'][:52]}")
        finally:
            _safe_close(ctx, pw)
    notifier.send(f"reconcile done: {tally['confirmed']} confirmed, "
                  f"{tally['reset']} reset to needs_you, {tally['unknown']} unclear")
    print(tally)


def cmd_report(args):
    settings, profile, targets, answers, tracker, notifier = shared()
    stats = tracker.stats()
    print("== Tracker report ==")
    for site, s in sorted(stats.items()):
        pretty = ", ".join(f"{k}:{v}" for k, v in sorted(s.items()))
        print(f"  {site:<12} {pretty}")
    print(f"  applied today: {tracker.applied_today(None)}")
    recent = tracker.recent(days=2)
    print(f"  recent {min(8, len(recent))} of {len(recent)} updated in last 2d:")
    for r in recent[:8]:
        print(f"    {r['status']:<9} {r['title'][:46]:<46} {r['source']}")


def main():
    ap = argparse.ArgumentParser(prog="run_apply.py",
                                 description="Job application workflow")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="sanity check + create dirs").set_defaults(fn=cmd_init)

    p = sub.add_parser("login"); p.add_argument("site"); p.set_defaults(fn=cmd_login)

    p = sub.add_parser("fetch"); p.add_argument("site")
    p.add_argument("--title", action="append", help="extra title for THIS fetch only")
    p.add_argument("--min-score", type=int, default=None,
                   help="override targets.min_score for this run (calibration)")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("apply"); p.add_argument("site")
    p.add_argument("--max", type=int, default=None, help="cap apps this run")
    p.add_argument("--force", action="store_true", help="ignore business hours")
    p.add_argument("--no-learn", action="store_true",
                   help="don't prompt for unknown screening questions")
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("run"); p.add_argument("site")
    p.add_argument("--title", action="append")
    p.add_argument("--max", type=int, default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--min-score", type=int, default=None)
    p.add_argument("--no-learn", action="store_true",
                   help="don't prompt for unknown screening questions")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("calibrate"); p.add_argument("site")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_calibrate)

    sub.add_parser("rescore",
                   help="re-apply targets.yaml to the existing queue (no browser)"
                   ).set_defaults(fn=cmd_rescore)

    sub.add_parser("report").set_defaults(fn=cmd_report)

    p = sub.add_parser("retry",
                       help="requeue parked rows (needs_you/failed) for another attempt")
    p.add_argument("site")
    p.add_argument("--reason", default=None,
                   help="only rows whose note contains this text")
    p.add_argument("--id", type=int, action="append", default=None,
                   help="specific row id(s); repeatable")
    p.set_defaults(fn=cmd_retry)

    p = sub.add_parser("validate",
                       help="sweep the front of the queue for dead listings")
    p.add_argument("site")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("reconcile",
                       help="audit applied rows against the site's own state")
    p.add_argument("site", nargs="?", default=None)
    p.set_defaults(fn=cmd_reconcile)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
