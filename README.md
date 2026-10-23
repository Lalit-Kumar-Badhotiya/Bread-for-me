# Bread-for-me — Job Application Workflow (₹0, local, human-in-the-loop)

Automated job-application assistants for Naukri (the volume engine) and the
Indeed group (indeed, glassdoor, simplyhired — one shared budget), with a
scoring engine that decides **which** roles to apply to, a cross-site dedupe
tracker, a self-learning screener-answer library, and rate-capped human-like
apply runs.

Wellfound was **removed 2026-10-19**: ~180 fetches yielded 0 applications —
the site skews to US/EU-remote startups, and the only postings that reached
the form were location-ineligible for an India profile ("This job does not
support the locations or timezones"). The code is still here, disabled via
`enabled: false` in `config/settings.yaml`.

**LinkedIn is manual by design, forever.** The bot may queue LinkedIn jobs for
you in later phases, but no automation ever touches a LinkedIn session.
**Amazon was removed from scope** and is not part of anything here.

## Principles

1. **₹0 stack:** Python + Playwright + SQLite + free desktop notifications.
2. **Human-in-the-loop:** CAPTCHA / OTP / unknown screening question / external
   apply flow → the app pauses as `needs_you`, pings you (desktop toast /
   optional Telegram), saves a screenshot, and never blind-submits. A Cloudflare
   "verify you are human" page gets a real wait window (toast + up to
   `captcha_manual_wait_min`) so you can click it while the run is watching.
3. **Low & slow:** daily caps per site + a global cap, business-hours-only
   runs, randomized human-like delays. Aggressive autopilots get accounts
   flagged — this is built to age well.
4. **Your Gmail stays safe:** nothing in this pipeline ever sends outreach
   email. Site portals and replies to humans only.

## Setup (one-time)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\playwright.exe install chromium
```

1. `copy config\profile.example.yaml config\profile.yaml` and fill it in
   (gitignored — your data stays local).
2. `copy config\answers.example.yaml config\answers.yaml` (it will learn
   additional screeners automatically over time).
3. Put your resume at `resume\resume.pdf`.
4. Fill `config\targets.yaml`: titles, must-have skills, locations, etc.
5. One-time login per site you use:
   `python run_apply.py login naukri` → a browser opens, log in (incl. OTP),
   press ENTER in the terminal. Session persists in `data/state/naukri`.

## Daily use

```powershell
# look at what would be queued, tune targets.yaml until it looks right:
python run_apply.py calibrate naukri
# after editing targets.yaml, re-filter the existing queue instantly (no browser):
python run_apply.py rescore
# fetch + apply in one go (capped, business hours, headed browser):
python run_apply.py run naukri
# or separately:
python run_apply.py fetch naukri
python run_apply.py apply naukri --max 5
# stuck on an unknown screening question? the bot asks you in the terminal,
# saves the answer to answers.yaml, and retries that application automatically
# (use --no-learn with apply/run to disable this for unattended runs)
# stats:
python run_apply.py report
# recovery + audit tools:
python run_apply.py retry naukri                    # requeue parked needs_you/failed rows
python run_apply.py retry indeed --id 123 --id 124  # or just specific rows
python run_apply.py validate naukri                 # sweep top of queue for dead listings
python run_apply.py reconcile                       # audit every applied row against the live site
python run_apply.py run-all                         # every site in sequence, one summary
python run_apply.py questions                       # answer parked screening questions (learns)
python run_apply.py review                          # parked jobs grouped by reason (+ links)
python run_apply.py export --open                   # applied jobs -> CSV + HTML
python -m unittest discover -s tests -t . -v        # offline rule tests (no browser, no site)
```

Idempotency guard: before clicking Apply the bot checks whether the site
already shows an "Applied" state (crash between submit and record, or a manual
application) and records it instead of re-submitting. Applied verdicts need a
strong confirmation phrase (or the site's own applied state) — never just the
absence of a form. `reconcile` double-checks every applied row against the
site's own state and moves anything the site does not confirm to `needs_you`.
Long sessions recycle the browser every `apply_chunk_size` jobs, each apply
flow has a wall-clock budget (`per_job_budget_sec` -> needs_you), and `run`
sweeps the top of the queue for dead listings after each fetch. Unanswered
screening questions from `--no-learn` runs are parked in
`data/logs/unknown_questions.jsonl` for later batch-answering.
A circuit breaker (`circuit_breaker_streak`) stops a run after N consecutive
*unexpected* `needs_you`/`failed` jobs instead of burning through the whole
queue — expected parks (external apply flows, parked questions, captcha
waits) don't count toward it. The
apply loop skips near-duplicate cross-postings (same company+title on another
site already applied to, or a stronger queued copy). `tracker.db` is backed up
to `data/backups/` before mutating runs (newest 10 kept), zombie browser
processes holding a site profile are cleared automatically at session start,
and `needs_you`/`failed` pages are saved as HTML next to their screenshots
(`data/logs/shots/`) for offline debugging.

Quiet runs: `run.browser_window: minimized` (the default) launches the
browser straight into the taskbar — no window popping over your monitors.
It restores itself only where a human is strictly required (login /
Cloudflare "verify you are human") and goes back to the taskbar afterwards;
throttle-off flags keep pages fully active while minimized (there is a
~1s window flash at launch, since Playwright cannot spawn pre-minimized).
Set `browser_window: normal` for the old always-visible behavior, and keep
`headless: false` either way — headless windows can't be watched or repaired.

First 3–5 days: run `fetch` + `calibrate` only (no `apply`) and correct the
queue — your rejects become new `exclude_keywords`. Then start `apply`.

## Where things live

| Path | What |
|---|---|
| `run_apply.py` | CLI entry |
| `bot/main.py` | commands + session plumbing |
| `bot/tracker.py` | SQLite: jobs, applications, audit log, dedupe keys |
| `bot/scorer.py` | targets.yaml → 0–100 score → queued/skipped |
| `bot/answers.py` | screener Q/A library (learns from your answers) |
| `bot/sites/base.py` | wizard engine + form filling shared by all sites |
| `bot/sites/naukri.py` | Naukri fetch + apply (incl. the recruiter-chat apply flow) |
| `tests/` | offline rule tests (scorer, answers, tracker, site helpers) |
| `data/` | tracker.db, browser sessions, screenshots (gitignored) |
| `data/logs/shots/` | screenshots for every `needs_you` pause |

Naukri apply quirks handled automatically: newer postings open a scripted
recruiter chat instead of a form. The bot hands the resume over through its
"Upload Resume" chip (setting the hidden file input directly gets rejected by
the site), answers chips / radio options / free-text questions from
`answers.yaml` + `profile.yaml`, tolerates the page reloads the chat triggers,
and presses the chat's Save only once it unlocks. An answer the chat rejects
("The input seems invalid.") is retried with a variant (e.g. `0` -> `0.0` ->
`Fresher`), then parked for you if the chat keeps refusing. Postings that
route applications through a web form (Google/Microsoft Forms, Typeform,
JotForm, Tally, Zoho, Airtable — common with consultancy listings) are parked
as external flows **with the link** instead of being chased. Questions the
bot cannot answer return `needs_you` with the exact wording, so the screener
learning loop can capture them for next time.

Indeed-group cards are often sparse (no experience, no skills, sometimes not
even the company). With `verify_on_detail: true` for such a site, the rows
queue tentatively and get a final check on the full job page right before
applying: if the page states more years than your band allows, or never
mentions a must-have skill, the job is skipped and never submitted
(screenshot saved).

## Status / roadmap

- [x] P0: scaffolding, tracker, scorer, answers, notifier, configs
- [x] P1: Naukri bot (fetch + apply)
- [x] P2: Wellfound bot — REMOVED 2026-10-19 (international skew, 0 applies;
      re-enable in settings.yaml if you ever want it back)
- [x] P3: Indeed group (indeed, glassdoor, simplyhired — shared daily budget)
- [x] LinkedIn: manual forever (by your decision) — never automated
- [x] Tests: offline rule suite (`tests/`, stdlib unittest)

## Notes on the rules

This automates **your own applications with your own credentials**, at low
volume, with pauses for anything sensitive. It is not scraping-with-rotation
or mass-posting infra. Keep caps conservative; you can always raise them.
