"""Shared site-bot framework.

BaseSiteBot owns:
  * quota math (per-site cap + global daily cap + business hours)
  * human-like pacing
  * the fetch contract: `fetch_new_jobs(page)` (browser session passed in,
    since some sites must be scraped through a real browser profile)
  * the apply engine: pulls queued jobs in score order, hands each to the
    subclass `apply_job()`; typically a small wrapper around `wizard()`
    which drives: find Apply -> click -> login guard -> fill sweep ->
    declaration ticks -> resume upload -> leftover check -> Next/Submit,
    with screenshots + needs_you pings for anything it cannot handle.
  * generic form helpers (field collection, profile mapping, radio groups)

Subclasses implement: fetch_new_jobs() and apply_job().
LinkedIn stays MANUAL forever and is never part of this framework's apply
engine (user decision).
"""
import datetime as dt
import random
import re
import time
from pathlib import Path

from playwright.sync_api import Page

from ..answers import AnswerLibrary
from ..notify import Notifier
from ..tracker import Tracker

ROOT = Path(__file__).resolve().parents[2]
SHOT_DIR = ROOT / "data" / "logs" / "shots"
RESUME_PATH = ROOT / "resume" / "resume.pdf"

LOGIN_URL_RX = re.compile(r"login|signin|sign-in|/ap/|auth", re.I)

# Human-ish context snippet (lowercase) -> profile.yaml leaf key
ALIAS_MAP = {
    "first name": "first_name", "given name": "first_name", "fname": "first_name",
    "last name": "last_name", "surname": "last_name", "lname": "last_name",
    "e-mail": "email", "email": "email",
    "phone": "phone", "mobile": "phone", "contact number": "phone",
    "city": "city",
    "linkedin": "linkedin",
    "github": "portfolio", "portfolio": "portfolio", "website": "portfolio",
    "university": "university", "college": "university", "school/institute": "university",
    "degree": "highest_degree",
    "headline": "resume_headline",
    "zip": "city", "postal": "city",  # crude but better than empty
}

# JS: collect every fillable control with the context needed to map it.
# Text/select/textarea that are still empty; radio groups grouped by name.
COLLECT_JS = """
() => {
  const els = [...document.querySelectorAll('input, select, textarea')]
    .filter(e => e.offsetParent !== null);
  const cells = [];
  const radios = {};
  els.forEach(el => {
    const type = (el.type || '').toLowerCase();
    let label = '';
    try {
      const lbl = (el.id && document.querySelector(`label[for=\"${el.id}\"]`)) || el.closest('label');
      label = lbl ? lbl.textContent.trim().slice(0, 160) : '';
    } catch (e) {}
    let near = '';
    try {
      const host = el.closest('fieldset, .field, .form-group, section, li, td, div');
      near = host ? host.textContent.replace(/\\s+/g, ' ').trim().slice(0, 240) : '';
    } catch (e) {}

    if (type === 'radio') {
      const g = (radios[el.name] = radios[el.name] || {
        tag: 'radio-group', name: el.name, idx: cells.length,
        question: (near.slice(0, 240)) || label,
        options: [],
        required: !!(el.required || el.getAttribute('aria-required') === 'true'),
      });
      g.options.push({ value: el.value, label: '' + (label || el.value),
                       option_idx: cells.length });
      return;
    }
    if (['hidden','password','submit','button','file','image','reset'].includes(type)) return;
    if (type === 'checkbox') return;             // checkboxes handled separately
    if (el.value && el.tagName !== 'SELECT') return;  // already filled

    const cell = {
      tag: el.tagName.toLowerCase(), type, name: el.name || '', id: el.id || '',
      placeholder: el.placeholder || '',
      required: !!(el.required || el.getAttribute('aria-required') === 'true'),
      label, near, idx: cells.length,
    };
    if (cell.tag === 'select') cell.options = [...el.options].map(o => o.textContent.trim());
    cells.push(cell);
  });
  Object.values(radios).forEach(g => cells.push(g));
  return cells;
}
"""

# JS: after a fill sweep, list required-but-still-unhandled controls.
REQUIRED_JS = """
() => {
  const bad = [];
  const done = new Set();
  [...document.querySelectorAll('input, select, textarea')].forEach(el => {
    if (el.offsetParent === null) return;
    const t = (el.type || '').toLowerCase();
    if (t === 'radio') {
      if (done.has(el.name)) return;
      const group = [...document.querySelectorAll(`input[type=radio][name=\"${el.name}\"]`)]
        .filter(g => g.offsetParent !== null);
      done.add(el.name);
      const req = group.some(g => g.required || g.getAttribute('aria-required') === 'true');
      if (req && !group.some(g => g.checked)) {
        let q = '';
        try { const l = group[0].closest('label'); q = l ? l.textContent.trim() : '';
              if (!q) { const host = group[0].closest('fieldset, .field, section, li, div');
                        q = host ? host.textContent.replace(/\\s+/g,' ').trim().slice(0,80) : ''; } } catch (e) {}
        bad.push((q || el.name || 'choice') + ' [radio]');
      }
      return;
    }
    if (['hidden','submit','button','file','image','reset','checkbox'].includes(t)) return;
    const req = !!(el.required || el.getAttribute('aria-required') === 'true');
    if (req && !el.value) {
      let label = '';
      try { const l = (el.id && document.querySelector(`label[for=\"${el.id}\"]`)) || el.closest('label');
            label = l ? l.textContent.trim().slice(0, 80) : ''; } catch (e) {}
      bad.push(label || el.name || el.id || 'field');
    }
  });
  return bad.slice(0, 8);
}
"""


def posted_date_from_text(raw: str) -> str:
    """Relative date chips ('3 days ago', '2 weeks ago', 'today') -> ISO date
    so the scorer's freshness/max_age_days checks actually work."""
    l = (raw or "").lower()
    today = dt.date.today()
    if re.search(r"\bfew hours?\b|\bjust now\b|\btoday\b", l):
        return today.isoformat()
    m = re.search(r"(\d+)\s*hour", l)
    if m:
        return today.isoformat()
    for rx, mult in ((r"(\d+)\s*day", 1), (r"(\d+)\s*week", 7), (r"(\d+)\s*month", 30)):
        m = re.search(rx, l)
        if m:
            return (today - dt.timedelta(days=int(m.group(1)) * mult)).isoformat()
    return ""


def _flatten(d):
    """Leaf-key -> value (keys lowercased) across nested dicts."""
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            out.update(_flatten(v))
        else:
            out[str(k).lower()] = v
    return out


class BaseSiteBot:
    name = "base"
    home = ""
    # subclass knobs ---------------------------------------------------------
    NEXT_PATTERNS = [r"^next$", r"^continue$", r"^next step"]   # try BEFORE submit
    SUBMIT_PATTERNS = [r"^submit application$", r"^submit", r"^apply now$", r"^apply$"]
    APPLY_LOCATORS = ('button:has-text("Apply")', 'a:has-text("Apply")',
                      'button:has-text("Apply now")', 'a:has-text("Apply now")')
    CONFIRM_HINTS = ("thank you for your interest", "application has been",
                     "application submitted", "thank you!", "successfully applied",
                     "your application", "have applied")
    DECLARATION_RX = None          # subclass regex -> auto-tick declaration boxes
    EXTERNAL_APPLY_GUARD = False   # if True: leaving our domain = needs_you

    def __init__(self, cfg: dict, *, tracker: Tracker, settings: dict,
                 profile: dict, targets: dict, answers: AnswerLibrary,
                 notifier: Notifier):
        self.cfg = cfg or {}
        self.tracker = tracker
        self.settings = settings
        self.profile = profile
        self.targets = targets
        self.answers = answers
        self.notify = notifier
        self._leaf_index = _flatten(profile)
        SHOT_DIR.mkdir(parents=True, exist_ok=True)

    # ================================================================== quota
    @property
    def daily_cap(self) -> int:
        return int(self.cfg.get("daily_cap", 0))

    def quota_left(self) -> int:
        if not self.cfg.get("enabled", False):
            return 0
        site_left = self.daily_cap - self.tracker.applied_today(self.name)
        total_cap = int((self.settings.get("limits", {}) or {}).get("daily_total_cap", 0))
        total_left = total_cap - self.tracker.applied_today(None)
        return max(0, min(site_left, total_left))

    def in_business_hours(self) -> bool:
        r = self.settings.get("run", {}) or {}
        if not r.get("business_hours_only", True):
            return True
        h = dt.datetime.now().hour
        return int(r.get("start_hour", 9)) <= h < int(r.get("end_hour", 19))

    def human_pause(self, lo=None, hi=None):
        p = self.settings.get("pacing", {}) or {}
        lo = float(lo if lo is not None else p.get("min_action_delay_sec", 4))
        hi = float(hi if hi is not None else p.get("max_action_delay_sec", 10))
        time.sleep(random.uniform(lo, hi))

    # ========================================================= status/audit
    def shot(self, page: Page, tag: str) -> str:
        try:
            path = SHOT_DIR / f"{self.name}_{tag}_{int(time.time())}.png"
            page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:
            return ""

    def safe_shot(self, page: Page, tag: str) -> str:
        try:
            return self.shot(page, tag)
        except Exception:
            return ""

    # ================================================================ apply
    def apply(self, page: Page, max_apps: int | None = None):
        """Main entry: walk the queue and submit applications."""
        quota = self.quota_left()
        if max_apps:
            quota = min(quota, max_apps)
        if quota <= 0:
            self.notify.send(f"[{self.name}] no quota left today "
                             f"(applied today: {self.tracker.applied_today(self.name)}/{self.daily_cap})")
            return
        jobs = self.tracker.queued(source=self.name, limit=quota)
        self.notify.send(f"[{self.name}] apply run: {len(jobs)} queued, quota {quota}")
        done = 0
        for job in jobs:
            if not self.quota_left():
                break
            if not self.in_business_hours():
                self.notify.send(f"[{self.name}] outside business hours — stopping")
                break
            app_id = self.tracker.start_application(job["id"])
            try:
                status, note, shot = self.apply_job(page, job)
            except Exception as e:
                status, note = "failed", f"exception: {type(e).__name__}: {e}"
                shot = self.safe_shot(page, f"err_{job['id']}")
                self.notify.send(f"[{self.name}] EXCEPTION on '{(job['title'] or '')[:40]}': {e}",
                                 toast=True)
            self.tracker.finish_application(app_id, status, note, shot)
            self.tracker.set_status(job["id"], status, note)
            self.notify.send(f"[{self.name}] {status.upper()} — '{(job['title'] or '')[:48]}' "
                             f"@ {(job['company'] or '')[:20]}" + (f" | {note}" if note else ""))
            done += 1
            self.human_pause()
        self.notify.send(f"[{self.name}] apply run finished: {done} processed "
                         f"(applied today: {self.tracker.applied_today(self.name)})")

    # ==================================================== profile mapping
    def profile_value_for(self, context: str) -> str | None:
        ctx_l = (context or "").lower()
        for alias, key in ALIAS_MAP.items():
            if alias in ctx_l:
                v = self._leaf_index.get(key)
                if v is not None and str(v).strip():
                    return str(v)
        return None

    def infer_screener(self, question: str) -> str | None:
        """Standard screeners answered straight from the profile."""
        q = (question or "").lower()
        if "authorized" in q or "legally" in q or "eligible" in q:
            return self._s((self.profile.get("work_auth") or {}).get("legally_authorized")) or "Yes"
        if "sponsorship" in q or "visa" in q:
            return self._s((self.profile.get("work_auth") or {}).get("require_sponsorship")) or "No"
        if "relocat" in q:
            return self._s((self.profile.get("experience") or {}).get("willing_to_relocate"))
        if "notice" in q:
            return self._s((self.profile.get("experience") or {}).get("notice_period"))
        if "salary" in q or "compensation" in q or "ctc" in q:
            return self._s((self.profile.get("experience") or {}).get("expected_salary"))
        if "currently employed" in q or "currently working" in q:
            return self._s((self.profile.get("experience") or {}).get("currently_employed"))
        return None

    @staticmethod
    def _s(v) -> str | None:
        return str(v).strip() if v is not None and str(v).strip() else None

    def answer_for(self, question: str) -> str | None:
        """Answers library first (curated), then profile inference."""
        return self.answers.match(question) or self.infer_screener(question)

    # ======================================================= form helpers
    @staticmethod
    def collect(page: Page):
        return page.evaluate(COLLECT_JS)

    @staticmethod
    def required_leftover(page: Page) -> list[str]:
        try:
            return page.evaluate(REQUIRED_JS)
        except Exception:
            return []

    def _resolve_form(self, page: Page):
        """Where the apply form actually lives: main page by default; sites
        whose apply flow is embedded in a child iframe override this."""
        return page

    @staticmethod
    def _selector(cell: dict) -> str:
        if cell.get("id"):
            return f"#{cell['id']}"
        if cell.get("name"):
            n = str(cell["name"]).replace('"', '\\"')
            return f'[name="{n}"]'
        return ""

    def set_value(self, page: Page, cell: dict, value: str) -> bool:
        sel = self._selector(cell)
        if not sel:
            return False
        loc = page.locator(sel).first
        try:
            if cell["tag"] == "select":
                opts = cell.get("options") or []
                match = next((o for o in opts if o.strip().lower() == value.strip().lower()), None)
                if not match:
                    match = next((o for o in opts
                                  if value.strip().lower() in o.strip().lower()), None)
                if not match:
                    return False
                loc.select_option(label=match, timeout=4000)
                return True
            loc.fill(value, timeout=4000)
            return True
        except Exception:
            try:  # JS fallback for stubborn widgets
                page.eval_on_selector(sel, """(el, v) => {
                    el.value = v;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""", value)
                return True
            except Exception:
                return False

    @staticmethod
    def click_radio(page: Page, group: dict, answer: str) -> bool:
        a = (answer or "").strip().lower()
        if not a:
            return False
        for opt in group.get("options", []):
            lbl = (opt.get("label") or "").strip().lower()
            if a == lbl or a in lbl or lbl in a:
                name = str(group["name"]).replace('"', '\\"')
                val = str(opt.get("value") or "").replace('"', '\\"')
                sel = f'input[name="{name}"][value="{val}"]'
                try:
                    page.check(sel, timeout=3000)
                    return True
                except Exception:
                    continue
        return False

    @staticmethod
    def upload_resume_once(page: Page) -> bool:
        if not RESUME_PATH.exists():
            return False
        try:
            loc = page.locator("input[type=file]").first
            if loc.count() == 0:
                return False
            loc.set_input_files(str(RESUME_PATH), timeout=5000)
            return True
        except Exception:
            return False

    @staticmethod
    def click_matching(page: Page, patterns: list[str]) -> str | None:
        """Click the first visible clickable matching any pattern (regex), in order.

        Order matters: Next/Continue must be tried BEFORE submit so wizard
        steps are never skipped.
        """
        import re as _re
        for pat in patterns:
            try:
                rx = _re.compile(pat, _re.I)
                loc = page.get_by_role("button", name=rx).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=3000)
                    return pat
                subs = page.locator("input[type=submit]")
                for i in range(min(subs.count(), 6)):
                    el = subs.nth(i)
                    if el.is_visible() and rx.search(el.get_attribute("value") or ""):
                        el.click(timeout=3000)
                        return pat
                link = page.locator("a").filter(has_text=rx).first
                if link.count() and link.is_visible():
                    link.click(timeout=3000)
                    return pat
            except Exception:
                continue
        return None

    @staticmethod
    def find_apply(page: Page, locators, role_rx=None):
        for sel in locators:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                continue
        if role_rx:
            try:
                loc = page.get_by_role("button", name=role_rx).first
                if loc.count() and loc.is_visible():
                    return loc
                loc2 = page.get_by_role("link", name=role_rx).first
                if loc2.count() and loc2.is_visible():
                    return loc2
            except Exception:
                pass
        return None

    # ============================================================ wizard
    def tick_declarations(self, page: Page) -> int:
        """Auto-tick consent/authorization check-boxes if the site asks for them."""
        if not self.DECLARATION_RX:
            return 0
        n = 0
        try:
            boxes = page.locator("input[type=checkbox]:visible")
            for i in range(min(boxes.count(), 10)):
                box = boxes.nth(i)
                if box.is_checked():
                    continue
                txt = ""
                try:
                    lbl = (box.evaluate("el => (el.closest('label')||el.closest('div'))?.textContent") or "")
                    txt = re.sub(r"\s+", " ", lbl).lower()
                except Exception:
                    pass
                if txt and self.DECLARATION_RX.search(txt):
                    box.check(timeout=2000)
                    n += 1
                    self.human_pause(1.0, 2.5)
        except Exception:
            pass
        return n

    def _fill_pass(self, page: Page) -> bool:
        """One sweep: fill everything mappable. Returns True if anything filled."""
        progress = False
        for cell in self.collect(page):
            if cell.get("tag") == "radio-group":
                ans = self.answer_for(cell.get("question") or "")
                if ans and self.click_radio(page, cell, ans):
                    progress = True
                    self.human_pause(1.5, 3.5)
                continue
            ctx = " | ".join(x for x in (cell.get("label"), cell.get("placeholder"),
                                         cell.get("name"), cell.get("id")) if x)
            zone = " ".join(x for x in (cell.get("near"), cell.get("label")) if x)
            value = self.profile_value_for(ctx)
            if not value:
                value = self.answer_for(zone)
            if cell.get("tag") == "textarea" and not value:
                blob = self.answers.blobs.get("cover_letter") or ""
                value = self.answers.resolve(blob) if blob else None
            if value and self.set_value(page, cell, value):
                progress = True
                self.human_pause(1.5, 3.5)
        return progress

    def _login_needed(self, page: Page) -> bool:
        try:
            if re.search(LOGIN_URL_RX, (page.url or "")):
                return True
            return page.locator("input[type=password]:visible").count() > 0
        except Exception:
            return False

    def logged_in_signal(self, page: Page) -> bool | None:
        """Optional per-site override: extra evidence login is complete.

        None = unknown; bool otherwise. The wait considers login done when
        url/password checks clear AND signal is not False.
        """
        return None

    def await_manual_login(self, page: Page) -> bool:
        minutes = int((self.settings.get("limits", {}) or {}).get("login_manual_wait_min", 10))
        self.notify.send(f"ACTION NEEDED — log in to {self.name} in the OPENED browser "
                         f"window (waiting up to {minutes} min)", toast=True)
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            page.wait_for_timeout(5000)
            try:
                if (re.search(LOGIN_URL_RX, page.url or "")
                        or page.locator("input[type=password]:visible").count() > 0):
                    continue
                if self.logged_in_signal(page) is False:
                    continue
                return True
            except Exception:
                continue
        return False

    def _at_confirmation(self, page: Page) -> bool:
        try:
            content = page.content().lower()
        except Exception:
            return False
        url_l = (page.url or "").lower()
        if re.search(r"confirmation|complete", url_l):
            return True
        return any(h in content for h in self.CONFIRM_HINTS)

    def wizard(self, page: Page, jid, apply_loc) -> tuple[str, str, str]:
        """Generic apply flow from the Apply click to submission/confirmation.

        `page` = the root page (screenshots, url checks, confirm scans);
        the actual form target is resolved per-iteration via _resolve_form()
        so iframe-hosted flows work too.
        """
        if apply_loc:
            try:
                apply_loc.click(timeout=5000)
            except Exception as e:
                return ("needs_you", f"apply click failed: {type(e).__name__}",
                        self.safe_shot(page, f"clickfail_{jid}"))
            page.wait_for_timeout(random.randint(1500, 3000))
            if self.EXTERNAL_APPLY_GUARD:
                host = re.match(r"https?://([^/]+)", page.url or "").group(1) \
                    if re.match(r"https?://", page.url or "") else ""
                if host and re.sub(r"^www\.", "", host) != re.sub(r"^www\.", "",
                                                                  self.home.split("//")[-1]):
                    return ("needs_you", f"external apply flow ({host})",
                            self.safe_shot(page, f"external_{jid}"))
        if self._login_needed(page):
            if not self.await_manual_login(page):
                return ("needs_you", "manual login required (timed out)",
                        self.safe_shot(page, f"login_{jid}"))
        max_steps = int((self.settings.get("limits", {}) or {}).get("max_wizard_steps", 30))
        stall = 0
        for _step in range(max_steps):
            target = self._resolve_form(page)
            progress = self._fill_pass(target)
            self.tick_declarations(target)
            self.upload_resume_once(target)
            leftover = self.required_leftover(target)
            if leftover and not progress:
                return ("needs_you", "needs your input: " + "; ".join(leftover[:4]),
                        self.safe_shot(page, f"stuck_{jid}"))
            if leftover and progress:
                stall += 1
                if stall > 2:
                    return ("needs_you", "partial: " + "; ".join(leftover[:3]),
                            self.safe_shot(page, f"stall_{jid}"))
            self.human_pause()
            clicked = self.click_matching(target, self.NEXT_PATTERNS)
            if not clicked:
                clicked = self.click_matching(target, self.SUBMIT_PATTERNS)
            if not clicked:
                if self._at_confirmation(page) or self._at_confirmation_safe(target):
                    return ("applied", "", "")
                if not leftover and not self._form_present(target):
                    return ("applied", "form closed without confirmation", "")
                return ("needs_you", "no next/submit button found",
                        self.safe_shot(page, f"nobtn_{jid}"))
            page.wait_for_timeout(random.randint(1500, 3500))
            if self._at_confirmation(page) or self._at_confirmation_safe(target):
                return ("applied", "", "")
            if "submit" in str(clicked).lower():
                if not self._form_present(self._resolve_form(page)) \
                        and not self.required_leftover(self._resolve_form(page)):
                    return ("applied", "submitted (form closed)", "")
                return ("needs_you", "verify submission manually",
                        self.safe_shot(page, f"verify_{jid}"))
        return ("needs_you", f"wizard exceeded {max_steps} steps",
                self.safe_shot(page, f"long_{jid}"))

    def _at_confirmation_safe(self, target) -> bool:
        try:
            content = (target.content() or "").lower()
        except Exception:
            return False
        return any(h in content for h in self.CONFIRM_HINTS)

    def _form_present(self, page: Page) -> bool:
        try:
            return page.locator("input, select, textarea").count() >= 2
        except Exception:
            return False

    # ========================================================= to subclass
    def fetch_new_jobs(self, page: Page) -> int:
        raise NotImplementedError

    def apply_job(self, page: Page, job) -> tuple[str, str, str]:
        raise NotImplementedError
