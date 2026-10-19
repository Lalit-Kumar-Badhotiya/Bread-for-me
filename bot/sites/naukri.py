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
from .base import RESUME_PATH, BaseSiteBot, Page, posted_date_from_text

CONFIRM_HINTS_EXTRA = ("you have applied", "your application has been sent",
                       "applied successfully", "has been sent to")

# Some postings (consulting/training-farm style) route applications through a
# web form ("Apply link: https://forms.gle/..."). The bot can never fill
# those — park them as an external flow with the link instead of flailing
# through the recruiter chat (found live 2026-10-19, jobs 267/426). Google
# Forms dominate, but the same pattern shows up on Microsoft Forms, Typeform,
# JotForm, Tally, Zoho and Airtable.
FORM_LINK_RX = re.compile(
    r"""https?://(?:
            forms\.gle/ | docs\.google\.com/forms |
            forms\.(?:office|microsoft)\.com/ |
            [\w.-]*typeform\.com/ | [\w.-]*jotform\.com/ |
            tally\.so/ | [\w.-]*zohopublic\.[a-z.]+/ |
            airtable\.com/shr
        )[^\s"'<>()]*""", re.I | re.X)

# The recruiter chat rejects an answer with phrasing like "The input seems
# invalid." — matched so the flow can try a variant instead of re-sending the
# same rejected text (found live 2026-10-19, job 260 CTC=0 sent 3×).
CHAT_INVALID_RX = re.compile(
    r"(seems invalid|invalid input|invalid format|input is not valid|"
    r"not a valid (?:input|value|number|response)|validation failed)", re.I)

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

# Snapshot of the recruiter-chat panel (Naukri's Chatbotworkflow v5 widget).
# Everything is driven through these five anchors; the element ids carry a
# per-render random suffix, so only classes / id-suffixes are stable.
CHAT_STATE_JS = """
() => {
  const box = document.querySelector('[id$="ChatbotContainer"]');
  if (!box) return null;
  const vis = e => e && e.offsetParent !== null;
  const chips = [...box.querySelectorAll('div.chatbot_Chip')]
    .filter(e => e.offsetParent !== null)
    .map(e => (e.innerText || '').trim()).filter(Boolean);
  const botMsgs = [...box.querySelectorAll('li.botItem span, li[class*=botItem] span')]
    .filter(e => e.offsetParent !== null)
    .map(e => (e.innerText || '').trim()).filter(Boolean);
  const sendEl = box.querySelector('.sendMsgbtn_container .sendMsg');
  const sendBox = sendEl ? sendEl.closest('.send') : null;
  const ta = box.querySelector('div.textArea[contenteditable=true]');
  const radios = [...box.querySelectorAll('input[type=radio]')]
    .filter(e => e.offsetParent !== null)
    .map(e => {
      const host = e.closest('label') || e.closest('div');
      return {id: e.id || '',
              text: host ? (host.innerText || '').trim().slice(0, 90) : ''};
    });
  return {chips: chips, botMsgs: botMsgs,
          sendText: sendEl ? (sendEl.innerText || '').trim() : '',
          sendEnabled: sendBox ? !sendBox.className.includes('disabled') : false,
          inputVisible: vis(ta),
          radios: radios,
          text: (box.innerText || '').slice(0, 3000)};
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
        fl = self._external_form_link(page)
        if fl:
            host, url = fl
            return ("needs_you", f"external apply flow ({host}): {url[:120]}",
                    self.safe_shot(page, f"formlink_{job['id']}"))
        apply_loc = self.find_apply(
            page, self.APPLY_LOCATORS,
            role_rx=re.compile(r"^apply( now)?$", re.I))
        if not apply_loc:
            return ("needs_you", "no apply button", self.safe_shot(page, f"noapply_{job['id']}"))
        return self.wizard(page, job["id"], apply_loc)

    # ========================================================== chat apply
    # Naukri's newer apply flows open a scripted recruiter chat (the
    # "Chatbotworkflow v5" widget) instead of a form:
    #   * option chips are plain <div class="chatbot_Chip">, NOT buttons
    #   * the resume must be handed over by clicking the 'Upload Resume'
    #     chip; setting the hidden input directly is rejected server-side
    #     ("File upload was unsuccessful")
    #   * the footer is a div.sendMsg ('Send'/'Save') that stays greyed out
    #     until the current step has been answered
    CHATBOT_BOX = '[id$="ChatbotContainer"]'

    # "is the chat open?" probe: ANY visible anchor counts. wait_for_selector
    # resolves a selector list to the single FIRST element in document order —
    # in radio-question states that first match sits hidden while the chat is
    # clearly open, so the wizard never handed over (found live 2026-10-19,
    # jobs 267/426: the resume got fed to the chat as a raw file input and
    # naukri rejected it). Returns 'none' fast when no widget is on the page.
    CHAT_VIS_JS = """
    () => {
      const box = document.querySelector('[id$="ChatbotContainer"]');
      if (!box) return 'none';
      const sels = ['.sendMsgbtn_container', 'div.chatbot_Chip',
                    'div.textArea[contenteditable="true"]',
                    'input[type=radio]'];
      const vis = e => {
        const r = e.getBoundingClientRect();
        const st = getComputedStyle(e);
        return r.width > 1 && r.height > 1
               && st.visibility !== 'hidden' && st.display !== 'none';
      };
      return sels.some(s => [...box.querySelectorAll(s)].some(vis))
             ? 'visible' : 'hidden';
    }
    """

    def chatbot_present(self, page: Page, quick: bool = False) -> bool:
        """True when any chat anchor (footer, chip, input, radio card) is on
        screen. `quick=True` = single probe (the wizard's per-step check);
        otherwise the widget may be mid-render / mid-SPA-reload, so retry a
        few times before declaring it gone."""
        tries = 1 if quick else 4
        for i in range(tries):
            try:
                state = page.evaluate(self.CHAT_VIS_JS)
            except Exception:
                try:
                    if page.is_closed():
                        return False
                except Exception:
                    return False
                state = "none"
            if state == "visible":
                return True
            if state == "none":   # no widget on the page at all
                return False
            if i + 1 < tries:
                page.wait_for_timeout(2000)
        return False

    def _chat_state(self, page: Page):
        try:
            return page.evaluate(CHAT_STATE_JS)
        except Exception:
            return None

    def chatbot_flow(self, page: Page, jid) -> tuple[str, str, str]:
        max_steps = int((self.settings.get("limits", {}) or {}).get("max_wizard_steps", 30))
        uploads = 0
        seen = None
        stall = 0
        # last free-text answer we sent + alternates still worth trying when
        # the chat replies "The input seems invalid." (question, tried, left)
        sent: tuple[str, list[str], list[str]] | None = None
        for _ in range(max_steps):
            page.wait_for_timeout(1800)
            if self.applied_signal(page):
                return ("applied", "applied via recruiter chat", "")
            if not self.chatbot_present(page):
                return ("needs_you", "chat panel closed without confirmation",
                        self.safe_shot(page, f"chatgone_{jid}"))
            st = self._chat_state(page)
            if st is None:
                continue
            fl = FORM_LINK_RX.search(st.get("text") or "")
            if fl:
                # recruiter wants an external form filled ("Kindly submit in
                # the link...") — the bot can't do that; park with the link
                url = fl.group(0)
                host = (url.split("//", 1)[1].split("/", 1)[0]
                        if "//" in url else "web form")
                return ("needs_you",
                        f"external apply flow ({host}): {url[:120]}",
                        self.safe_shot(page, f"formlink_{jid}"))
            last_q = (st["botMsgs"][-1] if st["botMsgs"] else "")[:160]
            if sent:
                if CHAT_INVALID_RX.search(last_q):
                    q0, tried, left = sent
                    if left:
                        nxt = left.pop(0)
                        tried.append(nxt)
                        if self._chat_send_text(page, nxt):
                            sent = (q0, tried, left)
                            continue
                    # nothing left to try: park so the user can fix the answer
                    self.log_unknown_questions(jid, [q0])
                    tried_s = ", ".join(repr(t) for t in tried)
                    return ("needs_you",
                            f"needs your input: chat rejected '{q0}' "
                            f"(tried {tried_s}) — edit answers.yaml, then retry",
                            self.safe_shot(page, f"chatbad_{jid}"))
                if last_q and last_q[:60] != sent[0][:60]:
                    sent = None   # answer accepted; the conversation moved on
            sig = (tuple(st["chips"]), last_q, st["inputVisible"], len(st["radios"]))
            if sig == seen:
                stall += 1
                if stall >= 3:
                    if last_q:
                        # park it so `questions` can batch-answer it later
                        # (chat unknowns never hit the wizard's leftover log)
                        self.log_unknown_questions(jid, [last_q])
                    return ("needs_you", f"needs your input: {last_q}",
                            self.safe_shot(page, f"chatstuck_{jid}"))
            else:
                stall, seen = 0, sig

            # 1) resume step: click the chip so the workflow accepts the file
            up = next((c for c in st["chips"] if "upload resume" in c.lower()), None)
            if up and uploads < 2:
                uploads += 1
                self._chat_upload(page, up)
                continue
            if up:  # both attempts failed -> use the chat's explicit skip
                if self._click_chat_chip(page, "do it later"):
                    continue

            # 2) multiple-choice chips (recruiter questions)
            ctl = ("upload resume", "do it later", "skip", "not now")
            opts = [c for c in st["chips"] if c.lower() not in ctl]
            if opts and last_q:
                ans = self.answer_for(last_q)
                if ans and self._click_chat_option(page, ans, opts):
                    continue

            # 3) radio options inside the chat
            if st["radios"] and last_q:
                ans = self.answer_for(last_q)
                if ans and self._click_chat_radio(page, ans, st["radios"]):
                    continue

            # 4) free-text question
            if st["inputVisible"] and last_q:
                ans = self.answer_for(last_q)
                if ans and self._chat_send_text(page, ans):
                    sent = (last_q, [ans], self._chat_answer_variants(ans))
                    continue

            # 5) nothing to answer -> the footer button advances/submits
            if st["sendEnabled"] and self._chat_click_send(page):
                continue
        return ("needs_you", f"chat flow exceeded {max_steps} steps",
                self.safe_shot(page, f"chatlong_{jid}"))

    # ------------------------------------------------------ chat internals
    def _click_chat_chip(self, page: Page, text: str) -> bool:
        try:
            loc = page.locator(f'{self.CHATBOT_BOX} div.chatbot_Chip').filter(
                has_text=re.compile(re.escape(text), re.I)).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=3000)
                page.wait_for_timeout(1500)
                return True
        except Exception:
            pass
        return False

    def _click_chat_option(self, page: Page, ans: str, opts: list[str]) -> bool:
        a = (ans or "").strip().lower()
        if not a:
            return False
        hit = next((o for o in opts if o.lower() == a), None)
        if not hit:
            hit = next((o for o in opts if a in o.lower() or o.lower() in a), None)
        return bool(hit) and self._click_chat_chip(page, hit)

    def _click_chat_radio(self, page: Page, ans: str, radios: list[dict]) -> bool:
        a = (ans or "").strip().lower()
        if not a:
            return False
        for r in radios:
            txt = (r.get("text") or "").lower()
            if not (a == txt or a in txt or txt in a):
                continue
            try:
                if r.get("id"):
                    loc = page.locator(f'#{r["id"]}')
                    try:
                        loc.check(timeout=2500)
                    except Exception:
                        try:
                            loc.click(force=True, timeout=2000)
                        except Exception:
                            loc.evaluate(
                                "e => (e.closest('label') || e.parentElement).click()")
                else:
                    page.get_by_text(r.get("text") or "", exact=True).first.click(timeout=3000)
                page.wait_for_timeout(1200)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _chat_answer_variants(ans: str) -> list[str]:
        """Alternate phrasings to try when the chat replies "The input seems
        invalid." (observed on a numeric CTC question: '0' was rejected, so
        try '0.0' / 'Fresher' before parking for the user)."""
        a = (ans or "").strip()
        out: list[str] = []
        if re.fullmatch(r"\d+(?:\.\d+)?", a):
            if "." not in a:
                out.append(f"{a}.0")
            out.append("Fresher")
        return out

    def _chat_send_text(self, page: Page, text: str) -> bool:
        try:
            ta = page.locator(f'{self.CHATBOT_BOX} div.textArea[contenteditable="true"]').first
            ta.click(timeout=3000)
            ta.fill(text, timeout=3000)
            page.wait_for_timeout(600)
            if not self._chat_click_send(page):
                try:  # some steps submit the message on Enter instead
                    ta.press("Enter", timeout=2000)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def _chat_click_send(self, page: Page) -> bool:
        try:
            box = page.locator(f'{self.CHATBOT_BOX} .sendMsgbtn_container .send').first
            if "disabled" in (box.get_attribute("class") or ""):
                return False
            page.locator(f'{self.CHATBOT_BOX} .sendMsgbtn_container .sendMsg').first.click(
                timeout=3000)
            page.wait_for_timeout(1500)
            return True
        except Exception:
            return False

    def _chat_upload(self, page: Page, chip_text: str) -> bool:
        loc = page.locator(f'{self.CHATBOT_BOX} div.chatbot_Chip').filter(
            has_text=re.compile(re.escape(chip_text), re.I)).first
        try:
            with page.expect_file_chooser(timeout=6000) as fc:
                loc.click(timeout=4000)
            fc.value.set_files(str(RESUME_PATH))
            page.wait_for_timeout(4000)
            return True
        except Exception:
            pass
        try:  # chip clicked but no chooser event: use the workflow's own input
            page.locator(f'{self.CHATBOT_BOX} input.chatbot_Uploader').set_input_files(
                str(RESUME_PATH), timeout=4000)
            page.wait_for_timeout(3000)
            return True
        except Exception:
            return False

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

    @staticmethod
    def _external_form_link(page: Page) -> tuple[str, str] | None:
        """(host, url) of a web-form apply link on the page, if any.

        Nxtwave-style JDs carry 'Apply link: https://forms.gle/...' right in
        the description — applying means filling that form manually, so the
        honest outcome is a needs_you park with the link (never chased)."""
        try:
            m = FORM_LINK_RX.search(page.content() or "")
        except Exception:
            return None
        if not m:
            return None
        url = m.group(0)
        try:
            host = url.split("//", 1)[1].split("/", 1)[0]
        except Exception:
            host = "web form"
        return host, url

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

    # ------------------------------------------------ detail-gate page text
    def _page_text(self, page: Page) -> str:
        """Text handed to the apply-time detail gate.

        The authoritative experience requirement lives in the JD header chip
        ("3 - 8 years"), NOT in the description body, and the old
        #jobDescriptionText id no longer exists on the current site — compose
        header chip + JD content container, falling back to the full page text
        if those selectors ever drift. (Found live 2026-10-19: a 3-8 years
        posting was applied because only the card badge — which can go stale —
        had ever been checked.)"""
        try:
            return page.evaluate("""() => {
                const parts = [];
                const add = (el) => { if (el && el.innerText) parts.push(el.innerText); };
                add(document.querySelector('[class*="jhc__exp__"]'));
                add(document.querySelector('[class*="jdc__content"]'));
                add(document.querySelector('#jobDescriptionText'));
                const joined = parts.join('\\n');
                if (joined.length >= 400) return joined.slice(0, 15000);
                return ((document.body && document.body.innerText) || '').slice(0, 15000);
            }""") or ""
        except Exception:
            return ""
