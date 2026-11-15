"""New abilities read from plain notes: when a change became true, repeating things, chores, nested places, typed
fields, who is who, corrections kept as test cases, and duplicate flags. The suites (recurring, chores, places, typed,
valid, profile, tidy) measure them on made-up worlds; this checks the parsing and the plumbing around them.

  python tests/test_abilities.py
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import mind as M, structure as S, tidy  # noqa: E402
from homemind.cases import Cases, check as case_ok  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Fake:
    type, cfg = "fake", {}

    def __init__(self, reply):
        self.reply, self.sent = reply, []

    async def chat(self, system, messages, max_tokens=800, temperature=0.2):
        self.sent.append(messages[-1]["content"])
        return {"text": self.reply, "ms": 1, "tokens": 0}


class Models:
    def __init__(self, **ms):
        self.models = ms

    def get(self, n):
        return self.models[n]


async def main():
    fri = datetime(2026, 11, 13, 15, 0).timestamp()
    day = lambda ts: datetime.fromtimestamp(ts).date()

    # ---- parsing ---------------------------------------------------------------------------------------------------
    check("last Tuesday", day(S.valid_from("Last Tuesday the shed code changed to 1111.", fri)) == date(2026, 11, 10))
    check("N days ago", day(S.valid_from("Three days ago I moved the bikes.", fri)) == date(2026, 11, 10))
    check("'on Monday I changed'", day(S.valid_from("On Monday I changed the code to 5.", fri)) == date(2026, 11, 9))
    check("a habit is not a date", S.valid_from("The fern gets watered on Monday.", fri) is None)
    check("'changed back ... on Sunday' is a habit", S.valid_from("Changed back: the fern gets watered on Sunday.", fri) is None)
    check("the future is not 'since'", S.valid_from("Starting Monday the code will be 3.", fri) is None)
    nt = lambda t: S.next_time(S.recurrence(t, fri), fri)
    check("every Tuesday", nt("The trash goes out every Tuesday.") == date(2026, 11, 17))
    check("on Wednesdays", nt("Yard waste goes out on Wednesdays.") == date(2026, 11, 18))
    check("every other, from a date", nt("Recycling is every other Thursday starting Nov 19.") == date(2026, 11, 19))
    check("the 1st of every month", nt("Rent is due on the 1st of every month.") == date(2026, 12, 1))
    check("chore with -ing", (S.annotate("The gutters need cleaning every 6 months.", "", fri).get("chore") or {}).get("every_days") == 180)
    check("chore done", S.done_of("Finally descaled the coffee machine.") == ("descaled", "coffee machine"))
    check("place: is in", S.place("The drill is in the red toolbox.") == {"thing": "drill", "in": "red toolbox"})
    check("place: moved to", S.place("I moved the red toolbox to the shed.") == {"thing": "red toolbox", "in": "shed"})
    check("not a place: a code", S.place("The garage code is 4417.") is None)
    check("not a place: in charge", S.place("Dana is in charge of the garden.") is None)
    for t in ("My meeting is on Friday.", "My therapist is on Tuesdays.", "My exam is in two weeks.",
              "Mom's birthday is on March 3.", "The party is at 8pm.", "The report is on the 3rd.", "The trip is in June."):
        check(f"not a place: a time ({t})", S.place(t) is None, S.place(t))
    check("a place that looks like a month is still a place", S.place("The boat is in the marsh.") ==
          {"thing": "boat", "in": "marsh"})
    f = S.fields("The dishwasher is a Bosch SHX78, serial 4471A, bought March 2024, warranty until March 2027.", fri)
    check("fields read", f and f["fields"] == {"serial": "4471A", "bought": "2024-03-01", "warranty_until": "2027-03-31",
                                               "brand": "Bosch", "model": "SHX78"} and f["type"] == "appliance", f)
    check("warranty phrased another way", (S.fields("Warranty on the fridge ends Mar 3, 2028.", fri) or {}).get("fields", {})
          .get("warranty_until") == "2028-03-03")
    check("alias both ways", S.alias("Dana is my partner.") == ("partner", "Dana") and S.alias("My dentist is Dr Kell.")
          == ("dentist", "Dr Kell"))

    # ---- filing and the answer page ---------------------------------------------------------------------------------
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    a = st.remember(text="Dana is my partner.")
    check("who-is-who goes to the profile shelf", st.get(a["id"])["shelf"] == "profile")
    st.remember(text="Dana works as a nurse at Wellmont.")
    st.remember(text="Orrin works as a nurse at Brackley.")
    hits = M.find(st, "Where does my partner work?", 5, touch=False)
    check("'my partner' finds the note about Dana", "Wellmont" in hits[0]["body"], [h["body"] for h in hits])
    ctx, _ = M.build_context(st, "What should I cook tonight?", [])
    check("profile always on the page", "ABOUT YOU AND THE HOUSE" in ctx and "Dana is my partner" in ctx, ctx[:200])
    st.remember(text="The drill is in the red toolbox.")
    st.remember(text="The red toolbox is in the garage.")
    st.remember(text="I moved the red toolbox to the shed.")
    q = "Where is the drill?"
    ctx, _ = M.build_context(st, q, M.find(st, q, 8, touch=False))
    check("worked-out place on the page", "WORKED OUT" in ctx and "red toolbox" in ctx.split("\n\n")[1] and
          "shed" in ctx.split("\n\n")[1] and "garage" not in ctx.split("\n\n")[1], ctx[:400])
    k = "the kosk locker|code"
    t0 = time.time()
    st.clock = lambda: t0 - 2 * 86400
    st.remember(text="The Kosk locker code is 2222.", key=k, kind="fact")
    st.clock = lambda: t0
    late = st.remember(text="Four days ago I changed the Kosk locker code to 1111.", key=k, kind="fact")
    check("late-filed older change kept as history", late["action"] == "history" and
          st.active_by_key(k)["body"] == "The Kosk locker code is 2222.", late)
    st.clock = time.time

    # ---- corrections become test cases --------------------------------------------------------------------------
    d = tempfile.mkdtemp()
    m = M.Mind(d, Models(plain=Fake("The shed padlock code is 3381.")))
    old = m.store.remember(text="The shed padlock code is 3381.", key="the shed padlock|code", kind="fact")
    await m.ask("What's the shed padlock code?", "plain", fallback=False, conversation_id="c")
    await m.ask("That's wrong", "plain", fallback=False, conversation_id="c")
    r = await m.ask("No, it's 5512", "plain", fallback=False, conversation_id="c")
    check("the right value is filed", r.get("filed") and m.store.active_by_key("the shed padlock|code")["body"]
          .endswith("5512."), r)
    check("... without the 'No, it's'", "No," not in m.store.active_by_key("the shed padlock|code")["body"],
          m.store.active_by_key("the shed padlock|code")["body"])
    cases = Cases(m.store).all()
    check("a case is kept", len(cases) == 1 and cases[0]["right"].endswith("5512.") and "3381" in cases[0]["wrong"], cases)
    run = await m.run_test_now("corrections", "brain")
    check("corrections suite passes now", run["status"] == "done" and run["summary"]["accuracy"] == 1.0, run["summary"])
    m.store.remember(text="The shed padlock code is 3381.", key="the shed padlock|code", kind="fact")
    run = await m.run_test_now("corrections", "brain")
    check("... and catches the mistake coming back", run["summary"]["accuracy"] == 0.0, run["details"])
    check("case check by values", case_ok({"question": "code?", "right": "It is 5512.", "wrong": "3381"}, "The code is 5512")
          and not case_ok({"question": "code?", "right": "5512", "wrong": "3381"}, "3381 or 5512"))
    r = await m.ask("What's the weather like?", "plain", fallback=False, conversation_id="c")
    check("a question after it is just a question", "filed" not in r)
    # backing out of a correction files nothing
    d2 = tempfile.mkdtemp()
    m2 = M.Mind(d2, Models(plain=Fake("The garage code is 4417.")))
    m2.store.remember(text="The garage code is 4417.", key="garage|code", kind="fact")
    for reply in ("never mind", "ok", "Nevermind.", "thanks!"):
        await m2.ask("What's the garage code?", "plain", fallback=False, conversation_id=reply)
        await m2.ask("That's wrong", "plain", fallback=False, conversation_id=reply)
        r = await m2.ask(reply, "plain", fallback=False, conversation_id=reply)
        check(f"'{reply}' backs out", r.get("cancelled") and "filed" not in r and
              m2.store.active_by_key("garage|code")["body"] == "The garage code is 4417.", r)
    await m2.ask("What's the garage code?", "plain", fallback=False, conversation_id="shed")
    await m2.ask("That's wrong", "plain", fallback=False, conversation_id="shed")
    await m2.ask("it's 9090", "plain", fallback=False, conversation_id="shed")
    check("a bare value says what it's the value of", "9090" in m2.store.active_by_key("garage|code")["body"] and
          "garage" in m2.store.active_by_key("garage|code")["body"].lower(), m2.store.active_by_key("garage|code"))

    # ---- duplicates: flagged, never merged ---------------------------------------------------------------------
    st2 = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    x = st2.remember(text="The guest wifi password is sunflower88.")
    y = st2.remember(text="the guest wifi password is sunflower88")
    st2.remember(text="The guest wifi password is sunflower89.")
    pairs = tidy.run(st2)
    check("duplicate pair flagged", pairs == [(x["id"], y["id"])], pairs)
    from homemind.morning import _short
    line = tidy.summary_line(st2, _short)
    check("summary line hides the secret", line and "sunflower88" not in line and f"#{x['id']}" in line, line)
    check("nothing merged", st2.get(y["id"])["status"] == "active")

    print("ABILITIES OK" if not FAILS else f"ABILITIES FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
