# Changelog

## 0.24.0
- Home tab (was House): a map of the bedroom with every device where it really is. Lights glow on the map when on; tap anything to control it in the panel beside the map: brightness and color for lights (plus the White, Warm, Dim, Night and Rope night presets for all lights), target temperature and presets for the heater, speed, auto and night mode for the purifier, tilt for the blind, and a full 3D-printer panel (status, progress, layers, time left, temperatures, chamber light, pause / resume / stop with a confirm).
- Quick actions: Bedtime (lights off but a dim under-bed glow, light sleep mode, heat to your night setting), Wake up (lights on, blind open), Focus (desk power and desk light) and All off.
- Comfort as tall sliders (heat and fan; humidity joins once a humidifier is set up) with the hot / just right / cold taps and a switch to let Home Mind run comfort; tonight's forecast, the next 5 days, sunrise and sunset; your routines with on/off switches; plugs and media; phone and laptop battery.
- Meal ideas no longer fail on a long answer: the prompt asks for compact meals, and if a reply is still cut off the finished meals are kept instead of showing nothing. (The Claude AI Task in Home Assistant was also raised from 3000 to 6000 response tokens.)
- A nudge from the learned habits: past your usual lights-off time with lights on, it offers to run Bedtime.
- Safety: the panel can only act on the devices on the map, with only the services each needs (new endpoint /api/controls; anything else is refused).
- Scale (Wyze integration 0.4): weigh-in notes now include fat mass, skeletal muscle %, fat-free mass index, arm and leg balance and the fluid-balance reading; fluid balance is watched as a possible brain-fog link.
- Phone: the tab bar no longer leaves an empty gap under it in the Home Assistant app.
- Learned habits, suggested automations, "ask for an automation" and usual ranges moved under Home › Learned & automations.

## 0.23.1
- Panel: more compact on a computer (smaller cards, headings, ring and buttons; bedroom tiles no longer stretch across the page).
- Light mode: buttons on teal and violet use white text so they're readable; bedroom tiles stand out from the card.
- Bedroom: cold / just right / hot are outlined choices with icons; the one you tap stays marked and says what was logged.

## 0.23.0
- New layout for the web panel: 5 sections (Today, Food, Health, Memory, House) plus More for tests, models and updates. A side menu on a computer, tabs along the bottom on a phone. Dark by default, light when the phone or computer is set to light.
- Today (replaces Overview): readiness ring with last night's sleep, the brain-fog check-in (tap 0-5), a week chart of sleep, fog and readiness, food to eat first with protein so far against the goal, the bedroom's temperature, humidity, air quality and filter, and what was remembered today. New endpoint /api/today gathers it in one call.
- Health: the brain-fog record on its own page: rated days until patterns unlock, the week's report, a 14-day table of fog, sleep, readiness and bedtime, the suspects being watched (tagged sleep, pacing, room, body...) and any experiment.
- House starts with the bedroom readings and cold / just right / hot buttons; Food, Memory and activity lists read as two-line rows instead of wide tables; the item/shelf/event counts moved to More.
- Test fix: the monthly roll-up check failed when its notes straddled a month boundary.

## 0.22.0
- Evening brief: "Remembered" lists only notes in your own words, not meals logged, fog check-ins or summaries.
- Automation engine: new triggers time_pattern (`/15`, `*`), sun (sunrise and sunset with an offset), event (event_type, event_data) and template (fires when it turns true), and the `event` action. Automations using them can now be run by Home Mind.
- Habits: each night the last 4 weeks of house history are read for things that change to the same state at about the same time on most days ("Dana usually gets home around 6:30 PM on weekdays"). A habit must also beat chance and hold most of that thing's changes, and weekday or weekend habits must hold on more of their days. They are filed as notes on home/patterns, so questions find them. Habits suite: 0.994 (0.844 for a plain "same time on most days" rule, which took a kettle switched at random 8 times a day for a habit).
- Suggested automations: a habit done by hand on something Home Mind can switch, with no automation already touching it, becomes a drafted automation on the new House tab. Create puts it in Home Assistant switched off; Dismiss hides it.
- Usual ranges: each reading's 2-98% range for every 3 hours of the day. Two readings in a row far outside it make a phone notice (at most one a day for each thing; option house_notices). Baselines suite: 1.000 (0.667 with one range per reading, which misses a night-time value at 2 pm and a sharp drop).
- Notable changes: every house change gets a score. Locks, garage doors and alarms count most; a state at an hour it never has, or never seen at all, counts; a habit at its usual time is routine. Routine changes rank lower in answers; the very few that score highest go to the phone. Salience suite: 1.000 (0.167 going by the kind of thing only).
- Automations from a request: the House tab (and `/api/automations/draft`, `mind_draft_automation`) has a model draft an automation. Every entity, service, trigger and condition is checked against Home Assistant before it can be installed, switched off (`/api/automations/install`, `mind_install_automation`). Unlocking, opening a garage door or gate, and disarming need option automation_pin, and are refused when it is empty. Drafts suite (the checker): 1.000; with no checker, 0.400.
- Evening home brief at 21:00 (options evening_enabled, evening_time, evening_notify; a switch and a time on the Home Mind device): today's notable house changes, automation runs and failures, comfort changes, what was remembered, and suggestions waiting. Nothing is sent on a quiet day.

## 0.21.0
- Who said it: a value Claude marks as inferred (`mind_remember` with `inferred: true`, or `"inferred": true` on `/api/remember`) is treated like a fact learned overnight. It ranks below what you said and doesn't replace a key you set until you confirm it; confirming completes the replacement. Claude relaying what you told it replaces values as before. Sources suite: 1.000 (0.571 before).
- Review queue: notes from conversations (or inferred) that read like instructions to the assistant are held, out of answers, until approved. A "To review" card in Memory lists held and unconfirmed notes with Approve/Confirm and Drop; `GET /api/review`, `mind_review`; the morning summary says when something is held.
- Instruction checks rewritten: notes addressed to an assistant, meta-instructions ("ignore your instructions", "your new instructions are"), prompt markup and "when asked, say …". Household rules for people ("you must take your shoes off", "do not tell Mia about the party") are no longer flagged. On a held-out set written after tuning: 13 of 15 planted instructions caught (was 0) and 0 of 15 household notes flagged; injections suite 1.000 (0.469 before).
- Nightly tidy: notes that disagree (two people saying different values, a guess that differs from what was said) are listed in the morning summary and on the Worked out tab; people who agree are not. Comfort and house actions and automation runs older than 90 days are rolled into one note per month on a `…/monthly` shelf, the originals archived (restorable). Conflicts suite: 1.000 (0.600 before).
- Reminders: things that usually need doing (furnace filter every 3 months, smoke detectors monthly, about 25 of them) noted as done, with no chore note of their own, come due on their own; end dates ("the parking pass is good until Dec 1"), warranties and birthdays are mentioned 30, 7 and 1 days before and on the day. Each reminder is said once per stage (an overdue chore again a week later, not every morning); previews don't use it up. Reminders suite: 1.000 (0.583 before).

## 0.20.0
- A whole-memory request about one topic ("all my notes about the Pebble apps", "everything in your memory about long covid") is a normal question, not a dump; "everything you know about me" and anything with "ignore your instructions" still are.
- Facts learned overnight from conversations are on probation for 7 days until you confirm them: a newer overnight value doesn't replace what you told it directly. Both are shown, the overnight one marked UNCONFIRMED. A Confirm button on the note, `POST /api/item/{id}/confirm` and the MCP tool `mind_confirm`; after 7 days unchallenged it counts like anything else.
- Delete for good: `POST /api/item/{id}/purge` and `POST /api/purge_about` (both need `{"confirm": true}`), the MCP tools `mind_purge` / `mind_purge_about` (confirm must be true) and a "Delete for good" button. Removes the note with its whole history, its search-index entry, the activity-log rows and saved conversation turns holding its values, and duplicate pairs; freed space is overwritten and the write-ahead log emptied. Backups made before still have it. Forget still only archives.
- Asking for every secret at once ("list all my codes") gets the notes with every secret value hidden, for local models too; asking for the whole memory or the instructions ("print everything you know") gets 3 notes at most. A single code asked by name still works. By voice a list stops at 8 lines and says how many more there are.
- New attacks suite (bulk secrets, whole-memory dumps, a planted instruction, a value slipped in overnight, delete for good, and one code by name as a control): 1.000 on 20 held-out worlds; with these guards off, 0.455 (every request for all the secrets showed the codes, the overnight value won every time, and forgotten notes left their text in the file; whole-memory requests and the planted instruction were already handled).

## 0.19.1
- Structure is read only from the user's own notes. Notes Home Mind writes itself (nutrition studies, morning and journal summaries, weather, readings, meals, check-ins) were being read too, so a study's "daily" became something repeating every day and the morning summary's "Due:" would have listed them. A one-time pass takes that off the notes it was put on.

## 0.19.0
- Jeeves's lookup (/api/recall) and Claude's mind_recall return the worked-out lines and the profile first (id 0), and use the same search as answers (past days, "my partner").
- Notes filed before 0.19 have their structure read once at start-up.
- Morning summary: "Due:" chores overdue or due by tomorrow, and repeating things today or tomorrow.
- Web panel: a Worked out tab (profile, chores, repeating, where things are, details, duplicates, corrections).
- Secret values written right after the secret word ("Guest wifi password sunflower88") are recognized and hidden; a value needs a digit or symbol, so "password reset" stays.
- "Weekly, on Tuesday" means every Tuesday (it was read as every 7 days from when it was filed). "When did I last …" no longer gets next-date lines.
- Option overnight_structure (off): the overnight model reads what the patterns missed; only what it copies from the note is kept. New paraphrase suite and tests/compare_structure.py to decide on your model. Test runs take ingest "structure".

## 0.18.0
- Fixed before release: a time is not a place ("the meeting is on Friday", "the exam is in two weeks", "the birthday is on March 3"); backing out of a correction ("never mind", "ok") files nothing; "No, it's 5512" files just the value, and a bare value or phrase is filed as the value of what was wrong ("The garage code is 9090").
- Read from plain notes as they are filed, no model needed, and worked out when asked (a WORKED OUT section on the page models see):
  - who is who ("Dana is my partner"): kept on the profile shelf, always shown; "my partner" finds notes that only say Dana. Profile suite: 80/80 (was 4/80).
  - when a change became true ("Last Tuesday I changed the code to 1111"): a late note about an older change is kept as history instead of replacing a newer value. Valid suite: 160/160, keyed and unkeyed (was 80/160).
  - repeating things (every Tuesday, every other Thursday from a date, the 1st of every month): when is the next one.
  - chores ("change the furnace filter every 3 months" + "I changed the furnace filter"): last done, next due, overdue.
  - where things are, nested ("the drill is in the red toolbox", "I moved the red toolbox to the shed").
  - typed fields (model, serial, bought, warranty until, phone, birthday): one field, and which warranties end this year or ran out.
- Corrections become test cases: "that's wrong" then the right value files it and keeps the question; the corrections suite asks them again of your real memory.
- Duplicates flagged once a day (never merged) and listed in the morning summary, secret values hidden.
- New suites: recurring, chores, places, typed, valid, profile, tidy, corrections. API: /api/profile, /api/tidy, /api/cases.

## 0.17.0
- Model use: calls, failures, tokens in/out and time per model per day; a table on the Models tab, `GET /api/usage`, and today's calls, tokens and cost as sensors on the Home Mind device. Optional `price_in` / `price_out` per model (dollars per million tokens) for cost.
- Activity log: events older than `events_keep_days` (default 90) are rolled up into daily counts and removed once a day, so the database stops growing; notes are never touched.
- Quiet hours (default on, 22:00-07:00): phone notifications wait and go out when they end; optional daily limit (`notify_daily_cap`). The Home Assistant notification list still gets everything at once.
- Old facts: the morning summary asks about one old, unconfirmed fact a day with Still true / It changed buttons (`checkin_old_facts`, `checkin_after_days`, default 180).
- The morning summary hides secret values in the titles it lists (it shows on the phone's lock screen).
- Prompt caching left out on purpose: Home Mind's fixed instructions (about 500 tokens) are below Claude's minimum cacheable length (1,024 to 4,096 tokens), so it would change nothing.
- New switches, numbers and times for these on the Home Mind device; sensor for phone notifications waiting.

## 0.16.0
- Past days: a question about yesterday, a past weekday, a date or weeks ago also searches replaced versions, and the version current that day comes first. On the hard suite the right version is now first for yesterday 60/60 (was 0/60) and a past weekday 52/60 (was 14/60), so a brain-only answer gives it too.
- "Nothing close": the model is told when the best note is a poor match (rare question words count more; a word no note has counts most), and to say it doesn't know unless a note plainly answers. New `abstain` suite: about 7 in 10 never-filed answers flagged (names alone caught 4 in 10), about 1 in 30 answerable questions. Answers carry `weak`.
- Sources: answers carry `sources` (note id, title, date noted). "That's wrong" right after an answer marks that note not helpful and asks for the right value. Test runs report how often a right answer points to its note.
- Two answer options, off until they prove themselves on your models: repeat the best note before the question on long pages, and name the notes before answering (not for voice). Switches on the Home Mind device too. `tests/compare_answers.py` compares them on a real model through the Tests runner.
- The test runner now uses the same search and the same page as real answers.

## 0.15.0
- Controls in Home Assistant: the bridge (0.2.0) adds a **Home Mind** device with switches (comfort brain, watching the house, overnight learning, morning summary, fog check-ins, automatic pack updates), numbers (voice time limit, fade days, test hours), times (morning summary, overnight learning), buttons (run overnight learning, send the morning summary, sync calendars, check models, install pack updates, comfort cycle, refresh the daily record, brain tests, hand automations back) and sensors (memory items, conversation turns waiting, last overnight run, facts filed, automations run by Home Mind, version). Needs a Home Assistant restart to load the new bridge.
- Changes made there are kept across restarts until the same option is changed on the Configuration tab, which then wins.
- New options `overnight_enabled` and `morning_enabled` (on by default): off skips the scheduled run; the buttons still run it.
- API: `GET /api/control`, `POST /api/control/set`, `/api/control/reset`, `/api/control/run/<job>`. The bridge's `call` service may reach `/api/control`.

## 0.14.1
- Meal ideas: room for longer answers now that foods carry nutrition (they were cut off).
- Food lookups list a product once even when the database has it several times.
- Shopping-list sync no longer errors while Home Assistant restarts.

## 0.14.0
- Knowledge: reference data on the Home Assistant machine, in /data/knowledge (left out of backups).
- Food: USDA FoodData Central (Foundation Foods, SR Legacy and Branded Foods with UPC barcodes, October 2026) is downloaded once (about 450 MB) and built into a searchable database: per 100 g and per serving calories, protein, carbs, fat, fiber, sugar and sodium. Meal ideas now get the matched numbers for pantry foods and base protein and calories on them.
- Wikipedia: the newest English text-only Kiwix file (about 50 GB) downloads inside the download window (default midnight to 7 am), resumable, pausing outside it. For general questions (not about the user) the brain adds the best two passages, labelled with the snapshot date; the user's own notes always win, and answers say they came from Wikipedia.
- API: /api/knowledge (status), /api/knowledge/food?q= or ?upc=, /api/knowledge/search?q=, POST /api/knowledge/food or /wiki to start now.

## 0.13.2
- Patterns: the daily record adds naps (Steady's nap buttons), lights-off time, bedroom air overnight (particles, VOCs, air quality index), outdoor air quality, time out of the house (phone location, the day before and two days before), daylight hours and too-hot/too-cold votes in the night, each a factor in the comparisons.
- Button presses (bedtime, wake, naps, lights off) are read from the press time itself; history's window-start snapshot of an old press no longer counts. The daily record is rebuilt once.

## 0.13.1
- Patterns: a fog value Steady sends again (it re-sends on every sync and restart) is not a new rating; the check-in asks instead. Zero steps or calm means the watch did not sync, not a still day. The daily record is rebuilt once.
- Bridge 0.1.3: Home Assistant may call /api/patterns (Jeeves can rate fog by voice, read the patterns and start or stop the fog experiment). Needs a Home Assistant restart to load.

## 0.13.0
- Patterns: a daily record kept in Home Mind (sleep, morning readings, the day before and two days before, bedroom overnight, weather, logged food and caffeine, calendar) next to the brain-fog rating, backfilled from the ~10 days Home Assistant still has.
- Plain comparisons find what goes with more fog: at least 5 days each side, a difference of 0.75+ points, a shuffle test against chance, and "holding" only after two weekly reports 10+ days apart. Exertion is checked a day and two days later (crashes after exertion), and its experiments are about pacing.
- Experiments: one small change for 2 weeks ("start the fog experiment"), judged against the two weeks before.
- Check-ins: if the fog rating or last night's sleep is missing at 11:00 (again at 19:00 for fog), the phone asks with tap buttons; answers go to Steady and memory.
- Weekly fog report Saturday 08:00, to the phone and summaries/patterns.
- Weather: an hourly snapshot and one memory note a day (temperature, humidity, pressure and its change).
- Pantry: "ate" logs meals and drinks with the time and whether they had caffeine, without touching the pantry.
- The automation engine passes other Home Assistant events (tapped notification buttons) to Home Mind.

## 0.12.0
- Nutrition evidence: 45 studies picked from Yitz's evidence tracker (protein and muscle, losing weight, timing, and the foods in the house) ship with the app and are filed on the shelf nutrition/evidence at start, so questions like "do chickpeas help cholesterol?" find them.
- Meal ideas get the studies that apply to what's in the house (plus protein target, protein while cutting, portions and ultra-processed food every time) and name them briefly in the reason; preliminary findings are called preliminary, and stated dislikes beat any study.
- Protein target: about 1.6 g per kg of the latest weigh-in (where muscle gains top out), in meal ideas and the morning summary.

## 0.11.12
- Pantry: snacks, chocolate, candy, soda and nuts added, so pizza snacks and milk chocolate stay in the pantry.

## 0.11.11
- Pantry: adding something already past its printed date says so, with the year, instead of a weekday that reads like a future date. Oatmeal and popcorn added to the shelf-life table.

## 0.11.10
- Pantry: boxed products named by brand (Rice-A-Roni, Hamburger Helper, Instant Lunch) keep like boxed food; macaroni and cheese, grits, hot cocoa and other mixes added.

## 0.11.9
- Pantry: a container word at the end is skipped ("ramen cups" keeps like ramen); canned and dried foods go in the pantry; boxed mac and cheese and seeds added.

## 0.11.8
- Pantry: where an item goes is decided by the food the name ends with, so chicken ramen and Milk Bikis biscuits go in the pantry, not the fridge. Ramen, noodles, biscuits, cookies and pastries added to the shelf-life table.

## 0.11.7
- Pantry, from shelf photos: tea, coffee, salt, cocoa, spices and powders, soy sauce and water enhancers keep for years, not months (ginger mango green tea keeps like tea, not mango).

## 0.11.6
- Pantry, from the first real receipt: the last word names the food (strawberry banana yogurt keeps like yogurt, not banana); canned and dried foods keep about 2 years; mangoes and other fruit, frozen dinners added to the shelf-life table; "peas and carrots" stays one item; "a pint blueberries" is 1 pint of blueberries.

## 0.11.5
- Pantry: "a bag of baby carrots" files baby carrots (1 bag), not "bag of baby carrots". Jeeves can add, use, list and ask for meals (scripts Pantry in Home Mind and Meal ideas from Home Mind).

## 0.11.4
- Pantry: remove a line entered by mistake; long use-by dates show month and year; meal steps shown numbered.

## 0.11.3
- Photos can also be sent as JSON (image_b64), so a photo shared with Claude in chat can be added to the pantry.

## 0.11.2
- Fix: the shopping-list sync and photo reading weren't getting Home Assistant's answer back.

## 0.11.1
- Bridge 0.1.2: new service home_mind_bridge.call, so scripts (and Jeeves) can use the pantry and meal ideas. Needs a Home Assistant restart to load.

## 0.11.0
- Pantry: what food is in the house (fridge, freezer, pantry) with a use-by estimated from typical shelf life when none is given. Add from a photo (receipt, fridge, shelf; read by Claude through Home Assistant's AI Task), by typing or telling Jeeves, or by ticking items off the shopping list (they move into the pantry). Mark used or tossed.
- Meal ideas (Pantry tab, mind_meals, POST /api/pantry/suggest): from what's in the house, soonest-to-expire first, fitted to today (the Steady watch decides tired days: lower-carb, steady energy) and your goals (more protein, fewer calories). Rough protein and calorie estimates; missing ingredients go to the shopping list in one tap.
- Goals and foods to avoid are editable in the Pantry tab (defaults: more protein, fewer calories, lower-carb on tired days; no mushy vegetables, very spicy food or mushrooms).
- Morning summary: "use soon" and "past their date, still have them?" lines.
- Jeeves's lookups about food get the pantry list. The app now maps Home Assistant's media folder (for photos).

## 0.10.2
- Overnight duplicates: facts about different names or numbers are never treated as the same fact (two toolboxes in the laundry room were being merged).

## 0.10.1
- Overnight learning, from its first real run: facts are written in the first person ("I keep…", not "the user…"); turns Jeeves already saved ("Saved: …") are left out; facts that say nothing on their own are skipped; duplicates are caught by comparing content words (within the night and against memory).
- Extraction bench: facts already saved through Jeeves are mixed in, and duplicates filed are counted.

## 0.10.0
- Conversation log: what's said to Jeeves (Assist pipeline runs, voice or text) and questions asked through Home Mind are copied every minute into a raw table that search never reads. Deleted 30 days after processing (60 if never processed).
- Overnight learning (options overnight_time 03:00, overnight_model): a local model reads the day's conversations and files only what you stated as true (no questions, jokes, hypotheticals, retracted values or things only the assistant said), skipping what memory already has, with the time it was said and source "overnight". Skipped and retried the next night if the model's computer is off. The morning summary lists what was learned. GET /api/overnight, POST /api/overnight/run.
- Extraction bench (suite extract, ingest=extract or raw): facts buried in chatty conversations; reports facts found, junk filed and unmatched notes.
- Calendars to leave out (option calendar_quiet; Hebcal and Jewish holidays by default) stay in memory but out of answers and the morning summary unless asked about. Morning summary lists today's events by time and title only.
- Fix: the scale's early kilogram readings in the backfilled history were labelled lb; converted.

## 0.9.1
- Morning summary at 07:30 (options morning_time, morning_notify): what was remembered, learned, journaled and weighed, automation runs and failures, calendar changes and today's events, to your phone and as a Home Assistant notification. GET /api/morning to preview.
- Unknown names: a name is only typo-repaired to one that has the thing the question names ("Orvok fern" no longer becomes Harvok, who has a locker). More unanswerable questions now get the plain "nothing in memory mentions it" line. (A score cutoff was measured and doesn't separate unanswerable questions: they often score higher than answerable ones.)
- Follow-up questions: ask with a conversation_id and "And the Bexbel one?", "Where is it?", "And its code?", "And in the garage?" are rewritten from the question before (kept 10 minutes). New suite followup: 100% with rewriting (without it: "where is it?" 18%, "and its code?" 53%).

## 0.9.0
- Tests run on every push (GitHub Actions): end to end, comfort, automation engine, privacy, and every brain suite on 20 held-out worlds with a floor each.
- Model fallback: if the chosen model fails or is slow, the fallbacks (option "fallback", default Haiku) are tried, then the best matching note. Voice questions (voice: true) are capped at "voice_seconds" (8 s), leaving time for the fallback. A model that just failed is skipped for 5 minutes.
- Secrets: notes pairing a code, password, PIN or combination with a value are marked secret (🔒). Their values, and IP/MAC addresses, are hidden from cloud models and from Jeeves's lookups unless the question asks for that one. Local models see everything. Mark or unmark by hand in the panel.
- Memory page: recently added items (filter by kind of source and time), a readable item view with other versions, edit, forget/restore, not helpful, and mark secret.

## 0.8.4
- Gathered lists come with worked-out lines for small models: which things are in the place asked about, which changed in the stretch asked about, the count (or, for one named thing, how many times it changed), the most recent and the oldest, which have no code yet.
- A question naming something no note mentions is told so plainly (and whether it was forgotten); other things' notes are no longer grouped as its versions.
- Typo repair leaves forgotten names alone instead of turning them into other names.

## 0.8.3
- Fix: a note about a whole group ("changed all the cabinet codes") was dropped from the model's context by the name filter, so models saw only the replaced value. It now stays, marked as applying to the thing asked about.

## 0.8.2
- Questions about how a reading went ("coldest this week", "last few days") get a per-day low/high/average and the lowest and highest moments, for models and for Jeeves's lookups.

## 0.8.1
- Searches that name a day (what Jeeves sends for "what do I have Monday?") get that day's calendar events first.

## 0.8.0
- Calendars: every hour Home Mind reads your Home Assistant calendars (14 days back, 60 ahead) into calendar/<name> notes, written with full weekday and date. Changed events are rewritten, cancelled ones archived, duplicates across calendars kept once. Questions naming a day (today, tomorrow, Tuesday, Nov 20, this week, upcoming) get that day's events in order. GET /api/calendar, POST /api/calendar/sync.
- History: once per thing, Home Mind files what Home Assistant still has from before it was watching: the last 10 days of changes (same rules as live) and, for sensors with long-term statistics, one note per day (average, low, high) for up to a year (home/daily).

## 0.7.1
- The Mind Journal lives in Home Mind: entries on the journal shelf, a nightly 23:30 summary by Haiku (same instructions as before) saved on journal/summaries. GET /api/journal, POST /api/journal/summarize.
- Automations can be retired (stop running, Home Assistant copy left off and unlabelled) when their job moves into Home Mind.

## 0.7.0
- Home Mind can run your Home Assistant automations itself. They stay written in Home Assistant (edit them there as before); Home Mind reads their configs, follows Home Assistant's events over the websocket, checks triggers and conditions, and calls the services. Templates are rendered by Home Assistant's own engine.
- Taking one over turns the Home Assistant copy off and labels it "Run by Home Mind". Home Mind never runs one whose Home Assistant copy is on.
- sensor.home_mind_heartbeat updates every minute, for the "Home Mind watchdog" automation in Home Assistant, which hands everything back to Home Assistant if Home Mind stops.
- Every run is filed in memory (home/automations) with what it did; each automation's description is kept as a note.
- GET /api/automations, POST /api/automations/takeover|release|run|reload, MCP tool mind_automations.

## 0.6.0
- The house: every 5 minutes the brain reads Home Assistant (lights, Dyson fan, humidifier, heater, switches, scripts, scenes, room and air readings, Steady watch numbers, phone, weather), skipping noise like light segments and connection sensors. Changes are filed as dated notes (home/devices, home/readings), one series per thing.
- Questions that name something in the house get its live state (HOME NOW) alongside the notes.
- Controls: mind_control / POST /api/home/act (on, off, toggle, brightness, color, fan speed, humidity, run) and mind_do / POST /api/do (a request in words, planned by a model). Only things on the control list (option home_controls), only while input_boolean.mind_control_enabled is on, with limits (brightness 1-100, fan up to home_fan_max, humidity within mind_humidity_min/max, 20 actions a minute). The heater stays with the comfort driver. Every action is filed in memory (home/actions).
- GET /api/home lists what the brain watches and what it may control.

## 0.5.4
- Fix: weigh-in backfill asked Home Assistant for one day of history instead of ten.

## 0.5.3
- POST /api/body/backfill files weigh-ins from Home Assistant history on demand and reports what it found.

## 0.5.2
- Fix: the start-up weigh-in backfill sent a time Home Assistant couldn't read, so only the newest weigh-in was filed.
- The version shown in the log and status is right again.

## 0.5.1
- Weigh-ins from the scale go into memory: each new weigh-in becomes a dated note on the body/weight shelf (weight, body fat, BMI, muscle mass, body water and more), checked every minute. At start-up the last 10 days of Home Assistant history are filed too. Old kg readings are converted to lb.
- Readings in a series are versions of one thing: the newest comes first, older ones are shown with dates, so "what's my weight?" and "what did I weigh on Nov 11?" both work.
- /api/remember accepts "at" (when it happened).

## 0.5.0
- Questions that need every match (how many, which, everything about): a GATHERED list with one line per thing, its newest note, first/last dates and change count, sorted by newest change.
- Time words: this week, last week, this morning, today, since <weekday>, N weeks ago, on <month> <day>; the oldest version is labelled as the first.
- Group notes ("changed all the cabinet codes", "the shed was cleared out") count as the newest note for each member.
- Guesses ("probably", "I think") never replace stated facts and are marked GUESS.
- Notes containing instructions are marked, never treated as a fact's version, and the model is told not to follow them.
- Corrections ("Actually that was a typo, it's 4471") link to the note before them.
- New tool mind_forget_about: forget every note about a name.
- New test suite hard2 (30 types). Typo repair breaks ties by which name owns the thing asked about.

## 0.4.4 — 2026-11-14
- The "TRUE ON <day>" heading says how the question's words were read (= "last Tuesday" in the question): qwen 4b
  read "last Tuesday" as the Tuesday before and ignored the heading.

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
