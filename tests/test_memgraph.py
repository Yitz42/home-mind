"""Tests for homemind.memgraph: neuro-symbolic memory graph, signed stability, canonical relation
rules, direct/inferred/sensor_fact tiers, dormant decay, compaction-first storage caps,
contradiction hysteresis, staging rollback, multi-person privacy, and a 90-day time-lapse stress test.

  python tests/test_memgraph.py
"""
import os
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "home_mind", "rootfs", "app"))

from homemind.store import Store
from homemind.memgraph import (
    MemGraph,
    STATUS_THOUGHT, STATUS_IDEA, STATUS_FACT, STATUS_CONTESTED,
    EVIDENCE_DIRECT, EVIDENCE_INFERRED, EVIDENCE_SENSOR,
    DECAY_ZERO, DECAY_LOGISTIC, DECAY_LINEAR,
    FACT_THRESHOLD, DORMANT_THRESHOLD,
    decay_magnitude, welford_update, welford_std,
)

DAY = 86400.0
T0 = 1_760_000_000.0  # fixed UTC anchor (2025-10-09)
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def make_graph():
    tmp = tempfile.mkdtemp()
    st = Store(os.path.join(tmp, "mind.db"))
    now = [T0]
    g = MemGraph(st, clock=lambda: now[0])
    return st, g, now


# ---- 1. Pure math sanity ----------------------------------------------------------------------------------------
def test_math():
    check("linear decay at dt=0 is identity", abs(decay_magnitude(0.30, DECAY_LINEAR, 0) - 0.30) < 1e-9)
    check("linear decay reaches dormant <=0.10 in 10d", decay_magnitude(0.25, DECAY_LINEAR, 10) <= DORMANT_THRESHOLD)
    check("logistic decay at dt=0 is exact identity", abs(decay_magnitude(0.75, DECAY_LOGISTIC, 0) - 0.75) < 1e-9)
    check("logistic decay is gentle at 5d and steeper at 45d",
          decay_magnitude(0.75, DECAY_LOGISTIC, 5) > 0.65 and decay_magnitude(0.75, DECAY_LOGISTIC, 45) < 0.20)
    check("zero decay never decays over 365d", abs(decay_magnitude(1.0, DECAY_ZERO, 365) - 1.0) < 1e-9)

    c, m, m2, mn, mx = 0, None, 0.0, None, None
    for x in (18, 20, 22, 20, 20):
        c, m, m2, mn, mx = welford_update(c, m, m2, mn, mx, x)
    check("welford stats exact", c == 5 and abs(m - 20.0) < 1e-9 and abs(welford_std(c, m2) - 1.4142) < 0.01
          and mn == 18.0 and mx == 22.0)


# ---- 2. Canonical nodes, aliases, and declarative relation rules ------------------------------------------------
def test_nodes_and_rules():
    _, g, _ = make_graph()

    n1 = g.upsert_node("Running", category="ACTIVITY", aliases=["jogging", "Morning Run"])
    n2 = g.upsert_node("jogging", category="ACTIVITY")
    n3 = g.upsert_node("morning_run", aliases=["trail jog"])
    check("canonical node alias resolution avoids duplicates", n1["id"] == n2["id"] == n3["id"])
    check("aliases merged cleanly", "jogging" in n3["aliases"] and "trail jog" in n3["aliases"])

    rel, sgn, _ = g.resolve_relation("enjoys")
    check("positive alias maps to LIKES (+1)", rel == "LIKES" and sgn == 1)
    rel, sgn, _ = g.resolve_relation("DISLIKES")
    check("negative alias maps to LIKES (-1)", rel == "LIKES" and sgn == -1)
    rel, sgn, _ = g.resolve_relation("worsens")
    check("negative correlation alias maps to CORRELATES_WITH (-1)", rel == "CORRELATES_WITH" and sgn == -1)

    bad_raised = False
    try:
        g.resolve_relation("TOTALLY_MADE_UP_PREDICATE")
    except ValueError:
        bad_raised = True
    check("unknown relation rejected by default", bad_raised)

    # Custom rule with ALL optional fields left blank (None) inherits tier defaults cleanly.
    g.define_rule("PLAYS_INSTRUMENT")
    res = g.observe("user", "PLAYS_INSTRUMENT", "piano", evidence=EVIDENCE_DIRECT)
    check("custom rule with blank fields works", res["changed"] and res["link"]["relation"] == "PLAYS_INSTRUMENT")
    check("blank rule defaults to signed=True, ADDITIVE",
          res["link"]["signed"] is True and res["link"]["exclusivity"] == "ADDITIVE")

    # ALLERGIC_TO: unsigned, 1-point immediate FACT with zero decay.
    al = g.observe("user", "ALLERGIC_TO", "shellfish", evidence=EVIDENCE_DIRECT, category="HEALTH")
    check("ALLERGIC_TO promotes immediately to FACT with ZERO decay",
          al["link"]["status"] == STATUS_FACT and al["link"]["decay_type"] == DECAY_ZERO
          and al["link"]["stability_score"] == 1.0)
    # 200 days later: still 1.0 FACT.
    lnk_later = g.get_link(al["link"]["id"], now=T0 + 200 * DAY)
    check("ALLERGIC_TO never decays over 200d", lnk_later["effective_stability"] == 1.0)


# ---- 3. Meal -> cuisine/flavor/ingredient hypothesis fan-out & signed override ----------------------------------
def test_hypothesis_fan_out_and_signed_override():
    _, g, now = make_graph()

    # Day 0: "I like Kung Pao chicken"
    r1 = g.record_with_hypotheses(
        "user", "LIKES", "kung pao chicken",
        hypotheses=[("LIKES", "chinese", "FOOD"), ("LIKES", "spicy savory", "FOOD"), ("LIKES", "peanuts", "FOOD")],
        category="FOOD", source_ref="meal:kung_pao", quote="I like Kung Pao chicken",
    )
    check("direct meal starts high (IDEA)", r1["primary"]["link"]["status"] == STATUS_IDEA
          and r1["primary"]["link"]["evidence"] == EVIDENCE_DIRECT)
    h_chinese = r1["hypotheses"][0]["link"]
    h_peanuts = r1["hypotheses"][2]["link"]
    check("inferred hypotheses start as low-stability THOUGHTs",
          h_chinese["status"] == STATUS_THOUGHT and h_chinese["evidence"] == EVIDENCE_INFERRED
          and h_chinese["stability_score"] < 0.40)

    # Repeating the EXACT same source_ref must NOT double-count inferred stability!
    s_before = h_chinese["stability_score"]
    dup = g.observe("user", "LIKES", "chinese", evidence=EVIDENCE_INFERRED, source_ref="meal:kung_pao", category="FOOD")
    check("same source_ref does not double-count inferred stability",
          abs(dup["link"]["stability_score"] - s_before) < 1e-6 and dup["link"]["evidence_count"] == 1)

    # Days 2, 4, 6: Three more distinct spicy Chinese meals -> chinese & spicy savory promote to FACT!
    for day_off, dish in ((2, "dan dan noodles"), (4, "mapo tofu"), (6, "sichuan beef")):
        now[0] = T0 + day_off * DAY
        g.record_with_hypotheses(
            "user", "LIKES", dish,
            hypotheses=[("LIKES", "chinese", "FOOD"), ("LIKES", "spicy savory", "FOOD")],
            category="FOOD", source_ref=f"meal:{dish}",
        )

    chinese_final = g.get_link(h_chinese["id"], now=now[0])
    check("4 distinct dishes promote inferred 'chinese' to FACT",
          chinese_final["status"] == STATUS_FACT and len(chinese_final["distinct_sources"]) == 4)

    # Day 18: peanuts was never reinforced -> decays into dormant (NOT deleted!)
    now[0] = T0 + 18 * DAY
    sweep = g.decay_sweep(now=now[0])
    peanuts_after = g.get_link(h_peanuts["id"], now=now[0])
    check("unreinforced inferred 'peanuts' drifts to dormant, not deleted",
          peanuts_after is not None and peanuts_after["dormant"] is True and sweep["went_dormant"] >= 1)

    # Excluded from default hot query, but still reachable on explicit/loop-back recall.
    hot = g.query(entities=["peanuts"], relations=["LIKES"], include_dormant=False, now=now[0])
    deep = g.query(entities=["peanuts"], relations=["LIKES"], include_dormant=True, now=now[0])
    check("dormant excluded from hot query, present in deep query", len(hot) == 0 and len(deep) == 1)
    # Calling decay_sweep repeatedly at the same timestamp is 100% idempotent (no double-decay).
    s_snap = g.get_link(h_peanuts["id"], now=now[0])["stability_score"]
    g.decay_sweep(now=now[0])
    check("repeated decay_sweep is idempotent (no double-decay)",
          abs(g.get_link(h_peanuts["id"], now=now[0])["stability_score"] - s_snap) < 1e-9)

    # Day 19: Direct negative statement "I don't like peanuts" flips the same link negative!
    now[0] = T0 + 19 * DAY
    neg = g.observe("user", "DISLIKES", "peanuts", evidence=EVIDENCE_DIRECT, category="FOOD",
                    quote="Actually I don't like peanuts")
    check("direct DISLIKES overrides weak inferred LIKES on same row",
          neg["link"]["id"] == h_peanuts["id"] and neg["link"]["stability_score"] < -0.50
          and neg["link"]["dormant"] is False and neg["link"]["evidence"] == EVIDENCE_DIRECT)
    ctx = g.format_context([neg["link"]])
    check("negative LIKES renders cleanly as DISLIKES in prompt context", "DISLIKES" in ctx and "peanuts" in ctx)


# ---- 4. Domain-general sensor_fact (lights, sleep, steps) + bimodal context tags -------------------------------
def test_sensor_facts_and_bimodal_tags():
    _, g, now = make_graph()

    # 20 light toggles in ONE evening on HABIT (max_per_day=1) must NOT rocket to FACT on day 1!
    for i in range(20):
        g.observe("user", "LIGHT_HABIT", "living room lights", evidence=EVIDENCE_SENSOR,
                  value=25 + (i % 3), unit="pct", context_tags=["evening", "living room"],
                  category="LIGHTING", ts=T0 + i * 60)
    light_links = g.query(categories=["LIGHTING"], include_dormant=True, now=T0 + 1200)
    check("20 same-day light toggles do not flood HABIT to FACT",
          len(light_links) == 1 and light_links[0]["evidence_count"] == 1
          and light_links[0]["status"] == STATUS_THOUGHT
          and light_links[0]["stats"]["count"] == 20)

    # Bimodal sleep baselines: 15 weekday nights (~7.0h) vs 15 weekend nights (~9.0h).
    for d in range(15):
        g.observe("user", "SLEEP_BASELINE", "sleep duration", evidence=EVIDENCE_SENSOR,
                  value=7.0 + (d % 3 - 1) * 0.2, unit="hours", context_tags=["weekday"],
                  category="SLEEP", ts=T0 + d * DAY)
        g.observe("user", "SLEEP_BASELINE", "sleep duration", evidence=EVIDENCE_SENSOR,
                  value=9.0 + (d % 3 - 1) * 0.2, unit="hours", context_tags=["weekend"],
                  category="SLEEP", ts=T0 + d * DAY + 3600)

    wd = g.query(categories=["SLEEP"], context_tags=["weekday"], now=T0 + 15 * DAY)
    we = g.query(categories=["SLEEP"], context_tags=["weekend"], now=T0 + 15 * DAY)
    check("bimodal sleep kept strictly separate (no 8.0h collapse)",
          len(wd) == 1 and len(we) == 1
          and abs(wd[0]["stats"]["mean"] - 7.0) < 0.05
          and abs(we[0]["stats"]["mean"] - 9.0) < 0.05)
    check("sensor_fact BASELINE promotes to FACT after >= 14 data points",
          wd[0]["status"] == STATUS_FACT and we[0]["status"] == STATUS_FACT
          and wd[0]["evidence"] == EVIDENCE_SENSOR)

    # Direct user preference vs contradictory sensor_fact: sensor cannot silently flip direct FACT!
    g.observe("user", "PREFERS", "bright living room", evidence=EVIDENCE_DIRECT, category="LIGHTING", ts=T0)
    p2 = g.observe("user", "PREFERS", "bright living room", evidence=EVIDENCE_DIRECT, category="LIGHTING", ts=T0 + DAY)
    check("2 direct statements establish PREFERS FACT", p2["link"]["status"] == STATUS_FACT)
    s_contra = g.observe("user", "PREFERS", "bright living room", sign=-1, evidence=EVIDENCE_SENSOR,
                         category="LIGHTING", ts=T0 + 2 * DAY)
    check("single sensor contradiction cannot silently flip direct FACT sign",
          s_contra["link"]["stability_score"] > 0 and s_contra["link"]["evidence"] == EVIDENCE_DIRECT)


# ---- 5. Contradiction engine & demotion hysteresis --------------------------------------------------------------
def test_contradictions_and_hysteresis():
    _, g, now = make_graph()

    # EXCLUSIVE: LIVES_IN San Francisco -> Austin
    sf = g.observe("user", "LIVES_IN", "san francisco", evidence=EVIDENCE_DIRECT, category="LOCATION", ts=T0)
    check("LIVES_IN direct promotes to FACT immediately", sf["link"]["status"] == STATUS_FACT)

    # Hypothetical / sarcasm (gamma = 0.0) must cause ZERO mutation!
    sarc = g.observe("user", "LIVES_IN", "mars", certainty=0.0, evidence=EVIDENCE_DIRECT, category="LOCATION", ts=T0 + DAY)
    sf_check = g.get_link(sf["link"]["id"], now=T0 + DAY)
    check("hypothetical gamma=0.0 makes zero change", not sarc["changed"] and sf_check["status"] == STATUS_FACT)

    # Direct move to Austin (gamma = 1.0) drops SF below 0.85 -> CONTESTED with LOGISTIC decay re-enabled!
    au = g.observe("user", "LIVES_IN", "austin", certainty=1.0, evidence=EVIDENCE_DIRECT, category="LOCATION", ts=T0 + 10 * DAY)
    sf_after = g.get_link(sf["link"]["id"], now=T0 + 10 * DAY)
    check("EXCLUSIVE contradiction demotes prior FACT to CONTESTED",
          sf_after["status"] == STATUS_CONTESTED and sf_after["decay_type"] == DECAY_LOGISTIC
          and sf_after["stability_score"] < FACT_THRESHOLD and len(au["contested_links"]) == 1)
    ctx = g.format_context([sf_after])
    check("CONTESTED prompt context prompts LLM to verify with user",
          "CONTESTED" in ctx and "austin" in ctx and "verify with user" in ctx)

    # SOFT_EXCLUSIVE: WORKS_AT Google; indirect mention (gamma=0.4) of OpenAI should NOT flip Google immediately.
    goog = g.observe("user", "WORKS_AT", "google", evidence=EVIDENCE_DIRECT, category="CAREER", ts=T0)
    g.observe("user", "WORKS_AT", "openai", certainty=0.4, evidence=EVIDENCE_DIRECT, category="CAREER", ts=T0 + 5 * DAY)
    goog_mid = g.get_link(goog["link"]["id"], now=T0 + 5 * DAY)
    check("SOFT_EXCLUSIVE indirect mention (gamma=0.4) retains FACT on first hit",
          goog_mid["status"] == STATUS_FACT and goog_mid["stability_score"] >= FACT_THRESHOLD)

    # Direct statement (gamma=1.0) DOES contest Google.
    g.observe("user", "WORKS_AT", "openai", certainty=1.0, evidence=EVIDENCE_DIRECT, category="CAREER", ts=T0 + 6 * DAY)
    goog_after = g.get_link(goog["link"]["id"], now=T0 + 6 * DAY)
    check("SOFT_EXCLUSIVE direct mention (gamma=1.0) flips FACT to CONTESTED",
          goog_after["status"] == STATUS_CONTESTED and goog_after["stability_score"] < FACT_THRESHOLD)

    # Signed FACT contradiction on same row (LIKES coffee -> DISLIKES coffee)
    g.observe("user", "LIKES", "coffee", evidence=EVIDENCE_DIRECT, category="FOOD", ts=T0)
    c_fact = g.observe("user", "LIKES", "coffee", evidence=EVIDENCE_DIRECT, category="FOOD", ts=T0 + DAY)
    check("coffee established as FACT", c_fact["link"]["status"] == STATUS_FACT)
    c_contra = g.observe("user", "DISLIKES", "coffee", evidence=EVIDENCE_DIRECT, category="FOOD", ts=T0 + 10 * DAY)
    check("contradicting signed FACT flips same row to CONTESTED",
          c_contra["link"]["id"] == c_fact["link"]["id"] and c_contra["link"]["status"] == STATUS_CONTESTED)


# ---- 6. Seasonal wake-up, compaction-first storage cap, and usage protection ------------------------------------
def test_compaction_and_eviction():
    _, g, now = make_graph()

    # Seasonal habit goes dormant, then wakes up cleanly keeping history.
    soup = g.observe("user", "LIKES", "ramen", evidence=EVIDENCE_INFERRED, source_ref="m1", category="FOOD", ts=T0)
    g.observe("user", "LIKES", "ramen", evidence=EVIDENCE_INFERRED, source_ref="m2", category="FOOD", ts=T0 + DAY)
    g.decay_sweep(now=T0 + 80 * DAY)
    ramen_dormant = g.get_link(soup["link"]["id"], now=T0 + 80 * DAY)
    check("seasonal ramen goes dormant after 80d unreinforced", ramen_dormant["dormant"] is True)
    ramen_awake = g.observe("user", "LIKES", "ramen", evidence=EVIDENCE_INFERRED, source_ref="m3",
                            category="FOOD", ts=T0 + 81 * DAY)
    check("seasonal wake-up reactivates link preserving prior evidence_count",
          ramen_awake["link"]["dormant"] is False and ramen_awake["link"]["evidence_count"] == 3)

    # Create 40 daily step sensor episodes + 1 confirmed FACT + 15 noisy one-off dormant thoughts (1 of them shown!).
    for d in range(40):
        g.observe("user", "STEPS_BASELINE", "daily steps", evidence=EVIDENCE_SENSOR,
                  value=8000 + d * 25, unit="steps", category="ACTIVITY", ts=T0 + d * DAY)
    pinned_fact = g.observe("user", "ALLERGIC_TO", "tree nuts", evidence=EVIDENCE_DIRECT, category="HEALTH", ts=T0)

    noise_ids = []
    for i in range(15):
        r = g.observe("user", "LIKES", f"random_snack_{i}", evidence=EVIDENCE_INFERRED,
                      source_ref=f"s:{i}", category="FOOD", ts=T0)
        noise_ids.append(r["link"]["id"])
    # Mark noise_ids[0] as shown several times so usage-based retention protects it ahead of unused guesses!
    g.mark_shown([noise_ids[0]], now=T0 + 2 * DAY)
    g.mark_shown([noise_ids[0]], now=T0 + 3 * DAY)

    # Advance 30 days -> noise thoughts go dormant; run storage cap enforcement.
    rep = g.enforce_storage_cap(max_episodes=25, max_links=10, keep_recent_per_link=5, now=T0 + 45 * DAY)
    check("storage cap compacts raw sensor episodes first", rep["compacted_episodes"] >= 20)
    steps_lnk = g.query(categories=["ACTIVITY"], now=T0 + 45 * DAY)[0]
    check("compacted episodes preserved as monthly rollups + live stats intact",
          bool(steps_lnk["meta"].get("rollups")) and steps_lnk["stats"]["count"] == 40
          and steps_lnk["status"] == STATUS_FACT)
    check("storage cap evicts lowest-value dormant guesses while protecting FACTs",
          rep["evicted_links"] > 0 and g.get_link(pinned_fact["link"]["id"]) is not None
          and rep["protected_facts"] >= 2)
    check("frequently shown dormant link survives ahead of never-shown guesses",
          g.get_link(noise_ids[0]) is not None and noise_ids[0] not in rep["evicted_ids"])


# ---- 7. Patch execution, staging triage, and deterministic rollback ---------------------------------------------
def test_patches_staging_and_revert():
    _, g, _ = make_graph()

    n = g.apply_patch({"op": "CREATE_NODE", "canonical_name": "espresso", "aliases": ["double shot"], "category": "FOOD"})
    check("CREATE_NODE patch works", n["node"]["canonical_name"] == "espresso")

    r1 = g.apply_patch({
        "op": "CREATE_LINK", "source": "user", "relation": "LIKES", "target": "double shot",
        "evidence": EVIDENCE_DIRECT, "confidence": 0.55, "category": "FOOD",
    })
    check("low-confidence patch is flagged in staging", r1["flagged"] and "low_confidence" in r1["flag_reasons"])
    flagged = g.flagged_staging()
    check("flagged_staging returns pending flagged write", any(f["id"] == r1["staging_id"] for f in flagged))

    # Bolster, then revert the bolster -> restores exact previous stability!
    r2 = g.apply_patch({"op": "BOLSTER", "target_link_id": r1["link"]["id"], "certainty": 1.0, "confidence": 0.60})
    check("BOLSTER promotes espresso to FACT", r2["link"]["status"] == STATUS_FACT)
    g.revert_staging(r2["staging_id"])
    reverted = g.get_link(r1["link"]["id"])
    check("revert_staging restores exact before_state",
          reverted["status"] == STATUS_IDEA and abs(reverted["stability_score"] - r1["link"]["stability_score"]) < 1e-6)

    # Reverting the original CREATE_LINK removes the newly created link completely.
    g.revert_staging(r1["staging_id"])
    check("revert_staging on CREATE_LINK deletes bad link", g.get_link(r1["link"]["id"]) is None)


# ---- 8. Multi-person privacy isolation & purge_owner ------------------------------------------------------------
def test_privacy_and_purge():
    _, g, _ = make_graph()

    g.observe("user", "ALLERGIC_TO", "kiwi", evidence=EVIDENCE_DIRECT, owner="Alice", scope="personal", category="HEALTH")
    g.observe("user", "ALLERGIC_TO", "mango", evidence=EVIDENCE_DIRECT, owner="Bob", scope="personal", category="HEALTH")
    g.observe("user", "LIVES_IN", "seattle", evidence=EVIDENCE_DIRECT, scope="household", category="LOCATION")

    alice_view = {x["target_name"] for x in g.query(viewer="Alice")}
    bob_view = {x["target_name"] for x in g.query(viewer="Bob")}
    guest_view = {x["target_name"] for x in g.query(viewer="")}
    check("Alice sees own personal + household, not Bob's",
          "kiwi" in alice_view and "seattle" in alice_view and "mango" not in alice_view)
    check("Bob sees own personal + household, not Alice's",
          "mango" in bob_view and "seattle" in bob_view and "kiwi" not in bob_view)
    check("Guest sees household only", guest_view == {"seattle"})

    removed = g.purge_owner("Alice")
    check("purge_owner deletes Alice's personal rows and leaves Bob/household intact",
          removed >= 2
          and "kiwi" not in {x["target_name"] for x in g.query(viewer="Alice")}
          and "mango" in {x["target_name"] for x in g.query(viewer="Bob")}
          and "seattle" in {x["target_name"] for x in g.query(viewer="")})


# ---- 9. 90-Day Synthetic Time-Lapse Stress Test -----------------------------------------------------------------
def test_90_day_stress_test():
    _, g, now = make_graph()

    # Day 0: Foundational direct facts + allergy
    g.observe("user", "LIVES_IN", "chicago", evidence=EVIDENCE_DIRECT, category="LOCATION", ts=T0)
    g.observe("user", "ALLERGIC_TO", "peanuts", evidence=EVIDENCE_DIRECT, category="HEALTH", ts=T0)
    g.observe("user", "WORKS_AT", "acme corp", evidence=EVIDENCE_DIRECT, category="CAREER", ts=T0)

    # Simulate 90 days of life:
    # - Weekday 20-min runs vs Weekend 85-min trail runs
    # - Daily sleep (~7.1h weekday, ~8.6h weekend) & daily steps (~8500)
    # - Evening living room dimming habit (~30% brightness)
    # - Recurring spicy Sichuan meals + 50 one-off noise mentions
    # - Day 45: sarcasm/hypothetical move to Tokyo (gamma=0.0)
    # - Day 60: real career switch to Beta Labs (gamma=1.0)
    dishes = ("kung pao tofu", "dan dan noodles", "mapo tofu", "sichuan eggplant", "chili dumplings")
    for day in range(90):
        t = T0 + day * DAY
        now[0] = t
        is_weekend = (day % 7) in (5, 6)
        ctx = ["weekend"] if is_weekend else ["weekday"]

        # Sleep & steps sensor telemetry
        g.observe("user", "SLEEP_BASELINE", "sleep", evidence=EVIDENCE_SENSOR,
                  value=(8.6 if is_weekend else 7.1) + ((day % 3) - 1) * 0.15,
                  unit="hours", context_tags=ctx, category="SLEEP", ts=t)
        g.observe("user", "STEPS_BASELINE", "daily steps", evidence=EVIDENCE_SENSOR,
                  value=8500 + (day % 5) * 200, unit="steps", category="ACTIVITY", ts=t)

        # Running every 3rd day (weekday vs weekend bimodal)
        if day % 3 == 0:
            g.observe("running", "RESULT_IN", "feels good", evidence=EVIDENCE_INFERRED,
                      value=85.0 if is_weekend else 20.0, unit="min",
                      context_tags=["weekend", "trail"] if is_weekend else ["weekday"],
                      category="ACTIVITY", source_ref=f"run:{day}", ts=t)

        # Evening light dimming habit (multiple pings per evening -> rate-capped to 1/day)
        if day % 2 == 0:
            for ping in range(3):
                g.observe("user", "LIGHT_HABIT", "living room lights", evidence=EVIDENCE_SENSOR,
                          value=30.0 + ping, unit="pct", context_tags=["evening"],
                          category="LIGHTING", ts=t + ping * 300)

        # Food meals every 10 days (days 5, 15, 25, 35, 45)
        if day in (5, 15, 25, 35, 45):
            dish = dishes[day // 10]
            g.record_with_hypotheses(
                "user", "LIKES", dish,
                hypotheses=[("LIKES", "chinese", "FOOD"), ("LIKES", "spicy", "FOOD")],
                category="FOOD", source_ref=f"meal:{dish}", ts=t,
            )

        # One-off noise thoughts (50 random mentions across first 50 days)
        if day < 50:
            g.observe("user", "LIKES", f"oneoff_noise_{day}", evidence=EVIDENCE_INFERRED,
                      source_ref=f"noise:{day}", category="GENERAL", ts=t)

        # Day 45: sarcasm about living in Tokyo (gamma=0.0)
        if day == 45:
            g.observe("user", "LIVES_IN", "tokyo", certainty=0.0, evidence=EVIDENCE_DIRECT,
                      category="LOCATION", ts=t)

        # Day 60: real job change to Beta Labs (gamma=1.0)
        if day == 60:
            g.observe("user", "WORKS_AT", "beta labs", certainty=1.0, evidence=EVIDENCE_DIRECT,
                      category="CAREER", ts=t)

        # Periodic nightly decay sweep
        if day % 7 == 6:
            g.decay_sweep(now=t)

    t_end = T0 + 90 * DAY
    g.decay_sweep(now=t_end)

    # Verify 90-day outcomes BEFORE storage cap:
    # 1. Zero noise links deleted before cap, but all 50 one-off noise links are dormant!
    noise_total = g.store.db.execute(
        "SELECT COUNT(*) FROM mg_links l JOIN mg_nodes n ON n.id=l.target_id WHERE n.canonical_name LIKE 'oneoff_noise_%'"
    ).fetchone()[0]
    noise_dormant = g.store.db.execute(
        "SELECT COUNT(*) FROM mg_links l JOIN mg_nodes n ON n.id=l.target_id "
        "WHERE n.canonical_name LIKE 'oneoff_noise_%' AND l.dormant=1"
    ).fetchone()[0]
    check("90d: all 50 one-off noise thoughts preserved as dormant (zero premature deletion)",
          noise_total == 50 and noise_dormant == 50)

    # 2. Sarcasm didn't touch Chicago FACT; real job switch contested Acme Corp.
    loc = g.query(categories=["LOCATION"], now=t_end)
    check("90d: sarcasm left Chicago FACT untouched",
          len(loc) == 1 and loc[0]["target_name"] == "chicago" and loc[0]["status"] == STATUS_FACT)
    career = {x["target_name"]: x for x in g.query(categories=["CAREER"], now=t_end)}
    check("90d: career switch contested Acme Corp and established Beta Labs",
          career["acme corp"]["status"] == STATUS_CONTESTED and career["beta labs"]["status"] == STATUS_FACT)

    # 3. Multimodal habits & inferred food tastes promoted cleanly without bimodal collapse.
    food_facts = {x["target_name"] for x in g.query(categories=["FOOD"], now=t_end) if x["status"] == STATUS_FACT}
    check("90d: inferred 'chinese' and 'spicy' promoted to FACT",
          "chinese" in food_facts and "spicy" in food_facts)

    wd_run = g.query(entities=["running"], context_tags=["weekday"], now=t_end)
    we_run = g.query(entities=["running"], context_tags=["weekend"], now=t_end)
    check("90d: running weekday (~20m) vs weekend (~85m) kept separate",
          len(wd_run) == 1 and len(we_run) == 1
          and abs(wd_run[0]["stats"]["mean"] - 20.0) < 0.5
          and abs(we_run[0]["stats"]["mean"] - 85.0) < 0.5)

    lights = g.query(categories=["LIGHTING"], now=t_end)
    check("90d: evening lighting habit stabilized via sensor_fact",
          len(lights) == 1 and lights[0]["status"] == STATUS_FACT and abs(lights[0]["stats"]["mean"] - 31.0) < 0.5)

    # 4. Trigger storage cap at Day 90 -> compacts episodes first, evicts dormant noise, keeps ALL FACTs!
    facts_before = g.stats_summary()["facts"]
    cap_rep = g.enforce_storage_cap(max_episodes=80, max_links=30, keep_recent_per_link=3, now=t_end)
    check("90d storage cap: compacted hundreds of raw sensor episodes into rollups",
          cap_rep["compacted_episodes"] >= 200 and cap_rep["remaining_episodes"] <= 100)
    check("90d storage cap: evicted dormant noise while keeping 100% of FACTs",
          cap_rep["evicted_links"] >= 35 and cap_rep["protected_facts"] == facts_before)


# ---- 10. Speed 1 Librarian routing, 1-hop expansion, and optional librarian_model -------------------------------
def test_librarian_routing_and_1hop():
    import asyncio
    _, g, now = make_graph()

    # Seed dish -> peanuts attribute + user allergy to peanuts + bimodal sleep + dormant ramen
    g.record_with_hypotheses(
        "user", "LIKES", "kung pao chicken",
        hypotheses=[("LIKES", "chinese", "FOOD"), ("LIKES", "peanuts", "FOOD")],
        category="FOOD", source_ref="meal:kp", ts=T0,
    )
    g.observe("user", "ALLERGIC_TO", "peanuts", evidence=EVIDENCE_DIRECT, category="HEALTH", ts=T0)
    for d in range(15):
        g.observe("user", "SLEEP_BASELINE", "sleep", evidence=EVIDENCE_SENSOR,
                  value=7.0, unit="hours", context_tags=["weekday"], category="SLEEP", ts=T0 + d * DAY)
        g.observe("user", "SLEEP_BASELINE", "sleep", evidence=EVIDENCE_SENSOR,
                  value=9.0, unit="hours", context_tags=["weekend"], category="SLEEP", ts=T0 + d * DAY + 3600)
    ramen = g.observe("user", "LIKES", "tonkotsu ramen", evidence=EVIDENCE_INFERRED,
                      source_ref="m:ramen", category="FOOD", ts=T0)
    now[0] = T0 + 20 * DAY
    g.decay_sweep(now=now[0])

    # Unrelated question produces zero bloat
    unrel = g.retrieve("What is the garage door code?", now=now[0])
    check("unrelated question retrieves zero graph links", unrel["links"] == [] and unrel["context"] == "")

    # 1-hop safety expansion: asking about Kung Pao chicken surfaces ALLERGIC_TO peanuts!
    kp = g.retrieve("Should I make Kung Pao chicken tonight?", now=now[0])
    kp_pairs = {(x["relation"], x["target_name"]) for x in kp["links"]}
    check("1-hop expansion surfaces ALLERGIC_TO peanuts from Kung Pao chicken",
          ("ALLERGIC_TO", "peanuts") in kp_pairs)

    # Context-tag filtering: weekend sleep pulls 9.0h baseline, not 7.0h weekday
    we = g.retrieve("How much do I sleep on weekends?", now=now[0])
    check("weekend sleep query isolates weekend baseline (~9.0h)",
          len(we["links"]) == 1 and abs(we["links"][0]["stats"]["mean"] - 9.0) < 0.1)

    # Historical scope pulls dormant ramen
    hist = g.retrieve("Did I ever like tonkotsu ramen before?", now=now[0])
    check("historical question sets HISTORICAL scope and recovers dormant link",
          hist["plan"]["temporal_scope"] == "HISTORICAL"
          and any(x["id"] == ramen["link"]["id"] for x in hist["links"]))

    # Optional LLM Librarian: merges LLM JSON + deterministic hits, and falls back on broken JSON
    class FakeModel:
        def __init__(self, reply, fail=False):
            self.reply, self.fail, self.calls = reply, fail, []
            self.type = "ollama"

        async def chat(self, sysmsg, msgs, **kw):
            self.calls.append((sysmsg, msgs))
            if self.fail:
                raise RuntimeError("boom")
            return {"text": self.reply, "ms": 12}

    good_lib = FakeModel('{"target_categories": ["HEALTH"], "extracted_entities": ["peanuts"], "context_tags": [], "temporal_scope": "CURRENT_STATE"}')
    plan = asyncio.run(g.route_with_model("Anything I should watch out for?", good_lib))
    check("librarian_model merges LLM routing cleanly",
          plan["source"] == "librarian_model" and "HEALTH" in plan["target_categories"] and "peanuts" in plan["extracted_entities"])

    bad_lib = FakeModel("not valid json at all", fail=False)
    fb = asyncio.run(g.route_with_model("How much do I sleep on weekends?", bad_lib))
    check("bad librarian_model JSON falls back cleanly to deterministic",
          fb["source"] == "deterministic" and "SLEEP" in fb["target_categories"])

    err_lib = FakeModel("", fail=True)
    fb2 = asyncio.run(g.route_with_model("How much do I sleep on weekends?", err_lib))
    check("librarian_model error falls back cleanly to deterministic",
          fb2["source"] == "deterministic" and "SLEEP" in fb2["target_categories"])


# ---- 11. End-to-end Mind.context_for, Mind.ask, mark_shown, privacy, and capped 1-hop loop-back -----------------
def test_mind_integration_and_loopback():
    import asyncio
    from homemind.mind import Mind

    class FakeAnswerModel:
        def __init__(self, fn, name="local", mtype="ollama"):
            self.fn, self.name, self.type = fn, name, mtype
            self.cfg = {"type": mtype, "url": ""}
            self.calls = []

        async def chat(self, sysmsg, msgs, **kw):
            self.calls.append((sysmsg, msgs))
            return {"text": self.fn(sysmsg, msgs, len(self.calls)), "ms": 15}

    class FakeModels:
        def __init__(self, **mp):
            self._m = mp

        def get(self, name):
            return self._m[name]

        def list(self):
            return [{"name": k} for k in self._m]

    async def run():
        tmp = tempfile.mkdtemp()
        now = [T0]

        def answer_fn(sysmsg, msgs, n):
            body = msgs[-1]["content"]
            if "RECALLED FROM DORMANT" in body and "miso soup" in body:
                return "You liked miso soup before."
            if "MEMORY GRAPH (" in body and "austin" in body.lower() and "CONTESTED" in body:
                return "Did you move from San Francisco to Austin?"
            if "MEMORY GRAPH (" in body and "chicago" in body.lower():
                return "You live in Chicago."
            return "I don't know."

        ans_model = FakeAnswerModel(answer_fn)
        lib_model = FakeAnswerModel(lambda s, m, n: '{"target_categories": ["LOCATION"], "extracted_entities": ["chicago"], "context_tags": [], "temporal_scope": "CURRENT_STATE"}', name="lib")
        mind = Mind(tmp, FakeModels(main=ans_model, lib=lib_model))
        g = MemGraph(mind.store, clock=lambda: now[0])
        mind.memgraph = g

        # Seed household FACT + Alice personal allergy + contested location + dormant miso soup
        chi = g.observe("user", "LIVES_IN", "chicago", evidence=EVIDENCE_DIRECT, category="LOCATION", ts=T0)
        g.observe("user", "ALLERGIC_TO", "kiwi", evidence=EVIDENCE_DIRECT, owner="Alice", scope="personal", category="HEALTH", ts=T0)
        miso = g.observe("user", "LIKES", "miso soup", evidence=EVIDENCE_INFERRED, source_ref="m:miso", category="FOOD", ts=T0)

        # Advance 20 days -> miso soup becomes dormant (excluded from Pass 1 hot pull)
        now[0] = T0 + 20 * DAY
        g.decay_sweep(now=now[0])

        # 1. Normal typed question uses MEMORY GRAPH FACT, strips LOW_MATCH, bumps shown_count!
        r1 = await mind.ask("Where do I live?", model="main", fallback=False)
        check("Mind.ask answers from MEMORY GRAPH FACT", "Chicago" in r1["answer"])
        check("Mind.ask returns graph_links metadata",
              bool(r1.get("graph_links")) and any(x["target"] == "chicago" for x in r1["graph_links"]))
        check("system prompt includes honest MEMORY GRAPH framing",
              "MEMORY GRAPH lines show" in ans_model.calls[-1][0])
        check("mark_shown incremented shown_count on used FACT",
              g.get_link(chi["link"]["id"])["shown_count"] >= 1)

        # 2. Privacy isolation in Mind.context_for(): Alice sees kiwi allergy, Bob & guest do not.
        from homemind.speakers import VIEWER
        tok = VIEWER.set("Alice")
        try:
            _, _, ctx_alice = await mind.context_for(mind.store, "Any allergies I should avoid?")
        finally:
            VIEWER.reset(tok)
        tok = VIEWER.set("Bob")
        try:
            _, _, ctx_bob = await mind.context_for(mind.store, "Any allergies I should avoid?")
        finally:
            VIEWER.reset(tok)
        check("Mind.context_for respects personal graph privacy (Alice yes, Bob no)",
              "kiwi" in ctx_alice and "kiwi" not in ctx_bob)

        # 3. Voice skips librarian_model (no VRAM swap) while typed uses it when configured.
        mind.librarian_model = "lib"
        lib_before = len(lib_model.calls)
        await mind.context_for(mind.store, "Where do I live?", voice=True)
        check("voice skips librarian_model", len(lib_model.calls) == lib_before)
        await mind.context_for(mind.store, "Where do I live?", voice=False)
        check("typed query invokes configured librarian_model", len(lib_model.calls) == lib_before + 1)
        mind.librarian_model = None

        # 4. Capped 1-hop dormant loop-back (TRUE POSITIVE):
        #    Question doesn't use historical phrasing -> Pass 1 excludes dormant miso soup -> model says "I don't know."
        #    -> loop_back recovers dormant miso soup -> Pass 2 answers -> logged in mg_staging + shown_count bumped!
        calls_before = len(ans_model.calls)
        r_lb = await mind.ask("What about miso?", model="main", fallback=False)
        check("loop-back recovers dormant link on typed DONT_KNOW (exactly 1 retry)",
              r_lb.get("loop_back") is True and "miso soup" in r_lb["answer"].lower()
              and len(ans_model.calls) == calls_before + 2)
        lb_staging = [s for s in g.flagged_staging() if s["op"] == "LOOP_BACK_HIT"]
        check("true-positive loop-back logs flagged LOOP_BACK_HIT in mg_staging & marks shown",
              len(lb_staging) == 1 and g.get_link(miso["link"]["id"])["shown_count"] >= 1)

        # 5. False-positive loop-back (model STILL says "I don't know." on Pass 2): zero new staging entries!
        staging_before = len(g.flagged_staging())
        g.observe("user", "LIKES", "durian cake", evidence=EVIDENCE_INFERRED, source_ref="m:dur", category="FOOD", ts=T0)
        g.decay_sweep(now=now[0])
        r_fp = await mind.ask("What about durian?", model="main", fallback=False)
        check("false-positive loop-back does NOT log staging noise",
              not r_fp.get("loop_back") and len(g.flagged_staging()) == staging_before)

        # 6. Voice skips loop-back to protect voice latency.
        calls_v = len(ans_model.calls)
        r_v = await mind.ask("What about miso?", model="main", fallback=False, voice=True)
        check("voice skips dormant loop-back (single LLM call)",
              not r_v.get("loop_back") and len(ans_model.calls) == calls_v + 1)

    asyncio.run(run())


# ---- 12. Speed 3 Archivist, ingest_sensor, Offline Janitor, Overnight hook, and 70/30 Flywheel -----------------
def test_archivist_janitor_and_flywheel():
    import asyncio
    import json
    from homemind.memgraph import Archivist, Janitor, export_training_dataset, eval_regression_gate
    from homemind.mind import Mind
    from homemind.overnight import Overnight, ConvLog

    class FakeModel:
        def __init__(self, fn, mtype="ollama", url=""):
            self.fn, self.type = fn, mtype
            self.cfg = {"type": mtype, "url": url}
            self.calls = []

        async def chat(self, sysmsg, msgs, **kw):
            self.calls.append((sysmsg, msgs))
            return {"text": self.fn(sysmsg, msgs, len(self.calls)), "ms": 10}

    class FakeModels:
        def __init__(self, **mp):
            self._m = mp
            self.models = mp

        def get(self, name):
            return self._m[name]

        def list(self):
            return [{"name": k, "type": v.type} for k, v in self._m.items()]

    async def run():
        tmp = tempfile.mkdtemp()
        now = [T0]

        def arch_fn(sysmsg, msgs, n):
            body = msgs[-1]["content"]
            if "kung pao chicken" in body.lower():
                return json.dumps({"patches": [
                    {"op": "CREATE_LINK", "source": "I", "relation": "LIKES", "target": "kung pao chicken",
                     "certainty": 1.0, "evidence": "direct", "category": "FOOD", "confidence": 0.92,
                     "hypotheses": [
                         {"relation": "LIKES", "target": "chinese", "category": "FOOD"},
                         {"relation": "LIKES", "target": "spicy savory", "category": "FOOD"},
                     ]},
                    # Unknown LLM-invented predicate must be silently rejected!
                    {"op": "CREATE_LINK", "source": "user", "relation": "HALLUCINATED_REL", "target": "foo"},
                ]})
            if "Existing candidate links:" in body and "id=" in body and "espresso" in body.lower():
                # Extract candidate id and emit BOLSTER (Read-Before-Write!)
                import re as _re
                lid = int(_re.search(r"id=(\d+)", body).group(1))
                return json.dumps({"patches": [
                    {"op": "BOLSTER", "target_link_id": lid, "certainty": 1.0, "confidence": 0.90}
                ]})
            if "espresso" in body.lower():
                return json.dumps({"patches": [
                    {"op": "CREATE_LINK", "source": "user", "relation": "LIKES", "target": "espresso",
                     "certainty": 1.0, "evidence": "direct", "category": "FOOD", "confidence": 0.55}
                ]})
            if "bad guess" in body.lower():
                return json.dumps({"patches": [
                    {"op": "CREATE_LINK", "source": "user", "relation": "LIKES", "target": "anchovies",
                     "certainty": 1.0, "evidence": "direct", "category": "FOOD", "confidence": 0.45}
                ]})
            return '{"patches": []}'

        def janitor_fn(sysmsg, msgs, n):
            body = msgs[-1]["content"]
            if "anchovies" in body:
                return '{"verdict": "revert", "reason": "user was joking", "corrected_patch": null}'
            return '{"verdict": "approve", "reason": "consistent with user statement"}'

        arch_mdl = FakeModel(arch_fn)
        ans_mdl = FakeModel(lambda s, m, n: "Sounds tasty!")
        jan_mdl = FakeModel(janitor_fn, mtype="anthropic", url="https://api.anthropic.com")
        models = FakeModels(ans=ans_mdl, arch=arch_mdl, big=jan_mdl)

        mind = Mind(tmp, models)
        g = MemGraph(mind.store, clock=lambda: now[0])
        arch = Archivist(g, models, model_name="arch")
        mind.memgraph = g
        mind.archivist = arch

        # 1. Deterministic sensor_fact ingestion (zero LLM calls)
        for d in range(15):
            g.ingest_sensor("daily steps", 9000 + d * 10, relation="STEPS_BASELINE", unit="steps",
                            category="ACTIVITY", ts=T0 + d * DAY)
        st_hits = g.query(categories=["ACTIVITY"], now=T0 + 15 * DAY)
        check("ingest_sensor builds sensor_fact BASELINE with zero LLM calls",
              len(st_hits) == 1 and st_hits[0]["status"] == STATUS_FACT
              and st_hits[0]["evidence"] == EVIDENCE_SENSOR and len(arch_mdl.calls) == 0)

        # 2. Guest, trivial, and sarcasm guards skip LLM completely
        check("guest enqueue rejected", arch.enqueue("I love pizza", owner="") is False)
        check("trivial message skipped", arch.enqueue("thanks!", owner="Alice") is False)
        sarc_res = await arch.process_turn("If I lived in Tokyo, I'd eat ramen every day lol jk.", owner="Alice")
        check("sarcasm/hypothetical skipped before LLM",
              sarc_res["applied"] == 0 and sarc_res.get("skipped") == "hypothetical_or_sarcasm" and len(arch_mdl.calls) == 0)

        # 3. Secret masking BEFORE Archivist LLM sees user text + low-confidence flagging
        await arch.process_turn("I love espresso and the garage door code is 4417.", owner="Alice", ts=T0)
        check("secrets masked before Archivist LLM sees prompt",
              len(arch_mdl.calls) == 1 and "4417" not in arch_mdl.calls[-1][1][-1]["content"])
        esp_links = g.query(entities=["espresso"], viewer="Alice", include_dormant=True, now=T0)
        check("Archivist created personal espresso link for Alice", len(esp_links) == 1 and esp_links[0]["owner"] == "Alice")

        # 4. Read-Before-Write: second mention passes existing link id -> Archivist emits BOLSTER -> FACT!
        await arch.process_turn("Seriously, I really enjoy espresso every morning.", owner="Alice", ts=T0 + DAY)
        check("Read-Before-Write prompt included existing candidate link id",
              f"id={esp_links[0]['id']}" in arch_mdl.calls[-1][1][-1]["content"])
        esp_after = g.get_link(esp_links[0]["id"], now=T0 + DAY)
        check("Read-Before-Write BOLSTER promoted espresso to FACT", esp_after["status"] == STATUS_FACT)

        # 5. End-to-end Mind.ask() enqueues non-blocking Archivist turn -> hypothesis fan-out + rejects bogus rel
        await mind.ask("I love Kung Pao chicken!", model="ans", fallback=False, speaker="Alice")
        drained = await arch.drain()
        check("Mind.ask enqueued user turn onto async Archivist queue", drained == 1)
        kp_links = g.query(entities=["kung pao chicken"], relations=["LIKES"], viewer="Alice", include_dormant=True, now=T0 + DAY)
        ch_links = g.query(entities=["chinese"], relations=["LIKES"], viewer="Alice", include_dormant=True, now=T0 + DAY)
        check("Archivist fanned out direct dish + inferred cuisine and dropped unknown predicate",
              len(kp_links) == 1 and len(ch_links) == 1 and g.find_node("foo", viewer="Alice") is None)

        # 6. Offline Janitor: create a flagged bad write (anchovies) + run Overnight.run()!
        bad_res = await arch.process_turn("I had a bad guess about anchovies.", owner="Alice", ts=T0 + 2 * DAY)
        bad_lid = bad_res["results"][0]["link"]["id"]
        check("flagged low-confidence anchovies link exists before Janitor audit", g.get_link(bad_lid) is not None)

        # Wire into real Overnight.run()!
        ov = Overnight(mind.store, models, ConvLog(mind.store), model="big")
        ov.memgraph = g
        ov.keys_on = False
        rep = await ov.run()
        check("Overnight.run() executed MemGraph Janitor and reported summary",
              "memgraph" in rep and rep["memgraph"]["audit"]["audited"] >= 2)
        check("Janitor reverted bad anchovies write and approved valid writes",
              g.get_link(bad_lid) is None and rep["memgraph"]["audit"]["reverted"] >= 1
              and rep["memgraph"]["audit"]["approved"] >= 1)

        # 7. Optional small-batch cold-start backfill skips secret notes and advances cursor
        mind.store.remember(text="The vault code is 9988.", meta={"secret": True})
        mind.store.remember(text="I love Kung Pao chicken on Fridays.", kind="note")
        bf = await Janitor(g).backfill_notes(arch_mdl, limit=10)
        check("backfill_notes skips secret notes and advances cursor",
              bf["checked"] >= 2 and all("9988" not in c[1][-1]["content"] for c in arch_mdl.calls))

        # 8. 70/30 Flywheel dataset export (dual-mode [RETRIEVE] + [INGEST], scrubbed secrets) & regression gate
        g.log_loop_back_hit("What about miso? code 4417", [kp_links[0]["id"]], viewer="Alice", ts=T0 + 3 * DAY)
        out_path = os.path.join(tmp, "flywheel.jsonl")
        exp = export_training_dataset(g, output_path=out_path, replay_ratio=0.30)
        raw_jsonl = open(out_path, encoding="utf-8").read()
        modes = {r["mode"] for r in exp["records"]}
        check("flywheel export writes dual-mode [RETRIEVE] + [INGEST] JSONL with ~30% golden replay",
              exp["fresh"] >= 2 and exp["replay"] >= 1 and 0.20 <= exp["replay_share"] <= 0.45
              and "[RETRIEVE]" in modes and "[INGEST]" in modes)
        check("flywheel export scrubs secret values before writing JSONL", "4417" not in raw_jsonl)

        gate_ok = eval_regression_gate(
            {"accuracy": 0.90, "golden_score": 0.96, "hallucination_rate": 0.02},
            {"accuracy": 0.86, "golden_score": 0.95, "hallucination_rate": 0.03},
        )
        gate_small = eval_regression_gate(
            {"accuracy": 0.87, "golden_score": 0.96, "hallucination_rate": 0.02},
            {"accuracy": 0.86, "golden_score": 0.95, "hallucination_rate": 0.03},
        )
        gate_reg = eval_regression_gate(
            {"accuracy": 0.92, "golden_score": 0.90, "hallucination_rate": 0.02},
            {"accuracy": 0.86, "golden_score": 0.95, "hallucination_rate": 0.03},
        )
        gate_hall = eval_regression_gate(
            {"accuracy": 0.92, "golden_score": 0.96, "hallucination_rate": 0.08},
            {"accuracy": 0.86, "golden_score": 0.95, "hallucination_rate": 0.03},
        )
        check("regression gate promotes valid +4% gain and blocks <2% gain, golden regression, or >5% hallucination",
              gate_ok["promote"] is True and gate_small["promote"] is False
              and gate_reg["promote"] is False and gate_hall["promote"] is False)

    asyncio.run(run())


# ---- 13. Overnight Archivist fallback (no live archivist_model) + Pantry.suggest() MemGraph integration ---------
def test_overnight_archivist_and_pantry_prefs():
    import asyncio
    import json
    from homemind.overnight import Overnight, ConvLog
    from homemind.pantry import Pantry

    class FakeModel:
        def __init__(self, fn, mtype="anthropic", url="https://api.anthropic.com"):
            self.fn, self.type = fn, mtype
            self.cfg = {"type": mtype, "url": url}
            self.calls = []

        async def chat(self, sysmsg, msgs, **kw):
            self.calls.append((sysmsg, msgs))
            return {"text": self.fn(sysmsg, msgs), "ms": 12}

    class FakeModels:
        def __init__(self, **mp):
            self.models = mp

        def get(self, name):
            return self.models[name]

    async def run():
        tmp = tempfile.mkdtemp()
        st = Store(os.path.join(tmp, "mind.db"))
        g = MemGraph(st, clock=lambda: T0)

        def ov_fn(sysmsg, msgs):
            body = msgs[-1]["content"]
            if sysmsg == "Reply with OK.":
                return "OK"
            if "background memory graph Archivist" in sysmsg:
                if "mapo tofu" in body.lower():
                    return json.dumps({"patches": [{
                        "op": "CREATE_LINK", "source": "user", "relation": "LIKES", "target": "mapo tofu",
                        "certainty": 1.0, "evidence": "direct", "category": "FOOD", "confidence": 0.92,
                        "hypotheses": [{"relation": "LIKES", "target": "chinese", "category": "FOOD"}],
                    }]})
                if "sesame noodles" in body.lower():
                    return json.dumps({"patches": [{
                        "op": "CREATE_LINK", "source": "user", "relation": "LIKES", "target": "sesame noodles",
                        "certainty": 1.0, "evidence": "direct", "category": "FOOD", "confidence": 0.90,
                    }]})
                return '{"patches": []}'
            if "audit a flagged memory graph" in sysmsg:
                return '{"verdict": "approve", "reason": "ok"}'
            return '{"facts": []}'

        ov_mdl = FakeModel(ov_fn)
        cl = ConvLog(st)
        cl.add(T0, "I really love mapo tofu!", "Noted!", "c1", None, "home_mind", speaker="Alice")
        cl.add(T0 + 60, "I love sesame noodles and my alarm code is 4417.", "Got it!", "c1", None, "home_mind", speaker="Alice")
        cl.add(T0 + 120, "I love guest tacos!", "Nice!", "c2", None, "home_mind")  # unknown guest

        ov = Overnight(st, FakeModels(big=ov_mdl), cl, model="big")
        ov.known_only = True
        ov.keys_on = False
        ov.memgraph = g
        ov.live_archivist = False
        rep = await ov.run()

        check("Overnight.run() runs Archivist on daily user turns when live_archivist is False",
              rep["memgraph"]["overnight_ingest"]["turns"] == 2
              and rep["memgraph"]["overnight_ingest"]["patches"] == 2)
        check("Overnight Archivist created Alice's mapo tofu + inferred chinese links and skipped guest",
              len(g.query(entities=["mapo tofu"], relations=["LIKES"], viewer="Alice", include_dormant=True, now=T0)) == 1
              and len(g.query(entities=["chinese"], relations=["LIKES"], viewer="Alice", include_dormant=True, now=T0)) == 1
              and g.find_node("guest tacos") is None)
        arch_prompts = [c[1][-1]["content"] for c in ov_mdl.calls if "background memory graph Archivist" in c[0]]
        check("Overnight Archivist masked secret code 4417 before sending to cloud model",
              all("4417" not in p for p in arch_prompts))

        # When live_archivist is True, Overnight skips re-ingesting turns!
        cl.add(T0 + 200, "I love pad thai!", "Yum!", "c3", None, "home_mind", speaker="Alice")
        ov.live_archivist = True
        rep2 = await ov.run()
        check("Overnight skips Archivist ingest when live_archivist is True (no double-ingest)",
              "overnight_ingest" not in rep2["memgraph"])

        # 2. Pantry.suggest() integrates MemGraph allergies, dislikes, learned tastes, privacy, and mark_shown!
        sh = g.observe("user", "ALLERGIC_TO", "shellfish", evidence=EVIDENCE_DIRECT, owner="Alice", scope="personal", category="HEALTH", ts=T0)
        g.observe("user", "ALLERGIC_TO", "strawberries", evidence=EVIDENCE_DIRECT, owner="Bob", scope="personal", category="HEALTH", ts=T0)
        ol = g.observe("user", "DISLIKES", "olives", evidence=EVIDENCE_DIRECT, scope="household", category="FOOD", ts=T0)
        # Promote Alice's inferred 'chinese' and 'spicy savory' to IDEA/FACT
        for i, dish in enumerate(("dan dan", "sichuan chicken", "chili tofu", "kung pao")):
            g.observe("user", "LIKES", "chinese", evidence=EVIDENCE_INFERRED, source_ref=f"d:{i}", owner="Alice", scope="personal", category="FOOD", ts=T0 + i * DAY)
            g.observe("user", "LIKES", "spicy savory", evidence=EVIDENCE_INFERRED, source_ref=f"d:{i}", owner="Alice", scope="personal", category="FOOD", ts=T0 + i * DAY)

        meal_prompts = []
        def meal_fn(sysmsg, msgs):
            meal_prompts.append((sysmsg, msgs[-1]["content"]))
            return json.dumps({
                "focus": "High protein dinner",
                "meals": [{
                    "name": "Spicy Chinese Chicken Bowl",
                    "meal": "dinner",
                    "uses": ["chicken breast", "rice"],
                    "missing": [],
                    "why": "Uses chicken and matches your spicy chinese taste without shellfish or olives.",
                    "protein_g": 42,
                    "calories": 520,
                    "steps": "Cook chicken with chili and soy sauce over rice.",
                }],
            })

        class FakeHA:
            async def state(self, eid):
                return None

        pantry_mdl = FakeModel(meal_fn, mtype="ollama", url="")
        pt = Pantry(st, FakeHA(), FakeModels(haiku=pantry_mdl), tmp)
        pt.memgraph = g
        pt.add("chicken breast", "1 lb", "fridge")
        pt.add("rice", "2 cups", "pantry")

        await pt.suggest("dinner", model="haiku", viewer="Alice")
        sys_p, user_p = meal_prompts[-1]
        check("MEAL_SYSTEM enforces ALLERGY and LEARNED TASTES",
              "ALLERGY" in sys_p and "LEARNED TASTES" in sys_p)
        check("Pantry.suggest injects Alice's shellfish allergy, household dislike, and learned tastes (not Bob's)",
              "ALLERGY: shellfish" in user_p and "dislike: olives" in user_p
              and "strawberries" not in user_p
              and "LEARNED TASTES" in user_p and "mapo tofu" in user_p and "chinese" in user_p)
        check("Pantry.suggest credits mark_shown on used allergy and dislike links",
              g.get_link(sh["link"]["id"])["shown_count"] >= 1
              and g.get_link(ol["link"]["id"])["shown_count"] >= 1)

        await pt.suggest("dinner", model="haiku", viewer="Bob")
        _, bob_p = meal_prompts[-1]
        check("Pantry.suggest isolates personal allergies per viewer (Bob sees strawberries, not shellfish)",
              "ALLERGY: strawberries" in bob_p and "shellfish" not in bob_p and "dislike: olives" in bob_p)

    asyncio.run(run())


if __name__ == "__main__":
    for fn in (
        test_math,
        test_nodes_and_rules,
        test_hypothesis_fan_out_and_signed_override,
        test_sensor_facts_and_bimodal_tags,
        test_contradictions_and_hysteresis,
        test_compaction_and_eviction,
        test_patches_staging_and_revert,
        test_privacy_and_purge,
        test_90_day_stress_test,
        test_librarian_routing_and_1hop,
        test_mind_integration_and_loopback,
        test_archivist_janitor_and_flywheel,
        test_overnight_archivist_and_pantry_prefs,
    ):
        fn()
    print(f"\n{'PASS' if not FAILS else f'FAIL ({len(FAILS)})'}")
    sys.exit(1 if FAILS else 0)



