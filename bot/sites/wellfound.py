"""Wellfound site bot (wellfound.com, ex-AngelList Talent) — startup focus.

FETCH: browser session visits wellfound.com/role/r/<slug> pages (public
listing views) and harvests job cards; the scorer decides queue vs skip.

APPLY: job page -> "One-click apply" -> shared wizard engine. Requires a
completed Wellfound profile (you fill that manually once — deliberate human
step from plan). External apply flows are flagged needs_you, never chased.
Login is email/password (do NOT use LinkedIn SSO — LinkedIn stays manual).
"""
import re
import time

from ..scorer import score_job
from .base import BaseSiteBot, Page, posted_date_from_text

APPLY_LOCATORS = (
    'button:has-text("One-click apply")',
    'button:has-text("One Click Apply")',
    'a:has-text("One-click apply")',
    'button:has-text("Apply")',
    'a:has-text("Apply")',
)

GATHER_JS = """
() => {
  const out = new Map();
  const idRx = /\\/jobs\\/\\d/;
  document.querySelectorAll('a[href*="/jobs/"]').forEach(a => {
    const raw = a.href || a.getAttribute('href') || '';
    if (!idRx.test(raw)) return;
    const clean = (raw.startsWith('http') ? raw : location.origin + raw).split('?')[0];
    if (out.has(clean)) return;
    let box = '';
    try {
      const host = a.closest('article')
        || a.closest('[class*="JobCard"]')
        || a.closest('[class*="job"]')
        || a.parentElement.parentElement
        || a.parentElement;
      box = (host && host.innerText || '').trim().slice(0, 600);
    } catch (e) {}
    out.set(clean, { url: clean, box: box });
  });
  return [...out.values()];
}
"""


def slugify(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (t or "").lower()).strip("-")


class WellfoundBot(BaseSiteBot):
    name = "wellfound"
    home = "https://wellfound.com"

    # wizard knobs ---------------------------------------------------------
    APPLY_LOCATORS = APPLY_LOCATORS
    SUBMIT_PATTERNS = [r"^submit application$", r"^submit$", r"^one.?click apply$"]
    EXTERNAL_APPLY_GUARD = True

    # ================================================================== FETCH
    def fetch_new_jobs(self, page: Page) -> int:
        titles = [t.strip() for t in (self.targets.get("titles") or []) if t.strip()]
        if not titles:
            self.notify.send("[wellfound] no titles in config/targets.yaml", toast=True)
            return 0
        max_titles = int(self.cfg.get("max_titles_per_run", 3))
        total = 0
        for title in titles[:max_titles]:
            slug = slugify(title)
            if not slug:
                continue
            url = f"{self.home}/role/r/{slug}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                self.notify.send(f"[wellfound] goto failed ({url}): {type(e).__name__}")
                continue
            try:
                page.wait_for_selector(r'a[href*="/jobs/"]', timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            items = page.evaluate(GATHER_JS)
            self.notify.send(f"[wellfound] '{title}': {len(items)} cards harvested")
            if not items:
                head = ""
                try:
                    head = page.content()[:200].lower()
                except Exception:
                    pass
                self.notify.send(f"[wellfound] 0 cards for '{title}' | head: {head!r}")
                continue
            new = 0
            for item in items:
                if self._classify(item):
                    new += 1
            total += new
            time.sleep(3.5)
        return total

    def _classify(self, item: dict) -> bool:
        raw = (item.get("box") or "").strip()
        url = (item.get("url") or "").strip()
        if not url or not raw:
            return False
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        title = (lines[0] if lines else "").strip()[:120]
        if not title:
            return False
        company = ""
        for l in lines[1:4]:
            if re.search(r"ago|•|saved|remote|bonus|equity|₹|\$", l, re.I):
                continue
            company = re.sub(r"\s*\d+\.\d\s*$", "", l).strip()
            if company:
                break
        location = ""
        for l in lines[1:6]:
            if l.lower() in ("remote",) or ("·" in l) or re.search(r" India|bengal|mumbai|delhi|hyder|chennai|pune|gurug|noida", l, re.I):
                location = re.sub(r"[·•]", ",", l).strip()[:80]
                break
        score, reasons, reject = score_job(title, company, location, raw,
                                           posted_date_from_text(raw), self.targets)
        min_score = int(self.targets.get("min_score", 70))
        status = "skipped" if (reject or score < min_score) else "queued"
        job_id = url.rstrip("/").rsplit("/", 1)[-1]
        jid, is_new = self.tracker.upsert_job(
            source=self.name, source_job_id=job_id, url=url, title=title,
            company=company, location=location, description=raw, posted_date="",
            score=score, score_reasons="; ".join(reasons), status=status)
        if not is_new:
            self.tracker.set_status(jid, status, "; ".join(reasons)[:200])
        return status == "queued"

    # ================================================================== APPLY
    def apply_job(self, page: Page, job) -> tuple[str, str, str]:
        url = job["url"]
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        apply_loc = self.find_apply(
            page, self.APPLY_LOCATORS,
            role_rx=re.compile(r"one.?click apply|^apply( now)?$", re.I))
        if not apply_loc:
            return ("needs_you", "no one-click apply button (profile incomplete? "
                                 "external apply?)", self.safe_shot(page, f"noapply_{job['id']}"))
        return self.wizard(page, job["id"], apply_loc)

    def logged_in_signal(self, page: Page) -> bool | None:
        try:
            n = page.locator("a:has-text('Log in')").count() \
                + page.locator("button:has-text('Log in')").count()
            if n == 0:
                return True
            return False
        except Exception:
            return None
