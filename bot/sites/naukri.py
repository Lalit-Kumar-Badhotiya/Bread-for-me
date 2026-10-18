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
from .base import BaseSiteBot, Page, posted_date_from_text

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


def posted_from_slug(slug_job_id: str) -> str:
    """Naukri slugs end with ...-<DDMMYY><vendor: naukri.com embeds the post
    date in the job id (e.g. ...171026015301 -> 17 Oct 26). Guaranteed fallback
    when the card text omits the date chip."""
    import datetime as _dt
    m = re.search(r"(\d{6})\d{5,}" , slug_job_id or "")
    if not m:
        return ""
    dd, mm, yy = m.group(1)[:2], m.group(1)[2:4], m.group(1)[4:6]
    try:
        return _dt.date(2000 + int(yy), int(mm), int(dd)).isoformat()
    except ValueError:
        return ""


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
    def _search_urls(self, title: str) -> list[str]:
        """Naukri paginates listing pages with a -2, -3... URL suffix
        (verified live: -2 returns a fresh card set). The ?experience= param
        is ignored on these pages, so fresher filtering stays in the scorer."""
        slug = slugify(title)
        if not slug:
            return []
        pages = max(1, int(self.cfg.get("pages_per_title", 1)))
        return [f"{self.home}/{slug}-jobs" if p == 1
                else f"{self.home}/{slug}-jobs-{p}" for p in range(1, pages + 1)]

    def fetch_new_jobs(self, page: Page) -> int:
        titles = [t.strip() for t in (self.targets.get("titles") or []) if t.strip()]
        if not titles:
            self.notify.send("[naukri] no titles in config/targets.yaml — add one first",
                             toast=True)
            return 0
        max_titles = int(self.cfg.get("max_titles_per_run", 3))
        total = 0
        for title in titles[:max_titles]:
            urls = self._search_urls(title)
            if not urls:
                continue
            new = 0
            for pageno, url in enumerate(urls, start=1):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception as e:
                    self.notify.send(f"[naukri] goto failed ({url}): {type(e).__name__}")
                    break
                # clouds/interstitials: give the listing anchor some time to appear
                try:
                    page.wait_for_selector('a[href*="job-listings-"]', timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                self._dismiss_overlays(page)
                items = page.evaluate(GATHER_JS)
                self.notify.send(f"[naukri] '{title}' p{pageno}: "
                                 f"{len(items)} cards harvested")
                if not items:
                    if pageno == 1:
                        head = ""
                        try:
                            head = page.content()[:200].lower()
                        except Exception:
                            pass
                        self.notify.send(f"[naukri] 0 cards for '{title}' | "
                                         f"page head: {head!r}")
                    break
                for item in items:
                    if self._classify(item):
                        new += 1
                time.sleep(3.0)
            total += new
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
        job_id = url.rstrip("/").rsplit("/", 1)[-1]
        posted = posted_date_from_text(raw) or posted_from_slug(job_id)
        score, reasons, reject = score_job(title, company, location, raw,
                                           posted, self.targets)
        min_score = int(self.targets.get("min_score", 70))
        status = "skipped" if (reject or score < min_score) else "queued"
        jid, is_new = self.tracker.upsert_job(
            source=self.name, source_job_id=job_id, url=url, title=title,
            company=company, location=location, description=raw,
            posted_date=posted,
            score=score, score_reasons="; ".join(reasons), status=status)
        if not is_new:
            prior = self.tracker.reclassify(jid, status, "; ".join(reasons)[:200])
            return bool(prior not in (None, "queued") and status == "queued")
        return status == "queued"

    # ================================================================== APPLY
    def apply_job(self, page: Page, job) -> tuple[str, str, str]:
        url = job["url"] or f"{self.home}/job-listings/{job['source_job_id']}"
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        self._dismiss_overlays(page)
        if self.applied_signal(page):
            return ("applied", "already applied on site (detected on job page)", "")
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

    # --------------------------------------------------- applied detection
    def applied_signal(self, page: Page) -> bool:
        """Naukri turns the Apply button into a green 'Applied' pill once done."""
        try:
            return bool(page.evaluate("""() => {
                const els = [...document.querySelectorAll('button, a, div, span')]
                    .filter(e => e.offsetParent !== null);
                return els.some(e => (e.innerText || '').trim().toLowerCase()
                    .replace(/[^a-z]/g, '') === 'applied');
            }"""))
        except Exception:
            return False

    def dismiss_interstitial(self, page: Page):
        """Post-apply profile nudge panel: take its explicit 'I'll do it later' skip."""
        try:
            loc = page.get_by_role("button", name=re.compile(r"do it later", re.I)).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=2000)
                page.wait_for_timeout(800)
        except Exception:
            pass

    def external_apply_signal(self, page: Page) -> bool:
        """Naukri hands off to the company website with this banner."""
        try:
            content = (page.content() or "").lower()
            return "redirected to the company website" in content
        except Exception:
            return False

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
