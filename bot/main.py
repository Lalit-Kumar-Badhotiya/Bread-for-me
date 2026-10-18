"""CLI orchestrator for the apply bot.

Commands:
  init            sanity-check configs/print what to fill
  login SITE      open a persistent browser session to log in manually once
  fetch SITE      pull + score + queue new jobs (no browser needed)
  apply SITE      submit queued jobs in the browser (quota-capped)
  run SITE        fetch + apply in one go
  calibrate SITE  show the current queue so you can tune targets.yaml
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
    # Phase 3+ registrations (wellfound, then indeed group incl. glassdoor &
    # simplyhired) land here. LinkedIn stays manual-by-design FOREVER and is
    # never registered or automated.
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
    """Launch persistent browser context; returns (playwright, context, page)."""
    from playwright.sync_api import sync_playwright
    state = ROOT / "data" / "state" / site
    state.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        str(state), headless=headless,
        args=["--disable-blink-features=AutomationControlled"])
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
    ctx.close()
    pw.stop()
    print(f"Session saved to data/state/{args.site} — future runs reuse it.")


def cmd_fetch(args):
    settings = load_yaml(CFG_DIR / "settings.yaml")
    bot, tracker, notifier = get_bot(args.site, extra_titles=args.title, settings=settings)
    if getattr(args, "min_score", None) is not None:
        bot.targets["min_score"] = int(args.min_score)
    headless = bool(settings.get("run", {}).get("headless", False))
    pw, ctx, page = open_session(args.site, headless)
    try:
        n = bot.fetch_new_jobs(page)
    except Exception as e:
        n = 0
        notifier.send(f"[{args.site}] fetch failed: {type(e).__name__}: {e}", toast=True)
    finally:
        ctx.close()
        pw.stop()
    queued = tracker.queued(source=args.site, limit=100000)
    notifier.send(f"[{args.site}] fetch done: {n} newly queued "
                  f"({len(queued)} in queue total)")


def cmd_apply(args):
    settings, profile, targets, answers, tracker, notifier = shared()
    bot, tracker, notifier = get_bot(args.site, settings=settings, profile=profile,
                                     targets=targets, answers_lib=answers,
                                     tracker=tracker, notifier=notifier)
    if not bot.in_business_hours() and not args.force:
        print("Outside business hours (settings.run) — use --force to override.")
        sys.exit(3)
    headless = bool(settings.get("run", {}).get("headless", False))
    pw, ctx, page = open_session(args.site, headless)
    try:
        bot.apply(page, max_apps=args.max)
    finally:
        ctx.close()
        pw.stop()


def cmd_run(args):
    settings, profile, targets, answers, tracker, notifier = shared()
    bot, tracker, notifier2 = get_bot(args.site, extra_titles=args.title,
                                      settings=settings, profile=profile,
                                      targets=targets, answers_lib=answers,
                                      tracker=tracker, notifier=notifier)
    if getattr(args, "min_score", None) is not None:
        bot.targets["min_score"] = int(args.min_score)
    if not bot.in_business_hours() and not args.force:
        print("Outside business hours (settings.run) — use --force to override.")
        sys.exit(3)
    headless = bool(settings.get("run", {}).get("headless", False))
    pw, ctx, page = open_session(args.site, headless)
    try:
        n = bot.fetch_new_jobs(page)
        bot.apply(page, max_apps=args.max)
        queued = tracker.queued(source=args.site, limit=100000)
        notifier2.send(f"[{args.site}] run done: fetched {n}, "
                       f"applied today {tracker.applied_today(args.site)}, "
                       f"queue {len(queued)}")
    finally:
        ctx.close()
        pw.stop()


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
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("run"); p.add_argument("site")
    p.add_argument("--title", action="append")
    p.add_argument("--max", type=int, default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--min-score", type=int, default=None)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("calibrate"); p.add_argument("site")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_calibrate)

    sub.add_parser("report").set_defaults(fn=cmd_report)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
