"""Nutrition evidence: a curated set of studies (from Yitz's evidence tracker) that meal ideas can lean on.

The studies live in data/nutrition.json. At start they're filed on the shelf nutrition/evidence, so recall finds them
when someone asks "is coffee OK?". Meal suggestions get the handful that bear on what's in the house, plus a few that
always apply (protein target, protein while cutting, portions, ultra-processed food).
"""
import json
import os

HERE = os.path.dirname(__file__)
SHELF = "nutrition/evidence"
PROTEIN_G_PER_KG = 1.6        # Morton et al. 2018: muscle gains level off around 1.6 g/kg/day
LB_PER_KG = 2.20462
RANK = {"Strong": 0, "Moderate": 1, "Preliminary": 2, "No benefit": 3, "Harm": 3}


def load(path=None):
    try:
        with open(path or os.path.join(HERE, "data", "nutrition.json"), encoding="utf-8") as f:
            return json.load(f).get("studies", [])
    except (OSError, ValueError):
        return []


def _text(s):
    return (f"{s['finding']} Takeaway: {s['takeaway']} Evidence: {s['evidence']}. "
            f"Study: {s['study']} ({s['design']}).")


def file_into(store, studies=None):
    """Put each study on the nutrition shelf once; an unchanged study is left alone, a changed one supersedes."""
    n = 0
    for s in studies if studies is not None else load():
        key = f"nutrition study|{s['topic']}".lower()
        old = store.active_by_key(key)
        body = _text(s)
        if old and old["body"] == body:
            continue
        store.remember(text=body, title=f"Nutrition study: {s['topic']}", shelf=SHELF, key=key,
                       source="nutrition-tracker", meta={"evidence": s["evidence"], "category": s["category"]})
        n += 1
    return n


def relevant(names, studies=None, limit=10):
    """The studies that always apply, then the ones about foods in the house, strongest evidence first."""
    studies = studies if studies is not None else load()
    hay = " ".join(n.lower() for n in names)
    always = [s for s in studies if s.get("always")]
    matched = [s for s in studies if not s.get("always") and any(w in hay for w in s.get("match") or [])]
    matched.sort(key=lambda s: RANK.get(s["evidence"], 2))
    return (always + matched)[:limit]


def lines(studies):
    return "\n".join(f"- {s['topic']} [{s['evidence']}]: {s['finding']} -> {s['takeaway']} ({s['study']})"
                     for s in studies)


def protein_target(weight_lb):
    """Grams of protein a day for building muscle, from body weight in pounds (None if no weight)."""
    if not isinstance(weight_lb, (int, float)) or weight_lb <= 0:
        return None
    return int(round(weight_lb / LB_PER_KG * PROTEIN_G_PER_KG / 5.0) * 5)
