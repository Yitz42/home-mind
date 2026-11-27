"""Extra search keys per note (#71, LongMemEval's fact-augmented key expansion): words a question might use that the
note doesn't. Kept in a hidden column the search index reads (items.keys), never shown or sent to a model.

  lexicon  built in, no model: a note about a "beagle" also files under dog, pet, animal, breed; "Civic" under car;
           "dentist" under doctor, appointment, health. Each line below is "broader words: narrower words".
  model    overnight, the big model reads recent notes and suggests other words for the same things (EXTRA_SYSTEM);
           only plain words, at most 12 per note.
"""
import json
import re

LEXICON = """
dog pet animal breed: beagle labrador retriever poodle terrier bulldog dachshund husky corgi spaniel shepherd collie pug chihuahua greyhound boxer rottweiler doodle puppy pup
cat pet animal breed: siamese persian tabby kitten maine coon ragdoll calico
pet animal: hamster rabbit bunny parrot budgie goldfish fish turtle tortoise guinea gecko lizard snake
car vehicle: civic corolla camry accord prius tesla jeep sedan suv hatchback minivan truck pickup outback rav4 crv f150 mustang
bike bicycle: ebike e-bike scooter
doctor appointment health medical: dentist pediatrician dermatologist cardiologist optometrist physio physiotherapist therapist chiropractor orthodontist gp physician surgeon vet veterinarian
medicine health medication: ibuprofen tylenol acetaminophen advil aspirin antibiotic inhaler insulin prescription pills vitamin
allergy health: allergic peanut gluten lactose penicillin shellfish pollen
appliance: dishwasher washer dryer fridge refrigerator freezer oven stove microwave furnace boiler heater humidifier dehumidifier kettle toaster blender vacuum
food meal: pasta pizza salad soup curry sushi tacos burrito sandwich steak chicken salmon rice noodles lasagna risotto ramen
drink beverage: coffee tea espresso latte wine beer juice smoothie soda
exercise workout fitness: running jogging yoga pilates swimming cycling gym lifting hiking marathon 5k 10k crossfit
hobby: guitar piano violin knitting painting drawing gardening baking photography chess pottery
job work career: manager engineer nurse teacher developer accountant lawyer designer analyst consultant promotion salary
school education: class course degree university college semester exam homework tuition
money finance: bank loan mortgage rent salary budget savings credit debit card bill invoice tax refund
trip travel vacation holiday: flight hotel airbnb cruise passport visa itinerary airport
family relative: mom mother dad father sister brother son daughter wife husband partner grandma grandpa grandmother grandfather aunt uncle cousin niece nephew
friend: buddy roommate neighbor neighbour coworker colleague
home house: apartment flat condo garage basement attic kitchen bedroom bathroom living room yard garden porch shed
code password secret: pin passcode combination wifi login
phone device: iphone android pixel galaxy tablet ipad laptop macbook
streaming show entertainment: netflix hulu disney hbo spotify podcast series episode movie film
book reading: novel author audiobook kindle chapter
plant garden: fern succulent cactus orchid basil tomato tomatoes rose roses tulip monstera pothos
music: concert band album song playlist
birthday celebration: anniversary party wedding
""".strip()

BROADER = {}
for line in LEXICON.splitlines():
    broad, narrow = line.split(":", 1)
    for w in narrow.split():
        BROADER.setdefault(w.lower(), [])
        for b in broad.split():
            if b not in BROADER[w.lower()]:
                BROADER[w.lower()].append(b)

WORD = re.compile(r"[a-z0-9][a-z0-9-]*")


def lexicon_keys(text):
    """Broader words for the narrower ones in a text, minus words already in it."""
    low = (text or "").lower()
    have = set(WORD.findall(low))
    out = []
    for w in have:
        for b in BROADER.get(w, ()) or BROADER.get(w.rstrip("s"), ()):
            if b not in have and b not in out:
                out.append(b)
    for two in re.findall(r"\b([a-z]+ [a-z]+)\b", low):          # "maine coon", "living room"
        for b in BROADER.get(two, ()):
            if b not in have and b not in out:
                out.append(b)
    return out


EXTRA_SYSTEM = (
    "For each numbered note, give other words someone might use when asking about it, which the note itself doesn't "
    "use: broader terms (beagle -> dog, pet), synonyms (sofa -> couch), the kind of thing (dentist -> appointment). "
    "Plain lowercase words only, at most 8 per note, no names or numbers from the note. Reply with JSON only: "
    '{"1": ["word", ...], "2": [...]}.')


def clean(words, text):
    have = set(WORD.findall((text or "").lower()))
    out = []
    for w in words or []:
        w = str(w).strip().lower()
        if re.fullmatch(r"[a-z][a-z -]{1,24}", w) and w not in have and w not in out:
            out.append(w)
    return out[:12]


async def model_keys(store, model, days=2, limit=40):
    """Overnight: the model suggests keys for recent notes that have only lexicon keys (or none)."""
    import time
    rows = store.db.execute(
        "SELECT id, title, body, keys, meta FROM items WHERE status='active' AND created > ? AND source NOT IN "
        "('home','calendar','ticktick','automations','habits','tidy','evening','morning','patterns') AND source NOT LIKE 'pack:%' "
        "AND kind != 'procedure' ORDER BY created DESC LIMIT ?", (time.time() - days * 86400, limit * 3)).fetchall()
    todo = [r for r in rows if not json.loads(r["meta"] or "{}").get("keys_by")][:limit]
    n = 0
    for i in range(0, len(todo), 10):
        batch = todo[i:i + 10]
        msg = "\n".join(f"{k + 1}. {r['title']}: {(r['body'] or '')[:300]}" for k, r in enumerate(batch))
        from .overnight import safe_for
        msg = safe_for(model, msg)          # a cloud model gets no secret values
        try:
            r = await model.chat(EXTRA_SYSTEM, [{"role": "user", "content": msg}], max_tokens=600, temperature=0)
            got = json.loads(re.search(r"\{.*\}", r.get("text") or "", re.S).group(0))
        except Exception:  # noqa: BLE001 - a bad reply skips the batch
            continue
        for k, row in enumerate(batch):
            extra = clean(got.get(str(k + 1)), (row["title"] or "") + " " + (row["body"] or ""))
            if extra:
                store.add_keys(row["id"], extra, by="model")
                n += 1
    return n
