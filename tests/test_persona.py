"""The persona (0.28): Jeeves by default. Each answer gets an emotion marker from what happened and the persona's manner
for it, and styling may add words but never change a fact: numbers, 'not's and 'Say yes' must survive or the plain
answer is used. No Home Assistant needed."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402
from homemind.persona import Persona, pick_emotion, check, EMOTIONS  # noqa: E402

FAILS = []


def check_(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def fresh():
    return Persona(Store(tempfile.mktemp()))


def test_emotions():
    cases = [
        ({"answer": "Do you want me to unlock the front door? Say yes.", "pending": True}, "", "alert"),
        ({"answer": "The front door is not locked."}, "", "alert"),
        ({"answer": "Fixed: 6 hours of sleep Monday night.", "fixed": 3}, "", "corrected"),
        ({"answer": "Removed that sleep log.", "unlogged": 3}, "", "corrected"),
        ({"answer": "Logged: squat 3×5 at 195 lb — a new best.", "workout": {}}, "", "proud"),
        ({"answer": "Logged: 7 hours.", "sleep": {"hours": 7.5}}, "", "pleased"),
        ({"answer": "Logged: 4 hours.", "sleep": {"hours": 4}}, "", "concerned"),
        ({"answer": "Logged: 6 hours.", "sleep": {"hours": 6}}, "", "neutral"),
        ({"answer": "The furnace filter was changed on March 3, 47 days ago."}, "", "disapproving"),
        ({"answer": "The furnace filter was changed on March 3, 12 days ago."}, "", "neutral"),
        ({"answer": "I don't have anything saved about the drill."}, "", "apologetic"),
        ({"answer": "The drill is in the garage.", "hits": [1]}, "", "neutral"),
    ]
    for res, q, want in cases:
        got = pick_emotion(res, q)
        check_(f"emotion {want}: {res['answer'][:40]!r}", got == want, got)
    check_("a tired day makes it sympathetic", pick_emotion({"answer": "3 meetings today."}, tired=True) == "sympathetic")
    check_("but a locked-door question stays alert on a tired day",
           pick_emotion({"answer": "Do you want me to unlock it? Say yes."}, tired=True) == "alert")
    check_("a new best is still proud on a tired day",
           pick_emotion({"answer": "Logged — a new best.", "workout": {}}, tired=True) == "proud")
    check_("asked again: amused", pick_emotion({"answer": "It's in the drawer."}, repeat=True) == "amused")
    check_("locking it: relieved", pick_emotion({"answer": "Done: front door locked."}, "lock the front door",
                                                kind="do") == "relieved")


def test_style():
    p = fresh()
    for e in p.examples():
        ok, why = check(e["plain"], e["styled"])
        check_(f"example keeps the facts [{e['emotion']}]", ok and e["styled"] != e["plain"], (why, e))
    s, applied = p.style("Do you want me to unlock the front door? Say yes.", "alert")
    check_("a safety question is left word for word", s == "Do you want me to unlock the front door? Say yes." and
           not applied)
    s, _ = p.style("The front door is not locked.", "alert")
    check_("an alert state: formal, no remark", s == "I regret to say the front door is not locked.", s)
    s, _ = p.style("Logged: squat 3×5 at 195 lb — a new best.", "proud", who="Yitz")
    check_("proud: congratulations by name", "Yitz" in s and "195" in s, s)
    s, _ = p.style("- one\n- two", "pleased")
    check_("lists aren't styled", s == "- one\n- two")
    s, _ = p.style("x " * 400, "pleased")
    check_("long answers aren't styled", s == "x " * 400)
    p.set({"wit": "off"})
    s, _ = p.style("The wifi password is on the note.", "amused")
    check_("wit off: no remark", s == "The wifi password is on the note.", s)
    s, _ = p.style("You have 3 meetings today.", "sympathetic")
    check_("wit off: kindness stays", "gently" in s, s)
    p.set({"wit": "normal", "address": "sir"})
    s, _ = p.style("Logged: squat 3×5 — a new best.", "proud", who="Yitz")
    check_("address sir", "sir" in s and "Yitz" not in s, s)
    s, _ = p.style("Logged: 7 hours of sleep.", "pleased", voice=True)
    check_("voice: one short remark", s.count(".") <= 3, s)
    p.set({"enabled": False})
    check_("persona off: plain", p.style("Done: lights off.", "neutral") == ("Done: lights off.", False))


def test_check():
    check_("a lost number fails", not check("Weight 186.2 lb.", "Weight 186 lb.")[0])
    check_("a lost 'not' fails", not check("The door is not locked.", "The door is locked.")[0])
    check_("a lost n't fails", not check("I don't have it.", "I have it.")[0])
    check_("a lost 'Say yes' fails", not check("Unlock? Say yes.", "Shall I unlock it?")[0])
    check_("too long fails", not check("Done.", "Done. " + "Very good indeed. " * 10)[0])
    check_("added words pass", check("Done: lights off.", "Very good. Done: lights off.")[0])


def test_settings_and_markers():
    p = fresh()
    for bad in [{"name": ""}, {"address": "dude"}, {"wit": "max"}, {"enabled": "yes"}, {"colour": 1}]:
        try:
            p.set(bad)
            check_(f"bad setting refused: {bad}", False)
        except ValueError:
            check_(f"bad setting refused: {bad}", True)
    p.set({"name": "Hudson", "ssml": True})
    r = p.apply({"answer": "The furnace filter was changed on March 3, 47 days ago.", "hits": [1]}, "filter?")
    check_("apply marks the emotion and keeps the plain answer", r["emotion"] == "disapproving" and
           r["plain"].startswith("The furnace") and r["answer"] != r["plain"], r)
    check_("ssml has pace, pitch and a pause", r["ssml"].startswith("<speak><prosody") and "<break" in r["ssml"], r)
    check_("persona named", r["persona"] == "Hudson")
    r = p.apply({"answer": "a < b & c", "hits": [1]})
    check_("ssml is escaped", "&lt;" in r["ssml"] and "&amp;" in r["ssml"], r)
    check_("errors aren't styled", p.apply({"error": "x", "answer": "no"}) == {"error": "x", "answer": "no"})
    p.apply({"answer": "In the drawer.", "hits": [1]}, "where's the wifi password", who="Yitz")
    r = p.apply({"answer": "In the drawer.", "hits": [1]}, "Where's the wifi password?", who="Yitz")
    check_("the same question again: amused", r["emotion"] == "amused", r)
    check_("prompt names the persona and the fact rule", "Hudson" in p.prompt() and "add no facts" in p.prompt())
    check_("every emotion has a description", all(v.get("when") and v.get("how") for v in EMOTIONS.values()))


def test_emotion_for():
    p = fresh()
    r = p.apply({"answer": "Logged: 4 hours of sleep Monday night.", "sleep": {"hours": 4}}, "slept 4 hours")
    check_("the spoken answer gets its own marker", p.emotion_for(r["answer"]) == ("concerned", "answer"),
           p.emotion_for(r["answer"]))
    check_("quoted by the voice agent, still found", p.emotion_for("Noted: 4 hours of sleep Monday night.")[0] ==
           "concerned", p.emotion_for("Noted: 4 hours of sleep Monday night."))
    check_("words Home Mind never said: read from the words",
           p.emotion_for("Very well. My congratulations on the promotion.") == ("proud", "words"))
    check_("nothing to go on: neutral", p.emotion_for("The kettle is on.") == ("neutral", "words"))


def test_people_and_briefs():
    from datetime import datetime
    p = fresh()
    p.set({"address": "name", "wit": "normal"})
    st = p.set({"address": "madam", "wit": "low"}, person="Dana")
    check_("a person's own settings", st["address"] == "madam" and st["wit"] == "low" and st["name"] == "Jeeves", st)
    check_("others keep the house's", p.settings("yitz")["address"] == "name" and p.settings()["wit"] == "normal")
    for bad in [({"name": "Hudson"}, "dana"), ({"ssml": True}, "dana"), ({"wit": "max"}, "dana")]:
        try:
            p.set(*bad[:1], person=bad[1])
            check_(f"per person refused: {bad[0]}", False)
        except ValueError:
            check_(f"per person refused: {bad[0]}", True)
    s, _ = p.style("Logged: squat 3×5 — a new best.", "proud", who="Dana")
    check_("styled with the person's address", "madam" in s, s)
    p.set({"enabled": False}, person="Dana")
    check_("off for one person only", p.style("Done: lights off.", "neutral", who="Dana")[1] is False and
           p.style("Done: lights off.", "neutral", who="Yitz")[1] is True)
    p.set({"enabled": None, "address": None, "wit": None}, person="Dana")
    check_("cleared: back to the house's", "dana" not in p.people())
    brief = "Weather: 49° sunny.\nWeigh-in: 167.1 lb."
    out = p.frame(brief, "morning", "Yitz", now=datetime(2026, 11, 18, 7, 30))
    check_("morning brief framed, facts untouched", out.startswith("Good morning, Yitz.\n") and brief in out, out)
    out = p.frame(brief, "morning", "Yitz", tired=True, now=datetime(2026, 11, 18, 7, 30))
    check_("a tired morning ends gently", out.endswith("Do take it gently today."), out)
    out = p.frame(brief, "morning", None, now=datetime(2026, 11, 21, 7, 30))
    check_("a weekend morning", out.startswith("Good morning.\n") and out.endswith("Do enjoy the weekend."), out)
    check_("evening: good night", p.frame(brief, "evening", "Yitz").endswith("Good night, Yitz."))
    p.set({"enabled": False})
    check_("persona off: the brief as it was", p.frame(brief, "evening", "Yitz") == brief)


def test_telegram():
    """A Telegram question is answered through the persona, by name."""
    import asyncio
    from types import SimpleNamespace
    from homemind.messaging import Telegram
    p = fresh()
    seen = {}

    async def ask(text, model, **k):
        return {"answer": "The furnace filter was changed on March 3, 47 days ago.", "hits": [1]}

    async def styled(res, q, who=None):
        seen["who"] = who
        return p.apply(res, q, who=who)
    app = SimpleNamespace(mind=SimpleNamespace(ask=ask, store=Store(tempfile.mktemp())), persona=p, styled=styled,
                          scope_meta=lambda m, *a: m)
    t = Telegram(app, "tok", ["123: Yitz"])
    out = asyncio.run(t.handle({"chat": {"id": 123}, "text": "when was the furnace filter changed?"}))
    check_("Telegram answers go through the persona", "47 days ago" in out and
           ("if I may say so" in out or "hesitate" in out), out)
    check_("as the person who asked", seen.get("who") == "Yitz", seen)
    app.persona = None
    out = asyncio.run(t.handle({"chat": {"id": 123}, "text": "when was the furnace filter changed?"}))
    check_("no persona: the plain answer", out == "The furnace filter was changed on March 3, 47 days ago.", out)


for fn in [test_emotions, test_style, test_check, test_settings_and_markers, test_emotion_for, test_people_and_briefs,
           test_telegram]:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check_(f"{fn.__name__} raised {e!r}", False)
print(f"\n{len(FAILS)} failed" if FAILS else "\nall persona checks passed")
sys.exit(1 if FAILS else 0)
