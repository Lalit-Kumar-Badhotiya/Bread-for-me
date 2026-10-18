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
        """Visit each search URL for this title (page 1..N) and classify."""
        total = 0
        for pageno, url in enumerate(self.search_urls_for(title), start=1):
            if not url:
                continue
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                self.notify.send(f"[{self.name}] goto failed ({url}): {type(e).__name__}")
                break
            try:
                page.wait_for_selector(self.CARD_ANCHOR_SEL, timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            items = page.evaluate(self.GATHER_JS)
            self.notify.send(f"[{self.name}] '{title}' p{pageno}: "
                             f"{len(items)} cards harvested")
            if not items:
                if pageno == 1:
                    dump = page.evaluate("""
() => {
  const set = new Set([...document.querySelectorAll('a')]
    .map(a => (a.href || '').split('#')[0])
    .filter(h => h.includes('job') || h.includes('/jobs/')));
  return [...set].slice(0, 20);
}
""")
                    extra = page.evaluate("""
() => {
  const c = s => document.querySelectorAll(s).length;
  return `selectors: a[data-jk]=${c('a[data-jk]')} serpjob=${c('[class*="SerpJob"]')} `
       + `jobcard=${c('[class*="jobcard"]')} card=${c('[class*="card"]')} `
       + `totalA=${document.querySelectorAll('a').length} `
       + `text=${(document.body.innerText||'').slice(0,120).replace(/\\n/g,' | ')}`;
}
""")
                    self.notify.send(f"[{self.name}] 0 cards | {extra} | href dump: {dump}")
                break
            for item in items:
                if self._classify(item):
                    total += 1
            self.human_pause(2.5, 5.0)
        return total

    def search_urls_for(self, title: str) -> list[str]:
        """Search URLs to harvest for a title; subclasses override to paginate."""
        url = self.search_url_for(title)
        return [url] if url else []

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
        posted = posted_date_from_text(raw)
        score, reasons, reject = score_job(title, company, location, raw,
                                           posted, self.targets)
        min_score = int(self.targets.get("min_score", 70))
        status = "skipped" if (reject or score < min_score) else "queued"
        job_id = url.rstrip("/").rsplit("/", 1)[-1] or url[:120]
        jid, is_new = self.tracker.upsert_job(
            source=self.name, source_job_id=job_id, url=url, title=title,
            company=company, location=location,
            description=raw, posted_date=posted,
            score=score, score_reasons="; ".join(reasons), status=status)
        if not is_new:
            prior = self.tracker.reclassify(jid, status, "; ".join(reasons)[:200])
            return bool(prior not in (None, "queued") and status == "queued")
        return status == "queued"

    # ------------------------------------------------- apply
    def apply_job(self, page: Page, job) -> tuple[str, str, str]:
        url = job["url"]
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        gate = self.detail_gate(page, job)
        if gate:
            return gate
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

    CARD_ANCHOR_SEL = 'a[data-jk], a[href*="viewjob"], a[href*="/cmp/"]'
    GATHER_JS = """
() => {
  const out = new Map();
  const push = (raw, box) => {
    if (!raw) return;
    const clean = raw.startsWith('http') ? raw.split('#')[0]
                                         : location.origin + raw.split('#')[0];
    if (out.has(clean)) return;
    out.set(clean, { url: clean, box: (box || '').trim().slice(0, 600) });
  };
  // 1) Indeed SERP cards: data-jk anchors build the canonical viewjob URL
  document.querySelectorAll('a[data-jk]').forEach(el => {
    const jk = el.getAttribute('data-jk');
    let box = '';
    try {
      const host = el.closest('.job_seen_beacon') || el.closest('article')
        || el.closest('li') || el.parentElement.parentElement || el.parentElement;
      box = (host && host.innerText || '');
    } catch (e) {}
    push(`${location.origin}/viewjob?jk=${jk}`, box);
  });
  // 2) any viewjob/cmp-jobs anchor
  const rx = /(viewjob\\?jk=|&jk=|\\/cmp\\/[^\"]+\\/jobs\\/)/;
  document.querySelectorAll('a').forEach(a => {
    const raw = a.href || '';
    if (!rx.test(raw)) return;
    let box = '';
    try {
      const host = a.closest('.job_seen_beacon') || a.closest('article')
        || a.closest('li') || a.parentElement.parentElement;
      box = (host && host.innerText || '');
    } catch (e) {}
    push(raw, box);
  });
  return [...out.values()].slice(0, 60);
}
"""

    def search_url_for(self, title: str) -> str:
        from urllib.parse import quote_plus
        # explvl=entry_level verified live to change the result set. Pagination
        # (&start=) redirects guests to the login wall — keep one page.
        return f"{self.home}/jobs?q={quote_plus(title)}&explvl=entry_level"

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
    // KEEP the query string — /job-listing/ pages 404 without jobListingId
    const clean = (raw.startsWith('http') ? raw : location.origin + raw).split('#')[0];
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
        return self.search_urls_for(title)[0]

    def search_urls_for(self, title: str) -> list[str]:
        """seniorityType=entrylevel changes the result set; pages paginate
        with an _IP2/_IP3... suffix (both verified live)."""
        kw = title.strip().lower().replace(" ", "-")
        kw = re.sub(r"[^a-z0-9-]", "", kw)
        n = len(title.strip())
        pages = max(1, int(self.cfg.get("pages_per_title", 1)))
        urls = [f"{self.home}/Job/{kw}-jobs-SRCH_KO0,{n}.htm"
                f"?seniorityType=entrylevel"]
        for p in range(2, pages + 1):
            urls.append(f"{self.home}/Job/{kw}-jobs-SRCH_KO0,{n}_IP{p}.htm"
                        f"?seniorityType=entrylevel")
        return urls

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

    CARD_ANCHOR_SEL = 'a[href*="/job/"]'
    GATHER_JS = """
() => {
  const out = new Map();
  const rx = /\\/job\\/[A-Za-z0-9_-]{12,}/;
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
        return f"{self.home}/search?q={quote_plus(title)}"

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
