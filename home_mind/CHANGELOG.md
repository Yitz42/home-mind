# Changelog

## 0.4.3 — 2026-11-14
- Days and months ("Tuesday", "Oct") are no longer taken for names, so the name filter works on questions about a
  day (it was letting other lockers through).
- For a question about a day, the version true then is shown first under "TRUE ON <day>" (qwen 4b took the newest
  value when the right one was only marked in the replaced list).
- hard test with 0.4.2, fresh world: brain alone 100%, Haiku + brain 100% (whole log 48%), qwen 4b + brain 96%
  (whole log 30%).

## 0.4.2 — 2026-11-14
- Versions of a fact are grouped by the name plus the word right after it ("Bexbel locker", "Nimsk fern"), so
  differently worded updates stay together and the same name on another thing stays apart. Brain only, held-out:
  recall 99.7% -> 100%, 20 changing facts 97% -> 100%.
- Models see version counts ("replaced by #N; 2 changes ago"), and for questions about a day ("yesterday",
  "last Tuesday", "3 days ago") the version current then is marked.
- An older note with two details ("in the garage and its code is 4417") is shown as partly updated, not replaced,
  when a newer note changes only one of them.
- Typo repair no longer changes ordinary words whose start is known ("changes" was becoming "changed").
- Pronoun linking: a sentence starting with "It" links only if it carries an update ("It rained" no longer links).
- Speed at 50,000 notes: questions 13-41 ms; a typo question after a new note 639 ms -> 19 ms (the word list is
  updated as notes arrive and built at start-up).
- New test suite "hard": yesterday, a past day, when it changed, 15 versions, partial updates, found by a detail,
  a long gap among ~300 notes. Brain only, held-out: 77% -> 99%. tests/scale.py measures speed and accuracy at N notes.

## 0.4.1 — 2026-11-14
- Models see disagreements spelled out: a DISAGREEMENT line and "(disagrees with #N)" on each note, when different
  people said different things about one thing (qwen 4b gave only one value with both notes in front of it).
- Models are told how memory spells a name the brain matched: "(in memory this is spelled: ...)" (qwen 4b refused
  "Cathy" when the note said "Kathy").
- tricky2 with 0.4.0, fresh world: brain alone 100%, Haiku + brain 100%, qwen 4b + brain 90%.

## 0.4.0 — 2026-11-14
- Sound-alike names: a name in a question that isn't stored is matched to a stored name one or two letters away,
  or else one that sounds the same (Double Metaphone): "Shawn" -> "Sean", "Covoc" -> "Kovok", "Jon" -> "John".
  Names are only repaired to stored names ("Jon" no longer becomes "Join").
- Numbers: thousands separators in questions are ignored ("4,417" finds "4417").
- Two facts combined: for "whoever runs X" / "the person who..." questions, the name in the best first result is
  searched too, and that result is placed second and kept for the model.
- Conflicting people: notes attributed to different people ("Marisol says...", "Tobiah told me...") are never
  treated as versions of each other; the model is told to give both and who said each.
- Models get TODAY's date, and note dates show the weekday, so "until Friday" can be judged.
- New test suite "tricky2": sound-alikes, sound-alike pairs, temporary facts, most recent, conflicting people,
  number formats, two facts combined. Brain only, held-out: 78% -> 100%; "tricky" still 100%; other suites unchanged.

## 0.3.6 — 2026-11-14
- Answer instructions: if the newest note says a thing was removed or is gone, say so instead of an old value.
- Typo repair also handles short capitalized names ("Lsk" -> "Losk").
- Tricky test: qwen 4b + brain 82% -> 97.7% on two worlds with 0.3.5; brain alone 100% on held-out worlds.

## 0.3.5 — 2026-11-14
- Typos in questions: a word found nowhere in memory is matched to the stored word one letter away (two for long
  words), keeping capitals so names stay names.
- Pronoun updates: a note that says "its"/"their", or starts a sentence with "It"/"There", and names nothing is
  linked to the note just before it (within 2 hours): "Changed its code to 8812 [re: Went to check on the Kosk
  locker today]". "Finish it with lemon" is not linked.
- Tricky test, brain only, held-out: 83% -> 99.9% (typos 19% -> 99%, pronoun updates 0% -> 100%); other suites
  unchanged.
- Tricky test: a mention and its pronoun update are now said together (they were scattered among other notes).

## 0.3.4 — 2026-11-14
- New test suite "tricky": look-alike names, shared names, no names, pronoun updates ("Changed its code to..."),
  change back, removed things, history ("what was it before?"), never-mentioned things, negation, typos in the
  question, and chatter. Results broken down per type (summary.by_type).

## 0.3.3 — 2026-11-14
- What models see: when the question names something and some found facts contain every name, other facts are
  left out (notes and ideas are kept). Found with qwen 4b, which answered with the "Putan" toolbox's room when
  asked about the "Pubel" toolbox although the brain had the right note first.
- Test fix: the locate suite only asks "the idea X gave me" when the note names X (it used to ask regardless, which
  made some questions unanswerable). Locate numbers before 0.3.3 aren't comparable.
- With 0.3.2 on 60 changing facts: qwen 9b 100% on two worlds (was 93%); qwen 4b 100% and 98.3% (was 92%).

## 0.3.2 — 2026-11-14
- Recall: a note containing every name in the question (capitalized words not starting it) ranks 1.5x higher.
- Versions of a fact are anchored on a name in the question when there is one (a rare word inside a value, like
  "laundry room", no longer becomes the anchor), and rare-word counts use whole words only ("log" no longer
  matches "Logor").
- Models see current notes first and older versions in a separate REPLACED section. Found with qwen 4b/9b: in
  most of their misses the brain had the newest note first and the model picked an older one anyway.
- Brain-only, held-out seeds: recall 95% -> 99.7%; changing facts 77% -> 97% (20), 94% -> 99.1% (60),
  98.5% -> 99.3% (150); finding notes 89.5% -> 87.8%.

## 0.3.1 — 2026-11-14
- Comfort: the first press of a never-pressed vote button (state "unknown") now counts as a vote.

## 0.3.0 — 2026-11-14
- Bedroom comfort brain (comfort.py). Votes (cold / hot / ok) from input_buttons pressed by Jeeves, the watch or
  a dashboard are stored with a snapshot: room temperature and humidity, target, heater/fan state, situation
  (night / morning / day), Steady watch numbers (HRV, resting HR, sleep, readiness, steps) and scale numbers.
- Learns a preferred temperature per situation (exact convex fit of the votes, weak pull to the old fixed values).
- Tests each body/room signal by fitting it together with the preferences and checking held-out votes (5-fold);
  used only when confirmed. Simulated person, 20 held-out seeds: uncomfortable moments 42% -> 17% vs fixed settings;
  real sleep effect found 9 of 11 times; 1 false signal in 200 tests.
- Control only through HA variables (input_number.mind_target_temp, mind_fan_speed, mind_humidity_target,
  sleep_temperature, wake_temperature), clamped to limit variables; input_boolean.mind_comfort_enabled is the
  switch and breaking point (also turned off automatically if the room is >3°F outside the limits for 30 min).
- API /api/comfort, /api/comfort/vote; MCP tool mind_comfort; sensor.home_mind_comfort.

## 0.2.2 — 2026-11-14
- Recall: versions of one fact are found by the query's most distinctive word (usually a name) plus at least 60%
  overlap with the best match's other matched words, instead of an exact match on rare words. Brain-only, held-out
  seeds: 60 changing facts 82% -> 94%, 150 changing facts 98.5%; 20 facts and recall unchanged.
- Test scoring: an answer counts as stale only when it gives an old value instead of the new one (mentioning the
  old value as replaced is right).

## 0.2.1 — 2026-11-14
- Newest-first grouping applies only to facts (kind "fact"); notes and ideas add up and are never marked as older.
  Found with Haiku: on 120 notes it was told separate ideas were "replaced" and answered 77% vs the brain's 87%.
- Older-fact marker now reads "(maybe an older version of #N)"; answer instructions say only different values for
  the same thing replace each other.
- When a model files a note it also chooses its kind (fact or note).
- Answer instructions: notes may use other words than the question for the same thing.
- Locate scoring accepts an answer that gives the note's id or all of its title words. "Shelf found" is not
  reported when the model filed the notes (its shelf names are its own).

## 0.2.0 — 2026-11-14
- Recall: results that match a query on the same words (with at least one rare word, ignoring words in over 25% of
  notes) are treated as notes about the same thing; the newest goes first and older ones are marked with the newer
  id. Brain-only accuracy on changing facts without keys, held-out seeds: 44% -> 76% (20 subjects), 47% -> 82%
  (60 subjects); recall, locate and tasks unchanged or slightly up.
- Model help with search (expand): in ask and in model+brain tests the model can suggest other words for the same
  things; both result lists are fused by rank.
- Models see "(older note: #N is newer on the same thing)" next to older notes.
- Tasks test: any open task tied on due day and priority counts as a right answer.
- tests/bench.py: brain-only benchmark over tuning or held-out seeds.

## 0.1.1 — 2026-11-14
- Bridge: Home Assistant webhooks accept GET/HEAD/POST/PUT only; dropped DELETE so the bridge sets up.

## 0.1.0 — 2026-11-14
- First version: memory store (versions, shelves, full-text search, fading), model connections (HA AI Task,
  HA conversation, Ollama, OpenAI-compatible, Anthropic), MCP endpoint via the Home Mind Bridge integration,
  knowledge packs with sha256-checked updates, five test suites with four arms, HA sensors for results.
