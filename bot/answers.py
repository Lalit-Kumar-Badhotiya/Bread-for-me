"""Screening-answer library.

Matches application questions against config/answers.yaml using exact /
substring / fuzzy matching, resolves {{placeholders}} from profile.yaml, and
saves newly answered questions back into the yaml (self-learning library).
"""
import difflib
from pathlib import Path

import yaml


def _flatten(d, prefix=""):
    """Yield (key, value) for every leaf dict key at any depth."""
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            yield from _flatten(v, k)
        else:
            yield k.lower(), v


class AnswerLibrary:
    def __init__(self, cfg: dict, profile: dict, path: Path):
        self.path = path
        self.pairs = [(p.get("q", ""), str(p.get("a", ""))) for p in cfg.get("pairs", [])]
        self.blobs = cfg.get("blobs", {}) or {}
        self.profile = profile  # full personal dict for placeholders

    # ---------- placeholder resolution ----------
    def resolve(self, text: str) -> str:
        import re
        idx = dict(_flatten(self.profile))
        blobs = {k.lower().replace("_", " "): v for k, v in self.blobs.items()}

        def sub(m):
            key = m.group(1).strip().lower()
            if key in idx:
                return str(idx[key])
            if key in blobs:
                return str(blobs[key])
            return m.group(0)  # unknown placeholder -> keep literal

        return re.sub(r"\{\{(.+?)\}\}", sub, text or "")

    # ---------- question matching ----------
    def match(self, question: str) -> str | None:
        q = (question or "").lower().strip()
        if not q:
            return None
        best, best_ratio = None, 0.0
        for pq, pa in self.pairs:
            pq_l = pq.lower().strip()
            if not pq_l:
                continue
            if pq_l == q or pq_l in q:
                return self.resolve(pa)
            ratio = difflib.SequenceMatcher(None, pq_l, q).ratio()
            if ratio > best_ratio:
                best, best_ratio = pa, ratio
        if best is not None and best_ratio >= 0.85:
            return self.resolve(best)
        return None

    # ---------- self-learning ----------
    def learn(self, question: str, answer: str, blob_key: str | None = None):
        """Persist a newly answered question (or cover-letter-style blob)."""
        cfg = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if blob_key:
            cfg.setdefault("blobs", {})[blob_key] = answer
        else:
            cfg.setdefault("pairs", []).append({"q": question, "a": answer})
        self.path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                             encoding="utf-8")
        if blob_key:
            self.blobs[blob_key] = answer
        else:
            self.pairs.append((question, answer))
