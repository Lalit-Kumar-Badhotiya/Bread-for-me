"""Job scoring engine — decides WHICH roles to apply to (targets.yaml).

Score budget (0-100):
  title exact/in-title     up to 40
  all must_have_skills     up to 40 (missing any => reject)
  bonus_skills             6 each, max 12
  location match           8
  freshness                <= max_age_days adds
Threshold from targets.min_score decides queued vs skipped.

Experience gate (zero-experience / fresher policy):
  * every "N years/yrs" requirement phrasing is extracted (ranges, "N+",
    single values, decimals, en/em dashes, mixed year/yrs spellings) and a
    posting whose minimum exceeds the band limit is rejected
    (fresher=0, junior=3, mid=5).
  * with targets.fresher_signal_required: true a posting must ALSO explicitly
    target zero-experience candidates (fresher / "0-x Yrs" / entry level /
    trainee / graduate / intern / no experience) to be queued at all.
"""
import datetime as dt
import re

DATE_FORMATS = ("%B %d, %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y")


def _age_days(posted_date: str) -> float | None:
    if not posted_date:
        return None
    for fmt in DATE_FORMATS:
        try:
            d = dt.datetime.strptime(posted_date.strip(), fmt)
            return (dt.datetime.now() - d).total_seconds() / 86400
        except ValueError:
            pass
    return None  # unknown age -> neutral, not rejected


BAND_LIMITS = {"fresher": 0, "junior": 3, "mid": 5}

# One left-to-right scan for "N year(s) of experience"-style requirements.
# Alternatives are ordered so the FIRST match at a position wins:
#   1) "3 yrs to 6 yrs"  — unit on BOTH sides of the range
#   2) "3-6 years", "3 to 6 Years", "3 – 6 yrs" — unit after the range only
#   3) "3+ years", "3 plus years", "3 years", "3.5 years" — single value
# Scanning left to right means "0-1 years" reads as min 0 (alt 2 consumes the
# whole range); otherwise the stray "1 years" would false-trigger alt 3.
_YEARS_RX = re.compile(
    r"(?P<lo_a>\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:-|–|—|to)\s*"
    r"(?P<hi_a>\d+(?:\.\d+)?)\s*(?:years?|yrs?)"
    r"|(?P<lo_b>\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(?P<hi_b>\d+(?:\.\d+)?)\s*"
    r"\+?\s*(?:years?|yrs?)"
    r"|(?P<one>\d+(?:\.\d+)?)\s*\+?\s*(?:plus\s+)?(?:years?|yrs?)",
    re.I)

# Text right after a years-phrase that marks company history / age, not a
# requirement ("established 20 years ago", "60 years of excellence", ...).
_YEARS_SKIP_AFTER_RX = re.compile(
    r"^\s*(?:ago\b|old\b|back\b|and\s+counting|in\s+business"
    r"|of\s+(?:service|operation|operations|excellence))", re.I)

# A posting that explicitly welcomes zero-experience candidates.
_FRESHER_SIGNAL_RX = re.compile(
    r"\bfreshers?\b"
    r"|\b0\s*(?:-|–|—|to)\s*\d+(?:\.\d+)?\s*(?:years?|yrs?)"
    r"|\b0\s*\+?\s*(?:years?|yrs?)"
    r"|\bentry[ -]?level\b"
    r"|\btrainees?\b"
    r"|\bgraduates?\b"
    r"|\bintern(?:ships?)?\b"
    r"|\bno\s+(?:prior\s+)?experience\b"
    r"|\bwithout\s+experience\b", re.I)


def _min_years_required(blob: str) -> list[float]:
    """Every minimum-years figure stated as an experience requirement."""
    out: list[float] = []
    blob = blob or ""
    for m in _YEARS_RX.finditer(blob):
        if _YEARS_SKIP_AFTER_RX.search(blob[m.end():m.end() + 24]):
            continue
        raw = m.group("lo_a") or m.group("lo_b") or m.group("one")
        try:
            out.append(float(raw))
        except (TypeError, ValueError):
            continue
    return out


def _norm_list(xs):
    return [str(x).strip() for x in (xs or []) if str(x).strip()]


def score_job(title, company, location, description, posted_date, targets):
    """Returns (score:int, reasons:list[str], reject:bool)."""
    reasons: list[str] = []
    blob = " ".join([title or "", company or "", description or ""]).lower()
    location_l = (location or "").lower()
    score = 0

    # 0) hard excludes
    for kw in _norm_list(targets.get("exclude_keywords")):
        if re.search(r"\b" + re.escape(kw.lower()) + r"\b", blob):
            return 0, [f"exclude: '{kw}'"], True

    band = str(targets.get("experience_band") or "").strip().lower()
    max_min = BAND_LIMITS.get(band)
    seen_years = _min_years_required(blob)
    if max_min is not None:
        over = [x for x in seen_years if x > max_min]
        if over:
            return 0, [f"experience: needs {min(over):g}+ yrs "
                       f"(band '{band}' allows {max_min:g})"], True
    if targets.get("fresher_signal_required") and not _FRESHER_SIGNAL_RX.search(blob):
        if targets.get("verify_on_detail"):
            reasons.append("experience: unknown on the card - verify on the detail page")
        else:
            return 0, ["experience: no fresher signal on the posting "
                       "(fresher_signal_required: true)"], True
    if seen_years:
        reasons.append(f"experience ok (min {min(seen_years):g} yr)")

    title_l = (title or "").lower()

    # 1) title match — weights adapt: without must-have skills configured the
    #    title alone carries more weight so the threshold stays reachable.
    must_cfg = [s.lower() for s in _norm_list(targets.get("must_have_skills"))]
    titles = [t.lower() for t in _norm_list(targets.get("titles"))]
    title_weight = 40 if must_cfg else 62
    if titles:
        if any(t in title_l for t in titles):
            score += title_weight
            reasons.append("title: exact match")
        else:
            for t in titles:
                if all(w in blob for w in t.split()):
                    score += int(title_weight * 0.55)
                    reasons.append(f"title: loose ({t})")
                    break

    # 2) must-have skills — ALL required (when configured). Sites with
    #    verify_on_detail list cards that omit skills entirely; those only see
    #    the final check on the job page right before applying.
    missing = [s for s in must_cfg if s not in blob]
    if must_cfg:
        if missing and not targets.get("verify_on_detail"):
            return 0, reasons + [f"missing must-have: {missing[0]}", "..."], True
        score += 40
        if missing:
            reasons.append(f"skills: '{missing[0]}' not shown on the card - "
                           "verify on the detail page")
        else:
            reasons.append(f"must-have skills: all {len(must_cfg)}")

    # 3) bonus skills
    bonus = [s.lower() for s in _norm_list(targets.get("bonus_skills"))]
    found = [s for s in bonus if s in blob]
    score += min(6 * len(found), 12)
    if found:
        reasons.append(f"bonus skills: {', '.join(found)}")

    # 4) location ("remote" matches both remote postings and target cities)
    locs = [s.lower() for s in _norm_list(targets.get("locations"))]
    if locs:
        remote_post = "remote" in location_l or "work from home" in blob
        cyber = any(l in location_l for l in locs if l != "remote")
        if cyber or (remote_post and "remote" in locs) or (remote_post and not cyber):
            score += 8
            reasons.append("location match")

    # 5) freshness
    max_age = float(targets.get("max_age_days") or 0)
    age = _age_days(posted_date)
    if age is not None:
        if max_age and age > max_age:
            return 0, reasons + [f"stale: {int(age)}d old > max {int(max_age)}d"], True
        score += 5
        reasons.append("fresh posting")

    return min(score, 100), reasons, False


def experience_verdict(text: str, targets: dict) -> tuple[bool, str]:
    """Apply-time experience gate for full pages (cards may hide experience).

    The hard rule: skip when the text states a requirement above the band
    limit (the 'never apply to an experienced-only posting' guarantee). No
    explicit requirement is fine here — the card was already vetted, and the
    detail page is the last checkpoint.

    Returns (ok_to_apply, reason).
    """
    band = str(targets.get("experience_band") or "").strip().lower()
    limit = BAND_LIMITS.get(band)
    seen = _min_years_required(text or "")
    if limit is not None:
        over = [x for x in seen if x > limit]
        if over:
            return (False, f"experience: detail page needs {min(over):g}+ yrs "
                           f"(band '{band}' allows {limit:g}) - not applied")
    if _FRESHER_SIGNAL_RX.search(text or ""):
        return True, "detail page: fresher signal"
    if seen:
        return True, f"detail page: needs {min(seen):g} yrs - within band"
    return True, "detail page: no experience requirement stated"


def detail_verdict(text: str, targets: dict) -> tuple[bool, str]:
    """Final gate right before submitting, for cards that showed no details.

    Enforces both hard rules on the full job page:
      1. never apply when it states more years than the band allows;
      2. never apply when the must-have skills are genuinely absent.
    Returns (ok_to_apply, reason). Used by sites with verify_on_detail.
    """
    ok, why = experience_verdict(text, targets)
    if not ok:
        return False, why
    blob = (text or "").lower()
    missing = [s.lower() for s in _norm_list(targets.get("must_have_skills"))
               if s.lower() not in blob]
    if missing:
        return False, f"skills: detail page never mentions must-have '{missing[0]}'"
    return True, why


def rescore_pending(tracker, targets, settings=None) -> int:
    """Re-evaluate every pending row against the CURRENT targets.yaml.

    Keeps the queue honest when you edit targets between runs: jobs you now
    exclude drop out, jobs that now qualify (older fetches say) come in.
    Applied/needs_you/failed rows are never touched. Used by the bots'
    fetch() and by `python run_apply.py rescore`.

    Sites with `verify_on_detail: true` in settings keep rows whose cards
    omit experience/skills queued (the detail-page check runs right before
    applying).
    """
    sites_cfg = (settings or {}).get("sites") or {}
    min_score = int(targets.get("min_score", 70))
    changed = 0
    for row in tracker.rescorable_rows():
        t = targets
        if (sites_cfg.get(row["source"]) or {}).get("verify_on_detail"):
            t = dict(targets)
            t["verify_on_detail"] = True
        score, reasons, reject = score_job(
            row["title"], row["company"], row["location"],
            row["description"], row["posted_date"] or "", t)
        status = "skipped" if (reject or score < min_score) else "queued"
        if status != row["status"] or score != row["score"]:
            tracker.rescore(row["id"], score, "; ".join(reasons)[:200],
                            status, "rescored against updated targets")
            changed += 1
    return changed
