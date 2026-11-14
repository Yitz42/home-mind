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
        body = rng.choice([f"Idea for a {m} {h}; finish it with {x}. {who} suggested it.",
                           f"Try a {m} {h} using {x}, maybe on {rng.choice(DAYS)}.",
                           f"Note: {m} {h}, {x} is the key part. Ask {who}."])
        events.append({"op": "remember", "ref": f"n{i}", "title": title, "text": body, "shelf": shelf, "kind": "note"})
        hq = SYN[h] if h in SYN and rng.random() < synonym_share else h
        q = rng.choice([f"Find my note about the {hq} with {x}.", f"What was that {m} {hq} idea?",
                        f"That {hq} thing with {x} — where is it?", f"The {hq} idea {who} gave me"])
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


SUITES = {"recall": recall, "update": update, "locate": locate, "tasks": tasks, "scale": scale}
SUITE_INFO = {
    "recall": "facts told once, asked later in other words",
    "update": "facts that change; the newest must win",
    "locate": "notes on shelves found from a vague description",
    "tasks": "a changing task list; what is open, next, how many",
    "scale": "locate with ~400 notes",
}
