# Home Mind

A long-term memory and test bench for models, living on your Home Assistant. Small models on other computers,
Claude (Haiku through Home Assistant, or the API), and Claude chats can all use the same memory.

## What it holds

- **Items**: facts, notes, tasks, procedures and episodes. Each sits on a **shelf** (a path like
  `projects/pebble`) with tags. Changing an item writes a new version; old versions are kept as history.
- **Keys**: a fact can carry a key `subject|attribute` (for example `garage door|code`). A new value for the same
  key replaces the old one, which is how the mind adapts to new information.
- **Events**: every store, search, answer and test, with the time.
- **Packs**: procedures written by a large model, downloaded from the update manifest (sha256-checked).

Everything is in one SQLite file in the app's data folder, included in Home Assistant backups.

## Setup

1. Install and start the app. It adds the **Home Mind Bridge** integration to `custom_components` and tells you
   in a notification.
2. Restart Home Assistant, then add **Home Mind Bridge** under Settings › Devices & services.
3. A notification shows the address. In Claude: Settings › Connectors › Add custom connector, and paste the
   **Remote** address. Treat it like a password.

## Controls in Home Assistant

The bridge adds a **Home Mind** device (Settings › Devices & services › Home Mind Bridge) whose entities control
the app from any dashboard, automation or voice command:

| kind | entities |
|---|---|
| switches | Bedroom comfort brain, Watch the house, Overnight learning, Morning summary, Brain-fog check-ins, Install new packs automatically, Repeat the best note before the question, Name the notes before answering, Quiet hours for phone notifications, Ask about old facts in the morning |
| numbers | Voice time limit (s), Fade unused notes after (days), Brain tests every (hours), Phone notifications a day, Keep the detailed activity log for (days), Ask about facts older than (days) |
| times | Morning summary time, Overnight learning time, Quiet hours start, Quiet hours end |
| buttons | Run overnight learning now, Send the morning summary, Sync calendars, Check models, Install pack updates, Run a comfort cycle, Refresh the daily record, Run brain tests, Hand automations back to Home Assistant |
| sensors | Memory items, Conversation turns to learn from, Last overnight run, Facts filed last night, Automations run by Home Mind, Model calls today, Model tokens today, Model cost today (priced models), Phone notifications waiting, Version |

A change made from Home Assistant is kept across restarts. If you later change the same option on the
Configuration tab, the Configuration tab wins and the Home Assistant change is dropped. Switches and numbers show
`changed_here` (and the Configuration tab's value) as attributes. Buttons start their job in the background and
show its last result (`last_run`, `last_ok`, `last_result` or `last_error`); a second press while it runs is
ignored. Turning Overnight learning or Morning summary off skips the scheduled run; the buttons still work.

If the app isn't running when Home Assistant starts, the entities appear on the first successful check (every
30 seconds). The same controls are at `GET /api/control`, `POST /api/control/set {"key", "value"}`,
`POST /api/control/reset {"key"?}` and `POST /api/control/run/<job>`.

## Models

Configured on the Configuration tab. Types:

| type | for | fields |
|---|---|---|
| `ha_ai_task` | an AI Task entity in HA (your Haiku) | `entity_id` |
| `ha_conversation` | a conversation agent in HA | `entity_id` |
| `ollama` | Ollama on another computer | `url` (e.g. `http://192.168.1.50:11434`), `model` |
| `openai` | anything OpenAI-compatible (LM Studio, llama.cpp, vLLM) | `url`, `model`, `api_key?` |
| `anthropic` | Claude API directly | `api_key`, `model` |
| `mock` | no model; copies the best-matching note (a floor for tests) | — |

For Ollama on Windows, set the environment variable `OLLAMA_HOST=0.0.0.0` and restart Ollama, so other
computers can reach it, and allow port 11434 through the firewall.

## Tests

Each suite builds a made-up world from a seed (invented names, so no model knows the answers) in a separate
sandbox database, then asks questions with hidden answers.

| suite | measures |
|---|---|
| recall | facts told once, asked later in other words |
| update | facts that change 0–3 times: does the newest win? (adapting) |
| locate | notes on shelves found from a vague description (finding what it organized) |
| tasks | a task list that changes: next task, how many open |
| scale | locate with ~400 notes |
| abstain | questions whose answer was never filed, mixed with answerable ones: is "nothing close" flagged only for the missing ones? |

Arms: **brain** (no model: the top search result is the answer), **model+brain** (model gets what the brain
found), **model+context** (model reads the whole log, no brain), **model** (alone). Ingest **model** lets the
model choose title, shelf and key for each item itself, so `locate` then measures how well it organized.

Numbers recorded: accuracy, found first (hit@1), in top 5, MRR, how often an old version ranked above the newest
(stale on top), shelf found, time per answer. Results are kept for trends and pushed to Home Assistant as
`sensor.home_mind_test_<suite>` and `sensor.home_mind_items`.

Model+brain runs also report **source** (a right answer points to the note it came from). Runs take
`"order": "edges"` and `"notes_first": true` to try the answer options; `tests/compare_answers.py` runs all four
combinations on your own model and prints them side by side.

## Read from your notes

Nothing to set up: these are read from plain sentences as they are filed (no model needed), and worked out when a
question needs them. Models see the results under **WORKED OUT** at the top of the page.

| you say | Home Mind keeps | ask |
|---|---|---|
| "Dana is my partner", "My dentist is Dr Kell" | who is who, on the **profile** shelf, always shown (up to 15 lines) | "Where does my partner work?" finds notes that only say Dana |
| "Last Tuesday I changed the shed code to 1111", "Three days ago ..." | when it became true, not just when it was filed | a late note about an older change doesn't replace a newer value |
| "The trash goes out every Tuesday", "every other Thursday starting Nov 19", "the 1st of every month" | how it repeats | "When is the trash next?" |
| "Change the furnace filter every 3 months" + "I changed the furnace filter" | the chore and each time it was done | "When did I last change it?", "When is it due?", "Which chores are overdue?" |
| "The drill is in the red toolbox", "I moved the red toolbox to the shed" | where each thing is, nested | "Where is the drill?" (red toolbox, in the shed), "What's in the shed?" |
| "The dishwasher is a Bosch SHX78, serial 4471A, bought March 2024, warranty until March 2027" | type and fields | "What's the dishwasher's serial?", "Which warranties end this year?" / "have expired?" |

**Corrections become tests.** Say "that's wrong" after an answer, then the right value ("No, it's 5512"): it is
filed (replacing the old value) and the question is kept as a case. The **corrections** suite asks every kept question
again of your real memory, read-only, and fails if the old mistake comes back. `GET/POST /api/cases`.

**Duplicates are flagged, never merged.** Once a day Home Mind looks for notes that say the same thing (same words, or
almost all the same words with exactly the same numbers and names) and lists them in the morning summary to forget
one by hand. `GET /api/tidy` (`?now=1` to look again).

New suites: recurring, chores, places, typed, valid, profile, tidy (brain arm; tidy is brain only) and corrections
(brain only, on your real memory).

**Everywhere memory is read.** The worked-out lines and the profile also come back from Jeeves's lookup
(`/api/recall`) and Claude's `mind_recall`, as the first results (id 0), so "Hey Jeeves, where's the drill?" gets
the answer and not only the notes. Notes filed before 0.19 were read once at start-up with the same rules.

**Due today.** The morning summary starts with **Due:** chores that are overdue or due by tomorrow, and repeating
things happening today or tomorrow. The **Worked out** tab of the panel shows the profile, chores, repeating things,
where things are, things with details, possible duplicates and kept corrections.

**What the patterns miss** (option *Overnight, read what the patterns missed*, off). The overnight model reads
recent notes where the patterns found nothing but a word hints at structure, and only what it copies word for word
from the note is kept (a place, a name or a serial it made up is dropped). The **paraphrase** suite says the same
things in ways the patterns miss; `tests/compare_structure.py` runs it with and without your model, and checks that
the other suites don't get worse. Turn it on only if both hold.

## Upkeep

- **Model use.** Every model call is counted per model per day: calls, failures, tokens in and out, time. The
  Models tab shows the last 30 days; `GET /api/usage?days=30`; sensors on the Home Mind device show today's calls,
  tokens and (if priced) cost. Give a model `price_in` and `price_out` (dollars per million tokens) on the
  Configuration tab to see its cost; none are built in because prices change. Home Assistant's AI Task and
  conversation models don't report tokens, so only their calls and time are counted.
- **Activity log.** Searches, answers and changes older than *Keep the detailed activity log for* (default 90 days)
  are rolled up into daily counts per type (`event_days`) and removed once a day. Notes are never touched.
- **Quiet hours.** Phone notifications (morning summary, fog check-ins, weekly report) wait between *Quiet hours
  start* and *end* (default 22:00 to 07:00) and go out when quiet hours end; the Home Assistant notification list
  still gets them at once. *Phone notifications a day* (default 0 = no limit) stops phone pushes for the rest of the
  day. Waiting notifications are kept in memory, so a restart drops them from the phone (the notification list keeps
  them).
- **Old facts.** Once a day the morning summary asks about one old fact you told it ("Still true?"): a current fact
  older than *Ask about facts older than* (default 180 days), not confirmed or asked about within that time, most
  used first. The phone notification has **Still true** (confirmed, strengthened) and **It changed** (weakened, and a
  notification asks for the new value) buttons. Secret values are never shown, there or in the summary's lists.
- **Not done: prompt caching.** Claude only caches a prompt prefix of at least 1,024 to 4,096 tokens (by model);
  Home Mind's longest fixed instruction is about 500 tokens, so marking it for caching would change nothing. Most
  answers also go through Home Assistant's AI Task, which builds its own requests.

## Answers

- **Past days.** A question about a past day ("yesterday", "last Tuesday", "on Mar 3", "two weeks ago") also
  searches replaced versions, and the version that was current that day comes first.
- **Nothing close.** If a name in the question is in no note found, or the best note holds less than a third of
  the question's word weight (rare words count more; a word in no note at all counts most), the model is told the
  notes may be about something else and to say it doesn't know unless one plainly answers. Notes are never dropped
  for this. On the abstain suite it flags about 7 in 10 questions whose answer was never filed (names alone: 4 in
  10) and about 1 in 30 answerable ones. Answers carry `"weak": true` when it applied; with no model, the brain's own answer says so.
- **Sources.** Answers carry `sources`: the note(s) the answer came from (the ids the model named, else the notes
  holding the answer's values), with the date each was noted. Saying "that's wrong" right after an answer (same
  conversation) marks that note not helpful, so it ranks lower, and asks for the right value.
- **Options, off by default** (Configuration tab or switches on the Home Mind device): *Repeat the best note
  before the question* (on pages of 6+ notes, not for lists or past days) and *Name the notes before answering*
  (the model lists note ids, then answers; never for voice). Small models differ, so turn one on only if
  `tests/compare_answers.py` shows it helps yours.

## Bedroom comfort

Say "I'm cold", "I'm hot" or "I'm comfortable" to Jeeves (also from the Pebble's Ask Jeeves), or press
`input_button.comfort_cold` / `_hot` / `_ok`. Each vote is saved with a snapshot of the room, your watch and your
scale. Home Mind learns what you like at night, in the morning and during the day, tests which body signals matter
(only confirmed ones are used), and writes **variables**: `input_number.mind_target_temp`, `mind_fan_speed`,
`mind_humidity_target`, `sleep_temperature`, `wake_temperature`. The **Home Mind comfort driver** automation is
the only thing that touches the heater, fan and humidifier, clamped to `mind_temp_min/max` and
`mind_humidity_min/max`. Turn `input_boolean.mind_comfort_enabled` off to stop it; it also turns itself off if the
room is more than 3°F outside your limits for 30 minutes.

## Updates

- App code: Settings › Apps › Home Mind › Update (or turn on auto-update).
- Packs: installed automatically every 12 hours when "Install new packs automatically" is on, or from the
  Updates tab.

## API

Inside Home Assistant the web panel uses ingress. Other callers need the token in `/data/token` as header
`X-Home-Mind-Token`. MCP is at `POST /mcp`.
