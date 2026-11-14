"""Random test suites with hidden answers. Each suite makes a fresh, made-up world from a seed (invented names, so a
model can't know the answers from training), feeds it to a sandbox mind, then asks questions.

  recall   facts told once, asked later in different words
  update   facts that change one or more times; the newest must win (adapting to new information)
  locate   notes spread over shelves, found again from a vague description (finding what it organized)
  tasks    a task list that is added to, finished and rescheduled; questions about what is open and next
  scale    'locate' with many more items, to see where finding things starts to fail

A suite returns {"events": [...], "questions": [...]}; the runner plays the events into a sandbox and scores the
questions. Events that store something carry a "ref" so the right item can be checked by id.
"""
import random

SYL_A = ["ta", "ve", "ko", "mi", "dra", "sel", "or", "pu", "zen", "lo", "fi", "ran", "bex", "quo", "har", "nim", "ul", "cy"]
SYL_B = ["rin", "vok", "dal", "mere", "sk", "tan", "bel", "gor", "wyn", "lix", "dor", "pax", "ven", "rue", "mott", "sha"]
PEOPLE = ["Odell", "Marisol", "Tobiah", "Quinta", "Rasmus", "Ilka", "Fenwick", "Juno", "Calla", "Bertrand", "Nadia",
          "Ozzie", "Petra", "Ludo", "Wren", "Hollis", "Saoirse", "Emrys", "Delphine", "Kofi"]
ROOMS = ["garage", "attic", "basement", "hallway closet", "back porch", "laundry room", "guest room", "workshop",
         "pantry", "mudroom", "office", "shed"]
COLORS = ["teal", "maroon", "mustard", "lilac", "charcoal", "coral", "olive", "navy", "ivory", "rust", "mint", "plum"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SPOTS = ["under the blue pot", "on the hook by the door", "in the red tin", "behind the fuse box", "in the junk drawer",
         "taped under the bench", "inside the old boot", "on the top shelf", "in the glovebox", "under the doormat"]


def name(rng):
    return (rng.choice(SYL_A) + rng.choice(SYL_B)).capitalize()


# (subject maker, attribute, value maker, statement templates, question templates)
FACTS = [
    (lambda r, n: f"the {n} locker", "code", lambda r: str(r.randint(1000, 9999)),
     ["The code for {s} is {v}.", "{S} opens with {v}.", "Set {s} combination to {v}."],
     ["What is the code for {s}?", "What number opens {s}?", "What's the combination on {s}?"]),
    (lambda r, n: f"the {n} toolbox", "location", lambda r: r.choice(ROOMS),
     ["{S} is kept in the {v}.", "I moved {s} to the {v}.", "{S} lives in the {v} now."],
     ["Where is {s}?", "Which room holds {s}?", "Where did {s} end up?"]),
    (lambda r, n: f"the {n} bike", "color", lambda r: r.choice(COLORS),
     ["{S} is painted {v}.", "{S} is {v}.", "The frame of {s} is {v}."],
     ["What color is {s}?", "What paint is on {s}?", "Which color is the frame of {s}?"]),
    (lambda r, n: f"the {n} wifi", "password", lambda r: f"{r.choice(COLORS)}{r.randint(10, 99)}",
     ["The password for {s} is {v}.", "{S} password: {v}.", "To join {s} use {v}."],
     ["What is the password for {s}?", "How do I log in to {s}?", "What's the key for {s}?"]),
    (lambda r, n: f"the {n} fern", "watering day", lambda r: r.choice(DAYS),
     ["Water {s} every {v}.", "{S} gets watered on {v}.", "{S}'s watering day is {v}."],
     ["When should {s} be watered?", "Which day does {s} need water?", "What day is watering for {s}?"]),
    (lambda r, n: f"the {n} spare key", "hiding spot", lambda r: r.choice(SPOTS),
     ["{S} is {v}.", "I hid {s} {v}.", "Find {s} {v}."],
     ["Where is {s}?", "Where did I hide {s}?", "Where can I find {s}?"]),
    (lambda r, n: f"the {n} project", "lead", lambda r: r.choice(PEOPLE),
     ["{v} is leading {s}.", "{S} is run by {v}.", "The lead on {s} is {v}."],
     ["Who leads {s}?", "Who is in charge of {s}?", "Who runs {s}?"]),
]


def _cap(s):
    return s[0].upper() + s[1:]


def _fact_world(rng, n):
    used, out = set(), []
    while len(out) < n:
        f = rng.choice(FACTS)
        subj = f[0](rng, name(rng))
        if (subj, f[1]) in used:
            continue
        used.add((subj, f[1]))
        out.append({"f": f, "subj": subj, "attr": f[1]})
    return out


def _say(rng, f, subj, v):
    return rng.choice(f[3]).format(s=subj, S=_cap(subj), v=v)


def _ask(rng, f, subj):
    return rng.choice(f[4]).format(s=subj, S=_cap(subj))


def recall(seed, n=30):
    rng = random.Random(seed)
    events, questions = [], []
    for i, w in enumerate(_fact_world(rng, n)):
        v = w["f"][2](rng)
        events.append({"op": "remember", "ref": f"f{i}", "text": _say(rng, w["f"], w["subj"], v),
                       "key": f"{w['subj']}|{w['attr']}", "kind": "fact"})
        questions.append({"q": _ask(rng, w["f"], w["subj"]), "gold": v, "ref": f"f{i}", "stale": []})
    rng.shuffle(questions)
    return {"events": events, "questions": questions}


def update(seed, n=20, max_changes=3):
    """Each subject is told once, then changed 0..max_changes times, mixed in with the others. Only the newest
    value is right."""
    rng = random.Random(seed)
    world = _fact_world(rng, n)
    stream, final = [], {}
    for i, w in enumerate(world):
        vals = []
        for _ in range(1 + rng.randint(0, max_changes)):
            v = w["f"][2](rng)
            while v in vals:
                v = w["f"][2](rng)
            vals.append(v)
        final[i] = vals
        for j, v in enumerate(vals):
            stream.append((rng.random() + j, i, j, v))   # later versions tend to come later, mixed with others
    stream.sort()
    seen = {}
    events = []
    for _, i, j, v in sorted(stream, key=lambda x: (x[0], x[2])):
        j = seen.get(i, -1) + 1                           # keep each subject's versions in order
        seen[i] = j
        v = final[i][j]
        w = world[i]
        text = _say(rng, w["f"], w["subj"], v)
        if j:
            text = rng.choice(["Update: ", "Change: ", "Correction: ", "As of today, ", ""]) + text
        events.append({"op": "remember", "ref": f"f{i}v{j}", "text": text, "key": f"{w['subj']}|{w['attr']}",
                       "kind": "fact"})
    questions = []
    for i, w in enumerate(world):
        vals = final[i]
        questions.append({"q": _ask(rng, w["f"], w["subj"]), "gold": vals[-1], "ref": f"f{i}v{len(vals) - 1}",
                          "stale": vals[:-1], "stale_refs": [f"f{i}v{j}" for j in range(len(vals) - 1)],
                          "changes": len(vals) - 1})
    rng.shuffle(questions)
    return {"events": events, "questions": questions}


# shelves with their own words; queries sometimes use a different word for the same thing
TOPICS = {
    "kitchen/recipes": (["soup", "stew", "salad", "curry", "bread", "pie", "tacos", "risotto"],
                        ["lentil", "pumpkin", "chickpea", "mushroom", "leek", "fennel", "beet", "squash"],
                        ["lemon", "smoked paprika", "miso", "tahini", "sage", "chili crisp", "dill", "harissa"]),
    "workshop/builds": (["shelf", "bench", "planter", "birdhouse", "frame", "stool", "rack", "crate"],
                        ["oak", "pine", "walnut", "cedar", "birch", "plywood", "maple", "bamboo"],
                        ["dovetail", "pocket screws", "epoxy", "hinges", "casters", "dowels", "brackets", "stain"]),
    "garden/plans": (["bed", "trellis", "border", "pot", "row", "patch", "hedge", "pond"],
                     ["tomato", "basil", "garlic", "lavender", "kale", "pepper", "strawberry", "mint"],
                     ["mulch", "compost", "drip line", "netting", "lime", "shade cloth", "stakes", "gravel"]),
    "projects/electronics": (["sensor", "clock", "remote", "lamp", "charger", "speaker", "display", "logger"],
                             ["esp32", "arduino", "pebble", "zigbee", "lora", "raspberry", "solar", "usb"],
                             ["battery", "enclosure", "relay", "antenna", "button", "buzzer", "e-ink", "magnet"]),
    "health/routines": (["stretch", "walk", "nap", "meal", "workout", "breathing", "journal", "bedtime"],
                        ["morning", "lunch", "evening", "weekend", "commute", "rainy-day", "travel", "desk"],
                        ["timer", "playlist", "water bottle", "notebook", "alarm", "foam roller", "tea", "app"]),
    "people/gifts": (["gift", "card", "visit", "call", "dinner", "trip", "surprise", "letter"],
                     ["birthday", "anniversary", "holiday", "graduation", "housewarming", "farewell", "thank-you", "spring"],
                     ["scarf", "puzzle", "plant", "book", "candle", "mug", "photo", "tickets"]),
}
SYN = {"soup": "broth", "stew": "casserole", "bench": "workbench", "shelf": "shelving", "bed": "raised bed",
       "lamp": "light", "walk": "stroll", "gift": "present", "call": "phone call", "bread": "loaf",
       "planter": "flower box", "sensor": "detector", "clock": "timepiece", "nap": "rest", "trip": "outing",
       "pot": "container", "speaker": "audio box", "crate": "box", "meal": "food"}


def locate(seed, n=40, synonym_share=0.3):
    rng = random.Random(seed)
    events, questions, seen = [], [], set()
    shelves = list(TOPICS)
    i = 0
    while i < n:
        shelf = rng.choice(shelves)
        heads, mids, extras = TOPICS[shelf]
        h, m, x = rng.choice(heads), rng.choice(mids), rng.choice(extras)
        if (shelf, h, m, x) in seen:
            continue
        seen.add((shelf, h, m, x))
        who = rng.choice(PEOPLE)
        title = f"{_cap(m)} {h} with {x}"
        k = rng.randrange(3)
        body = [f"Idea for a {m} {h}; finish it with {x}. {who} suggested it.",
                f"Try a {m} {h} using {x}, maybe on {rng.choice(DAYS)}.",
                f"Note: {m} {h}, {x} is the key part. Ask {who}."][k]
        events.append({"op": "remember", "ref": f"n{i}", "title": title, "text": body, "shelf": shelf, "kind": "note"})
        hq = SYN[h] if h in SYN and rng.random() < synonym_share else h
        asks = [f"Find my note about the {hq} with {x}.", f"What was that {m} {hq} idea?",
                f"That {hq} thing with {x} — where is it?"]
        if k != 1:      # only ask by person when the note names them (fixed 0.3.3: it used to ask regardless)
            asks.append(f"The {hq} idea {who} gave me")
        q = rng.choice(asks)
        questions.append({"q": q, "gold": title, "ref": f"n{i}", "shelf": shelf, "stale": [],
                          "synonym": hq != h})
        i += 1
    rng.shuffle(questions)
    return {"events": events, "questions": questions[:max(10, n // 2)]}


def scale(seed, n=400):
    return locate(seed, n=n)


def tasks(seed, n=18):
    """Days are numbers from day 0 (a Monday). A task's state can change after it is added."""
    rng = random.Random(seed)
    projects = rng.sample(["Pebble", "Garden", "School", "House", "LoRa", "Car"], 3)
    verbs = ["Order", "Fix", "Write", "Call about", "Test", "Clean", "Plan", "Measure", "Paint", "Return"]
    things = ["the gasket", "the battery pack", "lab report", "the gutter", "the antenna", "seed trays",
              "the watchface", "the invoice", "the trim", "the charger", "the manual", "the hose", "the bracket",
              "the slides", "the filter", "the cable", "the tiles", "the receipt", "the hinge", "the screen"]
    rng.shuffle(things)
    state, events = {}, []
    for i in range(n):
        p = rng.choice(projects)
        title = f"{rng.choice(verbs)} {things[i % len(things)]}" + (f" ({i})" if i >= len(things) else "")
        due, pr = rng.randint(0, 13), rng.randint(1, 3)
        state[f"t{i}"] = {"title": title, "project": p, "due": due, "priority": pr, "done": False}
        events.append({"op": "task_add", "ref": f"t{i}", "title": title, "project": p, "due": due, "priority": pr,
                       "text": f"Task: {title} for {p}, due day {due}, priority {pr}."})
    refs = list(state)
    for r in rng.sample(refs, max(1, n // 4)):
        state[r]["done"] = True
        events.append({"op": "task_done", "ref": r, "text": f"Done: {state[r]['title']}."})
    for r in rng.sample([x for x in refs if not state[x]["done"]], max(1, n // 5)):
        nd = rng.randint(0, 13)
        while nd == state[r]["due"]:
            nd = rng.randint(0, 13)
        state[r]["due"] = nd
        events.append({"op": "task_move", "ref": r, "due": nd,
                       "text": f"Moved: {state[r]['title']} is now due day {nd}."})

    def open_in(p=None):
        xs = [x for x in state.values() if not x["done"] and (p is None or x["project"] == p)]
        return sorted(xs, key=lambda x: (x["due"], -x["priority"], x["title"]))

    def _tied(xs):
        # other open tasks that tie with the first on due day and priority: just as right an answer
        return [x["title"] for x in xs[1:] if (x["due"], x["priority"]) == (xs[0]["due"], xs[0]["priority"])]

    questions = []
    for p in projects:
        xs = open_in(p)
        if xs:
            questions.append({"q": f"What is the next open task (soonest due) for {p}?", "gold": xs[0]["title"],
                              "params": {"type": "next", "project": p}, "stale": [], "also": _tied(xs)})
        questions.append({"q": f"How many open tasks are there for {p}?", "gold": str(len(xs)),
                          "params": {"type": "count", "project": p}, "stale": []})
    xs = open_in()
    if xs:
        questions.append({"q": "Which open task is due soonest overall?", "gold": xs[0]["title"],
                          "params": {"type": "next"}, "stale": [], "also": _tied(xs)})
    d = rng.randint(3, 9)
    due = [x["title"] for x in xs if x["due"] < d]
    questions.append({"q": f"How many open tasks are due before day {d}?", "gold": str(len(due)),
                      "params": {"type": "count", "due_before": d}, "stale": []})
    return {"events": events, "questions": questions}


# ---- tricky: things meant to trip it up -------------------------------------------------------------------------------
PLAIN = [("the garage door", "code", lambda r: str(r.randint(1000, 9999))),
         ("the back gate", "code", lambda r: str(r.randint(1000, 9999))),
         ("the shed padlock", "combination", lambda r: str(r.randint(100, 999))),
         ("the guest wifi", "password", lambda r: f"{r.choice(COLORS)}{r.randint(10, 99)}"),
         ("the office safe", "code", lambda r: str(r.randint(1000, 9999))),
         ("the storage unit", "code", lambda r: str(r.randint(1000, 9999))),
         ("the mailbox key", "spot", lambda r: r.choice(SPOTS)),
         ("the spare car key", "spot", lambda r: r.choice(SPOTS))]
CHATTER = ["Had leftover pasta for lunch.", "It rained most of the afternoon.", "Watched half a movie and fell asleep.",
           "Need to call the dentist back sometime.", "The neighbor's dog barked all morning.", "Coffee tasted burnt today.",
           "Ran out of printer paper.", "Traffic was bad on the way home.", "Started a new book about bridges.",
           "Forgot my umbrella at the library.", "The kitchen light flickers when the fridge kicks on.",
           "Thinking about repainting the hallway.", "Got a package with the wrong size shoes.",
           "Class ran late so I skipped the gym.", "Fixed the squeaky door hinge with some oil."]
UNKNOWN_WORDS = ("don't know", "do not know", "not sure", "no record", "no information", "not in", "doesn't say",
                 "does not say", "no longer", "removed", "gone", "unknown", "can't find", "cannot find", "no note",
                 "not mentioned", "isn't mentioned", "none")


def twin(name, rng, used):
    """A look-alike name: one vowel changed."""
    vowels = "aeiouy"
    for _ in range(30):
        i = rng.choice([k for k, c in enumerate(name) if c.lower() in vowels] or [1])
        c = rng.choice([v for v in vowels if v != name[i].lower()])
        t = name[:i] + (c.upper() if i == 0 else c) + name[i + 1:]
        if t not in used:
            return t
    return name + "a"


def typo(name, rng):
    i = rng.randrange(1, len(name) - 1)
    return name[:i] + name[i + 1:]


def tricky(seed, n=4):
    """Each trip-up type n times, plus chatter. Questions carry a 'type' so results break down per type.
    Types: plain (control), confusable, shared_name, no_name, pronoun, revert, removed, history, unknown, negation, typo."""
    rng = random.Random(seed)
    threads, questions, used = [], [], set()

    def fresh():
        while True:
            nm = name(rng)
            if nm not in used:
                used.add(nm)
                return nm

    def facts(tpl_filter=None):
        fs = [f for f in FACTS if not tpl_filter or tpl_filter(f)]
        return rng.choice(fs)

    def vals(f, k):
        out = []
        while len(out) < k:
            v = f[2](rng)
            if v not in out:
                out.append(v)
        return out

    def say(f, subj, v, pre=""):
        return pre + _say(rng, f, subj, v)

    for i in range(n):
        # plain change (control)
        f = facts(); subj = f[0](rng, fresh()); a, b = vals(f, 2)
        threads.append([("plain", say(f, subj, a), None), ("plain", say(f, subj, b, "Update: "), f"plain{i}")])
        questions.append({"type": "plain", "q": _ask(rng, f, subj), "gold": b, "ref": f"plain{i}", "stale": [a]})
        # look-alike names
        f = facts(); nm = fresh(); tw = twin(nm, rng, used); used.add(tw); a, b = vals(f, 2)
        threads.append([("confusable", say(f, f[0](rng, nm), a), f"conf{i}")])
        threads.append([("confusable", say(f, f[0](rng, tw), b), None)])
        questions.append({"type": "confusable", "q": _ask(rng, f, f[0](rng, nm)), "gold": a, "ref": f"conf{i}", "stale": [b]})
        # same name, different thing
        nm = fresh(); f1 = facts(); f2 = facts(lambda f: f[1] != f1[1])
        a = vals(f1, 1)[0]; b = vals(f2, 1)[0]
        threads.append([("shared_name", say(f1, f1[0](rng, nm), a), f"shared{i}")])
        threads.append([("shared_name", say(f2, f2[0](rng, nm), b), None)])
        questions.append({"type": "shared_name", "q": _ask(rng, f1, f1[0](rng, nm)), "gold": a, "ref": f"shared{i}", "stale": [b]})
        # no names: everyday things
        subj, attr, mk = PLAIN[(seed + i) % len(PLAIN)]
        a, b = mk(rng), mk(rng)
        while b == a:
            b = mk(rng)
        threads.append([("no_name", f"The {attr} for {subj} is {a}." if attr != "spot" else f"{_cap(subj)} is {a}.", None),
                        ("no_name", (f"Changed the {attr} for {subj} to {b}." if attr != "spot" else f"Moved {subj}: it's {b} now."), f"noname{i}")])
        questions.append({"type": "no_name", "q": (f"What's the {attr} for {subj}?" if attr != "spot" else f"Where is {subj}?"),
                          "gold": b, "ref": f"noname{i}", "stale": [a]})
        # pronoun update: the update doesn't repeat the name
        f = facts(lambda f: f[1] in ("code", "password")); subj = f[0](rng, fresh()); a, b = vals(f, 2)
        threads.append([("pronoun", say(f, subj, a), None), ("pronoun", f"Went to check on {subj} today.", None),
                        ("pronoun", f"Changed its {f[1]} to {b}.", f"pron{i}")])
        questions.append({"type": "pronoun", "q": _ask(rng, f, subj), "gold": b, "ref": f"pron{i}", "stale": [a]})
        # change back: A -> B -> A
        f = facts(); subj = f[0](rng, fresh()); a, b = vals(f, 2)
        threads.append([("revert", say(f, subj, a), None), ("revert", say(f, subj, b, "Change: "), None),
                        ("revert", say(f, subj, a, "Changed back: "), f"rev{i}")])
        questions.append({"type": "revert", "q": _ask(rng, f, subj), "gold": a, "ref": f"rev{i}", "stale": [b]})
        # removed
        f = facts(lambda f: f[1] in ("code", "password", "location", "hiding spot")); subj = f[0](rng, fresh())
        a = vals(f, 1)[0]
        threads.append([("removed", say(f, subj, a), None),
                        ("removed", f"{_cap(subj)} is gone now. It was removed, so forget its {f[1]}.", f"rem{i}")])
        questions.append({"type": "removed", "q": _ask(rng, f, subj), "gold": None, "unknown": True, "ref": f"rem{i}",
                          "stale": [a]})
        # history: what was it before?
        f = facts(); subj = f[0](rng, fresh()); a, b = vals(f, 2)
        threads.append([("history", say(f, subj, a), f"hist{i}"), ("history", say(f, subj, b, "Update: "), None)])
        questions.append({"type": "history", "q": f"What was the {f[1]} of {subj} before it last changed?", "gold": a,
                          "ref": f"hist{i}", "stale": [b]})
        # never mentioned
        f = facts(); nm = fresh(); subj = f[0](rng, nm)
        questions.append({"type": "unknown", "q": _ask(rng, f, subj), "gold": None, "unknown": True, "name": nm,
                          "stale": []})
        # negation
        nm = fresh()
        if rng.random() < 0.5:
            subj = f"the {nm} toolbox"; a, b = rng.sample(ROOMS, 2)
            neg = f"{_cap(subj)} is not in the {a} anymore; it's in the {b}."
        else:
            subj = f"the {nm} spare key"; a, b = rng.sample(SPOTS, 2)
            neg = f"{_cap(subj)} is no longer {a}. Now it's {b}."
        first = f"{_cap(subj)} is in the {a}." if "toolbox" in subj else f"{_cap(subj)} is {a}."
        threads.append([("negation", first, None), ("negation", neg, f"neg{i}")])
        questions.append({"type": "negation", "q": f"Where is {subj}?", "gold": b, "ref": f"neg{i}", "stale": [a]})
        # typo in the question
        f = facts(); nm = fresh(); subj = f[0](rng, nm); a, b = vals(f, 2)
        threads.append([("typo", say(f, subj, a), None), ("typo", say(f, subj, b, "Update: "), f"typo{i}")])
        questions.append({"type": "typo", "q": _ask(rng, f, f[0](rng, typo(nm, rng))), "gold": b, "ref": f"typo{i}",
                          "stale": [a]})
    # chatter: about as many lines as there are facts
    nfacts = sum(len(t) for t in threads)
    threads += [[("noise", rng.choice(CHATTER), None)] for _ in range(nfacts)]
    # interleave threads, keeping each thread's order
    events, live = [], [list(t) for t in threads]
    k = 0
    while live:
        t = rng.choice(live)
        typ, text, ref = t.pop(0)
        events.append({"op": "remember", "text": text, "kind": "note" if typ == "noise" else "fact",
                       "ref": ref or f"x{k}"})
        k += 1
        if not t:
            live.remove(t)
    rng.shuffle(questions)
    return {"events": events, "questions": questions}


SUITES = {"recall": recall, "update": update, "locate": locate, "tasks": tasks, "scale": scale, "tricky": tricky}
SUITE_INFO = {
    "recall": "facts told once, asked later in other words",
    "update": "facts that change; the newest must win",
    "locate": "notes on shelves found from a vague description",
    "tasks": "a changing task list; what is open, next, how many",
    "scale": "locate with ~400 notes",
    "tricky": "trip-ups: look-alike names, shared names, no names, pronoun updates, change back, removed, history, "
              "never mentioned, negation, typos, chatter; scored per type",
}
