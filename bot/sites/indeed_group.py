"""P3: the Indeed group — Indeed, Glassdoor, SimplyHired.

These three share one parent (Recruit Holdings), the same listings pool and
heavy anti-bot defenses. Consequently they share ONE conservative daily
budget (settings.limits.indeed_group_cap, default 8 total apps across all
three) no matter their individual caps.

Fetch: browser visits each site's guest search URL for a target title and
harvests cards via site-specific anchor selectors. Relative date chips are
converted by shared posted_date_from_text.

Apply: detail page -> "Apply now" -> shared wizard. Indeed's hosted apply
flow lives inside a child iframe, so these bots override _resolve_form().
External apply flows (company ATS) are flagged needs_you, never chased.
"""
import re

from ..scorer import score_job
from .base import BaseSiteBot, Page, posted_date_from_text


class IndeedGroupBot(BaseSiteBot):
    """Shared budget base. Subclasses set name/home/search_urls/gather."""
    group_members = ("indeed", "glassdoor", "simplyhired")
    # witch frame hosts the form (None = main page)
    FORM_FRAME_HINTS = ("gpi", "apply", "form", "iframe-apply")
    APPLY_LOCATORS = (
        'button:has-text("Apply now")', 'a:has-text("Apply now")',
        'button:has-text("Apply")', 'a:has-text("Apply")',
        '[data-testid*="apply"]',
    )
    EXTERNAL_APPLY_GUARD = True

    # ------------------------------------------------- shared group quota
    def quota_left(self) -> int:
        if not self.cfg.get("enabled", False):
            return 0
        tracker = self.tracker
        site_left = self.daily_cap - tracker.applied_today(self.name)
        used = sum(tracker.applied_today(s) for s in self.group_members)
        limit = (self.settings.get("limits", {}) or {})
        group_left = int(limit.get("indeed_group_cap", 8)) - used
        total_left = int(limit.get("daily_total_cap", 0)) - tracker.applied_today(None)
        return max(0, min(site_left, group_left, total_left))

    # ------------------------------------------------- shared harvester
    def harvest(self, page: Page, title: str) -> int:
        """Visit search URL(s) and classify cards; returns newly queued."""
        url = self.search_url_for(title)
        if not url:
            return 0
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            self.notify.send(f"[{self.name}] goto failed ({url}): {type(e).__name__}")
            return 0
        try:
            page.wait_for_selector(self.CARD_ANCHOR_SEL, timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        items = page.evaluate(self.GATHER_JS)
        self.notify.send(f"[{self.name}] '{title}': {len(items)} cards harvested")
        if not items:
            head = ""
            try:
                head = page.content()[:200].lower()
            except Exception:
                pass
            self.notify.send(f"[{self.name}] 0 cards | head: {head!r}")
            return 0
        new = 0
        for item in items:
            if self._classify(item):
                new += 1
        return new

    def search_url_for(self, title: str) -> str:
        raise NotImplementedError

    # ------------------------------------------------- shared classification
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
            if re.search(r"\d+\.\d|reviews?|ratings?|•|ago", l, re.I):
                continue
            company = re.sub(r"\s*\d+\.\d\s*$", "", l).strip()
            if company:
                break
        location = ""
        for l in lines[1:6]:
            if re.search(r"remote|,\s|\+ ?\d|india", l, re.I) and not re.search(r"\d+\.\d|ago", l, re.I):
                location = l.strip()[:80]
                break
        score, reasons, reject = score_job(title, company, location, raw,
                                           posted_date_from_text(raw), self.targets)
        min_score = int(self.targets.get("min_score", 70))
        status = "skipped" if (reject or score < min_score) else "queued"
        job_id = url.rstrip("/").rsplit("/", 1)[-1] or url[:120]
        jid, is_new = self.tracker.upsert_job(
            source=self.name, source_job_id=job_id, url=url, title=title,
            company=company, location=location,
            description=raw, posted_date="",
            score=score, score_reasons="; ".join(reasons), status=status)
        if not is_new:
            self.tracker.set_status(jid, status, "; ".join(reasons)[:200])
        return status == "queued"

    # ------------------------------------------------- apply
    def apply_job(self, page: Page, job) -> tuple[str, str, str]:
        url = job["url"]
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        apply_loc = self.find_apply(
            page, self.APPLY_LOCATORS,
            role_rx=re.compile(r"^apply( now)?$", re.I))
        if not apply_loc:
            return ("needs_you", "no apply button", self.safe_shot(page, f"noapply_{job['id']}"))
        return self.wizard(page, job["id"], apply_loc)

    def _resolve_form(self, page: Page):
        """Indeed-style flows embed the form in a child iframe — find it."""
        try:
            for frame in page.frames[1:]:
                try:
                    if frame.locator("input").count() >= 3:
                        return frame
                except Exception:
                    continue
        except Exception:
            pass
        return page

    def logged_in_signal(self, page: Page) -> bool | None:
        try:
            n = page.locator("a:has-text('Sign in')").count() \
                + page.locator("button:has-text('Sign in')").count()
            return True if n == 0 else False
        except Exception:
            return None


class IndeedBot(IndeedGroupBot):
    name = "indeed"
    home = "https://in.indeed.com"

    CARD_ANCHOR_SEL = 'a[href*="viewjob"], a[href*="/cmp/"]'
    GATHER_JS = """
() => {
  const out = new Map();
  const rx = /(viewjob\\?|\\/cmp\\/[^\"]+\\/jobs\\/)/;
  document.querySelectorAll('a').forEach(a => {
    const raw = a.href || '';
    if (!rx.test(raw)) return;
    const clean = raw.split('#')[0];
    if (out.has(clean)) return;
    let box = '';
    try {
      const host = a.closest('.job_seen_beacon') || a.closest('article')
        || a.closest('li') || a.parentElement.parentElement || a.parentElement;
      box = (host && host.innerText || '').trim().slice(0, 600);
    } catch (e) {}
    out.set(clean, { url: clean, box: box });
  });
  return [...out.values()].slice(0, 60);
}
"""

    def search_url_for(self, title: str) -> str:
        from urllib.parse import quote_plus
        return f"{self.home}/jobs?q={quote_plus(title)}"

    def fetch_new_jobs(self, page: Page) -> int:
        titles = [t.strip() for t in (self.targets.get("titles") or []) if t.strip()]
        if not titles:
            self.notify.send("[indeed] no titles in config/targets.yaml", toast=True)
            return 0
        total = 0
        for t in titles[:int(self.cfg.get("max_titles_per_run", 3))]:
            total += self.harvest(page, t)
            self.human_pause(3.0, 6.0)
        return total


class GlassdoorBot(IndeedGroupBot):
    name = "glassdoor"
    home = "https://www.glassdoor.co.in"

    CARD_ANCHOR_SEL = 'a[href*="/job-listing/"]'
    GATHER_JS = """
() => {
  const out = new Map();
  document.querySelectorAll('a[href*="/job-listing/"]').forEach(a => {
    const raw = a.href || a.getAttribute('href') || '';
    const clean = (raw.startsWith('http') ? raw : location.origin + raw).split('?')[0];
    if (out.has(clean)) return;
    let box = '';
    try {
      const host = a.closest('article') || a.closest('[class*="JobCard"]')
        || a.closest('li') || a.parentElement.parentElement;
      box = (host && host.innerText || '').trim().slice(0, 600);
    } catch (e) {}
    out.set(clean, { url: clean, box: box });
  });
  return [...out.values()].slice(0, 60);
}
"""

    def search_url_for(self, title: str) -> str:
        from urllib.parse import quote_plus
        return f"{self.home}/job-listings?sc.keyword={quote_plus(title)}"

    def fetch_new_jobs(self, page: Page) -> int:
        titles = [t.strip() for t in (self.targets.get("titles") or []) if t.strip()]
        if not titles:
            self.notify.send("[glassdoor] no titles in config/targets.yaml", toast=True)
            return 0
        total = 0
        for t in titles[:int(self.cfg.get("max_titles_per_run", 3))]:
            total += self.harvest(page, t)
            self.human_pause(3.0, 6.0)
        return total


class SimplyHiredBot(IndeedGroupBot):
    name = "simplyhired"
    home = "https://www.simplyhired.co.in"

    CARD_ANCHOR_SEL = 'a[href*="/job/view/"], a[href*="/job/"]'
    GATHER_JS = """
() => {
  const out = new Map();
  const rx = /\\/job\\/(view|listing)/;
  document.querySelectorAll('a').forEach(a => {
    const raw = a.href || a.getAttribute('href') || '';
    if (!rx.test(raw)) return;
    const clean = (raw.startsWith('http') ? raw : location.origin + raw).split('?')[0];
    if (out.has(clean)) return;
    let box = '';
    try {
      const host = a.closest('article') || a.closest('[class*="SerpJob"]')
        || a.closest('li') || a.parentElement.parentElement || a.parentElement;
      box = (host && host.innerText || '').trim().slice(0, 600);
    } catch (e) {}
    out.set(clean, { url: clean, box: box });
  });
  return [...out.values()].slice(0, 60);
}
"""

    def search_url_for(self, title: str) -> str:
        from urllib.parse import quote_plus
        return f"{self.home}/jobsearch?q={quote_plus(title)}"

    def fetch_new_jobs(self, page: Page) -> int:
        titles = [t.strip() for t in (self.targets.get("titles") or []) if t.strip()]
        if not titles:
            self.notify.send("[simplyhired] no titles in config/targets.yaml", toast=True)
            return 0
        total = 0
        for t in titles[:int(self.cfg.get("max_titles_per_run", 3))]:
            total += self.harvest(page, t)
            self.human_pause(3.0, 6.0)
        return total
