"""Naukri.com site bot — current top-priority source.

FETCH: browser session visits naukri.com/<slug>-jobs for each target title
(the guest listing pages are public) and harvests job cards; the scorer
decides queued vs skipped.

APPLY: job detail page -> Apply button -> base wizard engine. Naukri quirk:
many cards are actually "apply on company site" external flows — those come
back as needs_you with the link so you never apply twice by accident.
Login (email/OTP) is manual-once, then the session persists.
"""
import re
import time

from ..scorer import score_job
from .base import BaseSiteBot, Page

CONFIRM_HINTS_EXTRA = ("you have applied", "your application has been sent",
                       "applied successfully", "has been sent to")

APPLY_LOCATORS = (
    'button:has-text("Apply")',
    'a:has-text("Apply")',
    'button:has-text("Apply now")',
    'a:has-text("Apply now")',
    '[data-qa*="apply"]',
)

GATHER_JS = """
() => {
  const out = new Map();
  document.querySelectorAll('a[href*="job-listings-"]').forEach(a => {
    const raw = a.href || a.getAttribute('href') || '';
    const clean = (raw.startsWith('http') ? raw : location.origin + raw).split('?')[0];
    if (!out.has(clean)) {
      let box = '';
      try {
        const host = a.closest('article')
          || a.closest('[class*="job-tuple"]')
          || a.closest('[class*="cust-job"]')
          || a.parentElement.parentElement
          || a.parentElement;
        box = (host && host.innerText || '').trim().slice(0, 600);
      } catch (e) {}
      out.set(clean, { url: clean, box: box });
    }
  });
  return [...out.values()];
}
"""


def slugify(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (t or "").lower()).strip("-")


class NaukriBot(BaseSiteBot):
    name = "naukri"
    home = "https://www.naukri.com"

    # wizard knobs ---------------------------------------------------------
    APPLY_LOCATORS = APPLY_LOCATORS
    NEXT_PATTERNS = [r"^next$", r"^save and continue$"]
    SUBMIT_PATTERNS = [r"^submit application$", r"^submit$"]
    DECLARATION_RX = re.compile(r"authoriz|consent|agree|accept|undertak|terms", re.I)
    EXTERNAL_APPLY_GUARD = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.CONFIRM_HINTS = tuple(self.CONFIRM_HINTS) + CONFIRM_HINTS_EXTRA

    # ================================================================== FETCH
    def fetch_new_jobs(self, page: Page) -> int:
        titles = [t.strip() for t in (self.targets.get("titles") or []) if t.strip()]
        if not titles:
            self.notify.send("[naukri] no titles in config/targets.yaml — add one first",
                             toast=True)
            return 0
        max_titles = int(self.cfg.get("max_titles_per_run", 3))
        total = 0
        for title in titles[:max_titles]:
            slug = slugify(title)
            if not slug:
                continue
            url = f"{self.home}/{slug}-jobs"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                self.notify.send(f"[naukri] goto failed ({url}): {type(e).__name__}")
                continue
            # clouds/interstitials: give the listing anchor some time to appear
            try:
                page.wait_for_selector('a[href*="job-listings-"]', timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            self._dismiss_overlays(page)
            items = page.evaluate(GATHER_JS)
            self.notify.send(f"[naukri] '{title}': {len(items)} cards harvested")
            if not items:
                head = ""
                try:
                    head = page.content()[:200].lower()
                except Exception:
                    pass
                self.notify.send(f"[naukri] 0 cards for '{title}' | page head: {head!r}")
                continue
            new = 0
            for item in items:
                if self._classify(item):
                    new += 1
            total += new
            time.sleep(3.0)
        return total

    def _classify(self, item: dict) -> bool:
        raw = (item.get("box") or "").strip()
        url = (item.get("url") or "").strip()
        if not url:
            return False
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        title = (lines[0] if lines else "").replace(",", "").strip()[:120]
        if not title:
            return False
        company = ""
        for l in lines[1:4]:
            if re.search(r"\d+\.\d|reviews?|yrs|ago|key skills", l, re.I):
                continue
            company = l.strip()
            break
        location = ""
        for l in lines[1:6]:
            if ("," in l or "/" in l) and not re.search(r"\d+\.\d|reviews?|skills", l, re.I):
                location = l.strip()[:80]
                break
        score, reasons, reject = score_job(title, company, location, raw, "", self.targets)
        min_score = int(self.targets.get("min_score", 70))
        status = "skipped" if (reject or score < min_score) else "queued"
        job_id = url.rstrip("/").rsplit("/", 1)[-1]
        jid, is_new = self.tracker.upsert_job(
            source=self.name, source_job_id=job_id, url=url, title=title,
            company=company, location=location, description=raw, posted_date="",
            score=score, score_reasons="; ".join(reasons), status=status)
        if not is_new:
            self.tracker.set_status(jid, status, "; ".join(reasons)[:200])
            return False
        return status == "queued"

    # ================================================================== APPLY
    def apply_job(self, page: Page, job) -> tuple[str, str, str]:
        url = job["url"] or f"{self.home}/job-listings/{job['source_job_id']}"
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        self._dismiss_overlays(page)
        apply_loc = self.find_apply(
            page, self.APPLY_LOCATORS,
            role_rx=re.compile(r"^apply( now)?$", re.I))
        if not apply_loc:
            return ("needs_you", "no apply button", self.safe_shot(page, f"noapply_{job['id']}"))
        return self.wizard(page, job["id"], apply_loc)

    # ----------------------------------------------------------- naukri bits
    def _dismiss_overlays(self, page: Page):
        for sel in (".iconR-cross", "button[aria-label*='close' i]",
                    "[class*='close'] i[class*='cross']"):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=1500)
                    page.wait_for_timeout(400)
            except Exception:
                continue

    def _login_needed(self, page: Page) -> bool:
        if super()._login_needed(page):
            return True
        try:  # login modal opened in-place (email/tel + Login CTA)
            tel_like = page.locator("input[type=email]:visible, input[type=tel]:visible, "
                                    "input[placeholder*='mail' i]:visible").count()
            login_cta = page.get_by_role("button", name=re.compile(r"^log ?in$", re.I)).count()
            return bool(tel_like and login_cta)
        except Exception:
            return False

    def logged_in_signal(self, page: Page) -> bool | None:
        try:
            n_login_cta = page.get_by_role("button",
                                           name=re.compile(r"^log ?in$", re.I)).count() \
                + page.locator("a:has-text('Login')").count()
            if n_login_cta == 0:
                return True
            return False
        except Exception:
            return None
