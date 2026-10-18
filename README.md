# Bread-for-me — Job Application Workflow (₹0, local, human-in-the-loop)

Automated job-application assistants for Naukri (now live), Wellfound and the
Indeed group (next phases), with a scoring engine that decides **which** roles
to apply to, a cross-site dedupe tracker, a self-learning screener-answer
library, and rate-capped human-like apply runs.

**LinkedIn is manual by design, forever.** The bot may queue LinkedIn jobs for
you in later phases, but no automation ever touches a LinkedIn session.
**Amazon was removed from scope** and is not part of anything here.

## Principles

1. **₹0 stack:** Python + Playwright + SQLite + free desktop notifications.
2. **Human-in-the-loop:** CAPTCHA / OTP / unknown screening question / external
   apply flow → the app pauses as `needs_you`, pings you (desktop toast /
   optional Telegram), saves a screenshot, and never blind-submitting.
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
```

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
| `data/` | tracker.db, browser sessions, screenshots (gitignored) |
| `data/logs/shots/` | screenshots for every `needs_you` pause |

Naukri apply quirks handled automatically: newer postings open a scripted
recruiter chat instead of a form. The bot hands the resume over through its
"Upload Resume" chip (setting the hidden file input directly gets rejected by
the site), answers chips / radio options / free-text questions from
`answers.yaml` + `profile.yaml`, tolerates the page reloads the chat triggers,
and presses the chat's Save only once it unlocks. Questions it cannot answer
return `needs_you` with the exact wording, so the screener learning loop can
capture them for next time.

Wellfound and Indeed cards are often sparse (no experience, no skills,
sometimes not even the company). With `verify_on_detail: true` for such a
site, the rows queue tentatively and get a final check on the full job page
right before applying: if the page states more years than your band allows,
or never mentions a must-have skill, the job is skipped and never submitted
(screenshot saved).

## Status / roadmap

- [x] P0: scaffolding, tracker, scorer, answers, notifier, configs
- [x] P1: Naukri bot (fetch + apply)
- [x] P2: Wellfound bot (fetch live-verified; one-click apply via wizard)
- [x] P3: Indeed group (indeed, glassdoor, simplyhired — shared daily budget)
- [x] LinkedIn: manual forever (by your decision) — never automated

## Notes on the rules

This automates **your own applications with your own credentials**, at low
volume, with pauses for anything sensitive. It is not scraping-with-rotation
or mass-posting infra. Keep caps conservative; you can always raise them.
