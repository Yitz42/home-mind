"""Neuro-symbolic memory graph alongside Home Mind's notes/shelves (mind.db).

Additive associative & belief layer: existing notes/items remain the full-fidelity source of truth,
while MemGraph tracks canonical concept nodes, signed bi-temporal links, running numerical stats,
and stability/decay across domains (food, lighting, sleep, steps, routines, preferences, work).

Key design invariants:
- Signed stability S in [-1.0, +1.0] for bipolar relations (e.g. LIKES: + likes / - dislikes,
  CORRELATES_WITH: + helps / - worsens), and unsigned [0.0, 1.0] for safety/baselines
  (ALLERGIC_TO, BASELINE, LIVES_IN, WORKS_AT).
- Three evidence tiers with different significance and promotion thresholds:
    direct       explicit user statements (high significance, fast promotion, highest protection)
    inferred     hypotheses fanned out from statements (needs distinct sources to promote)
    sensor_fact  device/health/telemetry data (expects many data points, compacts episodes first,
                 never silently overwrites a direct FACT)
- Declarative canonical relation rules (mg_rules + built-ins): any field can be left blank (None)
  to inherit sensible tier defaults; synonym & negative aliases normalize automatically.
- No time-based auto-deletion: unreinforced links drift to dormant (|S| <= 0.10, excluded from hot
  context, wakeable later). Storage cleanup triggers only near a cap: compacts old raw episodes
  into aggregate rollups first, then prunes lowest-value dormant speculative links while protecting
  confirmed FACTs and frequently used/shown links.
- Multi-person privacy: every node/link/episode/staging row respects owner/scope visibility
  (speakers.visible) and purge_owner().
"""
import asyncio
from datetime import datetime, timezone
import json
import math
import re
import time

from .speakers import visible


# ---- operational tiers, thresholds, and decay constants ---------------------------------------------------------
STATUS_THOUGHT = "THOUGHT"
STATUS_IDEA = "IDEA"
STATUS_FACT = "FACT"
STATUS_CONTESTED = "CONTESTED"
STATUSES = (STATUS_THOUGHT, STATUS_IDEA, STATUS_FACT, STATUS_CONTESTED)

EXCLUSIVITY_EXCLUSIVE = "EXCLUSIVE"
EXCLUSIVITY_SOFT = "SOFT_EXCLUSIVE"
EXCLUSIVITY_ADDITIVE = "ADDITIVE"
EXCLUSIVITIES = (EXCLUSIVITY_EXCLUSIVE, EXCLUSIVITY_SOFT, EXCLUSIVITY_ADDITIVE)

DECAY_LINEAR = "LINEAR"
DECAY_LOGISTIC = "LOGISTIC"
DECAY_ZERO = "ZERO"
DECAY_TYPES = (DECAY_LINEAR, DECAY_LOGISTIC, DECAY_ZERO)

EVIDENCE_DIRECT = "direct"
EVIDENCE_INFERRED = "inferred"
EVIDENCE_SENSOR = "sensor_fact"
EVIDENCE_TIERS = {
    EVIDENCE_DIRECT: {
        "min_points": 2,
        "init_stability": 0.65,
        "bolster_step": 0.30,
        "evict_weight": 3.0,
    },
    EVIDENCE_INFERRED: {
        "min_points": 4,
        "init_stability": 0.25,
        "bolster_step": 0.25,
        "evict_weight": 1.0,
    },
    EVIDENCE_SENSOR: {
        "min_points": 10,
        "init_stability": 0.18,
        "bolster_step": 0.085,
        "evict_weight": 0.5,
    },
}

FACT_THRESHOLD = 0.85
IDEA_THRESHOLD = 0.40
DORMANT_THRESHOLD = 0.10

LINEAR_ALPHA_PER_DAY = 0.02       # THOUGHT drops 0.02/day -> 0.25 reaches dormant (~0.10) in ~8 days
LOGISTIC_BETA = 0.12              # IDEA shifted-logistic steepness
LOGISTIC_HALF_LIFE_DAYS = 30.0    # IDEA decays slowly at first, faster around ~30 days unreinforced

BASE_CONTRADICTION_PENALTY = 0.35
SOFT_EXCLUSIVE_FACTOR = 0.50
TRIAGE_CONFIDENCE_FLOOR = 0.70

MAX_CONTEXT_TAGS = 3
MAX_FAN_OUT = 5
MAX_PROVENANCE = 12
MAX_DISTINCT_SOURCES = 64

CATEGORIES = (
    "GENERAL", "FOOD", "HEALTH", "SLEEP", "ACTIVITY", "HOME",
    "LIGHTING", "HABITS", "CAREER", "LOCATION", "RELATIONSHIPS", "PREFERENCES",
)

# ---- Speed 1 Librarian routing cues (deterministic, sub-5ms, zero VRAM swap) ------------------------------------
CATEGORY_PATTERNS = {
    "FOOD": re.compile(
        r"\b(?:eat|eating|meal|meals|dinner|lunch|breakfast|snack|cook|cooking|recipe|recipes|"
        r"food|dish|dishes|cuisine|spicy|sweet|savory|sour|flavor|flavour|ingredient|ingredients|"
        r"restaurant|takeout|coffee|tea|drink|drinks|hungry|craving|taste|tastes|like to eat|"
        r"chinese|thai|italian|mexican|indian|japanese|sushi|ramen|noodles|chicken|tofu|peanut|peanuts)\b", re.I
    ),
    "HEALTH": re.compile(
        r"\b(?:allerg\w*|intoleran\w*|sensitiv\w*|symptom\w*|headache|migraine|stomach|bloat\w*|"
        r"medicine|medication|supplement|doctor|health|caffeine|lactose|gluten|dairy|nuts|shellfish)\b", re.I
    ),
    "SLEEP": re.compile(
        r"\b(?:sleep|slept|sleeping|bedtime|wake|woke|nap|insomnia|tired|rested|rem|deep sleep|hours of sleep)\b", re.I
    ),
    "ACTIVITY": re.compile(
        r"\b(?:step|steps|walk|walking|walked|run|running|ran|jog|jogging|workout|exercise|gym|"
        r"trail|miles|km|cardio|active|activity|cycling|bike|swim)\b", re.I
    ),
    "LIGHTING": re.compile(
        r"\b(?:light|lights|lighting|lamp|lamps|dim|dimmed|dimming|bright|brightness|warm light|cool light|cozy)\b", re.I
    ),
    "HOME": re.compile(
        r"\b(?:thermostat|temperature|hvac|heater|ac|fan|blinds|curtains|living room|bedroom|kitchen|office|house|home)\b", re.I
    ),
    "HABITS": re.compile(
        r"\b(?:habit|habits|routine|routines|usually|normally|typically|every day|everyday|daily|often|always|tend to)\b", re.I
    ),
    "CAREER": re.compile(
        r"\b(?:work|works|working|job|company|employer|office|role|team|boss|coworker|career|commute)\b", re.I
    ),
    "LOCATION": re.compile(
        r"\b(?:live|lives|living|city|town|moved|move|based in|hometown|neighborhood|address|state|country)\b", re.I
    ),
    "RELATIONSHIPS": re.compile(
        r"\b(?:wife|husband|partner|spouse|mom|mother|dad|father|sister|brother|friend|family|kids|son|daughter)\b", re.I
    ),
    "PREFERENCES": re.compile(
        r"\b(?:prefer|prefers|preferred|favorite|favourite|like|likes|dislike|dislikes|hate|hates|avoid|avoids|enjoy|enjoys)\b", re.I
    ),
}

CONTEXT_TAG_PATTERNS = (
    ("weekday", re.compile(r"\b(?:weekday|weekdays|workday|workdays|monday|tuesday|wednesday|thursday|friday)\b", re.I)),
    ("weekend", re.compile(r"\b(?:weekend|weekends|saturday|sunday)\b", re.I)),
    ("morning", re.compile(r"\b(?:morning|mornings|am|breakfast|wake)\b", re.I)),
    ("afternoon", re.compile(r"\b(?:afternoon|afternoons|lunch)\b", re.I)),
    ("evening", re.compile(r"\b(?:evening|evenings|dinner|tonight|pm)\b", re.I)),
    ("night", re.compile(r"\b(?:night|nights|bedtime|late)\b", re.I)),
    ("travel", re.compile(r"\b(?:travel|traveling|travelling|trip|hotel|away|vacation)\b", re.I)),
    ("trail", re.compile(r"\b(?:trail|trails|mountain|hike|hiking)\b", re.I)),
    ("living_room", re.compile(r"\bliving\s+room\b", re.I)),
    ("bedroom", re.compile(r"\bbedroom\b", re.I)),
    ("kitchen", re.compile(r"\bkitchen\b", re.I)),
    ("office", re.compile(r"\b(?:home\s+)?office\b", re.I)),
)

HISTORICAL_Q = re.compile(
    r"\b(?:used to|before|earlier|previously|last year|remember when|in the past|back when|ever liked|ever had|anymore|formerly)\b",
    re.I,
)

LOOP_BACK_STOP = frozenset(
    "the and for that this with from have what when where which who why how does did are was were "
    "about into your my our their its his her some any much many like likes know remember tell".split()
)

LIBRARIAN_SYSTEM = (
    "You route a user's question to a personal memory graph. "
    "Pick only matching categories from: " + ", ".join(CATEGORIES) + ". "
    'Reply with JSON only, no other text: '
    '{"target_categories": [], "extracted_entities": [], "context_tags": [], '
    '"temporal_scope": "CURRENT_STATE" or "HISTORICAL"}. '
    "Keep lists short (0-4 items). If the question is unrelated to personal facts/habits/preferences, return empty lists."
)


# ---- built-in canonical relation rules (any None field inherits tier defaults) ----------------------------------
DEFAULT_RULES = {
    "LIKES": {
        "aliases": ("LOVES", "ENJOYS", "FAVORITE_FOOD", "LIKES_FOOD", "LIKES_CUISINE",
                    "LIKES_FLAVOR", "LIKES_INGREDIENT"),
        "negative_aliases": ("DISLIKES", "HATES", "DISLIKES_FOOD", "DOES_NOT_LIKE"),
        "negative_label": "DISLIKES",
        "exclusivity": EXCLUSIVITY_ADDITIVE,
        "signed": True,
        "default_evidence": None,
        "min_points": None,
        "decay_override": None,
        "tracks_stats": None,
        "max_per_day": None,
        "unit": None,
    },
    "PREFERS": {
        "aliases": ("FAVORITE", "PRIMARY", "PRIMARY_TOOL", "WANTS"),
        "negative_aliases": ("AVOIDS", "REFUSES", "DISPREFERS"),
        "negative_label": "AVOIDS",
        "exclusivity": EXCLUSIVITY_EXCLUSIVE,
        "signed": True,
        "default_evidence": None,
        "min_points": None,
        "decay_override": None,
        "tracks_stats": None,
        "max_per_day": None,
        "unit": None,
    },
    "HABIT": {
        "aliases": ("ROUTINE", "USUALLY", "DOES_OFTEN", "LIGHT_HABIT", "SLEEP_HABIT"),
        "negative_aliases": ("NEVER_DOES", "RARELY_DOES"),
        "negative_label": "AVOIDS_HABIT",
        "exclusivity": EXCLUSIVITY_ADDITIVE,
        "signed": True,
        "default_evidence": EVIDENCE_SENSOR,
        "min_points": 7,
        "decay_override": None,
        "tracks_stats": True,
        "max_per_day": 1,
        "unit": None,
    },
    "BASELINE": {
        "aliases": ("AVERAGE", "TYPICAL", "SENSOR_PATTERN", "SLEEP_BASELINE",
                    "STEPS_BASELINE", "LIGHT_BASELINE"),
        "negative_aliases": (),
        "negative_label": None,
        "exclusivity": EXCLUSIVITY_ADDITIVE,
        "signed": False,
        "default_evidence": EVIDENCE_SENSOR,
        "min_points": 14,
        "decay_override": None,
        "tracks_stats": True,
        "max_per_day": None,
        "unit": None,
    },
    "CORRELATES_WITH": {
        "aliases": ("AFFECTS", "LINKED_TO", "RESULT_IN", "RESULTS_IN", "HELPS"),
        "negative_aliases": ("WORSENS", "HURTS", "DISRUPTS"),
        "negative_label": "NEGATIVELY_CORRELATES_WITH",
        "exclusivity": EXCLUSIVITY_ADDITIVE,
        "signed": True,
        "default_evidence": EVIDENCE_INFERRED,
        "min_points": 6,
        "decay_override": None,
        "tracks_stats": None,
        "max_per_day": 1,
        "unit": None,
    },
    "ALLERGIC_TO": {
        "aliases": ("ALLERGY", "INTOLERANT_TO", "SENSITIVE_TO"),
        "negative_aliases": (),
        "negative_label": None,
        "exclusivity": EXCLUSIVITY_ADDITIVE,
        "signed": False,
        "default_evidence": EVIDENCE_DIRECT,
        "min_points": 1,
        "decay_override": DECAY_ZERO,
        "tracks_stats": False,
        "max_per_day": None,
        "unit": None,
    },
    "LIVES_IN": {
        "aliases": ("PRIMARY_LOCATION", "HOME_CITY", "BASED_IN"),
        "negative_aliases": (),
        "negative_label": None,
        "exclusivity": EXCLUSIVITY_EXCLUSIVE,
        "signed": False,
        "default_evidence": EVIDENCE_DIRECT,
        "min_points": 1,
        "decay_override": None,
        "tracks_stats": False,
        "max_per_day": None,
        "unit": None,
    },
    "WORKS_AT": {
        "aliases": ("EMPLOYED_AT", "JOB_AT", "ROLE_AT"),
        "negative_aliases": (),
        "negative_label": None,
        "exclusivity": EXCLUSIVITY_SOFT,
        "signed": False,
        "default_evidence": EVIDENCE_DIRECT,
        "min_points": 1,
        "decay_override": None,
        "tracks_stats": False,
        "max_per_day": None,
        "unit": None,
    },
    "HAS_ATTRIBUTE": {
        "aliases": ("IS_A", "HAS_CUISINE", "HAS_FLAVOR", "HAS_INGREDIENT", "CATEGORY"),
        "negative_aliases": (),
        "negative_label": None,
        "exclusivity": EXCLUSIVITY_ADDITIVE,
        "signed": False,
        "default_evidence": EVIDENCE_INFERRED,
        "min_points": 1,
        "decay_override": None,
        "tracks_stats": False,
        "max_per_day": None,
        "unit": None,
    },
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS mg_rules (
  relation TEXT PRIMARY KEY,
  aliases TEXT NOT NULL DEFAULT '[]',
  negative_aliases TEXT NOT NULL DEFAULT '[]',
  negative_label TEXT,
  exclusivity TEXT,
  signed INTEGER,
  default_evidence TEXT,
  min_points INTEGER,
  decay_override TEXT,
  tracks_stats INTEGER,
  max_per_day INTEGER,
  unit TEXT
);
CREATE TABLE IF NOT EXISTS mg_nodes (
  id INTEGER PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'concept',
  category TEXT NOT NULL DEFAULT 'GENERAL',
  aliases TEXT NOT NULL DEFAULT '[]',
  owner TEXT,
  scope TEXT NOT NULL DEFAULT 'household',
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS mg_nodes_uniq
  ON mg_nodes(canonical_name, COALESCE(owner, ''), scope);
CREATE INDEX IF NOT EXISTS mg_nodes_cat ON mg_nodes(category, scope);

CREATE TABLE IF NOT EXISTS mg_links (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL,
  relation TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'GENERAL',
  context_tags TEXT NOT NULL DEFAULT '',
  exclusivity TEXT NOT NULL DEFAULT 'ADDITIVE',
  signed INTEGER NOT NULL DEFAULT 1,
  evidence TEXT NOT NULL DEFAULT 'inferred',
  status TEXT NOT NULL DEFAULT 'THOUGHT',
  stability_score REAL NOT NULL DEFAULT 0.25,
  anchor_stability REAL NOT NULL DEFAULT 0.25,
  anchor_ts REAL NOT NULL DEFAULT 0.0,
  decay_type TEXT NOT NULL DEFAULT 'LINEAR',
  dormant INTEGER NOT NULL DEFAULT 0,
  stat_count INTEGER NOT NULL DEFAULT 0,
  stat_mean REAL,
  stat_m2 REAL NOT NULL DEFAULT 0.0,
  stat_min REAL,
  stat_max REAL,
  stat_unit TEXT,
  evidence_count INTEGER NOT NULL DEFAULT 1,
  distinct_sources TEXT NOT NULL DEFAULT '[]',
  provenance TEXT NOT NULL DEFAULT '[]',
  access_count INTEGER NOT NULL DEFAULT 0,
  shown_count INTEGER NOT NULL DEFAULT 0,
  last_accessed_on REAL,
  created_on REAL NOT NULL,
  last_stated_on REAL NOT NULL,
  valid_from REAL NOT NULL,
  valid_to REAL,
  owner TEXT,
  scope TEXT NOT NULL DEFAULT 'household',
  superseded_by INTEGER,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS mg_links_uniq
  ON mg_links(source_id, target_id, relation, context_tags, COALESCE(owner, ''), scope);
CREATE INDEX IF NOT EXISTS mg_links_active
  ON mg_links(dormant, status, category, scope);
CREATE INDEX IF NOT EXISTS mg_links_owner
  ON mg_links(owner, scope, dormant);
CREATE INDEX IF NOT EXISTS mg_links_src_rel
  ON mg_links(source_id, relation, context_tags, dormant);

CREATE TABLE IF NOT EXISTS mg_episodes (
  id INTEGER PRIMARY KEY,
  link_id INTEGER NOT NULL,
  ts REAL NOT NULL,
  day TEXT NOT NULL,
  value REAL,
  unit TEXT,
  sign INTEGER NOT NULL DEFAULT 1,
  certainty REAL NOT NULL DEFAULT 1.0,
  evidence TEXT NOT NULL DEFAULT 'inferred',
  source_ref TEXT NOT NULL DEFAULT '',
  quote TEXT NOT NULL DEFAULT '',
  compacted INTEGER NOT NULL DEFAULT 0,
  owner TEXT,
  scope TEXT NOT NULL DEFAULT 'household'
);
CREATE INDEX IF NOT EXISTS mg_episodes_link ON mg_episodes(link_id, compacted, ts);
CREATE INDEX IF NOT EXISTS mg_episodes_owner ON mg_episodes(owner, scope, compacted, ts);

CREATE TABLE IF NOT EXISTS mg_staging (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  op TEXT NOT NULL,
  link_id INTEGER,
  patch TEXT NOT NULL,
  before_state TEXT,
  after_state TEXT,
  confidence REAL NOT NULL DEFAULT 1.0,
  reason TEXT NOT NULL DEFAULT '',
  flagged INTEGER NOT NULL DEFAULT 0,
  flag_reasons TEXT NOT NULL DEFAULT '[]',
  audit_status TEXT NOT NULL DEFAULT 'pending',
  owner TEXT,
  scope TEXT NOT NULL DEFAULT 'household'
);
CREATE INDEX IF NOT EXISTS mg_staging_flagged ON mg_staging(flagged, audit_status, ts);
"""


# ---- pure math & normalization helpers --------------------------------------------------------------------------
def normalize_name(name):
    """Canonical node name: lowercase, collapsed whitespace, underscores to spaces."""
    s = re.sub(r"[\s_]+", " ", str(name or "").strip().lower())
    return s.strip()


def normalize_relation_key(raw):
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(raw or "").strip().upper()).strip("_")
    return s


def normalize_tags(tags):
    """Sorted, deduplicated, coarse context tags (capped to MAX_CONTEXT_TAGS to prevent bucket explosion)."""
    if not tags:
        return []
    if isinstance(tags, str):
        raw = [x for x in re.split(r"[,;/]+", tags) if x.strip()]
    else:
        raw = list(tags)
    out = []
    for t in raw:
        k = re.sub(r"[^a-z0-9]+", "_", str(t or "").strip().lower()).strip("_")
        if k and k not in out:
            out.append(k)
    return sorted(out)[:MAX_CONTEXT_TAGS]


def tags_key(tags):
    return ",".join(normalize_tags(tags))


def iso_day(ts):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def iso_month(ts):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m")


def _sign(val):
    return -1 if val < 0 else 1


def decay_magnitude(mag, decay_type, dt_days):
    """Decay |S| >= 0 over dt_days >= 0. Zero decay is constant; linear drops linearly; logistic follows
    a continuous shifted S-curve equal to mag at dt_days == 0 and smoothly declining around LOGISTIC_HALF_LIFE_DAYS."""
    m = max(0.0, min(1.0, float(mag)))
    dt = max(0.0, float(dt_days))
    if dt <= 0.0 or decay_type == DECAY_ZERO or m <= 0.0:
        return m
    if decay_type == DECAY_LINEAR:
        return max(0.0, m - LINEAR_ALPHA_PER_DAY * dt)
    if decay_type == DECAY_LOGISTIC:
        num = 1.0 + math.exp(-LOGISTIC_BETA * LOGISTIC_HALF_LIFE_DAYS)
        den = 1.0 + math.exp(LOGISTIC_BETA * (dt - LOGISTIC_HALF_LIFE_DAYS))
        return max(0.0, min(m, m * (num / den)))
    return m


def effective_stability(stability_score, decay_type, last_stated_on, now, status=None):
    """Live decayed signed stability at `now`."""
    s = float(stability_score)
    if status == STATUS_FACT and decay_type == DECAY_ZERO:
        return s
    dt_days = max(0.0, (float(now) - float(last_stated_on)) / 86400.0)
    mag = decay_magnitude(abs(s), decay_type, dt_days)
    return math.copysign(mag, s) if mag > 0.0 else 0.0


def welford_update(count, mean, m2, vmin, vmax, value):
    """Exact online Welford mean/M2/min/max update."""
    x = float(value)
    n = int(count or 0) + 1
    if n == 1 or mean is None:
        return 1, x, 0.0, x, x
    delta = x - float(mean)
    new_mean = float(mean) + delta / n
    delta2 = x - new_mean
    new_m2 = float(m2 or 0.0) + delta * delta2
    new_min = x if vmin is None else min(float(vmin), x)
    new_max = x if vmax is None else max(float(vmax), x)
    return n, new_mean, new_m2, new_min, new_max


def welford_std(count, m2):
    n = int(count or 0)
    if n < 2:
        return 0.0
    return math.sqrt(max(0.0, float(m2 or 0.0)) / (n - 1))


def contradiction_penalty(base_penalty, certainty, age_days, exclusivity=EXCLUSIVITY_EXCLUSIVE):
    """Spec dampening formula: Base * (soft factor) * gamma * (1 / (1 + age_factor)).
    Age factor is bounded in [0, 1] so a direct (gamma=1.0) contradiction always drops S=1.0 below 0.85."""
    gamma = max(0.0, min(1.0, float(certainty)))
    if gamma <= 0.0:
        return 0.0
    factor = SOFT_EXCLUSIVE_FACTOR if exclusivity == EXCLUSIVITY_SOFT else 1.0
    age_factor = min(1.0, max(0.0, float(age_days)) / 365.0)
    return float(base_penalty) * factor * gamma * (1.0 / (1.0 + age_factor))


# ---- MemGraph engine --------------------------------------------------------------------------------------------
class MemGraph:
    """SQLite-backed neuro-symbolic memory graph sharing Home Mind's mind.db (store.db + store.lock)."""

    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time
        with self.store.lock:
            self.store.db.executescript(SCHEMA)
            self.store.db.commit()

    # ---- canonical relation rule registry -----------------------------------------------------------------------
    def rules(self):
        """Merged built-in + custom SQLite relation rules."""
        out = {k: dict(v) for k, v in DEFAULT_RULES.items()}
        for r in self.store.db.execute("SELECT * FROM mg_rules"):
            rel = r["relation"]
            base = dict(out.get(rel) or {
                "aliases": (),
                "negative_aliases": (),
                "negative_label": None,
                "exclusivity": None,
                "signed": None,
                "default_evidence": None,
                "min_points": None,
                "decay_override": None,
                "tracks_stats": None,
                "max_per_day": None,
                "unit": None,
            })
            base["aliases"] = tuple(json.loads(r["aliases"] or "[]"))
            base["negative_aliases"] = tuple(json.loads(r["negative_aliases"] or "[]"))
            if r["negative_label"] is not None:
                base["negative_label"] = r["negative_label"]
            for col in ("exclusivity", "default_evidence", "min_points", "decay_override", "max_per_day", "unit"):
                if r[col] is not None:
                    base[col] = r[col]
            if r["signed"] is not None:
                base["signed"] = bool(r["signed"])
            if r["tracks_stats"] is not None:
                base["tracks_stats"] = bool(r["tracks_stats"])
            out[rel] = base
        return out

    def define_rule(self, relation, aliases=(), negative_aliases=(), negative_label=None,
                    exclusivity=None, signed=None, default_evidence=None, min_points=None,
                    decay_override=None, tracks_stats=None, max_per_day=None, unit=None):
        """Register or override a canonical relation rule. Any optional field may be left blank (None)
        to inherit sensible defaults from the link's evidence tier."""
        rel = normalize_relation_key(relation)
        if not rel:
            raise ValueError("relation name is required")
        if exclusivity is not None and exclusivity not in EXCLUSIVITIES:
            raise ValueError(f"exclusivity must be one of {EXCLUSIVITIES}")
        if default_evidence is not None and default_evidence not in EVIDENCE_TIERS:
            raise ValueError(f"default_evidence must be one of {tuple(EVIDENCE_TIERS)}")
        if decay_override is not None and decay_override not in DECAY_TYPES:
            raise ValueError(f"decay_override must be one of {DECAY_TYPES}")
        al = [normalize_relation_key(a) for a in (aliases or ()) if normalize_relation_key(a)]
        nal = [normalize_relation_key(a) for a in (negative_aliases or ()) if normalize_relation_key(a)]
        with self.store.lock:
            self.store.db.execute(
                "INSERT OR REPLACE INTO mg_rules "
                "(relation, aliases, negative_aliases, negative_label, exclusivity, signed, "
                " default_evidence, min_points, decay_override, tracks_stats, max_per_day, unit) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (rel, json.dumps(al), json.dumps(nal),
                 normalize_relation_key(negative_label) if negative_label else None,
                 exclusivity, None if signed is None else int(bool(signed)),
                 default_evidence, None if min_points is None else int(min_points),
                 decay_override, None if tracks_stats is None else int(bool(tracks_stats)),
                 None if max_per_day is None else int(max_per_day), unit),
            )
            self.store.db.commit()
        return self.rules()[rel]

    def resolve_relation(self, raw_relation, sign=1, allow_unknown=False):
        """Resolve a raw predicate into (canonical_relation, effective_sign, rule_dict).
        Negative aliases (e.g. DISLIKES, WORSENS) automatically map to the canonical relation with sign=-1."""
        key = normalize_relation_key(raw_relation)
        if not key:
            raise ValueError("relation is required")
        s = -1 if int(sign or 1) < 0 else 1
        reg = self.rules()
        if key in reg:
            return key, s, reg[key]
        for canon, rule in reg.items():
            if key in rule.get("aliases", ()):
                return canon, s, rule
            if key in rule.get("negative_aliases", ()):
                return canon, -s, rule
        if allow_unknown:
            return key, s, {
                "aliases": (), "negative_aliases": (), "negative_label": None,
                "exclusivity": None, "signed": None, "default_evidence": None,
                "min_points": None, "decay_override": None, "tracks_stats": None,
                "max_per_day": None, "unit": None,
            }
        raise ValueError(f"unknown canonical relation: {raw_relation!r}")

    def _effective_rule(self, rule, evidence=None):
        """Fill in any blank (None) fields on a relation rule from the evidence tier defaults."""
        ev = evidence or rule.get("default_evidence") or EVIDENCE_INFERRED
        if ev not in EVIDENCE_TIERS:
            raise ValueError(f"evidence must be one of {tuple(EVIDENCE_TIERS)}")
        tier = EVIDENCE_TIERS[ev]
        exclusivity = rule.get("exclusivity") or EXCLUSIVITY_ADDITIVE
        signed = True if rule.get("signed") is None else bool(rule["signed"])
        # If the rule didn't hardcode min_points (left blank), use the evidence tier default.
        # If the rule did set min_points, direct evidence still respects at most rule["min_points"].
        min_points = rule["min_points"] if rule.get("min_points") is not None else tier["min_points"]
        return {
            "evidence": ev,
            "exclusivity": exclusivity,
            "signed": signed,
            "min_points": max(1, int(min_points)),
            "init_stability": tier["init_stability"],
            "bolster_step": tier["bolster_step"],
            "decay_override": rule.get("decay_override"),
            "tracks_stats": rule.get("tracks_stats"),
            "max_per_day": rule.get("max_per_day"),
            "unit": rule.get("unit"),
            "negative_label": rule.get("negative_label"),
        }

    # ---- nodes & canonical alias resolution ---------------------------------------------------------------------
    def _node_dict(self, r):
        if not r:
            return None
        d = dict(r)
        d["aliases"] = json.loads(d.get("aliases") or "[]")
        return d

    def get_node(self, node_id):
        r = self.store.db.execute("SELECT * FROM mg_nodes WHERE id=?", (node_id,)).fetchone()
        return self._node_dict(r)

    def find_node(self, name, owner=None, scope=None, viewer=None):
        """Resolve a node by canonical_name or any alias (respecting owner/scope visibility)."""
        target = normalize_name(name)
        if not target:
            return None
        rows = self.store.db.execute("SELECT * FROM mg_nodes ORDER BY id").fetchall()
        # Prefer exact owner+scope match first, then household match.
        candidates = []
        for r in rows:
            if scope is not None and r["scope"] != scope:
                continue
            if scope == "personal" and (r["owner"] or "").lower() != (owner or "").lower():
                continue
            if not visible({"scope": r["scope"], "owner": r["owner"]}, viewer if viewer is not None else owner):
                continue
            aliases = json.loads(r["aliases"] or "[]")
            if r["canonical_name"] == target or target in aliases:
                exact_owner = (r["owner"] or "").lower() == (owner or "").lower() and (scope is None or r["scope"] == scope)
                candidates.append((0 if exact_owner else 1, 0 if r["canonical_name"] == target else 1, r))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1], x[2]["id"]))
        return self._node_dict(candidates[0][2])

    def upsert_node(self, name, kind="concept", category="GENERAL", aliases=(),
                    owner=None, scope="household", ts=None):
        """Find existing node by canonical name or alias, or create one. Merges new aliases cleanly."""
        canon = normalize_name(name)
        if not canon:
            raise ValueError("node name is required")
        now = float(self.clock() if ts is None else ts)
        scope = "personal" if scope == "personal" and owner else "household"
        node_owner = owner if scope == "personal" else None
        norm_aliases = []
        for a in (aliases or ()):
            na = normalize_name(a)
            if na and na != canon and na not in norm_aliases:
                norm_aliases.append(na)

        existing = self.find_node(canon, owner=node_owner, scope=scope, viewer=node_owner)
        if not existing:
            for a in norm_aliases:
                existing = self.find_node(a, owner=node_owner, scope=scope, viewer=node_owner)
                if existing:
                    break
        if existing:
            merged = list(existing["aliases"])
            for a in ([canon] if canon != existing["canonical_name"] else []) + norm_aliases:
                if a and a != existing["canonical_name"] and a not in merged:
                    merged.append(a)
            cat = existing["category"]
            if cat == "GENERAL" and category and category != "GENERAL":
                cat = str(category).upper()
            with self.store.lock:
                self.store.db.execute(
                    "UPDATE mg_nodes SET aliases=?, category=?, updated=? WHERE id=?",
                    (json.dumps(merged), cat, now, existing["id"]),
                )
                self.store.db.commit()
            return self.get_node(existing["id"])

        cat = str(category or "GENERAL").upper()
        with self.store.lock:
            cur = self.store.db.execute(
                "INSERT INTO mg_nodes (canonical_name, kind, category, aliases, owner, scope, created, updated) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (canon, kind or "concept", cat, json.dumps(norm_aliases), node_owner, scope, now, now),
            )
            self.store.db.commit()
            nid = cur.lastrowid
        return self.get_node(nid)

    # ---- link helpers -------------------------------------------------------------------------------------------
    def _link_dict(self, r, now=None):
        if not r:
            return None
        d = dict(r)
        d["signed"] = bool(d["signed"])
        d["dormant"] = bool(d["dormant"])
        d["context_tags"] = [t for t in (d.get("context_tags") or "").split(",") if t]
        d["distinct_sources"] = json.loads(d.get("distinct_sources") or "[]")
        d["provenance"] = json.loads(d.get("provenance") or "[]")
        d["meta"] = json.loads(d.get("meta") or "{}")
        d["stats"] = {
            "count": int(d["stat_count"] or 0),
            "mean": d["stat_mean"],
            "std_dev": round(welford_std(d["stat_count"], d["stat_m2"]), 4) if d["stat_count"] else 0.0,
            "min": d["stat_min"],
            "max": d["stat_max"],
            "unit": d["stat_unit"],
            "context_tags": d["context_tags"],
        }
        if now is not None:
            anc_s = d["anchor_stability"] if d.get("anchor_ts") else d["stability_score"]
            anc_t = d["anchor_ts"] if d.get("anchor_ts") else d["last_stated_on"]
            d["effective_stability"] = effective_stability(
                anc_s, d["decay_type"], anc_t, now, status=d["status"]
            )
        return d

    def get_link(self, link_id, now=None):
        r = self.store.db.execute("SELECT * FROM mg_links WHERE id=?", (link_id,)).fetchone()
        return self._link_dict(r, now=now)

    def _raw_link_row(self, link_id):
        r = self.store.db.execute("SELECT * FROM mg_links WHERE id=?", (link_id,)).fetchone()
        return dict(r) if r else None

    def _find_link(self, source_id, target_id, relation, ctx_key, owner, scope):
        r = self.store.db.execute(
            "SELECT * FROM mg_links WHERE source_id=? AND target_id=? AND relation=? "
            "AND context_tags=? AND COALESCE(owner,'')=? AND scope=?",
            (source_id, target_id, relation, ctx_key, owner or "", scope),
        ).fetchone()
        return dict(r) if r else None

    def _next_status_and_decay(self, mag, evidence_count, eff_rule, current_status=None, contested=False):
        """Derive status & decay_type from |S|, evidence_count, and relation rule."""
        if contested:
            return STATUS_CONTESTED, DECAY_LOGISTIC
        if eff_rule.get("decay_override") == DECAY_ZERO and mag >= FACT_THRESHOLD and evidence_count >= eff_rule["min_points"]:
            return STATUS_FACT, DECAY_ZERO
        if mag >= FACT_THRESHOLD and evidence_count >= eff_rule["min_points"]:
            return STATUS_FACT, DECAY_ZERO
        if mag >= IDEA_THRESHOLD or current_status == STATUS_FACT:
            return STATUS_IDEA, DECAY_LOGISTIC
        return STATUS_THOUGHT, DECAY_LINEAR

    # ---- core observation, bolster, contradiction, and fan-out --------------------------------------------------
    def observe(self, source, relation, target, *,
                sign=1, certainty=1.0, evidence=None, value=None, unit=None,
                context_tags=(), category="GENERAL", source_ref="", quote="",
                ts=None, valid_from=None, owner=None, scope="household",
                confidence=1.0, loop_back=False, allow_unknown_relation=False,
                record_staging=True):
        """Record a single observation or reinforcement into the graph.

        Handles:
        - Hypothetical / sarcasm guard (certainty <= 0 -> no-op).
        - Canonical relation & negative-alias normalization (e.g. DISLIKES -> LIKES, sign=-1).
        - Distinct-source deduplication for inferred links and max_per_day caps for high-frequency sensors.
        - Signed contradiction & demotion hysteresis on same link AND EXCLUSIVE/SOFT_EXCLUSIVE siblings.
        - Direct-vs-sensor protection (sensor_fact never silently erases a direct FACT).
        - Welford online running statistics isolated by context_tags.
        - Dormant wake-up preserving historical stats & evidence_count.
        - Staging buffer logging with automatic triage flagging.
        """
        now = float(self.clock() if ts is None else ts)
        vf = min(float(valid_from), now) if valid_from is not None else now
        gamma = max(0.0, min(1.0, float(certainty)))
        scope = "personal" if scope == "personal" and owner else "household"
        link_owner = owner if scope == "personal" else None
        cat = str(category or "GENERAL").upper()

        canon_rel, eff_sign, rule = self.resolve_relation(relation, sign=sign, allow_unknown=allow_unknown_relation)
        eff = self._effective_rule(rule, evidence=evidence)
        ctx = normalize_tags(context_tags)
        ctx_k = ",".join(ctx)

        if gamma <= 0.0:
            # Hypothetical / sarcasm / question: zero mutation!
            return {"changed": False, "reason": "zero_certainty", "link": None, "contested_links": []}

        src_node = self.upsert_node(source, kind="entity", category=cat, owner=link_owner, scope=scope, ts=now)
        tgt_node = self.upsert_node(target, kind="concept", category=cat, owner=link_owner, scope=scope, ts=now)
        existing = self._find_link(src_node["id"], tgt_node["id"], canon_rel, ctx_k, link_owner, scope)
        before_state = dict(existing) if existing else None

        day = iso_day(now)
        stat_unit = unit or eff["unit"]
        src_ref = str(source_ref or "").strip()
        q_text = str(quote or "").strip()[:240]

        flag_reasons = []
        if loop_back:
            flag_reasons.append("loop_back")
        if float(confidence) < TRIAGE_CONFIDENCE_FLOOR:
            flag_reasons.append("low_confidence")

        contested_siblings = []
        created_new = existing is None

        if existing is None:
            # Unsigned relation with negative sign has nothing to demote yet -> no-op.
            if not eff["signed"] and eff_sign < 0:
                return {"changed": False, "reason": "unsigned_negative_no_target", "link": None, "contested_links": []}

            init_mag = eff["init_stability"] * gamma
            # Single-point direct rules (e.g. ALLERGIC_TO, LIVES_IN, WORKS_AT) with full certainty promote immediately.
            if eff["evidence"] == EVIDENCE_DIRECT and eff["min_points"] == 1 and gamma >= 0.85:
                init_mag = 1.0 if eff["decay_override"] == DECAY_ZERO else max(init_mag, 0.90)
            init_mag = max(0.0, min(1.0, init_mag))
            status, decay_type = self._next_status_and_decay(init_mag, 1, eff)
            if status == STATUS_FACT:
                init_mag = 1.0
                flag_reasons.append("crossed_fact_threshold")
            signed_s = init_mag * (eff_sign if eff["signed"] else 1)

            sc, sm, sm2, smin, smax = (0, None, 0.0, None, None)
            if value is not None:
                sc, sm, sm2, smin, smax = welford_update(0, None, 0.0, None, None, value)

            distinct = [src_ref] if src_ref else []
            prov = [{"ts": now, "source": src_ref, "quote": q_text, "evidence": eff["evidence"], "sign": eff_sign}] if (src_ref or q_text) else []
            meta = {"last_reinforce_day": day, "day_reinforce_count": 1}

            with self.store.lock:
                cur = self.store.db.execute(
                    "INSERT INTO mg_links ("
                    "source_id, target_id, relation, category, context_tags, exclusivity, signed, evidence, "
                    "status, stability_score, anchor_stability, anchor_ts, decay_type, dormant, "
                    "stat_count, stat_mean, stat_m2, stat_min, stat_max, stat_unit, "
                    "evidence_count, distinct_sources, provenance, access_count, shown_count, last_accessed_on, "
                    "created_on, last_stated_on, valid_from, valid_to, owner, scope, meta"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (src_node["id"], tgt_node["id"], canon_rel, cat, ctx_k, eff["exclusivity"], int(eff["signed"]),
                     eff["evidence"], status, signed_s, signed_s, now, decay_type, int(abs(signed_s) <= DORMANT_THRESHOLD),
                     sc, sm, sm2, smin, smax, stat_unit,
                     1, json.dumps(distinct), json.dumps(prov), 0, 0, None,
                     now, now, vf, None, link_owner, scope, json.dumps(meta)),
                )
                link_id = cur.lastrowid
                self.store.db.execute(
                    "INSERT INTO mg_episodes (link_id, ts, day, value, unit, sign, certainty, evidence, source_ref, quote, owner, scope) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (link_id, now, day, value, stat_unit, eff_sign, gamma, eff["evidence"], src_ref, q_text, link_owner, scope),
                )
                self.store.db.commit()

            # Check EXCLUSIVE / SOFT_EXCLUSIVE competing targets when a positive link is created.
            if eff_sign > 0 and eff["exclusivity"] in (EXCLUSIVITY_EXCLUSIVE, EXCLUSIVITY_SOFT):
                contested_siblings = self._dampen_competing(
                    src_node["id"], tgt_node["id"], canon_rel, ctx_k, link_owner, scope,
                    eff["exclusivity"], gamma, now, tgt_node["canonical_name"],
                )
        else:
            link_id = existing["id"]
            old_status = existing["status"]
            old_anc_s = float(existing["anchor_stability"] if existing.get("anchor_ts") else existing["stability_score"])
            old_anc_t = float(existing["anchor_ts"] if existing.get("anchor_ts") else existing["last_stated_on"])
            s_cur = effective_stability(old_anc_s, existing["decay_type"], old_anc_t, now, status=old_status)
            meta = json.loads(existing["meta"] or "{}")
            distinct = json.loads(existing["distinct_sources"] or "[]")
            prov = json.loads(existing["provenance"] or "[]")
            ev_count = int(existing["evidence_count"] or 1)
            link_ev = existing["evidence"]
            was_dormant = bool(existing["dormant"]) or abs(s_cur) <= DORMANT_THRESHOLD

            # Update Welford numeric stats unconditionally on every numeric sample.
            sc, sm, sm2, smin, smax = (
                existing["stat_count"], existing["stat_mean"], existing["stat_m2"],
                existing["stat_min"], existing["stat_max"],
            )
            if value is not None:
                sc, sm, sm2, smin, smax = welford_update(sc, sm, sm2, smin, smax, value)
                stat_unit = stat_unit or existing["stat_unit"]
            else:
                stat_unit = existing["stat_unit"] or stat_unit

            # Determine whether this observation reinforces the same polarity or contradicts it.
            cur_sign = _sign(s_cur) if abs(s_cur) > 1e-9 else _sign(old_anc_s)
            same_direction = (eff_sign == cur_sign) if eff["signed"] else (eff_sign > 0)

            # Check distinct-source deduplication (for inferred links) & max_per_day rate cap.
            duplicate_source = (
                same_direction
                and eff["evidence"] == EVIDENCE_INFERRED
                and bool(src_ref)
                and src_ref in distinct
            )
            day_capped = False
            if same_direction and eff["max_per_day"] is not None:
                if meta.get("last_reinforce_day") == day and int(meta.get("day_reinforce_count") or 0) >= int(eff["max_per_day"]):
                    day_capped = True

            contested_now = False
            new_anc_s = old_anc_s
            new_anc_t = old_anc_t
            if same_direction:
                # Upgrade evidence tier if a direct statement confirms an earlier inferred/sensor hypothesis.
                if eff["evidence"] == EVIDENCE_DIRECT and link_ev != EVIDENCE_DIRECT:
                    link_ev = EVIDENCE_DIRECT
                if src_ref and src_ref not in distinct:
                    distinct = (distinct + [src_ref])[-MAX_DISTINCT_SOURCES:]
                if not duplicate_source and not day_capped:
                    ev_count += 1
                    meta["last_reinforce_day"] = day
                    meta["day_reinforce_count"] = (
                        int(meta.get("day_reinforce_count") or 0) + 1
                        if meta.get("last_reinforce_day") == day else 1
                    )
                    base_mag = abs(s_cur)
                    # Seasonal dormant wake-up: recover smoothly if it had prior history.
                    if was_dormant and ev_count >= 2:
                        base_mag = max(base_mag, 0.22)
                    step = eff["bolster_step"] * gamma
                    if eff["evidence"] == EVIDENCE_DIRECT and eff["min_points"] == 1 and gamma >= 0.85:
                        new_mag = 1.0
                    elif base_mag < IDEA_THRESHOLD:
                        new_mag = min(1.0, base_mag + step)
                    else:
                        # Diminishing-returns climb in IDEA tier toward 1.0
                        headroom = max(0.05, 1.0 - base_mag)
                        if eff["evidence"] == EVIDENCE_SENSOR:
                            new_mag = min(1.0, base_mag + max(step * 0.65, step * headroom * 1.6))
                        else:
                            new_mag = min(1.0, base_mag + max(step * 0.80, step * headroom * 2.2))
                    # Respect min_points gate: do not overshoot FACT_THRESHOLD before hitting min_points.
                    eff_for_link = self._effective_rule(rule, evidence=link_ev)
                    if ev_count < eff_for_link["min_points"]:
                        new_mag = min(new_mag, FACT_THRESHOLD - 0.02)
                    status, decay_type = self._next_status_and_decay(new_mag, ev_count, eff_for_link, current_status=old_status)
                    if status == STATUS_FACT:
                        new_mag = 1.0
                        if old_status != STATUS_FACT:
                            flag_reasons.append("crossed_fact_threshold")
                    new_s = new_mag * (eff_sign if eff["signed"] else 1)
                    new_anc_s = new_s
                    new_anc_t = now
                    dormant_flag = int(abs(new_s) <= DORMANT_THRESHOLD)
                else:
                    # Deduplicated or daily-capped: preserve stability/status/anchor, still update stats & episode.
                    new_s = s_cur
                    status = old_status
                    decay_type = existing["decay_type"]
                    dormant_flag = int(existing["dormant"])
            else:
                # Contradictory signal (opposite sign on signed link, or negative demotion on unsigned link).
                age_days = max(0.0, (now - float(existing["last_stated_on"])) / 86400.0)
                base_pen = BASE_CONTRADICTION_PENALTY
                # Protect direct FACTs from silent sensor overwrite: sensor contradiction applies gentler penalty
                # and can only contest, never silently flip sign.
                if link_ev == EVIDENCE_DIRECT and eff["evidence"] == EVIDENCE_SENSOR:
                    pen = contradiction_penalty(base_pen * 0.5, gamma, age_days, EXCLUSIVITY_EXCLUSIVE)
                    new_mag = max(0.0, abs(s_cur) - pen)
                    if old_status == STATUS_FACT and new_mag < FACT_THRESHOLD:
                        contested_now = True
                        status, decay_type = STATUS_CONTESTED, DECAY_LOGISTIC
                        flag_reasons.append("contested")
                        flag_reasons.append("crossed_fact_threshold")
                    else:
                        status = old_status
                        decay_type = existing["decay_type"]
                    new_s = new_mag * cur_sign
                    dormant_flag = int(abs(new_s) <= DORMANT_THRESHOLD)
                elif eff["signed"] and link_ev != EVIDENCE_DIRECT and eff["evidence"] == EVIDENCE_DIRECT and old_status != STATUS_FACT:
                    # A direct user statement immediately overrides a weak inferred/sensor hypothesis!
                    link_ev = EVIDENCE_DIRECT
                    ev_count = 1
                    distinct = [src_ref] if src_ref else []
                    new_mag = max(eff["init_stability"] * gamma, 0.65 * gamma)
                    eff_for_link = self._effective_rule(rule, evidence=link_ev)
                    status, decay_type = self._next_status_and_decay(new_mag, ev_count, eff_for_link)
                    new_s = new_mag * eff_sign
                    dormant_flag = int(abs(new_s) <= DORMANT_THRESHOLD)
                else:
                    pen = contradiction_penalty(base_pen, gamma, age_days, EXCLUSIVITY_EXCLUSIVE)
                    if eff["signed"]:
                        # Push signed score across 0 toward opposite sign.
                        raw_new = s_cur + eff_sign * pen
                        if old_status == STATUS_FACT and abs(raw_new) < FACT_THRESHOLD:
                            contested_now = True
                            status, decay_type = STATUS_CONTESTED, DECAY_LOGISTIC
                            flag_reasons.append("contested")
                            flag_reasons.append("crossed_fact_threshold")
                            # Keep slight residual on original side until user resolves or repeats.
                            if _sign(raw_new) != cur_sign:
                                raw_new = 0.0
                        else:
                            eff_for_link = self._effective_rule(rule, evidence=link_ev)
                            if _sign(raw_new) != cur_sign and abs(raw_new) > DORMANT_THRESHOLD:
                                ev_count = 1
                                if eff["evidence"] == EVIDENCE_DIRECT:
                                    link_ev = EVIDENCE_DIRECT
                            status, decay_type = self._next_status_and_decay(
                                abs(raw_new), ev_count, eff_for_link,
                                contested=(old_status == STATUS_CONTESTED and _sign(raw_new) == cur_sign),
                            )
                        new_s = max(-1.0, min(1.0, raw_new))
                    else:
                        new_mag = max(0.0, abs(s_cur) - pen)
                        if old_status == STATUS_FACT and new_mag < FACT_THRESHOLD:
                            contested_now = True
                            status, decay_type = STATUS_CONTESTED, DECAY_LOGISTIC
                            flag_reasons.append("contested")
                            flag_reasons.append("crossed_fact_threshold")
                        else:
                            eff_for_link = self._effective_rule(rule, evidence=link_ev)
                            status, decay_type = self._next_status_and_decay(
                                new_mag, ev_count, eff_for_link,
                                contested=(old_status == STATUS_CONTESTED),
                            )
                        new_s = new_mag
                    dormant_flag = int(abs(new_s) <= DORMANT_THRESHOLD)
                new_anc_s = new_s
                new_anc_t = now

            if src_ref or q_text:
                prov = (prov + [{"ts": now, "source": src_ref, "quote": q_text,
                                 "evidence": eff["evidence"], "sign": eff_sign}])[-MAX_PROVENANCE:]

            with self.store.lock:
                self.store.db.execute(
                    "UPDATE mg_links SET "
                    "evidence=?, status=?, stability_score=?, anchor_stability=?, anchor_ts=?, decay_type=?, dormant=?, "
                    "stat_count=?, stat_mean=?, stat_m2=?, stat_min=?, stat_max=?, stat_unit=?, "
                    "evidence_count=?, distinct_sources=?, provenance=?, last_stated_on=?, meta=? "
                    "WHERE id=?",
                    (link_ev, status, new_s, new_anc_s, new_anc_t, decay_type, dormant_flag,
                     sc, sm, sm2, smin, smax, stat_unit,
                     ev_count, json.dumps(distinct), json.dumps(prov), now, json.dumps(meta),
                     link_id),
                )
                self.store.db.execute(
                    "INSERT INTO mg_episodes (link_id, ts, day, value, unit, sign, certainty, evidence, source_ref, quote, owner, scope) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (link_id, now, day, value, stat_unit, eff_sign, gamma, eff["evidence"], src_ref, q_text, link_owner, scope),
                )
                self.store.db.commit()

            if eff_sign > 0 and eff["exclusivity"] in (EXCLUSIVITY_EXCLUSIVE, EXCLUSIVITY_SOFT) and not contested_now:
                contested_siblings = self._dampen_competing(
                    src_node["id"], tgt_node["id"], canon_rel, ctx_k, link_owner, scope,
                    eff["exclusivity"], gamma, now, tgt_node["canonical_name"],
                )

        if contested_siblings and "contested" not in flag_reasons:
            flag_reasons.append("contested")

        after_state = self._raw_link_row(link_id)
        staging_id = None
        if record_staging:
            patch_payload = {
                "op": "CREATE_LINK" if created_new else ("BOLSTER" if eff_sign > 0 else "DEMOTE"),
                "source": src_node["canonical_name"],
                "relation": canon_rel,
                "target": tgt_node["canonical_name"],
                "sign": eff_sign,
                "certainty": gamma,
                "evidence": eff["evidence"],
                "value": value,
                "unit": stat_unit,
                "context_tags": ctx,
            }
            staging_id = self._record_staging(
                op=patch_payload["op"], link_id=link_id, patch=patch_payload,
                before_state=before_state, after_state=after_state,
                confidence=confidence, flag_reasons=flag_reasons,
                ts=now, owner=link_owner, scope=scope,
            )

        return {
            "changed": True,
            "created": created_new,
            "link": self.get_link(link_id, now=now),
            "contested_links": contested_siblings,
            "staging_id": staging_id,
            "flagged": bool(flag_reasons),
            "flag_reasons": flag_reasons,
        }

    def _dampen_competing(self, source_id, keep_target_id, relation, ctx_key, owner, scope,
                          exclusivity, certainty, now, new_target_name):
        """Apply contradiction dampening to competing targets on EXCLUSIVE / SOFT_EXCLUSIVE relations."""
        rows = self.store.db.execute(
            "SELECT * FROM mg_links WHERE source_id=? AND relation=? AND context_tags=? "
            "AND COALESCE(owner,'')=? AND scope=? AND target_id!=? AND dormant=0 AND stability_score > 0",
            (source_id, relation, ctx_key, owner or "", scope, keep_target_id),
        ).fetchall()
        contested = []
        for r in rows:
            old_status = r["status"]
            anc_s = r["anchor_stability"] if r["anchor_ts"] else r["stability_score"]
            anc_t = r["anchor_ts"] if r["anchor_ts"] else r["last_stated_on"]
            s_cur = effective_stability(anc_s, r["decay_type"], anc_t, now, status=old_status)
            age_days = max(0.0, (now - float(r["last_stated_on"])) / 86400.0)
            pen = contradiction_penalty(BASE_CONTRADICTION_PENALTY, certainty, age_days, exclusivity)
            new_s = max(0.0, s_cur - pen)
            meta = json.loads(r["meta"] or "{}")
            new_status = old_status
            new_decay = r["decay_type"]
            if old_status == STATUS_FACT and new_s < FACT_THRESHOLD:
                new_status = STATUS_CONTESTED
                new_decay = DECAY_LOGISTIC
                meta["contested_by"] = new_target_name
                meta["contested_on"] = now
            elif old_status != STATUS_FACT and new_s < IDEA_THRESHOLD:
                new_status = STATUS_THOUGHT
                new_decay = DECAY_LINEAR
            dormant = int(new_s <= DORMANT_THRESHOLD)
            # Reset decay anchor to (new_s, now) so newly CONTESTED links decay smoothly from contest time.
            with self.store.lock:
                self.store.db.execute(
                    "UPDATE mg_links SET status=?, stability_score=?, anchor_stability=?, anchor_ts=?, "
                    "decay_type=?, dormant=?, meta=? WHERE id=?",
                    (new_status, new_s, new_s, now, new_decay, dormant, json.dumps(meta), r["id"]),
                )
                self.store.db.commit()
            if new_status == STATUS_CONTESTED:
                contested.append(self.get_link(r["id"], now=now))
        return contested

    def record_with_hypotheses(self, source, relation, target, hypotheses=(), *,
                               category="GENERAL", context_tags=(), source_ref="", quote="",
                               ts=None, owner=None, scope="household", certainty=1.0,
                               link_attributes=True):
        """Record a direct statement AND fan out up to MAX_FAN_OUT inferred hypotheses tied to that source.

        Example: 'I like Kung Pao chicken' ->
          direct:   (user) --[LIKES]--> (kung pao chicken)
          inferred: (user) --[LIKES]--> (chinese), (spicy savory), (peanuts)
          plus optional structural edges: (kung pao chicken) --[HAS_ATTRIBUTE]--> (chinese), ...
        """
        now = float(self.clock() if ts is None else ts)
        ref = source_ref or f"{normalize_name(target)}:{iso_day(now)}"
        primary = self.observe(
            source, relation, target,
            certainty=certainty, evidence=EVIDENCE_DIRECT,
            context_tags=context_tags, category=category,
            source_ref=ref, quote=quote, ts=now, owner=owner, scope=scope,
        )
        inferred_results = []
        for h in list(hypotheses or ())[:MAX_FAN_OUT]:
            if isinstance(h, dict):
                h_rel = h.get("relation") or relation
                h_tgt = h["target"]
                h_cat = h.get("category") or category
                h_sign = h.get("sign", 1)
                h_tags = h.get("context_tags", context_tags)
            else:
                h_rel, h_tgt = h[0], h[1]
                h_cat = h[2] if len(h) > 2 else category
                h_sign = h[3] if len(h) > 3 else 1
                h_tags = context_tags
            res = self.observe(
                source, h_rel, h_tgt,
                sign=h_sign, certainty=certainty, evidence=EVIDENCE_INFERRED,
                context_tags=h_tags, category=h_cat,
                source_ref=ref, quote=quote, ts=now, owner=owner, scope=scope,
            )
            inferred_results.append(res)
            if link_attributes:
                self.observe(
                    target, "HAS_ATTRIBUTE", h_tgt,
                    certainty=certainty, evidence=EVIDENCE_INFERRED,
                    category=h_cat, source_ref=ref, ts=now,
                    owner=owner, scope=scope, record_staging=False,
                )
        return {"primary": primary, "hypotheses": inferred_results}

    # ---- patch execution & staging audit/revert -----------------------------------------------------------------
    def _record_staging(self, op, link_id, patch, before_state, after_state,
                        confidence=1.0, flag_reasons=(), ts=None, owner=None, scope="household"):
        now = float(self.clock() if ts is None else ts)
        reasons = list(dict.fromkeys(flag_reasons or ()))
        flagged = int(bool(reasons))
        with self.store.lock:
            cur = self.store.db.execute(
                "INSERT INTO mg_staging (ts, op, link_id, patch, before_state, after_state, "
                "confidence, flagged, flag_reasons, audit_status, owner, scope) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, op, link_id, json.dumps(patch),
                 json.dumps(before_state) if before_state is not None else None,
                 json.dumps(after_state) if after_state is not None else None,
                 float(confidence), flagged, json.dumps(reasons),
                 "pending" if flagged else "auto_ok", owner, scope),
            )
            self.store.db.commit()
            return cur.lastrowid

    def apply_patch(self, patch, *, ts=None, owner=None, scope="household", loop_back=False):
        """Execute a structured Archivist JSON patch (CREATE_NODE, CREATE_LINK, BOLSTER, DEMOTE, UPDATE)."""
        if not isinstance(patch, dict) or not patch.get("op"):
            raise ValueError("patch must be a dict with 'op'")
        op = str(patch["op"]).upper()
        now = float(self.clock() if ts is None else ts)
        conf = float(patch.get("confidence", 1.0))

        if op == "CREATE_NODE":
            node = self.upsert_node(
                patch["canonical_name"],
                kind=patch.get("kind", "concept"),
                category=patch.get("category", "GENERAL"),
                aliases=patch.get("aliases") or (),
                owner=owner, scope=scope, ts=now,
            )
            return {"op": op, "node": node}

        if op in ("CREATE_LINK", "BOLSTER", "DEMOTE"):
            val_upd = patch.get("value_update") or {}
            value = val_upd.get("observed_value", patch.get("value"))
            unit = val_upd.get("unit", patch.get("unit"))
            certainty = float(patch.get("certainty", 1.0))

            if patch.get("target_link_id") is not None:
                lnk = self.get_link(int(patch["target_link_id"]))
                if not lnk:
                    raise ValueError(f"link {patch['target_link_id']} not found")
                src = self.get_node(lnk["source_id"])["canonical_name"]
                tgt = self.get_node(lnk["target_id"])["canonical_name"]
                rel = lnk["relation"]
                ctx = patch.get("context_tags", lnk["context_tags"])
                cat = patch.get("category", lnk["category"])
                ev = patch.get("evidence", lnk["evidence"])
                sign = patch.get("sign", -1 if op == "DEMOTE" else _sign(lnk["stability_score"]))
            else:
                src = patch["source"]
                rel = patch["relation"]
                tgt = patch["target"]
                ctx = patch.get("context_tags") or ()
                cat = patch.get("category", "GENERAL")
                ev = patch.get("evidence")
                sign = patch.get("sign", -1 if op == "DEMOTE" else 1)

            return self.observe(
                src, rel, tgt,
                sign=sign, certainty=certainty, evidence=ev,
                value=value, unit=unit, context_tags=ctx, category=cat,
                source_ref=patch.get("source_ref", ""), quote=patch.get("quote", ""),
                ts=now, valid_from=patch.get("valid_from"),
                owner=owner, scope=scope, confidence=conf, loop_back=loop_back,
            )

        if op == "UPDATE":
            lid = int(patch["target_link_id"])
            before = self._raw_link_row(lid)
            if not before:
                raise ValueError(f"link {lid} not found")
            allowed = ("category", "valid_from", "valid_to", "status", "stat_unit")
            sets, vals = [], []
            for k in allowed:
                if k in patch:
                    sets.append(f"{k}=?")
                    vals.append(patch[k])
            if not sets:
                return {"op": op, "changed": False, "link": self.get_link(lid, now=now)}
            vals.append(lid)
            with self.store.lock:
                self.store.db.execute(f"UPDATE mg_links SET {', '.join(sets)} WHERE id=?", vals)
                self.store.db.commit()
            after = self._raw_link_row(lid)
            reasons = ["low_confidence"] if conf < TRIAGE_CONFIDENCE_FLOOR else []
            if patch.get("status") == STATUS_CONTESTED:
                reasons.append("contested")
            sid = self._record_staging(
                op=op, link_id=lid, patch=patch, before_state=before, after_state=after,
                confidence=conf, flag_reasons=reasons, ts=now, owner=before["owner"], scope=before["scope"],
            )
            return {"op": op, "changed": True, "link": self.get_link(lid, now=now), "staging_id": sid}

        raise ValueError(f"unsupported patch op: {op!r}")

    def flagged_staging(self, limit=50):
        rows = self.store.db.execute(
            "SELECT * FROM mg_staging WHERE flagged=1 AND audit_status='pending' ORDER BY ts ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["patch"] = json.loads(d["patch"] or "{}")
            d["flag_reasons"] = json.loads(d["flag_reasons"] or "[]")
            d["before_state"] = json.loads(d["before_state"]) if d["before_state"] else None
            d["after_state"] = json.loads(d["after_state"]) if d["after_state"] else None
            out.append(d)
        return out

    def revert_staging(self, staging_id):
        """Deterministically revert a staging entry back to its recorded before_state (or delete if newly created)."""
        r = self.store.db.execute("SELECT * FROM mg_staging WHERE id=?", (int(staging_id),)).fetchone()
        if not r:
            raise ValueError(f"staging {staging_id} not found")
        link_id = r["link_id"]
        before = json.loads(r["before_state"]) if r["before_state"] else None
        with self.store.lock:
            if before is None and link_id is not None:
                self.store.db.execute("DELETE FROM mg_episodes WHERE link_id=?", (link_id,))
                self.store.db.execute("DELETE FROM mg_links WHERE id=?", (link_id,))
            elif before is not None and link_id is not None:
                cols = [k for k in before.keys() if k != "id"]
                sets = ", ".join(f"{k}=?" for k in cols)
                self.store.db.execute(f"UPDATE mg_links SET {sets} WHERE id=?", [before[k] for k in cols] + [link_id])
            self.store.db.execute("UPDATE mg_staging SET audit_status='reverted' WHERE id=?", (int(staging_id),))
            self.store.db.commit()
        return {"reverted": True, "staging_id": int(staging_id), "link_id": link_id}

    # ---- passive decay sweep (dormant, never deletes) -----------------------------------------------------------
    def decay_sweep(self, now=None):
        """Materialize passive decay across all active non-ZERO links (idempotent via anchor_stability/anchor_ts).
        Links dropping to |S| <= DORMANT_THRESHOLD become dormant (never deleted)."""
        t = float(self.clock() if now is None else now)
        rows = self.store.db.execute(
            "SELECT id, status, stability_score, anchor_stability, anchor_ts, decay_type, last_stated_on "
            "FROM mg_links WHERE dormant=0 AND decay_type!='ZERO'"
        ).fetchall()
        went_dormant = 0
        demoted = 0
        with self.store.lock:
            for r in rows:
                anc_s = r["anchor_stability"] if r["anchor_ts"] else r["stability_score"]
                anc_t = r["anchor_ts"] if r["anchor_ts"] else r["last_stated_on"]
                s_eff = effective_stability(anc_s, r["decay_type"], anc_t, t, status=r["status"])
                mag = abs(s_eff)
                if mag <= DORMANT_THRESHOLD:
                    self.store.db.execute(
                        "UPDATE mg_links SET stability_score=?, dormant=1 WHERE id=?",
                        (s_eff, r["id"]),
                    )
                    went_dormant += 1
                elif r["status"] == STATUS_IDEA and mag < IDEA_THRESHOLD:
                    # Transitioning from LOGISTIC to LINEAR re-anchors cleanly at (s_eff, t).
                    self.store.db.execute(
                        "UPDATE mg_links SET stability_score=?, anchor_stability=?, anchor_ts=?, "
                        "status=?, decay_type=? WHERE id=?",
                        (s_eff, s_eff, t, STATUS_THOUGHT, DECAY_LINEAR, r["id"]),
                    )
                    demoted += 1
                else:
                    self.store.db.execute("UPDATE mg_links SET stability_score=? WHERE id=?", (s_eff, r["id"]))
            self.store.db.commit()
        return {"checked": len(rows), "went_dormant": went_dormant, "demoted_to_thought": demoted}

    # ---- compaction-first storage cap manager -------------------------------------------------------------------
    def compact_episodes(self, *, keep_recent_per_link=5, target_delete=None, min_age_days=0.0, now=None):
        """Roll old raw episodic rows into monthly aggregate rollups stored in link.meta['rollups'],
        then remove the compacted raw rows. Prioritizes sensor_fact -> inferred -> direct."""
        t = float(self.clock() if now is None else now)
        cutoff = t - max(0.0, float(min_age_days)) * 86400.0
        # Order candidate links: sensor_fact first, then inferred, then direct.
        order_case = "CASE evidence WHEN 'sensor_fact' THEN 0 WHEN 'inferred' THEN 1 ELSE 2 END"
        links = self.store.db.execute(
            f"SELECT id, meta, stat_unit FROM mg_links ORDER BY {order_case}, id"
        ).fetchall()

        compacted_total = 0
        links_compacted = 0
        for lnk in links:
            if target_delete is not None and compacted_total >= target_delete:
                break
            eps = self.store.db.execute(
                "SELECT * FROM mg_episodes WHERE link_id=? AND compacted=0 ORDER BY ts ASC",
                (lnk["id"],),
            ).fetchall()
            if len(eps) <= keep_recent_per_link:
                continue
            eligible = [e for e in eps[:-keep_recent_per_link] if e["ts"] <= cutoff]
            if not eligible:
                continue
            if target_delete is not None:
                need = target_delete - compacted_total
                eligible = eligible[:need]

            meta = json.loads(lnk["meta"] or "{}")
            rollups = dict(meta.get("rollups") or {})
            for e in eligible:
                period = iso_month(e["ts"])
                b = dict(rollups.get(period) or {
                    "count": 0, "num_count": 0, "mean": None, "m2": 0.0,
                    "min": None, "max": None, "unit": e["unit"] or lnk["stat_unit"],
                })
                b["count"] = int(b["count"]) + 1
                if e["value"] is not None:
                    nc, nm, nm2, nmin, nmax = welford_update(
                        b["num_count"], b["mean"], b["m2"], b["min"], b["max"], e["value"]
                    )
                    b.update({"num_count": nc, "mean": round(nm, 4), "m2": round(nm2, 6),
                              "std_dev": round(welford_std(nc, nm2), 4), "min": nmin, "max": nmax})
                rollups[period] = b
            meta["rollups"] = rollups
            ids = [e["id"] for e in eligible]
            qmarks = ",".join("?" for _ in ids)
            with self.store.lock:
                self.store.db.execute("UPDATE mg_links SET meta=? WHERE id=?", (json.dumps(meta), lnk["id"]))
                self.store.db.execute(f"DELETE FROM mg_episodes WHERE id IN ({qmarks})", ids)
                self.store.db.commit()
            compacted_total += len(ids)
            links_compacted += 1

        return {"compacted_episodes": compacted_total, "links_compacted": links_compacted}

    def enforce_storage_cap(self, *, max_episodes=5000, max_links=2000, keep_recent_per_link=5, now=None):
        """Tiered storage-pressure cleanup (only acts when over cap):
        1. Compact raw episodes into parent link rollups first (sensor_fact -> inferred -> direct).
        2. Only if total links still exceed max_links, evict lowest-value DORMANT non-FACT links,
           ranked by evidence tier, shown_count, access_count, recency, and |stability_score|.
        Confirmed FACTs, CONTESTED links, and active non-dormant links are never evicted."""
        t = float(self.clock() if now is None else now)
        ep_count = self.store.db.execute("SELECT COUNT(*) FROM mg_episodes WHERE compacted=0").fetchone()[0]
        link_count = self.store.db.execute("SELECT COUNT(*) FROM mg_links").fetchone()[0]

        compacted = {"compacted_episodes": 0, "links_compacted": 0}
        if ep_count > max_episodes:
            compacted = self.compact_episodes(
                keep_recent_per_link=keep_recent_per_link,
                target_delete=ep_count - max_episodes,
                now=t,
            )

        evicted_ids = []
        if link_count > max_links:
            # First ensure decay is up to date so dormant items are accurately identified.
            self.decay_sweep(now=t)
            need = link_count - max_links
            # Only dormant non-FACT, non-CONTESTED links are ever eligible for eviction.
            candidates = self.store.db.execute(
                "SELECT * FROM mg_links WHERE dormant=1 AND status NOT IN ('FACT', 'CONTESTED')"
            ).fetchall()

            def _retention_score(r):
                tier_w = EVIDENCE_TIERS.get(r["evidence"], EVIDENCE_TIERS[EVIDENCE_INFERRED])["evict_weight"]
                shown = int(r["shown_count"] or 0)
                accessed = int(r["access_count"] or 0)
                last_use = float(r["last_accessed_on"] or r["last_stated_on"] or 0.0)
                age_days = max(0.0, (t - last_use) / 86400.0)
                recency = 1.0 / (1.0 + age_days / 30.0)
                return (
                    shown * 5.0
                    + accessed * 1.5
                    + tier_w * 2.0
                    + abs(float(r["stability_score"])) * 2.0
                    + recency
                )

            ranked = sorted(candidates, key=_retention_score)
            to_evict = ranked[:need]
            if to_evict:
                evicted_ids = [r["id"] for r in to_evict]
                qmarks = ",".join("?" for _ in evicted_ids)
                with self.store.lock:
                    self.store.db.execute(f"DELETE FROM mg_episodes WHERE link_id IN ({qmarks})", evicted_ids)
                    self.store.db.execute(f"DELETE FROM mg_links WHERE id IN ({qmarks})", evicted_ids)
                    self.store.db.commit()

        protected_facts = self.store.db.execute("SELECT COUNT(*) FROM mg_links WHERE status='FACT'").fetchone()[0]
        return {
            "compacted_episodes": compacted["compacted_episodes"],
            "links_compacted": compacted["links_compacted"],
            "evicted_links": len(evicted_ids),
            "evicted_ids": evicted_ids,
            "protected_facts": protected_facts,
            "remaining_episodes": self.store.db.execute("SELECT COUNT(*) FROM mg_episodes").fetchone()[0],
            "remaining_links": self.store.db.execute("SELECT COUNT(*) FROM mg_links").fetchone()[0],
        }

    # ---- deterministic retrieval, usage tracking, and prompt formatting -----------------------------------------
    def query(self, *, categories=None, entities=None, relations=None, context_tags=None,
              viewer=None, include_dormant=False, min_idea_stability=IDEA_THRESHOLD,
              limit=10, now=None, record_access=True):
        """Deterministic graph pull (Speed 1 / Speed 2 loop-back).
        Pulls active FACT and CONTESTED links plus IDEA links with |S_eff| >= min_idea_stability.
        Excludes dormant links by default; include_dormant=True enables deep/loop-back recall."""
        t = float(self.clock() if now is None else now)
        cats = {str(c).upper() for c in (categories or ()) if c}
        rels = set()
        for r in (relations or ()):
            try:
                canon, _, _ = self.resolve_relation(r, allow_unknown=True)
                rels.add(canon)
            except ValueError:
                pass
        req_tags = set(normalize_tags(context_tags))
        ent_ids = set()
        for e in (entities or ()):
            n = self.find_node(e, viewer=viewer)
            if n:
                ent_ids.add(n["id"])
        if entities and not ent_ids:
            return []

        sql = "SELECT * FROM mg_links"
        if not include_dormant:
            sql += " WHERE dormant=0"
        rows = self.store.db.execute(sql).fetchall()

        node_cache = {}
        def _n(nid):
            if nid not in node_cache:
                node_cache[nid] = self.get_node(nid)
            return node_cache[nid]

        hits = []
        for r in rows:
            if not visible({"scope": r["scope"], "owner": r["owner"]}, viewer):
                continue
            if cats and r["category"] not in cats:
                continue
            if rels and r["relation"] not in rels:
                continue
            if ent_ids and r["source_id"] not in ent_ids and r["target_id"] not in ent_ids:
                continue
            l_tags = set(t for t in (r["context_tags"] or "").split(",") if t)
            if req_tags and not (req_tags <= l_tags):
                continue

            anc_s = r["anchor_stability"] if r["anchor_ts"] else r["stability_score"]
            anc_t = r["anchor_ts"] if r["anchor_ts"] else r["last_stated_on"]
            s_eff = effective_stability(anc_s, r["decay_type"], anc_t, t, status=r["status"])
            mag = abs(s_eff)
            is_dormant = bool(r["dormant"]) or (r["status"] != STATUS_FACT and mag <= DORMANT_THRESHOLD)
            if is_dormant and not include_dormant:
                continue
            if not include_dormant:
                if r["status"] not in (STATUS_FACT, STATUS_CONTESTED) and mag < float(min_idea_stability):
                    continue

            d = self._link_dict(r, now=t)
            d["source_name"] = (_n(r["source_id"]) or {}).get("canonical_name", "")
            d["target_name"] = (_n(r["target_id"]) or {}).get("canonical_name", "")
            hits.append(d)

        # Rank: CONTESTED first (needs clarification), then FACT, then IDEA/THOUGHT by |S_eff| + shown_count.
        prio = {STATUS_CONTESTED: 3, STATUS_FACT: 2, STATUS_IDEA: 1, STATUS_THOUGHT: 0}
        hits.sort(
            key=lambda d: (
                0 if d["dormant"] else 1,
                prio.get(d["status"], 0),
                abs(d["effective_stability"]),
                d["shown_count"],
                d["last_stated_on"],
            ),
            reverse=True,
        )
        hits = hits[:int(limit)]

        if record_access and hits:
            ids = [h["id"] for h in hits]
            qmarks = ",".join("?" for _ in ids)
            with self.store.lock:
                self.store.db.execute(
                    f"UPDATE mg_links SET access_count = access_count + 1, last_accessed_on=? WHERE id IN ({qmarks})",
                    [t] + ids,
                )
                self.store.db.commit()
            for h in hits:
                h["access_count"] = int(h["access_count"]) + 1
                h["last_accessed_on"] = t
        return hits

    def mark_shown(self, link_ids, now=None):
        """Record that specific links were actually surfaced in an answer (boosts storage retention)."""
        ids = [int(i) for i in (link_ids or ()) if i is not None]
        if not ids:
            return 0
        t = float(self.clock() if now is None else now)
        qmarks = ",".join("?" for _ in ids)
        with self.store.lock:
            self.store.db.execute(
                f"UPDATE mg_links SET shown_count = shown_count + 1, last_accessed_on=? WHERE id IN ({qmarks})",
                [t] + ids,
            )
            self.store.db.commit()
        return len(ids)

    def format_context(self, links):
        """Compact, minimal-token symbolic rendering for Speed 2 system prompts."""
        if not links:
            return ""
        reg = self.rules()
        lines = []
        for lnk in links:
            s_val = lnk.get("effective_stability", lnk["stability_score"])
            rel = lnk["relation"]
            if lnk.get("signed") and s_val < 0:
                neg = (reg.get(rel) or {}).get("negative_label")
                rel = neg or f"NOT_{rel}"
            parts = [rel, lnk["status"]]
            if lnk["status"] != STATUS_FACT:
                parts.append(f"S:{abs(s_val):.2f}")
            parts.append(lnk["evidence"])
            if lnk["evidence"] == EVIDENCE_INFERRED and lnk.get("distinct_sources"):
                parts[-1] += f" ({len(lnk['distinct_sources'])} sources)"
            st = lnk.get("stats") or {}
            if st.get("count") and st.get("mean") is not None:
                u = f" {st['unit']}" if st.get("unit") else ""
                parts.append(f"mean:{st['mean']:.1f}{u}, sd:{st['std_dev']:.1f}, n:{st['count']}")
            if lnk.get("context_tags"):
                parts.append("context:" + ",".join(lnk["context_tags"]))
            if lnk.get("dormant"):
                parts.append("dormant")
            src = lnk.get("source_name") or (self.get_node(lnk["source_id"]) or {}).get("canonical_name", "?")
            tgt = lnk.get("target_name") or (self.get_node(lnk["target_id"]) or {}).get("canonical_name", "?")
            tail = ""
            if lnk["status"] == STATUS_CONTESTED:
                prev = (lnk.get("meta") or {}).get("contested_by")
                tail = f" [CONTESTED{f' vs {prev}' if prev else ''} — verify with user]"
            lines.append(f"({src}) --[{' | '.join(parts)}]--> ({tgt}){tail}")
        return "\n".join(lines)

    # ---- Speed 1 Librarian routing, 1-hop retrieval, and Speed 2 capped loop-back -------------------------------
    def _parse_json_obj(self, text):
        if not text:
            return None
        s = str(text).strip()
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None

    def route_deterministic(self, question, viewer=None):
        """Sub-5ms deterministic Speed 1 router: extracts categories, known node names/aliases,
        coarse context_tags, and temporal_scope without any LLM call or VRAM swap."""
        q = str(question or "")
        q_norm = " " + re.sub(r"[^a-z0-9]+", " ", q.lower()).strip() + " "

        cats = [cat for cat, rx in CATEGORY_PATTERNS.items() if rx.search(q)]
        ctx_tags = [tag for tag, rx in CONTEXT_TAG_PATTERNS if rx.search(q)][:MAX_CONTEXT_TAGS]
        temporal = "HISTORICAL" if HISTORICAL_Q.search(q) else "CURRENT_STATE"

        # Match visible known nodes (canonical_name or alias, len >= 3, excluding generic "user").
        matched = []
        rows = self.store.db.execute("SELECT id, canonical_name, aliases, owner, scope FROM mg_nodes").fetchall()
        for r in rows:
            if not visible({"scope": r["scope"], "owner": r["owner"]}, viewer):
                continue
            names = [r["canonical_name"]] + list(json.loads(r["aliases"] or "[]"))
            best_len = 0
            for nm in names:
                if not nm or nm == "user" or len(nm) < 3:
                    continue
                if f" {nm} " in q_norm:
                    best_len = max(best_len, len(nm))
            if best_len:
                matched.append((best_len, r["canonical_name"]))
        matched.sort(key=lambda x: (-x[0], x[1]))
        entities = []
        for _, name in matched:
            if name not in entities:
                entities.append(name)
            if len(entities) >= 6:
                break

        return {
            "target_categories": cats[:4],
            "extracted_entities": entities,
            "context_tags": normalize_tags(ctx_tags),
            "temporal_scope": temporal,
            "source": "deterministic",
        }

    async def route_with_model(self, question, model, store=None, viewer=None, timeout=4.0):
        """Optional small-LLM Speed 1 Librarian router (option librarian_model).
        Masks secrets for cloud models, merges LLM routing with deterministic node matches,
        and falls back cleanly to route_deterministic() on timeout or bad JSON."""
        base = self.route_deterministic(question, viewer=viewer)
        if not model or not question:
            return base
        q_send = question
        if store is not None:
            from . import privacy as sec
            from .store import names_in, words
            if sec.is_cloud(model):
                q_send = sec.mask(question, store.fix_typos(question), words, names_in)
        try:
            r = await asyncio.wait_for(
                model.chat(LIBRARIAN_SYSTEM, [{"role": "user", "content": q_send}], max_tokens=120, temperature=0),
                timeout,
            )
            plan = self._parse_json_obj(r.get("text") if isinstance(r, dict) else str(r))
        except Exception:  # noqa: BLE001 - deterministic fallback always keeps answers working
            return base
        if not plan:
            return base

        valid_cats = set(CATEGORIES)
        cats = list(base["target_categories"])
        for c in (plan.get("target_categories") or ()):
            cu = str(c or "").strip().upper()
            if cu in valid_cats and cu not in cats:
                cats.append(cu)
        ents = list(base["extracted_entities"])
        for e in (plan.get("extracted_entities") or ()):
            ne = normalize_name(e)
            if ne and ne != "user" and ne not in ents:
                ents.append(ne)
        tags = normalize_tags(list(base["context_tags"]) + list(plan.get("context_tags") or ()))
        temp = str(plan.get("temporal_scope") or "").upper()
        if temp not in ("CURRENT_STATE", "HISTORICAL"):
            temp = base["temporal_scope"]
        return {
            "target_categories": cats[:5],
            "extracted_entities": ents[:8],
            "context_tags": tags,
            "temporal_scope": temp,
            "source": "librarian_model",
        }

    def _one_hop_links(self, seed_node_ids, viewer=None, include_dormant=False, now=None, limit=8):
        """Follow 1-hop HAS_ATTRIBUTE/related edges from seed entities to surface relevant user preferences
        or safety constraints (e.g. 'kung pao chicken' -> 'peanuts' -> ALLERGIC_TO peanuts)."""
        if not seed_node_ids:
            return []
        t = float(self.clock() if now is None else now)
        qmarks = ",".join("?" for _ in seed_node_ids)
        rows = self.store.db.execute(
            f"SELECT * FROM mg_links WHERE (source_id IN ({qmarks}) OR target_id IN ({qmarks})) "
            "AND relation='HAS_ATTRIBUTE'",
            list(seed_node_ids) + list(seed_node_ids),
        ).fetchall()
        neighbor_names = []
        seen_ids = set(seed_node_ids)
        for r in rows:
            if not visible({"scope": r["scope"], "owner": r["owner"]}, viewer):
                continue
            other_id = r["target_id"] if r["source_id"] in seed_node_ids else r["source_id"]
            if other_id not in seen_ids:
                seen_ids.add(other_id)
                n = self.get_node(other_id)
                if n and n["canonical_name"] != "user":
                    neighbor_names.append(n["canonical_name"])
        if not neighbor_names:
            return []
        found = self.query(
            entities=neighbor_names[:8], viewer=viewer,
            include_dormant=include_dormant, limit=limit, now=t, record_access=False,
        )
        # Skip redundant HAS_ATTRIBUTE edges in the 1-hop result; surface actionable preferences/allergies/facts.
        return [lnk for lnk in found if lnk["relation"] != "HAS_ATTRIBUTE"]

    def retrieve(self, question, *, viewer=None, limit=8, now=None, plan=None,
                 include_dormant=None, record_access=True):
        """Speed 1 deterministic graph pull with 1-hop expansion and strict token-budget capping.
        Returns {'links': [...], 'plan': {...}, 'context': str}."""
        t = float(self.clock() if now is None else now)
        plan = plan or self.route_deterministic(question, viewer=viewer)
        cats = list(plan.get("target_categories") or ())
        ents = list(plan.get("extracted_entities") or ())
        ctx_tags = list(plan.get("context_tags") or ())
        deep = (plan.get("temporal_scope") == "HISTORICAL") if include_dormant is None else bool(include_dormant)

        if not cats and not ents:
            return {"links": [], "plan": plan, "context": ""}

        collected = {}
        tiers = {}  # link_id -> precision tier (3 = entity direct, 2 = 1-hop, 1 = category)

        # 1. Direct entity hits (highest precision)
        if ents:
            ent_hits = self.query(
                entities=ents, context_tags=ctx_tags or None, viewer=viewer,
                include_dormant=deep, limit=limit, now=t, record_access=False,
            )
            if not ent_hits and ctx_tags:
                ent_hits = self.query(
                    entities=ents, viewer=viewer, include_dormant=deep,
                    limit=limit, now=t, record_access=False,
                )
            for h in ent_hits:
                if h["relation"] == "HAS_ATTRIBUTE":
                    continue
                collected[h["id"]] = h
                tiers[h["id"]] = 3

            # 2. 1-hop neighbor expansion (e.g. dish -> peanuts -> ALLERGIC_TO peanuts)
            seed_ids = []
            for e in ents:
                n = self.find_node(e, viewer=viewer)
                if n:
                    seed_ids.append(n["id"])
            for h in self._one_hop_links(seed_ids, viewer=viewer, include_dormant=deep, now=t, limit=limit):
                collected.setdefault(h["id"], h)
                tiers[h["id"]] = max(tiers.get(h["id"], 0), 2)

        # 3. Category hits (respecting context_tags when present, falling back to untagged if empty)
        if cats:
            cat_hits = self.query(
                categories=cats, context_tags=ctx_tags or None, viewer=viewer,
                include_dormant=deep, limit=limit, now=t, record_access=False,
            )
            if not cat_hits and ctx_tags:
                cat_hits = self.query(
                    categories=cats, viewer=viewer, include_dormant=deep,
                    limit=limit, now=t, record_access=False,
                )
            for h in cat_hits:
                if h["relation"] == "HAS_ATTRIBUTE":
                    continue
                collected.setdefault(h["id"], h)
                tiers[h["id"]] = max(tiers.get(h["id"], 0), 1)

        if not collected:
            return {"links": [], "plan": plan, "context": ""}

        prio = {STATUS_CONTESTED: 3, STATUS_FACT: 2, STATUS_IDEA: 1, STATUS_THOUGHT: 0}
        ranked = sorted(
            collected.values(),
            key=lambda d: (
                1 if d["status"] == STATUS_CONTESTED else 0,
                tiers.get(d["id"], 0),
                0 if d["dormant"] else 1,
                prio.get(d["status"], 0),
                abs(d.get("effective_stability", d["stability_score"])),
                d["shown_count"],
                d["last_stated_on"],
            ),
            reverse=True,
        )[:int(limit)]

        if record_access and ranked:
            ids = [h["id"] for h in ranked]
            qmarks = ",".join("?" for _ in ids)
            with self.store.lock:
                self.store.db.execute(
                    f"UPDATE mg_links SET access_count = access_count + 1, last_accessed_on=? WHERE id IN ({qmarks})",
                    [t] + ids,
                )
                self.store.db.commit()
            for h in ranked:
                h["access_count"] = int(h["access_count"]) + 1
                h["last_accessed_on"] = t

        return {"links": ranked, "plan": plan, "context": self.format_context(ranked)}

    def loop_back(self, question, *, viewer=None, exclude_ids=(), limit=6, now=None):
        """Speed 2 capped 1-hop deep recall (dormant + fuzzy node token overlap + category-wide dormant).
        Called only when Pass 1 produced an 'I don't know' answer on a typed query.
        Does NOT log to mg_staging until log_loop_back_hit() confirms a genuine true positive."""
        t = float(self.clock() if now is None else now)
        skip = {int(i) for i in (exclude_ids or ())}
        plan = self.route_deterministic(question, viewer=viewer)

        tokens = {
            w for w in re.findall(r"[a-z0-9]{3,}", str(question or "").lower())
            if w not in LOOP_BACK_STOP
        }

        # Fuzzy token match across visible node canonical names & aliases
        fuzzy_ents = list(plan["extracted_entities"])
        if tokens:
            rows = self.store.db.execute("SELECT canonical_name, aliases, owner, scope FROM mg_nodes").fetchall()
            for r in rows:
                if not visible({"scope": r["scope"], "owner": r["owner"]}, viewer):
                    continue
                if r["canonical_name"] == "user":
                    continue
                words_in_node = set(re.findall(r"[a-z0-9]{3,}", r["canonical_name"]))
                for al in json.loads(r["aliases"] or "[]"):
                    words_in_node.update(re.findall(r"[a-z0-9]{3,}", al))
                if tokens & words_in_node and r["canonical_name"] not in fuzzy_ents:
                    fuzzy_ents.append(r["canonical_name"])

        recovered = {}
        if fuzzy_ents:
            for h in self.query(
                entities=fuzzy_ents[:10], viewer=viewer, include_dormant=True,
                min_idea_stability=0.0, limit=limit * 2, now=t, record_access=False,
            ):
                if h["id"] not in skip and h["relation"] != "HAS_ATTRIBUTE":
                    recovered[h["id"]] = h
        if plan["target_categories"]:
            for h in self.query(
                categories=plan["target_categories"], viewer=viewer, include_dormant=True,
                min_idea_stability=0.0, limit=limit * 2, now=t, record_access=False,
            ):
                if h["id"] not in skip and h["relation"] != "HAS_ATTRIBUTE":
                    recovered.setdefault(h["id"], h)

        if not recovered:
            return {"links": [], "context": ""}

        hits = sorted(
            recovered.values(),
            key=lambda d: (
                1 if d["status"] == STATUS_CONTESTED else 0,
                1 if d["status"] == STATUS_FACT else 0,
                abs(d.get("effective_stability", d["stability_score"])),
                d["evidence_count"],
                d["last_stated_on"],
            ),
            reverse=True,
        )[:int(limit)]

        ids = [h["id"] for h in hits]
        qmarks = ",".join("?" for _ in ids)
        with self.store.lock:
            self.store.db.execute(
                f"UPDATE mg_links SET access_count = access_count + 1, last_accessed_on=? WHERE id IN ({qmarks})",
                [t] + ids,
            )
            self.store.db.commit()
        return {"links": hits, "context": self.format_context(hits)}

    def log_loop_back_hit(self, question, link_ids, *, viewer=None, ts=None):
        """Log a genuine true-positive Speed 2 loop-back miss into mg_staging for the Overnight Janitor / LoRA flywheel
        and mark the recovered links as shown."""
        ids = [int(i) for i in (link_ids or ()) if i is not None]
        if not ids:
            return None
        now = float(self.clock() if ts is None else ts)
        self.mark_shown(ids, now=now)
        scope = "personal" if viewer else "household"
        return self._record_staging(
            op="LOOP_BACK_HIT",
            link_id=ids[0],
            patch={"op": "LOOP_BACK_HIT", "question": str(question or "")[:300], "link_ids": ids},
            before_state=None,
            after_state={"recovered_link_ids": ids},
            confidence=0.65,
            flag_reasons=["loop_back"],
            ts=now,
            owner=viewer or None,
            scope=scope,
        )

    def used_links(self, answer_text, links):
        """Heuristic: which injected graph links actually contributed to the model's answer (for mark_shown)."""
        if not links or not answer_text:
            return []
        ans_low = str(answer_text).lower()
        ans_words = set(re.findall(r"[a-z0-9]{3,}", ans_low))
        used = []
        for lnk in links:
            tgt = (lnk.get("target_name") or "").lower()
            src = (lnk.get("source_name") or "").lower()
            cand_words = {
                w for w in re.findall(r"[a-z0-9]{3,}", f"{tgt} {'' if src == 'user' else src}")
                if w not in LOOP_BACK_STOP
            }
            if (tgt and tgt in ans_low) or (cand_words and (cand_words & ans_words)):
                used.append(lnk["id"])
                continue
            # Also credit CONTESTED links when the model asks a clarifying question, or numeric baselines matched by value.
            if lnk.get("status") == STATUS_CONTESTED and "?" in ans_low:
                used.append(lnk["id"])
                continue
            st = lnk.get("stats") or {}
            if st.get("mean") is not None:
                m_int = str(int(round(float(st["mean"]))))
                m_one = f"{float(st['mean']):.1f}"
                if re.search(rf"\b(?:{re.escape(m_int)}|{re.escape(m_one)})\b", ans_low):
                    used.append(lnk["id"])
        return used

    # ---- multi-person privacy & purge ---------------------------------------------------------------------------
    def purge_owner(self, owner):
        """Remove all personal graph data belonging to `owner` (called by Store.on_forget_name)."""
        if not owner:
            return 0
        key = str(owner).lower()
        with self.store.lock:
            n1 = self.store.db.execute(
                "DELETE FROM mg_episodes WHERE LOWER(COALESCE(owner,''))=? AND scope='personal'", (key,)
            ).rowcount
            n2 = self.store.db.execute(
                "DELETE FROM mg_staging WHERE LOWER(COALESCE(owner,''))=? AND scope='personal'", (key,)
            ).rowcount
            n3 = self.store.db.execute(
                "DELETE FROM mg_links WHERE LOWER(COALESCE(owner,''))=? AND scope='personal'", (key,)
            ).rowcount
            n4 = self.store.db.execute(
                "DELETE FROM mg_nodes WHERE LOWER(COALESCE(owner,''))=? AND scope='personal'", (key,)
            ).rowcount
            self.store.db.commit()
        return n1 + n2 + n3 + n4

    def stats_summary(self):
        """Compact counts for diagnostics and testing."""
        return {
            "nodes": self.store.db.execute("SELECT COUNT(*) FROM mg_nodes").fetchone()[0],
            "links": self.store.db.execute("SELECT COUNT(*) FROM mg_links").fetchone()[0],
            "active_links": self.store.db.execute("SELECT COUNT(*) FROM mg_links WHERE dormant=0").fetchone()[0],
            "dormant_links": self.store.db.execute("SELECT COUNT(*) FROM mg_links WHERE dormant=1").fetchone()[0],
            "facts": self.store.db.execute("SELECT COUNT(*) FROM mg_links WHERE status='FACT'").fetchone()[0],
            "contested": self.store.db.execute("SELECT COUNT(*) FROM mg_links WHERE status='CONTESTED'").fetchone()[0],
            "episodes": self.store.db.execute("SELECT COUNT(*) FROM mg_episodes").fetchone()[0],
            "flagged_staging": self.store.db.execute(
                "SELECT COUNT(*) FROM mg_staging WHERE flagged=1 AND audit_status='pending'"
            ).fetchone()[0],
        }
