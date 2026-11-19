"""The phrasebook (0.28): odd ways of saying sleep and workouts with the plain line Home Mind reads. It starts with a
checked starter set, learns from model readings and corrections, grows overnight (kept only when a blind second
reading agrees), reads known shapes with no model, and gives a small model the closest examples. No Home Assistant
or real model needed."""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402
from homemind.phrasebook import Phrasebook, STARTER, meaning, new_numbers, template, _norm  # noqa: E402
from homemind.fitness import Fitness  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def fresh():
    st = Store(tempfile.mktemp())
    return st, Phrasebook(st)


def test_starter():
    st, pb = fresh()
    for s, p in STARTER:
        m = meaning(p)
        check(f"starter reads: {s!r}", m is not None and (p == "NONE" or not new_numbers(s, p)), (m, new_numbers(s, p)))
    n = pb.stats()["active"]
    check("starter filed", n == len(STARTER), n)
    pb2 = Phrasebook(st)
    check("starting again adds nothing twice", pb2.stats()["active"] == n)
    e = pb.entries("active", source="starter", limit=1)[0]
    pb.remove(e["id"])
    Phrasebook(st)
    check("a removed starter entry stays removed", pb.stats()["active"] == n - 1)


def test_checks():
    st, pb = fresh()
    for said, plain in [("crashed at 12 up at 7", "slept from 12 am to 8 am"),      # 8 never said
                        ("did squats", "squat 3x8 at 185 lb"),
                        ("blah", "eat a sandwich"), ("", "NONE")]:
        try:
            pb.add(said, plain)
            check(f"refused: {said!r} -> {plain!r}", False)
        except ValueError:
            check(f"refused: {said!r} -> {plain!r}", True)
    e = pb.add("five sets of five on squats with 225", "squat 5x5 at 225 lb")
    check("number words count as said", e["kind"] == "workout")
    e2 = pb.add("Five sets of five on squats, with 225!", "squat 5x5 at 225 lb", "learned")
    check("same wording (case, punctuation) is one entry", e2["id"] == e["id"])
    pb.add("crashed at 12 up at 7", "slept 6 hours", "correction", numbers_said=False)
    e3 = pb.add("crashed at 12 up at 7", "slept from 12 am to 7 am", "grown")
    check("a guess never overwrites a correction", e3["plain"] == "slept 6 hours", e3)


def test_match_and_similar():
    st, pb = fresh()
    hit = pb.match("zonked at 12, kid woke me at 6:10")
    check("same shape, new numbers: read with no model", hit and hit[1] == "slept from 12 am to 6:10 am", hit)
    hit = pb.match("knocked out 3 sets of 8 on squats with 185")
    check("workout shape generalizes", hit and meaning(hit[1]) == ("workout", "squat 3×8 at 185 lb"), hit)
    hit = pb.match("watched TV for 3 hours last night")
    check("a known non-log shape says NONE", hit and hit[1] == "NONE", hit)
    check("unrelated: no match", pb.match("turn off the kitchen light") is None)
    sim = pb.similar("conked out at midnight, alarm went at 6", 3)
    check("similar finds the sleep slang", sim and "conked" in sim[0]["said"], [x["said"] for x in sim])
    ex = pb.examples("conked out at midnight, alarm went at 6")
    check("examples include a NONE so the model knows it may say no", "Line: NONE" in ex, ex)
    rx, pt = template("zonked at 1, kid woke me at 5:40", "slept from 1 am to 5:40 am")
    check("template keeps the numbers' places", pt == "slept from {0} am to {1} am", pt)


class FakeModel:
    """Answers the grow prompt with prepared cases and the read prompt from a table (what a model would say)."""

    def __init__(self, reads=None, cases=None):
        self.reads, self.cases, self.calls = reads or {}, cases or [], []

    async def chat(self, system, messages, **k):
        self.calls.append((system, messages[0]["content"]))
        if system.startswith("You write test cases"):
            return {"text": json.dumps(self.cases)}
        return {"text": self.reads.get(messages[0]["content"], "NONE")}


def test_grow_and_eval():
    st, pb = fresh()
    m = FakeModel(cases=[
        {"said": "dropped off around 12:30, baby got me up at 6", "plain": "slept from 12:30 am to 6 am"},   # agreed
        {"said": "out cold 1ish to 8ish", "plain": "slept from 1 am to 8 am"},                              # disputed
        {"said": "slept from 12 to 7", "plain": "slept from 12 am to 7 am"},          # the parsers already read it
        {"said": "slept a bit", "plain": "slept 6 hours"},                              # 6 nobody said
        {"said": "racked up 3 sets of 5 on the press w/ 95", "plain": "overhead press 3x5 at 95 lb"},  # parser misreads
        {"said": "thinking of doing 5x5 tomorrow", "plain": "NONE"},                    # the parsers agree: nothing new
        "not an object",
    ], reads={"dropped off around 12:30, baby got me up at 6": "slept from 12:30 am to 6 am",
              "out cold 1ish to 8ish": "slept 6 hours",
              "racked up 3 sets of 5 on the press w/ 95": "overhead press 3x5 at 95 lb"})
    g = asyncio.run(pb.grow(m, n=7, topics=["x"]))
    check("grow keeps agreed cases", g["kept"] == 2, g)
    check("a case the built-in reading gets wrong is counted", g["parser_misread"] == 1, g)
    check("a disagreement waits for review", g["review"] == 1 and pb.stats()["pending"] == 1, g)
    check("invented numbers and junk dropped", g["dropped"] == 2, g)
    check("cases the parsers already read aren't added", g["known"] == 2, g)
    pend = pb.entries("pending")[0]
    check("review note says what the second reading said", "slept 6 hours" in pend["note"], pend)
    check("a pending case isn't used to read", pb.match("out cold 1ish to 8ish") is None)
    pb.set_status(pend["id"], "active")
    check("approved: used", pb.match("out cold 1ish to 8ish") is not None)
    reads = {s: p for s, p in STARTER}
    reads["zonked at 1, kid woke me at 5:40"] = "slept from 1 am to 9 am"      # a wrong log (9 never said)
    reads["watched TV for 2 hours last night"] = "slept 2 hours"               # a wrong log
    ev = asyncio.run(pb.evaluate(FakeModel(reads=reads), limit=500))
    check("eval counts every entry", ev["n"] == pb.stats()["active"], ev)
    check("eval: invented number counts as missed, not right", any(x["said"].startswith("zonked") for x in ev["misses"]))
    check("eval: a log where none belongs is a wrong log", ev["wrong_log"] >= 1, ev)
    check("eval never shows a model its own entry", all(
        "Message: " + c[1] + "\n" not in c[0] for c in FakeModel(reads=reads).calls))


def make_mind(reads=None):
    from homemind.mind import Mind
    st = Store(tempfile.mktemp())
    m = Mind.__new__(Mind)
    m.store, m.views = st, None
    m.fitness = Fitness(st)
    m.phrasebook = Phrasebook(st)
    m._conversations = lambda store: {}
    m._handoff = lambda *a: None
    m._chain = lambda model: [model]
    fm = FakeModel(reads=reads)

    class Models:
        def get(self, name):
            return fm
    m.models = Models()

    async def ctx(*a, **k):
        raise RuntimeError("model path reached")
    m.context_for = ctx
    return m, st, fm


def ask(m, q, model="m"):
    try:
        return asyncio.run(m._ask(q, model=model))
    except RuntimeError:
        return None


def test_mind():
    m, st, fm = make_mind({"sawed logs from 12:30 till my 7 alarm": "slept from 12:30 am to 7 am"})
    out = ask(m, "zonked at 12, kid woke me at 6:10")
    check("phrasebook shape: logged with no model", out and out["sleep"]["hours"] == 6.17 and not fm.calls, out)
    out = ask(m, "watched TV for 3 hours last night")
    check("phrasebook NONE: not logged, answered as usual", out is None and not fm.calls)
    out = ask(m, "sawed logs from 12:30 till my 7 alarm")
    check("model reading logged", out and out["sleep"]["hours"] == 6.5, out)
    sys_prompt = fm.calls[-1][0]
    check("the model got phrasebook examples", "Examples:" in sys_prompt and "Message:" in sys_prompt)
    learned = m.phrasebook.entries("active", source="learned")
    check("the reading is learned", learned and learned[0]["said"] == "sawed logs from 12:30 till my 7 alarm", learned)
    n = len(fm.calls)
    out = ask(m, "sawed logs from 1:30 till my 8 alarm")
    check("next time the same shape needs no model", out and out["sleep"]["hours"] == 6.5 and len(fm.calls) == n, out)
    # a correction right after a log
    out = ask(m, "Slept from 12 to 7am last night")
    first = out["sleep"]["id"]
    out = ask(m, "no, 6 hours")
    check("'no, 6 hours' fixes the log", out and out.get("fixed") == first and out["sleep"]["hours"] == 6.0, out)
    check("the wrong log is archived", st.get(first)["status"] == "archived")
    c = m.phrasebook.entries("active", source="correction")
    check("the correction is kept", c and c[0]["plain"] == "slept 6 hours", c)
    out = ask(m, "Slept from 12 to 7am last night")
    check("the same words now read as corrected", out and out["sleep"]["hours"] == 6.0, out)
    # "that wasn't sleep": removed, and never read as a log again
    out = ask(m, "crashed at 12, up at 7")
    wid = out["sleep"]["id"]
    out = ask(m, "that wasn't sleep, that was the baby")
    check("'that wasn't sleep' removes it", out and out.get("unlogged") == wid and st.get(wid)["status"] == "archived", out)
    check("and that wording is NONE from now on", ask(m, "crashed at 12, up at 7") is None)
    # "that's wrong", then the right one
    out = ask(m, "squatted 185 for 3 sets of 8")
    ask(m, "that's wrong")
    out = ask(m, "squat 3x5 at 185 lb")
    check("'that's wrong' then the right numbers: fixed", out and out.get("workout") and out.get("fixed"), out)
    c = [e for e in m.phrasebook.entries("active", source="correction") if e["kind"] == "workout"]
    check("workout correction kept", c and c[0]["plain"] == "squat 3x5 at 185 lb", c)
    # nothing to fix
    m2, _, _ = make_mind()
    check("'no, 6 hours' with nothing logged isn't a fix", (ask(m2, "no, 6 hours") or {}).get("fixed") is None)


def test_functiongemma():
    """The phrasebook trains the small local model: log_sleep / log_workout tools, examples in its dataset, and the
    ask path uses its call (checked like any rewrite) before a big model."""
    from homemind import localctl
    st, pb = fresh()
    names = {t["function"]["name"] for t in localctl.TOOLS}
    check("log tools in the schema", {"log_sleep", "log_workout"} <= names and "not_a_command" in names)
    d = localctl.build_dataset([{"entity_id": "light.den_lamp", "name": "Den lamp"}], n_per_device=5,
                               phrasebook=pb.export())
    rows = d["train"] + d["test"]
    logs = [r for r in rows if r["messages"][-1]["tool_calls"][0]["function"]["name"] in localctl.LOG_TOOLS]
    check("phrasebook pairs become examples", d["counts"]["phrasebook"] == len(pb.export()) and len(logs) > 30, d["counts"])
    nones = [r for r in rows if r["messages"][-2]["content"] == "watched TV for 2 hours last night"]
    check("a NONE pair teaches not_a_command", nones and
          nones[0]["messages"][-1]["tool_calls"][0]["function"]["name"] == "not_a_command")
    check("some pairs held out for the score", any(r["messages"][-1]["tool_calls"][0]["function"]["name"] in
                                                  localctl.LOG_TOOLS for r in d["test"]))
    plan = localctl.to_plan("log_sleep", {"line": "slept 6 hours"}, {}, set())
    check("a log isn't a device action", plan.get("log") == "log_sleep" and plan["entity_id"] is None, plan)

    m, st2, fm = make_mind()
    raw = {"text": "<start_function_call>call:log_sleep{line:<escape>slept from 1 am to 6:20 am<escape>}<end_function_call>"}

    class Ctl:
        n = 0

        async def call_tool(self, messages, tools, system=None, max_tokens=96):
            Ctl.n += 1
            return raw
    models = m.models

    class Both:
        models = {"ctl": Ctl(), "m": fm}

        def get(self, name):
            return Ctl() if name == "ctl" else models.get(name)
    m.models = Both()
    m.CONTROL_MODEL = "ctl"
    out = ask(m, "conked after the late game at 1, little one had me up at 6:20")
    check("the local model's log call is used", out and out.get("sleep") and out["sleep"]["read_by"] == "ctl" and
          out["sleep"]["hours"] == 5.33 and not fm.calls, out)
    raw["text"] = "<start_function_call>call:log_sleep{line:<escape>slept from 1 am to 9 am<escape>}<end_function_call>"
    fm.reads["dozed off at 1, kid got me at 6:40"] = "slept from 1 am to 6:40 am"
    out = ask(m, "dozed off at 1, kid got me at 6:40")
    check("its line with a number nobody said is refused; the big model reads it", out and
          out["sleep"].get("read_by") == "m" and out["sleep"]["hours"] == 5.67, out)


for fn in [test_starter, test_checks, test_match_and_similar, test_grow_and_eval, test_mind, test_functiongemma]:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"{fn.__name__} raised {e!r}", False)
print(f"\n{len(FAILS)} failed" if FAILS else "\nall phrasebook checks passed")
sys.exit(1 if FAILS else 0)
