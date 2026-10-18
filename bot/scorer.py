"""Job scoring engine — decides WHICH roles to apply to (targets.yaml).

Score budget (0-100):
  title exact/in-title     up to 40
  all must_have_skills     up to 40 (missing any => reject)
  bonus_skills             6 each, max 12
  location match           8
  freshness                <= max_age_days adds
Threshold from targets.min_score decides queued vs skipped.
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

    # 2) must-have skills — ALL required (when configured)
    missing = [s for s in must_cfg if s not in blob]
    if must_cfg and missing:
        return 0, reasons + [f"missing must-have: {missing[0]}", "..."], True
    if must_cfg:
        score += 40
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
