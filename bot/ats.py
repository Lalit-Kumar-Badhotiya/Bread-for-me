"""External-ATS autofill handshake.

~220 rows sit parked as "external apply flow (host)": Naukri/indeed/
glassdoor/simplyhired hand the actual application off to a company site
(Lever, Greenhouse, Zoho Recruit, Workday, Darwinbox, plain company
forms, even Google Forms). Those forms carry the SAME standard fields
(name/email/phone/resume/CTC/notice) this bot already autofills on
SmartApply — so they should be chased, not just parked.

This module:
  * classifies an external host into a known ATS family (ats_family),
  * provides a conservative "does this page carry an application form"
    probe (SAFE_FORM_JS) so UNKNOWN hosts with a real form also get one
    automated attempt (and landing pages / spam forms don't), and
  * keeps the host list in ONE place for tests and future edits.
"""
import re

# host pattern -> family label; first match wins. Search is suffix-anchored
# (pattern ends with the registrable domain), so subdomains are covered.
ATS_FAMILIES: tuple = (
    (re.compile(r"forms\.gle$|forms\.google\.com$", re.I), "google-form"),
    (re.compile(r"\blever\.co$", re.I), "lever"),          # jobs/f/erp.lever.co
    (re.compile(r"greenhouse\.io$", re.I), "greenhouse"),  # boards/jobs.greenhouse.io
    (re.compile(r"grnhse\.io$", re.I), "greenhouse"),
    (re.compile(r"zohorecruit(?:ment|ing)?\.(?:com|in|io|net|org|co)$", re.I),
     "zoho"),
    (re.compile(r"darwinbox\.(?:in|io|com)$", re.I), "darwinbox"),
    (re.compile(r"myworkdayjobs\.com$", re.I), "workday"), # wd3.myworkdayjobs.com
    (re.compile(r"smartrecruiters\.com$", re.I), "smartrecruiters"),
    (re.compile(r"recruitee\.(?:com|in)$", re.I), "recruitee"),
    (re.compile(r"jobvite\.com$", re.I), "jobvite"),
    (re.compile(r"bamboohr\.com$", re.I), "bamboohr"),
    (re.compile(r"icims\.com$", re.I), "icims"),
    (re.compile(r"taleo\.net$", re.I), "taleo"),
    (re.compile(r"ashbyhq\.com$", re.I), "ashby"),
)


def ats_family(host_or_url: str | None) -> str | None:
    """Known-ATS family name for a host or URL, or None for unknown hosts."""
    h = (host_or_url or "").strip().lower()
    if not h:
        return None
    if "://" in h:
        m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", h)
        h = m.group(1) if m else ""
    h = h.split("/")[0].split(":")[0]      # path + port off
    h = re.sub(r"^www\.", "", h)
    if not h:
        return None
    for rx, fam in ATS_FAMILIES:
        if rx.search(h):
            return fam
    return None


# Evidence that an unknown host's page carries a REAL application form
# (not a contact/newsletter block the bot must never submit).
# Filled by _safe_external_form in base.py; kept here for docs/tests.
#   safe = submit-ish button AND (
#            file input                       (resume slot — strongest)
#         or >= 2 radio groups                (screening questions)
#         or email/text inputs totalling >= 4 (full application form)
#         or textarea + >= 4 other inputs     (message-style application))
SAFE_FORM_JS = """
() => {
  const vis = e => e && e.offsetParent !== null;
  const els = [...document.querySelectorAll('input, select, textarea')].filter(vis);
  let text = 0, email = 0, file = 0, radiogroups = new Set(), select = 0, textarea = 0;
  els.forEach(e => {
    const t = (e.type || '').toLowerCase();
    if (t === 'radio') { if (e.name) radiogroups.add(e.name); return; }
    if (t === 'text' || t === '' || t === 'tel') text++;
    else if (t === 'email') email++;
    else if (t === 'file') file++;
    else if (e.tagName.toLowerCase() === 'select') select++;
    else if (e.tagName.toLowerCase() === 'textarea') textarea++;
  });
  const total = text + email + file + select + textarea;
  const submitBtn = [...document.querySelectorAll(
        'button, input[type=submit], [role=button]')].filter(vis)
    .some(b => /apply|submit|send/i.test(((b.innerText || b.value || '') + '').trim()));
  return {text, email, file, radios: radiogroups.size, select,
          textarea, total, submitBtn};
}
"""


def safe_from_probe(res: dict | None) -> bool:
    """Pure decision over a SAFE_FORM_JS result (unit-tested; driven by real
    page.evaluate() output at runtime). A submit-ish button is required and
    the form must carry at least one strong application signal."""
    if not res or not res.get("submitBtn"):
        return False
    if res.get("file", 0) > 0:                          # resume slot — strongest
        return True
    if res.get("radios", 0) >= 2:                       # screening questions
        return True
    if res.get("email", 0) > 0 and res.get("total", 0) >= 4:
        return True                                     # full application form
    if res.get("textarea", 0) > 0 and res.get("total", 0) >= 4:
        return True                                     # message-style form
    return False
