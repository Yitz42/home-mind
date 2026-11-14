# Changelog

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
