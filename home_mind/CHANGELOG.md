# Changelog

## 0.46.1
- **Science data: molar masses** are asked from Wikidata only for the substances already found, in small batches (the one big query timed out), and only kept for chemicals (not the mass of a planet). Defining formulas are asked for items with an article in two or more languages, without descriptions, so the answer is no longer cut off.

## 0.46.0
- **Science data:** exact values and statements for math, science and engineering questions, kept on the Home Assistant machine: NIST CODATA physical constants, a science subset of Wikidata (elements, compounds and materials and their measured properties, physics formulas, units), ProofWiki theorems and definitions (the NaturalProofs copy), the NIST WebBook's tables for the 1,500 most common compounds, and, with your own key in `materials_project_key`, the Materials Project's computed properties. Downloaded inside the download window and compiled into `knowledge/science.db`; the answer's source line names the data it used. The look-up tool and `mind_wiki` return the data lines too. Option `knowledge_science` (on); `POST /api/knowledge/science` starts it now; `GET /api/knowledge/science?q=…` shows matches.

## 0.45.7
- **Think bench scoring of names:** a pick or ordering answer is judged by its conclusion (the last line naming any of the candidates), not by whichever name comes first, so a model that restates the clues before answering isn't marked wrong. Run details keep the start and the end of long answers.

## 0.45.6
- **Think bench stops cleanly:** when the model (or the app, during an update) stops answering, the run stops after 3 tasks in a row that got no answer and is saved as an error naming why, instead of finishing with a low score that would sit in the trend.

## 0.45.5
- **Think path, from held-out live runs (qwen 4b):** a keyword written inside another ("CALC: DATE: weekday of ...") runs the inner tool; CALC takes a comma list of values and `sort(...)`, and drops a "-> guess" written after the expression; a list of plain numbers (an average) no longer gets the "sort and pick" method, which is now for choosing among named items ("A, B, or C"); that method says plain dates and numbers need no tool to compare.
## 0.45.4
- **Wikipedia by meaning is about 8 times faster:** the question model stays in single precision all the way through, so a search takes a fraction of a second instead of about 1.5 s.
- **Fewer stray articles:** meaning matches below 0.83 are dropped (made-up questions score up to about 0.82).

## 0.45.3
- **Timing for Wikipedia by meaning:** `meaning=1` also shows how long each part took (the model, the index, the text) and takes `nprobe` to try a wider search.

## 0.45.2
- **See what Wikipedia by meaning found:** `GET /api/knowledge/search?q=…&meaning=1` lists the closest articles with their scores and how long the search took.

## 0.45.1
- **Think path finds your local models:** with no `router_local` and no local model in `fallback`, any connected local model thinks first (quickest first), so a setup with only Haiku in `fallback` still gets the small model and Haiku as its consultant.
- **Fixes from the first live bench (qwen 4b):** an oven temperature in °F gets the conversion method, not "explain"; a correct answer that repeats the question's numbers first is no longer marked unchecked; a model that takes two steps without the tool its method is built on (DATE for calendar work) is reminded once, with an example; "how many grams is 3.4 pounds" is answered by code.
- **Bench details** show each tool step and why a task stopped, and a running bench shows its progress every 4 tasks.
## 0.45.0
- **Wikipedia by meaning:** once the offline Wikipedia is in, Home Mind also downloads a search index of every article's first paragraph (NeuML's txtai-wikipedia, built with the e5-base model) and the model itself, about 9.5 GB, inside the same download window. A question then finds its articles by meaning, not only by sharing words ("is unbaked biscuit batter risky?" finds Cookie dough); the text still comes from the Wikipedia file. It runs on this machine with numpy alone (no new packages) and sends nothing out. Option `knowledge_wiki_meaning` (on); `POST /api/knowledge/wiki_sense` starts the download now.

## 0.44.0
- **Think path for general tasks:** math and word problems, percents, rates, units, dates and clock times, picking from
  a list, logic, plans, priorities, how-to, comparisons, explanations, messages, creative requests, summaries and small
  code go to a small local model that works step by step with exact tools (`CALC`, `DATE`, `CONVERT`, `RECALL` for your
  notes, `LOOKUP` for offline Wikipedia) and a method for the kind of task. Whole questions a tool can read are answered
  by code with no model. Answers are checked by code (numbers must come from a tool, dates from `DATE`, the request's own
  counts and word limits) and said back once if they fail. Start a question with `think:` to send it there.
- **Consult and learn:** when the small model fails a check, doesn't know or runs out of steps, the consultant (the
  first cloud model, or `think_consultant`) gets the same task and tools. When it solves a kind of task no method
  covers, it writes one: on trial first, trusted after 3 wins by the small model, retired if it keeps failing. 21
  methods written by a large model ship with the app. Never from private questions; private mode stays in the house.
- **Measured:** `GET /api/think` (answered by code, by the small model, with the consultant, failed, skills learned,
  share solved locally) and a new **think** test suite of 12 task kinds with answers worked out by code, with arms for
  the model alone, the think path, and the think path without skills or without tools.
- New options `think`, `think_model`, `think_consultant`, `think_steps`, `think_learn`, `think_learn_daily`.
## 0.43.1
- **No disambiguation pages from Wikipedia:** a list of other articles ("Eiffel Tower may also refer to…") is no longer added as a passage or named on the source line.

## 0.43.0
- **Wikipedia, smarter:** general questions that say "I" or "me" ("can I eat raw cookie dough?", "tell me about Mars") now get the offline Wikipedia passage too; only questions about your own things ("where did I put my keys?", "what did I eat?") are kept out. When the whole question finds nothing on topic, shorter search words are tried ("raw cookie dough", then single words), at most four more searches.
- **Wikipedia on the source line:** an answer that came from Wikipedia names the article under it ("Wikipedia: Eiffel Tower"), beside the notes. It is shown only, never logged as a note.
- **Look it up yourself:** Claude can search the offline Wikipedia over MCP with `mind_wiki` (read scope), and the look-up model (`tool_model`) gets a `wikipedia` tool, offered once the file is downloaded.
- **A fresh snapshot every six months:** once the Wikipedia file is six months old, Home Mind checks weekly for a newer one and downloads it the same way, inside the download window (about 50 GB more disk while it downloads). The old file stays in use until the new one is complete, then is removed.

## 0.42.0
- **TickTick tasks:** connect your TickTick account on System > Overview (setup in the docs) and Home Mind copies your open tasks every 15 minutes. "What's due today?" and "what homework do I have this week?" answer from them, overdue first; the morning summary lists overdue tasks and what's due today, and warns two weeks before the connection runs out. "Remind me to …" and "add a task …" go to your TickTick inbox with the day and time you say (option `ticktick_reminders`), and "check off the essay" completes it in TickTick. Tasks stay on their own shelf and never become ordinary notes; Disconnect forgets them.

## 0.41.6
- **No look-alike codes:** a code question naming something memory never heard of ("the Zendor vault") no longer gets the code of a similar name the typo fixer swaps in ("the Zandor locker"). It says it doesn't know that one and asks whether you meant the other.

## 0.41.5
- **"I don't know that code"** no longer names the thing back: the typo fixer could turn a name memory never heard ("Zephyr") into a word it knows, so the answer named the wrong thing.

## 0.41.4
- **Your logs go by date:** "what did I eat last?" is the newest meal you logged and "brain fog yesterday" is yesterday's check-ins, put ahead of older notes that only share the words. A day you name ("two days ago", "last Friday") gets that day's entries; someone else's personal entries never come up.
- **Codes from memory, with no model:** a plain "what's the code for the X?" is read straight from the top note when it is a current, stated note about X, so a local model can no longer repeat a planted instruction, give a value learned overnight and not checked, or name another thing's code after X was forgotten. A thing memory never heard of gets "I don't know". Past values, lists of codes and unknown voices go the usual way. The model+brain test arm does the same, so the attacks suite measures what an answer does.
- **Whole titles on the watch:** a calendar title with a long run of spaces ("Automatic Control          Dr. Wu Office Hours") shows as "Automatic Control · Dr. Wu Office Hours", wrapped onto a second line instead of cut.
- **Questions aren't meals:** text typed in the meal box that reads as a question ("What should I eat now?") is refused instead of logged as a meal, and skipped when it was logged before.

## 0.41.3
- **No more "HTTP 401: not a JSON answer" after the phone sleeps:** when Home Assistant's session for the page has run out, the page tries again twice over a few seconds, then reloads Home Assistant's page around it to get a fresh one, keeping what was typed in the ask box and the tab you were on. Coming back to the app after more than two minutes checks the session straight away. Home Mind's own refusals (a revoked token) still show as before, and it never reloads twice in a minute.

## 0.41.2
- **Bridge 0.3.3:** its switches, numbers, buttons and sensors no longer use an attribute name Home Assistant reserves (`group`), which it warns will stop working in 2027.2. Loads at Home Assistant's next restart; nothing changes until then.

## 0.41.1
- **Cleaner event names on the watch and in answers:** the glance and the situation lines drop the date a calendar event was filed with ("Work Remote", not "Work Remote (Wednesday Nov 25 2026)"), so the line fits the Pebble.

## 0.41.0
- **Overnight learning survives a sleeping laptop:** with no overnight model named it uses qwen9b, else qwen35b, whichever is awake. If the laptop is asleep it tries again every few minutes in the 3-hour window, then catches up later that morning once it wakes (until 12 hours after the window opened), instead of skipping the night. A conversation logged twice in one turn is now kept once.
- **Search by meaning, more easily:** with `embed_model` empty, the first model that has an `embed_model` field is used. While its computer sleeps, search falls back to keywords at once and indexing waits, instead of each search waiting on a timeout.
- **Where and when in every answer:** every answer's brief opens with a short situation built at question time: the time, where the person asking is (zone, home or away; nothing for a guest), the next two calendar events (title and time), sleep mode, and the top three tasks.
- **Pebble glance and one-tap capture:** `GET /api/glance` gives a watch-sized look at now (next event, top tasks, the day's check-in, approvals waiting, five lines of at most 30 characters), also as `sensor.home_mind_now` for Home Assistant cards. `POST /api/capture` saves an idea, journal entry or note in one call. Both are allowed through the bridge's `home_mind_bridge.call` (bridge 0.3.2; restart Home Assistant once to load it).
- **Claude with tools (option `tool_model`, off):** when an answer comes back "I don't know", a Claude model you name may search memory in other words, read the house, the calendar and the task list (up to three rounds), then answer. Billed to that model's API key, at most `tool_daily_limit` times a day (50), never for private questions, voice or whole-memory requests, through the same gates as the plain Claude link. `tool_writes` (off) also lets it store notes and control things on the control list, never locks, alarms or garage doors.
- **Claude 5 models through the API:** requests to `anthropic` models no longer send a temperature to models that reject it (Opus 4.7 and every 5.x model, including the default `claude-haiku-5-5`), which made every request fail with HTTP 400.
- The plain-link option's description now says "as before 0.40".

## 0.40.0
- **The safety bar can pass (`System › Tests`):** a zero-tolerance gate (a bar of 100%) could never pass, because it compared the bar against the low end of a confidence range. It now passes when every case passes, and fails on the first failure.
- **Private questions stay home:** questions the router marks private now go to an awake local model first in every mode, by voice too, instead of always going to Haiku. New option `private_to_cloud` (`never` answers from your notes alone when no local model is awake). The nightly journal summary uses the same rule and retries later if the laptop is asleep, instead of skipping the night.
- **The plain Claude link is limited:** the bridge's plain link can read and write notes but no longer control the house, purge or install automations, and secret values in notes it reads back (`mind_recall`, `mind_get`) are hidden unless the question asks for them. Option `plain_link_admin` restores full access. A person's own link and the main token on your network are unchanged. The bridge now marks plain-link requests and checks `home_mind_bridge.call` paths more strictly; the bridge is now 0.3.1; restart Home Assistant once to load it (older bridges are limited too, by Home Assistant's address).
- **Ideas and journal by voice:** "idea: …", "idea for <project>: …", "journal: …", "dear journal, …" and "note to self: …" are saved to the right shelf (Ideas › book and framework, projects or random; your personal journal) and answered "Saved to …". Before, only "remember…" and "note…" were saved.

## 0.39.0
- **Alarm tile (`House › Your tiles`):** shows your alarm time and lets you change it (time only, `input_datetime.alarm`; the Mon–Sat wake-up follows it). Added once to your tiles with the Updates tile.
- **Light scenes:** the room map's lights and any light tile get a scene picker with that light's own Govee scenes (`select.<light>_scene`). Only those lights' scene lists, and only their own scene names, can be set. Names load once per light from the new `GET /api/tiles/scenes`.
- **Trend lines:** number tiles (weight, body fat, any sensor) draw the last 7 days from Home Assistant's history, refreshed every 10 minutes.
- **Updates tile:** how many Home Assistant updates are waiting and which, to read only (install them in Home Assistant).

## 0.38.0
- **Simple view (on by default):** hides the tools you rarely need so the panel is easier to use: Health › Voice Learning, Food › Rules, House › Automations and Sensors & Habits, System › Models & Training and Tests, the Brain's Graph and Worked Out, and the Overview's test cards. Turn it off in System › Overview › Display & household view to see everything (kept per device).
- **Sleep and comfort buttons (`House › Your tiles`):** wind down, bedtime, nap start and end, and too cold / just right / too hot are added once to your tiles and drawn as a row of buttons above them (Edit shows them as tiles to move or remove). Buttons, scripts and scenes on tiles all join that row. `input_button` tiles can be pressed (`press` only).

## 0.37.2
- **Humidifier on the room map (`House › Live Controls`):** the bedroom humidifier now has its spot on the map, under the desk.
- `version.py` caught up with `config.yaml` (it still said 0.37.0 in 0.37.1).

## 0.37.1
- **Reading light preset (`House`, All lights):** a new **Reading** pill runs `script.reading_light`: everything else off, and only a small section of the wall rope above the bed lit in warm amber at 30%, high enough to stay out of your eyes.

## 0.37.0
- **Your tiles (`House › Live Controls`):** a new card that shows anything from Home Assistant, picked from a searchable list (**+ Add**), and drawn by its kind: lights, switches, fans, blinds, climate and humidifiers get their controls; speakers get play/pause, skip, volume and the album art; sensors show their value; images and cameras show their picture; to-do lists show their open items; scripts, scenes and buttons get a Run/Press button. **Edit** moves, renames and removes tiles. A first load adds the scale (weight, body fat), both speakers, the printer camera and the three Ideas lists. New `modules.py`, `GET /api/tiles`, `GET /api/tiles/catalog`, `GET /api/tiles/picture`, `POST /api/tiles/{op}`.
- **Tile safety:** a tile can only switch what is on it, through the same per-domain allowlist and value clamps as the room controls (now also cover open/close/stop/position, fan and humidifier toggle, speaker volume and scenes). Locks, alarms, valves, garage doors, gates and doors are never switchable from a tile; they show their state only. Adding or removing tiles needs the admin scope.
- **Dashboards (`House › Dashboards`):** Home Assistant dashboard views shown inside Home Mind, opened in kiosk mode, with pills to switch between them and a form to add one by its path (such as `/dashboard-bedroom/0`). Starts with the Overview. Works when Home Mind is opened from Home Assistant.
- **Humidifier on the Comfort card:** `humidifier.humidifier_bedroom` is now one of the House devices (not yet placed on the room map).

## 0.36.1
- **Printer time left in hours and minutes (`House › 3D printer`):** the A1 reports its remaining time in hours, and the card showed that number as minutes ("2.55 min left"). The time is now read in the sensor's own unit and shown as hours and minutes ("2h 33m left"); `/api/controls` adds `printer.remaining_min`.
- **Print preview on the printer card and map icon:** while a print is loaded, the Bambu cover image (`image.a1_cover_image`) shows in the card's picture tile and on the printer's icon on the room map, served by the new `GET /api/controls/printer/cover`.

## 0.36.0
- **General Data-Link Layer (`memgraph.py`):** expanded MemGraph beyond category-specific relations with 9 domain-agnostic structural & temporal link types: `AT` *(exclusive location)*, `PART_OF`, `DURING` *(era/chapter)*, `REQUIRES`, `AFFECTS` *(signed +/−, negative displays as `IMPAIRS`)*, `DERIVED_FROM`, `CONTRADICTS`, `SAME_AS`, and `SUPERSEDES`. Updated the Archivist prompt and dynamic signed display labels to support all of them.
- **Automatic Temporal Superseding & Era Closeout (`retire_link`, `close_era`, `POST /api/memgraph/close_era`):** superseded `EXCLUSIVE` links now automatically populate `valid_to` and `superseded_by` when they go dormant; retiring a link or closing an era/place (e.g. moving from `"Oak St House"` → `"Austin House"`) retires every active link tied to it in one step and records a `SUPERSEDES` edge, excluding past links from `CURRENT_STATE` answers while keeping them searchable for `HISTORICAL` questions (rendered with `ended:YYYY-MM-DD`). Confirming a past link reactivates it cleanly.
- **Brain Graph Controls (`System › Brain › Graph`):** added **`+ Link`** (`POST /api/memgraph/add`) and **`📦 Close era`** drawers right in the Graph toolbar, plus **`PAST`** badges, `ended` / `replaced by` dates, **`📦 Mark past`**, and **`↺ Reactivate`** buttons in the link inspector.

## 0.35.4
- **Fix Weekly Meal Plan Date Rollover Alignment (`Food › Meal Plan`):** saved meal plans now always render each day using its own saved calendar date (`plan.days[i].date`) instead of binding meals by row index to the rolling `week_calendar`. When a new day starts, today's real meals automatically move to the top (`· today`) with upcoming days below, earlier/past days tuck neatly into a collapsible **"Earlier days in this plan"** drawer (keeping all meal actions bound to the exact saved day index), and switching to **`1 · Pick`** aligns the effort grid by real calendar date.

## 0.35.3
- **Fix `bool` validation error on recipe cooking steps & toggle endpoints (`main.py`):** added `bool` type support to `check_types` and `_KINDS`, fixing the `KeyError: <class 'bool'>` error when fetching daily cooking instructions (`/api/pantry/meal_steps`), re-estimating meals (`/api/pantry/meal_edit`), toggling meds/habits (`/api/habits/toggle`), or changing Fitness sync preferences (`/api/fitness/prefs`).
- **Partial Pantry Consumption (`Food › Pantry`, `POST /api/pantry/part`, Ask/Voice):** record when you only took part of a stored food item instead of marking the whole thing used. Each pantry item now has a **`part`** button with 1-tap **`−¼`**, **`−½`**, **`−¾`** controls plus custom **`Took`** (supports counts, fractions, and weight math like `8 oz` from `2 lb` → `1.5 lb` or `3` from `a dozen` → `9`) and **`Set left`** actions. Also works naturally from Ask/Voice (e.g., *"I only used half of the chicken breast"*, *"I took 2 eggs"*).

## 0.35.2
- **Reversible Cloud Secret Masking in Overnight Extraction (`privacy.py`, `overnight.py`):** cloud models still never receive raw PINs, passwords, IPs, or MAC addresses during overnight fact extraction, while extracted facts now restore real values locally via reversible placeholders before saving to SQLite (marked `sensitive=True`). Also hardened `privacy.is_cloud()` against model objects without `.cfg`.
- **Meds & Maintenance Integrity Fixes (`meds.py`, `maintenance.py`):** toggling a med/supplement off now archives its `body/meds` note for that day and re-toggling avoids duplicate entries; `Maintenance.add()` now guards non-finite `every_days` values (`inf`/`nan`) cleanly with a `400` validation error.
- **Recipe Shopping Word-Boundary Matching & Scope Cleanup (`recipes.py`):** `Recipes.add_to_shopping()` now matches pantry stock on whole words so `"salt"` no longer skips `"unsalted butter"` and `"oil"` no longer skips `"boiled eggs"`; switching a recipe from `personal` back to `household` now clears its `owner`.
- **Multi-Person Fitness Scoping & Navigation Polish (`fitness.py`, `hub.py`, `web/index.html`):** passed the active `VIEWER` into Home's adaptive fitness targets, defaulted `Fitness.log()` to personal `VIEWER` ownership when called without explicit `meta`, routed Home's overdue **Upkeep** card straight to `House › Upkeep`, and added a synced pending-review count badge directly on the **System › Brain** sub-tab.

## 0.35.1
- **Daily Cooking Instructions (`Food › Today`, `Food › Meal Plan`, `Food › Recipes`):** added on-demand step-by-step cooking and reheating instructions (`POST /api/pantry/meal_steps`, cached directly on the saved weekly plan, with a deterministic fallback when no LLM is configured). `Food › Today` now shows today's planned meals with numbered instructions and 1-tap `✓ Eat` logging, the Meal Plan detail modal includes a `📖 Cooking steps` / `↻ Refresh steps` action, and `Food › Recipes` displays saved steps in an expandable list.
- **Cleaner 6-Item Sidebar (`System › Overview & Persona` + `System › Brain`):** moved the Household Viewer switcher and Family Wall / Kiosk mode toggle into `System › Overview & Persona` and folded **Brain** (`Notes`, `Graph`, `Worked Out`, `Docs & Data`) into `System › Brain`, keeping the left sidebar compact (`Home`, `Fitness`, `Food`, `Health`, `House`, `System`) while preserving all deep links.
- **Data Pipeline Hardening & Privacy Fixes:** fixed 15 privacy, ingestion, and concurrency issues across the data pipeline — masked secrets before cloud overnight extraction, scoped overnight note linking so personal notes never leak into household prompts, added `personal` owner scoping to check-ins, meals, weigh-ins, and coffee undo, prevented background meal estimation from overwriting manual macro edits, stopped transient calendar/IMAP errors from archiving events or marking rate-limited mail `\Seen`, guarded `NaN`/`inf` sensor inputs and SQLite write locks, hardened `sqlview` secret/Unicode owner checks and `MdSync` unflagged secret detection, and persisted the Telegram polling offset across restarts.

## 0.35.0
- **Saved Recipe Library & Barcode/UPC Quick-Log (`Food › Recipes`):** save favorite meals and staples with exact macros, ingredients, and prep notes; look up packaged foods locally by barcode/UPC via USDA data (`✓ Save & Eat` / `★ Save staple`); 1-tap `✓ Eat` logs directly to `body/meals` with exact scaled macros; 1-tap `+ Shop` subtracts fresh pantry stock before adding missing ingredients to Home Assistant's shopping list; plus a `★ Save Recipe` button inside the Weekly Meal Plan Detail modal.
- **Medications, Supplements & Daily Habit Streaks (`Health › Meds & Habits` + Home pill):** track recurring meds, supplements, and daily habits with 1-tap check-offs, consecutive day streaks, and 7-day completion dots. Checking off a med/supplement records it on `body/meds` for brain-fog and MemGraph correlation, and a new Home quick pill shows today's `done/total` progress.
- **Home Maintenance, Warranties & Recurring Chores (`House › Upkeep`):** track recurring home upkeep (furnace/water filters, car oil, safety checks), warranty expiration dates, and linked manual/receipt documents. Marking an item `✓ Done` logs to `home/maintenance` and advances `next_due`, and overdue items surface in Home's `For You` queue.
- **Multi-Person Household Switcher & Family Wall / Kiosk Mode:** switch views between `Everyone`, `Household only`, or any household person from the sidebar footer (backed by an admin-only `X-Home-Mind-Viewer` header that person-bound tokens cannot spoof), plus a 1-tap `⛶ Wall mode` kiosk layout for tablets/wall displays.
- **Desktop Home One-Page Layout & Spacing Polish:** fixed the active status tab seam and inactive tab spacing above the status detail card, lowered the 2-column desktop Home breakpoint to 960px, and fitted desktop Home cleanly within a single viewport (`100dvh`) with internal scrolling on tiles and detail panels.

## 0.34.0
- **Compact 7-Pillar Tabbed UI:** reorganized the web panel from 12 cluttered links and long stacked pages into 7 clean, single-screen tabbed pillars (**Home**, **Fitness**, **Food**, **Health**, **House**, **Brain**, **System**), consolidating Memory + Worked Out + Brain Graph under **Brain**, Settings + Models + Tests + Access Tokens + Updates under **System**, and splitting Food, Health, and House into compact horizontal sub-tabs.
- **Dedicated Fitness Section (`Fitness`):** compact 3-tab cockpit (`Today` · `Progress` · `Goals & Review`) featuring Auto-Baseline Goals (derived from scale BMR/weight, 14-day watch steps/sleep, and workout history, with per-field `Auto`/`Edit` pinning and `↺` reset), Adaptive Daily Goals layered on prior sleep/readiness/steps/protein (synced automatically to Home & Food), Progressive Workout Routine Generator (`+5 lb` progression, `10%` deload on stalled lifts, live rest timer, warm-up ramps, one-tap exercise swaps, MemGraph knee/shoulder swaps, and HA Calendar busy-day 30m Express trim), unified 3-in-1 SVG charts (`Strength e1RM`, `Cardio Pace`, `Body Weight`), muscle-group volume, and inline Workout History editing.
- **Daily Meals Log (`Food › Today` & `Home › Food`):** view every meal logged on any day (`‹ Date › Today`), see daily macro totals against your goals, log meals for past days, edit meal text/time/macros inline with optional **↻ Re-estimate**, and delete mistakes right from Food or the Home Food tile.
- **Weekly Meal Planner Enhancements (`Food › Meal Plan`):** added **♨️ Reheat** no-cook/packable effort brush, persisted **Prep Day & Time** selector, one-tap **Add prep / week to Home Assistant calendar**, fitness nutrition goal alignment with per-day macro totals and remaining headroom (*extra room left*), and a clickable **Meal Detail modal** with USDA per-100g food info and **✓ Log as eaten**.

## 0.33.0
- **Draft & one-tap install Home Assistant automations from Ask / Talk / Voice:** asking *"Can you set up an automation to turn off the bedroom lights and heater when I leave?"* now drafts and validates a real Home Assistant automation against your entities/services, supports follow-up tweaks (*"Also turn off the bedroom fan"*), and shows a one-tap **`+ Install switched off`** chip (prompting for `automation_pin` only if the draft unlocks/opens the house).
- **Direct Ask / Talk / Voice actions:** the Ask bar and voice pipeline now directly execute notes (*"Remember that..."*, *"Remember privately that..."*), shopping lists & reminders (*"Add milk and eggs to my shopping list"*, *"Check off milk"*, *"Remind me to call the dentist tomorrow at 2pm"*), food & pantry logs (*"I ate 2 eggs and toast"*, *"I bought chicken and spinach"*, *"We finished the spinach"*), existing automation controls (*"Disable my Wake up automation"*, *"Run Bedtime now"*), calendar events & timers (*"Add dentist Tuesday at 2pm to my calendar"*, *"Set a 10 minute timer"*), weigh-ins, fitness goals & nudges (*"I weigh 178.4 lbs"*, *"Set my protein goal to 160g"*, *"Nudge me to stretch at 3pm"*), and admin controls (*"Switch to private mode"*, *"Run a backup now"*, *"Check my email"*), refreshing the Home tiles automatically.
- **Brain Graph Visualizer (`Memory › Graph`):** interactive force-directed view of your MemGraph personal memory graph with node/edge inspection, stability & confidence indicators, search/filter controls, and one-click confirm/dispute/remove actions on learned connections.
- **Turnkey small-LLM training (`Tools › Training`):** one-click dataset export and LoRA fine-tuning for Control, Persona, and MemGraph models via a local GPU worker (`tools/trainer_worker.py`) or Google Colab (`tools/train_memgraph_lora.ipynb`).
- **Weekly meal plan reliability fix:** automatically chunks multi-day meal plans (≤ 6 meals per call) with compact JSON output and retry splitting so 7-day × 3-meal plans no longer hit Home Assistant AI Task output token limits.

## 0.32.0
- **2-step Weekly Meal Calendar Picker & Smart Shopping List:** the Food tab now has an interactive **1 · Pick → 2 · Plan** weekly calendar. Pick your effort brush (⚡ Quick, 🍳 Normal, 👩‍🍳 Big cook), tap meals/days/columns, skip dining-out slots (automatically pre-filled from your synced Home Assistant calendars), save **My usual week** or reuse **Last week** (aligned by weekday), and choose a cooking style (**Fresh**, **Cook once, eat twice**, or **Prep day**). In **2 · Plan**, matching batch colors link cook sessions to leftovers, **↻ swap** / **✕ skip** / **+ add** update the plan in place, and the aisle-grouped shopping list subtracts fresh in-stock pantry items and avoids double-counting batch ingredients before sending to Home Assistant's shopping list. Also available by voice or the Ask bar.
- **MemGraph personal memory graph:** adds a neuro-symbolic memory graph with signed stability, canonical relation rules, 1-hop safety expansion (e.g. allergies), automatic live sensor inputs (watch sleep/steps/HRV, bedroom climate, Home Assistant lights/thermostats/fans, and logged workouts), background Archivist ingestion with an automatic Overnight fallback when no dedicated `archivist_model` is set, and MemGraph allergies/dislikes/learned tastes wired into Pantry meal planning.
- Fixes a midnight-hour rounding quirk where items expiring today briefly showed "1 days left" instead of "use today".

## 0.31.8
- Food lookups favour the USDA food a name leads with ("oatmeal" finds "Oatmeal, regular or quick, made with water",
  not "Bread, oatmeal"), with dishes as eaten slightly ahead of lab entries. Portion lists drop USDA's bare code
  numbers (a portion called "90000"); the as-eaten foods are reloaded once to clean them out.

## 0.31.7
- USDA's as-eaten foods (FNDDS) load at last: that release writes the older SR nutrient numbers (208 for energy, 203
  for protein) in place of ids, and those are now read too. File names inside USDA's zips are matched without regard
  to folder or capital letters, and the load report lists each zip's files.

## 0.31.6
- The food database now really loads USDA's as-eaten (FNDDS) and Foundation foods: their nutrients are matched
  through the release's own nutrient list (by id, or by the older SR number) instead of fixed ids only, so a release
  that numbers them differently still loads. Both were being skipped (Foundation since the database was first
  built). They're added to the existing database on their own; portion weights are no longer doubled when a set is
  loaded again, and the status shows what each file held and what was kept.

## 0.31.5
- Meal ideas on Home come as cards instead of a block of text: the name, protein and calories as chips, one line on
  why, what it uses from the pantry as pills (anything to buy dashed), the steps folded away, and buttons for "I'm
  making this" (logs it as a meal) and adding what's missing to the shopping list.
- Food lookups always weigh USDA's lab and as-eaten foods, not just the 400,000 branded products that used to crowd
  them out ("bowl of chili with beans" finds the dish, not a can of chili beans). The food database's status lists
  how many foods of each kind it holds and how many portion weights, and the survey foods are loaded again with a
  reader that copes with a byte-order mark.
- `tools/convert_recipes.py`: works out per-serving nutrition for a recipe set without it (RecipeNLG) from the food
  database, and compares the result with a set that has real numbers. For a spare computer, not Home Assistant.

## 0.31.4
- Meals now carry carbs, fiber, sugar and fat as well as protein and calories. The estimate uses the food database,
  which adds USDA's FNDDS survey foods (dishes as people eat them, such as chili with beans or cheese pizza, with gram
  weights for "1 cup" or "1 slice"). An existing food database gets the survey foods (about 64 MB) added on its own, without
  downloading the rest again.
- The Food tile on Home shows the two nutrients furthest off for the time of day (behind on protein or fiber, ahead
  on sugar, carbs, fat or calories); opened, it shows all six with what's left. Goals for carbs, fiber, sugar and fat
  on the Health tab; without them fiber is 30 g, sugar at most 50 g, and carbs and fat follow the calorie goal.
- Today's meals logged before this are estimated again (`POST /api/pantry/estimate`) so they count too.

## 0.31.3
- Readings under Energy, Fog and Ready can be fixed: tap Sleep, HRV, Wakeups or Steps, type the right number and
  Save. A fixed reading is marked "edited", stays over what the watch says, and "Undo edit" puts the watch's back.
  A day with no sleep reading offers to add it (`/api/day_edit`).
- Bigger close buttons on Home's windows and on the answer under the ask bar.

## 0.31.2
- Home always shows four tiles: when the usual ones have nothing to show (no to-do list with due dates, no locks),
  Bedroom, Last night or Tomorrow fill in. On a phone the tiles grow to fill the screen below the status tabs.
- Calendar titles on Home drop the date filed with them ("Stop the Bleed", not "Stop the Bleed (Saturday Nov 21
  2026)"); untitled events show as Busy. "1 meal", not "1 meals".

## 0.31.1
Better answers from the Home bar, meal ideas wired to your pantry, multi-turn conversation history, and Home Ask bar UX improvements.
- **Meal & recipe ideas from the ask bar and voice** (`pantry.py`, `main.py`): questions like "What can I make with the large eggs?" (including the Home screen's **Meal idea** button) now route to `Pantry.suggest()` using what's in stock, expiry dates, goals, and today's energy, formatted for screen or voice. Pantry inventory is also injected into `Mind.context_for()` for food and fridge questions.
- **More natural open-ended answers & multi-turn history** (`mind.py`, `main.py`): open-ended asks (ideas, recipes, how-to, walkthroughs) can use general knowledge alongside your notes without inventing personal facts, and up to 4 recent turns are passed per conversation (cloud-safe and vault-masked, skipped for guests) across quick answers, meal suggestions, and connected answers.
- **Connected answers `numbers_ok()` fix** (`connect.py`): leading zeros (`05` vs `5`), simple rounding (`41.4` → `41`), and `6 h 30 min` → `6.5` hours are accepted so valid connected answers aren't silently discarded back to the quick path.
- **Home Ask bar UX** (`talk.py`, `web/index.html`, `hub.py`): shows the question you asked above the answer, adds a collapsible recent thread (`Thread (N)` / `New`) with a quick history toggle in the bar, clickable Memory source pills, a one-tap `+ Add … to shopping list` chip and `More meal ideas` button on meal replies, side-by-side connected-answer comparison (`/api/answer_pick`), and fixes `"1 note to check"` singularization.

## 0.31.0
A new Home page that runs the day from one screen, and check-ins by time instead of once a day.
- **Home** replaces Today and Talk (`hub.py`, `/api/hub`). Log bubbles (water, coffee, meal, note) sit above the ask
  bar and fill in the start of a sentence; the answer grows under the bar, with the choices as small buttons when it
  isn't sure ("Which desk light?", Yes / No). Energy, Fog and Ready are folder tabs whose details (seven days, tap a
  day; what went with it, each with a note on what to do and a tag) open right under them. A For you card shows one
  real thing at a time (a check-in that's due, food at its date, notes to check, an automation idea, a door unlocked
  while away). Four tiles are picked by the time of day and whether you're home (Reminders, Bedroom, Food, Calendar,
  and Last night, Tonight, Tomorrow, House, Shopping in their moments); on a phone a tile opens as a window, on a
  tablet or computer the tiles are open. The weather pulls down from the top.
- **Check-ins by time** (`checkins.py`, `/api/checkins`): log fog, energy, tags and a note as often as it changes, for
  now or earlier. Fog ratings still feed the patterns (the day's latest), and each check-in answers the day's
  good / low / rough. Water counts and coffee as a drink with caffeine (`/api/log`).
- Reminders and the shopping list on the tiles come from Home Assistant's to-do lists, tick off and add there
  (`/api/todo`). Navigation is Home, Health, Food, Memory and Settings on a phone; Room, Worked out, Tests, Models and
  Updates stay in the side bar on a computer and under Settings.

## 0.30.0
Design phases 7 to 15, the fixes from ten expert reviews, answers that connect your own data, and Talk in the panel.
Highlights: "why am I so gassy lately?" or "how is my health looking?" are answered from a brief of your readings,
check-ins, symptoms, medicines, meals and calendar (set `deep_model` to a stronger model for the best answers); a
Talk view with the mic, a kept thread and tap-to-answer buttons; red flags checked before everything and said word
for word; learned answers, the forget list and weekly test restores, channels with their own limits, rooms, things
and scenes, trackers and the medicines log, experiments, and other assistants as guests.
- **Talk** in the panel (`talk.py`): a conversation by voice (the browser's speech recognition, answers read out,
  optional hands-free) or typing, one thread per person kept on the server, follow-ups understood, and the answers
  Home Mind expects shown as buttons (Yes/No, the check-in, "Which one", "A or B?", follow-ups after a connected
  answer), with a text box always there. Voice turns now get connected answers too.
- **Connected answers** (`connect.py`): "why am I so gassy lately?", "how is my health looking?", "is my schedule
  really busy?" are answered from a brief code gathers (the issue and its general causes, labelled general, with the
  offline Wikipedia passage; every time you mentioned the symptom; readings against your usual; check-ins; new
  medicines; the foods that matter for it; the calendar load; your own pattern once there's enough data), connected by
  a model (`deep_model`) that may only use numbers from the brief. Symptoms you mention in any request are logged.
  Known issues and your or your doctor's suspects (`/api/issues`). The quick answer comes beside it in the panel with a
  tap to say which was better (`compare_answers`, `/api/answer_pick`).
- **Review fixes** (ten expert panels, `claude/review-2026-11-panels.md`): red flags are checked before every shortcut
  (money, "where is", the medicines log), said word for word (never restyled by the persona) and on other assistants
  too, and catch passive crisis phrasing, seizures, blue lips, sudden numbness and a child swallowing pills, with
  fewer false declines ("I can't get out of bed today" is a crash, not a decline); the safety contact is told once in
  6 hours. The experiment safety stop reads each person's own check-in (never the "asked" marker), counts washouts
  and the running block, and stops a harmful change; plans need 6 pairs and start the Monday after the real start.
  One rough day after a calm stretch no longer says "see your doctor", and the alarm allows for days carrying over.
  A guest token can't claim a speaker. The forget list only re-deletes notes from before the deletion, matches sealed
  notes, and scrubs on every re-apply. Learned reflexes never act on a request for later or on the opposite on/off.
  Back-off is wired, and never stops a safety call. A tap check-in never overrides someone's rough day; "good
  morning" isn't a check-in and "fine, just tired" is low. The medicines log skips questions and food, knows brand
  names, and logs a dose and a check-in said together. Email senders are checked against the receiving server's own
  header (`email_authserv_id`); a Telegram group speaks as the member who wrote. A shared key never shows or replaces
  someone else's personal note. The proposals batch runs (it read the wrong config key). The significant-events
  checklist moved to `/api/significant`. Replay errors count as wrong; a kind is earned on its interval, not a point
  estimate; "about usual" fits each reading's own spread.
- **Design checks** on phases 8 to 15 (`tests/test_design_checks.py`), and the fixes they called for: a personal
  note, an email or a document never makes a thing link or a thing suggestion; a purge also clears learned answers,
  their weekly saves, the outbox, the surprises queue and tracker details; learned answers are replay-gated even
  before any corrections exist; travel mode only switches an input_boolean or a switch; scene previews are undone
  after a restart; a change agreed while Home Assistant is down is applied when it's back; the heart-rate sensor is
  read on its own instead of every state every 20 seconds.
- **Front ends and views** (design phase 15): other assistants as guest front ends (`other_assistants`: open-level
  only, nothing private, safety replies to the phone, off per person and in guest mode, never teaching templates);
  normal, quiet and plain registers with a plain voice switch; the four plain views (`/api/plain/...`); Firefly III
  totals for their owner, said by code.
- **Experiments and proposals** (design phase 14, steps 30 and 32): written ABAB plans for reducing load, sleep or
  meal timing only (never medicines or supplements), agreed on a clear day, random order inside pairs, washouts,
  crash episodes per block as the safety outcome (open decision 9's definition), an exact randomisation test, a new
  medicine ending its pair; gaps seen 3 times turned into proposals, cheapest fix first, with optional Opus drafts
  through the Batch API from the schema and made-up data only (`proposals_model`). Arrive and leave nudges now see
  people coming and going.
- **Trackers and nudges** (design phase 13, step 28): up to 3 trackers in the ask cap with one-word logs; a medicines
  log (tracking only, "You logged that at..." inside its window); a weekly significant-events checklist (unknown,
  never none); nudges for your own goals that stop after 3 ignored; live exertion zones, off by default, with the
  clinical safeguards; travel mode.
- **Things and scenes** (design phase 12): things as an item type with typed links (about, in, part of, belongs to)
  from Home Assistant's devices or your yes; look-alikes proposed for merging; renamed devices keep their thing;
  "where's the drill?" follows the links; links filtered by who's asking. Scenes drafted from a description (never
  heaters, heating, locks, doors or covers), previewed for 10 minutes and undone unless kept (`/api/things`,
  `/api/scenes`).
- **Rooms and conflicts** (design phase 11): device access by room and group with the most restrictive winning, and
  personal spaces only their person can open up; a room model kept in memory only (who was heard where, which rooms
  have movement) that decides "on your own"; "where is <person>?" is never answered; Home Mind's automations back off
  a device for 3 hours after a person undoes it; one surprises and gaps queue (`/api/surprises`); settling what
  several people want from one device.
- **Channels and household rules** (design phase 10): Telegram groups answer only listed people; email is read only
  when the mail server vouched for the sender (`email_require_verified`); limits per channel (Telegram, email, MCP);
  nothing from email or documents becomes a learned answer; guest mode (`/api/guest`); a monthly spend cap on cloud
  models (`monthly_spend_cap`); new behaviours agreed on a clear day take effect 24 hours later (`/api/consent`).
- **Behaviour change:** "install and switch on" for an automation now installs it switched off and switches it on 24
  hours after a yes on a clear day.
- **Keeping things sound** (design phase 9): an outbox for phone messages (sent once, retried until accepted, given
  up on when too late, held ones kept across restarts); a forget list that re-deletes what you deleted for good after
  any restore (fingerprints only, mirrored in a file so a restore can't roll it back); one check of every store at
  startup and nightly (`/api/check`); a weekly test restore; weekly saves of learned records with a change log, a daily
  cap and rollback (`/api/learned/weeks`, `/api/learned/rollback`); after a model change, trusted learned records and
  earned kinds go back on trial (step 35).
- **Learned answers** (design phase 8). A question a model answered well from one note (cited, not weak, not
  corrected within 15 minutes) becomes an answer template; once trusted it is answered with no model call, only while
  its note is unchanged, never for sealed notes, never with a number or "not" in the question, personal notes only to
  their owner. 1 in 20 audited; a "that's wrong" counts against it; promotions at night, gated by replaying your
  corrections; house and answer promotions take turns (one kind of change a night). `/api/learned/answers`.
- **Tie-break** (step 12): when two trusted templates or answers fit almost equally, the small local model (only if
  awake) picks one; how often is counted.
- **Health questions are answered from your watch's readings.** "How's my health looking today?" now gives today's
  readiness, sleep, HRV and resting heart rate, each against your usual (the median of the 28 days before), plus fog
  and your check-in. Before, it went to a model with whatever notes the search found (a fog reading, the scale's
  BMR) and answered about the wrong data. Only descriptive (higher, lower or about usual), no advice.
- **"What's my heart rate?"** uses a live heart-rate sensor in Home Assistant if one was updated in the last 30
  minutes; otherwise it says there's no live heart rate and gives this morning's resting and overnight low.
- Health readings count as personal: an unknown voice gets no numbers, and by voice they're spoken only when you're
  on your own (otherwise sent to your phone).
- **No "Just a second." in the phone app's chat:** the phone's Assist has a device too, so the acknowledgement now
  goes only to voice satellites (Home Assistant's satellite_id, where your version sends it).
- **A house request just after the check-in question is a house request again.** For 15 minutes after "good, low or
  rough today?" (by voice or the morning notification), 0.29.1 took "set the fan to low" as a low day and "ok, turn off
  the kitchen lights" as a good one, and did neither request. A reply that names a device or a house verb is now never
  taken as the check-in answer.

## 0.29.1
Fixes from the first real conversation with the Home Mind voice agent:
- **The check-in understood more answers:** right after "good, low or rough today?", a short reply with the word in it
  counts ("I would say it's about goodish", "pretty much fine", "not too bad"). It was asking which device was meant.
  Questions and long replies still don't count.
- **No more "Do take it gently" on every answer:** a remark about how you're doing comes at most once every 3 hours per
  person, and never on a question back. Remarks about the news in an answer (a new best) are unchanged.
- **Short answers for many devices:** "turn off the lights" says "Done: 6 lights off." instead of naming every light;
  one or two keep their names. A light group (Bedroom Lights) in the request is switched on its own, not alongside each
  of its members.
- **"Let me check." only on a voice device:** the acknowledgement fills silence on a speaker; typed in the app it ran into
  the answer.

## 0.29.0
The design's version 1 and the first phases after it (#30–#36), brought onto 0.28 in one piece. Where they met 0.27–0.28:
- **Several actions in one request follow the safety ladder:** "lights off and lock up" turns the lights off now and
  sends each lock to your phone for approval (a spoken yes no longer covers locks and garage doors); devices you set to
  "ask me first" still wait for one spoken yes together. Several actions planned by the cloud model work the same way.
- **Order for house requests:** the rules (no model) first, then a trusted learned template, then planning by model
  mode. Every route goes through the same checks, and every action is logged with its Home Assistant context ID.
- **Jeeves styles every answer** from the shared answer path, so the voice agent and the Ask box sound the same.
- **Replay and the nightly gate** plan the way do() does, rules included.

- **Decision log** (phase 1, #30). Every action taken or refused is logged with how it was decided and its Home Assistant context ID. Outcomes are checked (match, mismatch, unknown) and overrides spotted; devices get outcome profiles. Logged requests can be replayed through the planner. See "The decision log" in the docs.
- **Approvals** (phase 2). Locks, alarms, valves, garage doors, openers, heaters, ovens and extreme temperatures need a fresh approval on your phone (Face ID or the passcode); a spoken yes is no longer enough for them. Heating within normal limits needs a spoken yes. Per device you can choose ask me first, tell me after or just do it, never below what the code requires.
- **Device access.** Each device is none, read or act. New locks, alarms, valves, garage doors, heating, cameras and microphones start hidden. Loosening waits 24 hours; every change is announced.
- **Red flags.** Emergencies, crisis, a double dose, a big decline and dizziness on standing get fixed replies in code before any model, pointing to help. Option `safety_contact` tells someone if you choose.
- **Notification tiers.** Safety always goes out; important skips the daily limit; routine as before.
- **Automations if Home Mind stops.** Home Mind installs the watchdog: after 3 missed heartbeats Home Assistant runs them. Home Mind no longer takes them back by itself; use "Take automations back". Missed times are caught up once if under 30 minutes late and safe to repeat; a restart never runs a time twice.
- **A limited Home Assistant user** (option `ha_user_token`) for control and readings, and a guard that refuses lock, alarm and valve calls that didn't pass the approval checks.
- **Daily safety suite**: `sensor.home_mind_safety_suite`, `GET /api/safety`.
- **The check-in** (phase 3). Good, low or rough, once a day: asked by voice the first time you speak, as three buttons on the morning notification, or on the panel. It sets the day's routine asks (good ~4, low 1, rough 0, unknown 1) and holds down energy left on Today. A rough day keeps every device quiet except safety and what you just asked for.
- **Undo, why, help me.** "Undo" puts back the last thing you asked for in the last 10 minutes, exactly as it was; again steps back further. "Why did you…" answers in one sentence from the decision log. "Help me…" walks you through without doing it.
- **Private answers by voice** are spoken only when you're the only one home; otherwise they go to your phone (`notify` per person under `people`). Slow voice answers go to your phone after 6 seconds.
- **Two times per fact:** `/api/as_of` answers what was true then, or what Home Mind believed then.
- **Who is asking.** The panel answers as the signed-in Home Assistant person. **Read and write tokens not tied to a person are now guests** (household notes only): make one per person for Claude or other apps. Tokens expire after 90 days unless made with `days: 0`; tokens made before keep working.
- **Measuring** (phase 4). A scorecard in three sections, never averaged: house (a frozen v1 set of your real requests replayed through today's planner), memory (your corrections) and safety (the daily check), each with a 90% interval and a pass, fail or unclear verdict. The v1 set is replayed weekly, with a notification if it gets worse. A shadow baseline (option `shadow_rate`, default 1 in 10) has the other model plan in the background, never acting, scored win/tie/loss by what happened, with cost per correct answer. A weekly labelling queue (about 15 minutes, with repeats for agreement and a gold set of 30 every quarter), burden per day, and response times by front end. All admin-only API; nothing is shown on Today.
- **Home Mind as a voice assistant** (phase 5). The bridge (0.3.0) adds a Home Mind conversation agent: Home Assistant's own commands first ("Prefer handling commands locally"), then Home Mind, with "One moment." after 1.5 s and the answer streamed a sentence at a time. New `/api/converse`. A Claude link per person (`home_mind_bridge.person_link`, `?token=` on the link). Routine notices are held for the morning summary.
- **Model modes, confidence, the now board, the worsening alarm** (phase 6, after v1). Option `model_mode`: cloud, balanced (default) or private. **In balanced mode the local control model now earns each kind of request** (95% right over 20 requests in 10 wordings, your yes, a passing replay) before it acts alone; until then the cloud model decides and the local plan is kept as a comparison. Set `model_mode: private` for the old local-only behaviour. Every house plan gets a confidence; below `confidence_dial` Home Mind asks the cloud model or asks you. The now board (who's home, the day, what was just done, waiting approvals) leads every answer's brief. A trend alarm on rough days and fog (about one false alarm a year) says once, calmly, when to see the doctor. `local_model` answers in private mode.
- **Learned house requests** (phase 7). Requests that worked become templates (one device, one action, where the number goes). They start in shadow, earn trust only from checked outcomes (sceptical prior, judged on the low end of the interval, fading), are promoted at night only if a replay of the scorecard doesn't get worse, and then act as reflexes without a model call. Never for anything that needs a yes; 1 in 20 is audited; two failures put a template back on trial. `/api/learned`.

## 0.28.0
Fitness goals, built on the scale, meal and watch data Home Mind already collects.
- **Goals as numbers:** target weight (and a date), calories, protein (or automatic from body weight, 1.6 g/kg), steps, workouts a week and body fat. Set them on the Health tab, with `POST /api/fitness/goals` or the `mind_fitness` tool. Calorie goals below 1,200 are refused.
- **Progress against them:** a 7-day average weight (daily swings are water), the weekly rate from the last 28 days, the date you're on pace for, and whether that's on track for your date. A date that needs more than about 1% of body weight a week is flagged.
- **The day's tally:** protein and calories eaten so far against the targets. The evening brief says how far short you are and names protein foods that would close it, the ones in the pantry first.
- **Workout log:** say or type "squats 3x8 at 185", "bench 3 sets of 5 at 135", "ran 3 miles in 28 minutes" or "30 min yoga". Home Mind files it without a model and answers with last time, whether it went up and any new best (estimated one-rep max). Logged by voice, a workout is personal to the speaker.
- **Stalled lifts:** a lift with no gain over its last three sessions (3+ weeks) gets a nudge: a lighter week, or different reps.
- **Weekly review** in Sunday's evening brief (`GET /api/fitness/review`): weight against the week before, protein days on target, average calories, workouts against the goal, new bests, steps and sleep, and one suggestion.
- **Targets that adjust:** if weight has held for three weeks while you ate to target, it suggests about 150 kcal less; losing too fast, 150 more; aiming to gain and not gaining, 150 more. Suggestions only: nothing changes until you accept one, and it never suggests under 1,500 kcal.
- Questions like "am I on track?" or "what did I bench last time?" get the fitness numbers in the answer. The morning brief shows weight against the goal and uses a set protein goal.
- Fitness lines in the briefs only appear once a goal is set.
- **Sleep in words:** "Slept from 12 to 7am last night", "went to bed at 11:30 and woke up at 6:45" or "slept 6.5 hours" typed or said to Home Mind is filed as last night's sleep and replaces the watch's reading. Before, it was treated as a question and answered with an old sleep note.
- **Other ways of saying it:** the pieces are found in any order and wording. Examples: "crashed at 12, up at 7", "in bed 12-7", "dozed off around 11:30, woke 6:45", "slept like 6 hours", "did 3 sets of 8 squats at 185", "squatted 185 for 3 sets of 8", "went for a 3 mile run", "did yoga for 30 minutes", "walked for an hour". Number words work too ("five sets of five", "half past twelve"). Plans, habits and questions ("I need 8 hours", "going to bed at 11", "remind me to run") aren't logged, and nor is "last night" without sleep ("watched TV for 2 hours last night").
- **Anything else, in any language:** when a message clearly mentions sleep or exercise but can't be read, the model rewrites it into the plain form and it's logged ("read as: …" in the reply). The rewrite may only use numbers that were actually said, or nothing is logged.
- **The phrasebook: Home Mind learns how you say things.** A list of odd wordings for sleep and workouts, each with the plain line it reads ("zonked at 1, kid woke me at 5:40" → "slept from 1 am to 5:40 am"; "watched TV for 2 hours last night" → not a log). It builds itself four ways:
  - **Starter set:** 56 checked cases, so it works from day one.
  - **Learned from you:** when the model reads a message the built-in reading couldn't, the pair is kept.
  - **Corrections:** right after a log, "no, 6 hours" fixes it and "that wasn't sleep" removes it. Either way that wording is read correctly from then on.
  - **Grown overnight** (option `phrasebook_grow`, on): the overnight model writes new hard cases. One is kept only if Home Mind can read its line, it adds no number that wasn't said, and a second blind reading agrees. Disagreements wait for **Right / Wrong** on the Health tab. It also counts the cases the built-in reading gets wrong.
  It's used two ways. A message with the same shape as an entry (same words, new numbers) is read instantly with no model. Otherwise the closest entries go into the model's prompt as examples, which is what lets a small model on modest hardware copy the pattern. **Test the model** scores a model on the phrasebook (right, wrong logs, missed), so you can see whether a small local model is good enough. `GET /api/phrasebook/export` gives the pairs for training. Also `/api/phrasebook` and the `mind_phrasebook` tool.
- **Jeeves, with emotions:** answers come in a persona's voice (Jeeves by default: an English butler, composed and dry). Each one carries an emotion marker chosen from what happened: alert for locks and an unlocked door, proud for a new best, sympathetic on a tired day, corrected after "that's wrong", amused when asked again, apologetic when it doesn't know, and more. Styling may add words but never change facts: numbers, "not" and "Say yes" must survive or the plain answer is used. You can set the name, how it addresses you, the wit and SSML on the System page. It also gives instructions for your Home Assistant voice agent, so the agent's own words match. The answer also returns `emotion`, `plain` and optional `ssml`. This is rule-based for now; a trained style model can take over through the same step and the same check.
- **Home Mind Voice (new app, 0.1.0):** Jeeves's voice from Voicebox on another computer (one with a GPU), with each answer's emotion as a delivery instruction, and Piper whenever that computer is off, slow or restarting. Home Assistant sees one Wyoming voice. Home Mind adds `GET /api/persona/emotion` so the voice gets the exact marker an answer was given. `tools/voice_check.py` times and saves one line per emotion.
- **A GPU computer as a model server:**
  - Models on a computer that sleeps or restarts are checked every 30 seconds and skipped while it's away, so the next model answers at once. A computer that can't be reached now fails in 4 seconds, not after the whole timeout.
  - The Models page shows each computer as up or away.
  - New Ollama settings `num_gpu` (0 = on the CPU, leaving the GPU to Voicebox), `keep_alive`, `num_ctx` and `num_thread`, plus `connect_timeout` for any model.
  - New option `persona_model`: a model on that computer rewords answers in Jeeves's voice, with the same fact check, and the rules take over if it's slow, away or changes a fact.
  - The docs give a starting setup for an 8 GB GPU shared with Voicebox.
- **Commands said in a room:** with the voice device's room known (Home Assistant areas), "lights off" means that room's lights and "turn off the light" means its only light. A named room or "all the lights" still wins, and anything unclear works across the house as before.
- **The database's safety net:** a daily snapshot (the last 7 kept). At startup a damaged database is moved aside, the newest good snapshot is restored, and you're told from when.
- **Training sets:**
  - **Jeeves style model:** `tools/train_jeeves_style.ipynb` trains Qwen3-0.6B. Gemini writes Jeeves's versions of a made-up practice set downloaded from the Persona card, only versions that pass Home Mind's own fact check are kept, and the model is saved as a GGUF for Ollama to use as `persona_model`.
  - **Phrasebook → FunctionGemma:** the house-control training file now includes the phrasebook, with two new tools, `log_sleep` and `log_workout`. Once retrained, the small local model reads odd sleep and workout wording before any big model, through the same checks.
- **The router:** when no model is named, simple lookups go to a local model (the GPU computer) first, harder questions (why, compare, plan, summarise) to the cloud model first, and private ones (health, money, personal notes) local first. A computer that's away goes last. A local "I don't know" despite good notes gets one more try. Before, a question with no model named (the Ask box, Telegram) got only the best matching note, never a model answer, which is why "Slept from 12 to 7am" came back as an old note.
- **Fixed: "I don't know" was never recognised.** The pattern for it began with a stray control character instead of a word boundary (since 0.16), so an answer saying it didn't know could still list sources. It now matches.
- **Data answers:** questions about your own numbers are computed from your saved data and written from a template, with no model doing the arithmetic. This covers sleep, bedtime, readiness, steps, weight, body fat, protein, calories, workouts, lifts and distance, over last night, this week, last month, "in October", the past N days and more. Examples: "average sleep this week?", "how many workouts in October?", "best squat?". With no data it says so. Anything that isn't one measure over one period still goes to a model.
- **An alert when a model computer is away:** if the GPU laptop (or any computer your models run on) hasn't answered for `away_alert_hours` (3 by default), you get one notification saying what's answering meanwhile, and another when it's back.
- **Jeeves everywhere:** Telegram replies go through the persona, as Ask-box and voice answers do. The morning and evening briefs get his opening and closing lines ("Good morning, Yitz." … "Do take it gently today." on a tired day), with the facts in between unchanged.
- **Per-person persona:** how Jeeves addresses each person, the wit and on/off can differ per person, while his name stays the house's. Use the **For** selector on the Persona card.
- **Sleep goals:** target hours and a bedtime. The weekly review counts the nights that met them, the evening brief carries the bedtime, and a mostly-missed goal brings a wind-down suggestion.
- **Recent logs on the Health tab:** the last 30 days of sleep and workouts, each with **Edit** (kept on the same day, the old one archived) and **Delete**. Before, a log could only be fixed right after it was made.
- **Fixed: days ended at midnight UTC.** The image had no time zone data, so the time zone Supervisor passes was ignored. After 7 PM in New York a meal counted for tomorrow, and "last night" and the weekly review could be off by a day. The image now includes tzdata, Home Mind applies Home Assistant's own time zone at startup, and the Overview warns if it can't.
- **Watch zeros aren't readings:** a readiness or sleep of 0 (watch off or not synced) shows as "–" and no longer makes a "tired day". Yesterday's fog rating no longer counts as today's.

## 0.27.0
- **House commands without a model.** Plain commands are read directly, instantly and on any hardware (option `rule_planner`, on). Examples: "kitchen light off", "den lamp to 40%", "dim the den lamp a bit", "close all the blinds", "movie night". A name that fits several devices gets "which one?". Anything uncertain (questions, schedules, "everything off", vague requests) still goes to the models.
- **Several actions in one request:** "lights off and lock up", "kitchen light off, porch light on, then run goodnight", "all the lights off but leave the hall on". The ordinary actions run at once. Locks, alarms and garage doors among them wait for one "yes" together. The general model can also plan several actions (`{"actions": [...]}`).
- **One request counts once** against the 20-actions-a-minute safety limit, so "lights off" in a house with 12 lights no longer trips it.
- **Name matching** (rules and local model): every word you say must be in the name. An exact name wins over longer ones ("kitchen light" vs "kitchen counter lights"), several equally good names get "which one?", and a partial match is no longer offered as a guess.
- **Control accuracy test** (`tests/control_bench.py`, in CI): a sample house with overlapping names and 139 labelled requests. 40 of them were written after the rules were tuned and are kept out of tuning. Rules: 100% on the tuned set, 90% right on the held-out set, never a wrong action. `--llama URL` scores a local model on the same requests.
- **Control model app 0.1.1:** no watchdog while it waits for the model file. Before, Home Assistant restarted it over and over until the file was copied in.

## 0.26.0
- **A small local model for house control.** For Home Assistant on modest hardware (an old laptop), a 270M-parameter FunctionGemma fine-tuned on your own devices can turn "dim the den lamp to 30" into an action on the CPU, without a cloud model.
  - `GET /api/control_model/dataset?part=train|test` writes training data from your control list. Add `&paraphrase=1` to have your normal model add more varied wording, kept only when the device name and numbers survive.
  - `tools/finetune_functiongemma.ipynb` trains it in Google Colab on a free T4. It scores before and after on wording and devices held out of training, and exports a GGUF for `llama-server`.
  - Option `control_model` sends house requests to it first. It only proposes one tool call from a fixed set. Home Mind matches the device name against the control list, asks "which one?" when several match, and applies the usual checks: locks and garage doors still need a yes.
  - Questions and multi-action requests (`not_a_command`), names it can't place, and a sleeping laptop all fall back to your normal model.
- `/api/do` planning is split into the model step and one shared set of checks (`_run_plan`), so every planner goes through the same safety rules.

## 0.25.1
- 0.25.0 couldn't be installed over an existing setup: three new options (homebox_url, paperless_url, web_search_url)
  were typed as URLs with an empty default, which the Supervisor refuses. They're plain text now (the app checks them
  itself), and a new test checks every default against its type the way the Supervisor does.

## 0.25.0
The rest of the roadmap, built overnight as a stack of ten pull requests (#12–#21) and brought onto this version in one
piece. Their own version numbers clashed with 0.23–0.24.1 here, so they're listed below under the set they came from.
Also in this version: the Home tab, meal protein estimates and the routes added in 0.24 now follow the new token scopes
and per-person rules.

### Expert review (six rounds) (built as 0.32.0 on its own branch)
Six rounds of expert code review (24 reviewers: security, privacy, concurrency, search, data integrity, LLM safety, Home Assistant, web panel, performance, overnight learning, network integrations, API contract, cryptography, time, asking, test quality, MCP, documents, maintainability, storage growth, red team, voice, packaging, first run). Each fix has a regression check in `tests/test_review_*.py`, run in CI.

**Behavior you'll notice**
- Locks, alarms, valves and garage doors (and scripts, scenes or switches that open them) need confirmation. By voice, Home Mind asks and a "yes" within 30 seconds does it. Drafted automations that open the house need the PIN in more cases.
- A token tied to a person only sees and changes that person's personal notes and household notes, and can't file notes as someone else. House control over HTTP needs an admin token.
- Short replies like "yes", "um" or "never mind" no longer read out a stored note. "That's wrong… never mind" no longer overwrites the fact; "No, it's 5512" updates it in one step.
- Raw house readings older than `events_keep_days` (at least 30) roll up into one daily note per thing. Database growth drops from ~140 to ~20 KiB a day.
- Shelves keep non-ASCII letters ("Küche"); a shelf already stored under its old name keeps it.
- The panel shows a getting-started card on a fresh install. DOCS now lists every option, all API routes with their scopes, and the MCP tools.

**Important fixes**
- Purging one note could delete unrelated notes, and over MCP a blank name purged everything.
- A missing vault key led to a new key that later replaced sealed secrets with "[hidden]". Home Mind now refuses and reports it.
- Secrets reached cloud models through the re-ranker, procedures, overnight passes and journal summaries. They are now masked.
- The panel had XSS holes through onclick values and model output.
- One error stopped the upkeep loop for good, and a silent mail server hung it. Emails that failed to file were lost.
- Monthly and yearly reminders drifted (30/365-day months), and journal entries were lost on the DST night.
- Filing learned from its own guesses.
- People with accented names couldn't find their own personal notes.
- Small sets of documents were never found by search.
- A forged future-dated email made a planted fact impossible to correct.

**Speed (30,000 notes):** context per question 57 → 22 ms, search 23 → 14 ms, daily duplicate check 20 s → 0.4 s.

**Tests:** two test files that could never fail now assert. Hand mutation testing went from 19 to 41 of 43 mutants killed. New CI checks: lint (AST), fuzzing of every route and MCP tool, docs coverage, and config/schema/translation consistency.

### Bug-hunting set (built as 0.31.0 on its own branch)
A bug-hunting test set (`tests/test_bugs.py`, in CI): odd inputs, many threads at once, an old database, failing and hanging models, unreadable model replies, broken files and corrupt saved state. Fixes for what it found:
- **Threads could clash on the database.** Concurrent remember and search from worker threads (documents, Markdown sync, email, backups) raised `InterfaceError` or returned broken rows, 20+ times in a 200-note run. Every statement now runs under the store's lock and reads its rows before letting go: 0 errors, nothing lost.
- **SQL could read other people's personal notes.** The read-only SQL view and saved SQL views ignored who was asking. They now show a person only household notes and their own, including links. `/api/sql` and `mind_sql` need only read access. Running a model comparison and checking email now need admin.
- **SQL could build huge values.** `zeroblob(1e9)` returned a gigabyte. Values are now capped at 1 MB, and blobs show as their size.
- **An empty model reply used up the night's conversations.** Unreadable replies are now tried again the next night (three nights at most). `{"facts": "nope"}` no longer files one note per letter.
- **An empty question got a random note** ("No model answered, so here is the best matching note…"). It now asks you to ask something.
- **Uploads over 8 MB were refused** although photos may be 12 MB. The request limit is now 32 MB, enough for photos and large imports.
- **Imports:** one broken item no longer stops the rest, and a non-numeric version is a clear error.
- **Markdown sync:** a file naming a note that was never here (copied from another memory) becomes a new note instead of being ignored forever. Files over 200 KB are left alone.
- **Documents:** a broken PDF or an empty file is a clear error instead of a crash, and a name can't be a path.
- **Notes:** over 200,000 characters, meta that isn't an object, and a time that isn't one are refused with a reason. NUL bytes are dropped.
- **Telegram:** "note:" with nothing after it no longer files ":" as a note.
- **Saved views:** tags given as text are split into words, not letters. Unreadable saved views, conversations and comparison history no longer break anything.
- **"Everything about X":** a place that holds the thing ("the garage is next to the toolbox") is no longer listed as kept inside it.

### Set 8 (built as 0.30.0 on its own branch)
- Telegram (#52): options `telegram_token` and `telegram_chats` ("chat id: name"). Ask anything, or say "remember ..." to file a note. A photo with a caption is filed with the photo. Each chat is its own conversation and asks as its person, so personal notes stay personal. Chats not on the list are ignored (except /start, which only says the chat id).
- Web lookups for current facts (#55): option `web_search` (`searxng` with `web_search_url`, or `brave` with `web_search_key`). Used only when a question is about the world now ("latest version", "open today", "price of") and the notes don't answer it. Results are dated and linked in the context. Only the question is sent, never one with a secret or a household member's name.
- Email ingestion (#64): options `email_host`, `email_user`, `email_password`, `email_folder`, `email_senders`, `email_minutes`. Mail from allowed senders becomes a note: the subject is the title, the text the note (HTML turned to text, quoted replies cut), dated by the email. PDF and text attachments go to Documents; images become photos of the note. Each message is read once.
- Prompt caching (#45), revisited: Anthropic models now mark long repeated prefixes for caching. That means a system prompt or a message prefix of at least `cache_min_tokens` (default 4,096, per model), such as the whole log in model + whole-log tests, which is sent again for every question. Cached tokens are reported. Everyday answers are still too short to be cached, as before. Turn it off with `cache: false` on a model.
- `/api/connections` shows whether Telegram, web lookups and email are on and how the last try went.

### Set 7 (built as 0.29.0 on its own branch)
- Learned settings (#30): how bright you like each light, how fast each fan, how far each blind and how loud each speaker, by time of day and weekday or weekend. Only settings a person chose count (not automations, not Home Mind). Five that agree make a preference: it is filed as a note and used when you ask to turn a light or fan on without saying how much (option `use_preferences`). `/api/preferences`.
- Reshelving over time (#32): each night, notes filed by the model (or left in the inbox) a week or more ago move when filing now clearly knows better, and say where they came from. A note you placed yourself only gets a suggestion; one you moved or kept stays put. `/api/reshelve`.
- Photos with notes (#36): add photos to any note from the Memory tab (the phone camera works). Captions become search words. Files are checked (JPEG, PNG, GIF, WebP, HEIC, at most 12 MB) and removed when no note uses them.
- QR labels (#63): print a label for a box, shelf or place. Scanning it opens the panel on everything known about that thing, with a box to add a note. Built-in QR encoder, no new package. Option `label_url`.
- Conversations across devices (#53): a follow-up on your phone right after asking the kitchen speaker ("and where is it?") knows what came before, for the same person, within 10 minutes. Live conversations survive a restart.

### Set 6 (built as 0.28.0 on its own branch)
- Export and import (#35): `/api/export` gives every note with its history, links and fields as one JSON file (secrets masked unless asked for with the vault key loaded; house readings left out). `/api/import` merges one in: notes already here are skipped, ids renumbered, versions and links follow. A dry run says what would be added. Buttons on the Memory tab.
- Two-way Markdown sync (#67): option `markdown_folder`. Every note becomes a Markdown file, one folder per shelf. Editing a file makes a new version; a new file becomes a note on its folder's shelf; deleting a file archives the note; archiving a note deletes its file. If both changed, the note wins and the file's text is kept as `.conflict.md`. Secrets and house readings are never written out.
- Read-only SQL (#70): `/api/sql` and the `mind_sql` tool run SELECT queries on two views, `notes` (secret text shown as [secret]) and `links`. The connection is read-only, every statement is checked (no other tables, no writes, no ATTACH or PRAGMA), and queries stop after 3 seconds or 500 rows.
- Saved views (#61): named searches or filters (words, shelf, kind, tags, days back, order) or a SQL query. `/api/views`, the `mind_view` tool, the Memory tab, and by voice: "show my warranties".
- Model comparison on a schedule (#34): options `compare_days`, `compare_models`, `compare_suites`. The models answer the same suites from the same seed; results on the Tests tab, at `/api/compare` and as a sensor per model. A drop of 5 points or more since the last comparison is logged.

### Set 5 (built as 0.27.0 on its own branch)
- Prefix-only matches weigh less (#25): a note that matches a question word only as the start of a longer word ("garages" for "garage") gets a quarter of the weight. The keys suite goes from 0.95 to 1.000.
- Links followed as a walk (#12, #81): from the best few results, the links are walked up to three steps (personalized PageRank). A linked note is added only when it brings question words the results don't have yet. Three-step questions ("who has the key for the place where the mower is kept?") go from 0/40 with one hop to 33/40 (held-out).
- Older notes learn words from newer linked ones (#82): when a new note names the same thing as older notes, the older notes take up to five of its rare words as extra search keys. Their text is never changed. Only notes that share a name take part, so two different "spare keys" don't swap words. "Whose scooter was stolen?" now finds the owner 40/40.
- A model re-orders the top notes (#54): option `rerank_model`. Not used for voice. A bad reply keeps the search's own order.
- Everything known about a thing (#20): "Tell me everything about the Halvor drill" gathers its place (following the chain of places), notes, linked notes, chores and fields into one list for the model.
- Other languages (#21): guesses, corrections and "X says" are read in Spanish, German, French, Dutch, Portuguese and Italian as well as English. Notes in any other language are tagged overnight by the model (a guess, a correction, who said it, when it became true), so versions, guesses and disagreements work for them too.
- Fix: "Went to check on the Quillon safe" then "Changed its code" stays linked to the safe.

### Set 4 (built as 0.26.0 on its own branch)
- Documents and manuals (#50): PDF, text, Markdown and HTML files dropped in the documents folder (option `docs_folder`, default /share/home-mind/docs, read every hour) or added on the Memory tab are split into pages or sections and kept apart from the notes with their own search index. The best two sections are added to the page after the notes, and the model is asked to name the document and page. `/api/docs` lists, searches, adds and removes. pypdf is added to the image; the add-on can now see /share.
- Learned filing (#58): a note that would land in the inbox is filed on the shelf notes like it are on when that is clear (naive Bayes over the words of notes already on shelves, no model), and otherwise gets that shelf suggested. Moving a note teaches it. Option `learn_filing`. Filing suite: 0.994 (topic notes 397/400 on the right shelf, vague notes 80/80 left in the inbox).
- Inbox review (#59): the Memory tab shows the inbox with the suggested shelf (File in …) or any other shelf, one tap each (`POST /api/item/{id}/move`, `GET /api/inbox`).
- Homebox and Paperless (#62): with `homebox_url`/`homebox_token` and `paperless_url`/`paperless_token`, questions about things and papers also ask those apps (2.5 s limit), and what they return is added to the page labelled with where it came from. Nothing is copied into memory.
- Pronoun links (#13): "Changed its code to 9002" right after a note about something else now links to the recent note about the thing that has a code (code, combination, PIN and "opens with" count as the same), unless the last note's thing has one too. Overnight, the big model re-checks recent links and moves any it disagrees with. Pronouns suite: 1.000 (0.500 before).

### Set 3 (built as 0.25.0 on its own branch)
- Scoped tokens (#27): make a token per client on the Models tab (or `POST /api/tokens`), each read, write or admin, each revocable, optionally tied to a person (answered as them, with their personal notes). read can ask and search; write can also remember, change and forget; deleting for good, installing automations, updates, controls, tokens and the vault key need admin. MCP lists and allows only the tools a token's scope covers. Only a hash of each token is kept. The main token stays admin.
- Signed pack manifests (#28): put a public key in `homemind/pack_keys.txt` and Home Mind installs packs only from a manifest signed with it (`manifest.json.sig`, Ed25519). `tools/sign_manifest.py keygen` makes a key pair, `sign` signs the manifest. With no key set, packs are checked by sha256 as before. Ed25519 is in plain Python, checked against the RFC 8032 test vectors.
- Secrets encrypted at rest (#66): with option `encrypt_secrets`, notes marked secret are stored encrypted (AES-256-GCM) with a masked copy for search; the key is in `/data/vault.key`, which Home Assistant backups leave out, and answers logged in the activity log have secret values masked. Keep a copy of the key (`GET /api/vault/key`); turning the option off opens everything again. The whole file isn't encrypted (SQLCipher isn't in the add-on image); ordinary notes stay plain. `cryptography` is added to the image.
- Off-site backups (#60): option `backup_target` folder (e.g. a NAS share in /media or /share), webdav or s3 (AWS, Backblaze B2, MinIO, R2; requests signed with SigV4). A snapshot every `backup_every_hours` when the memory has changed, encrypted with `backup_passphrase` (required off-site), the newest `backup_keep` kept. `tools/restore_backup.py` restores one with the passphrase. `GET /api/backup`, `POST /api/backup/now`.
- Comfort rooms from options (#29): option `comfort_rooms` gives each room its entities, person, body signals and helpers (no more hard-coded entity IDs), and more than one room each learns from its own votes. Existing votes belong to the first room. `GET /api/comfort/check?room=NAME` lists configured entities Home Assistant doesn't have; the comfort endpoints take `room`.

### Set 2 (built as 0.24.0 on its own branch)
- Search by meaning (#10): with option `embed_model` (one of your models, type ollama or openai, with an embedding model such as nomic-embed-text), notes are embedded in the background and a question's nearest notes are fused with the keyword results by rank, so "planter" finds "flower box". Personal notes stay personal; deleting for good removes the vector; if the embedding model is down, keyword search carries on. numpy is added to the image (a pure-Python fallback remains). `tests/longmemeval.py retrieve --embed NAME` measures it.
- Things without capitalized names (#11): a note about "the chest freezer" ranks below one about "the garage freezer" when that's what was asked. Versions and lists of lowercase things already worked (lowercase suite 0.997 with and without this rule); the rule fixes the case where the other thing's note matches the question's wording better.
- Links between notes (#12): a new note is linked to the few notes that name the same specific thing (a rare name, "the red Trek", "the side gate"). Search follows a link when the linked note has a word of the question the found note lacks, which answers two-step questions ("where is Dana's bike locked?" → "Dana's bike is the red Trek" → "the red Trek is locked at the station"). Links suite: 1.000 (two-step 0.85 without links).
- Importance (#75): safety notes (allergies, an EpiPen, the gas or water shutoff, medication, the breaker panel) count most, then identity, preferences and devices. Search leans toward more important notes slightly, and safety notes never fade.
- Categories with their own fading (#38): each note is filed as safety, identity, correction, preference, device, episode or note. Safety, identity and corrections never fade; preferences fade 3 times and devices 4 times more slowly than plain notes.

### Set 1 (built as 0.23.0 on its own branch)
- Who is speaking (#3): from the Home Assistant user (`user` or `user_id` on `/api/ask`, e.g. `{{ context.user_id }}` from an LLM tool), the device (a phone or satellite listed for one person; voice runs carry the satellite's device ID), the room (an area only one person is listed for), or the only person home. Option `people` lists who lives here; person entities are recognised with no setup. `GET /api/people` shows who can be recognised.
- Personal and household notes (#48): a note a known person files about themselves ("my", "I") is theirs alone; everything else is shared. Unknown speakers see household notes only. Calls with no speaker information (the panel, Claude, the API) see everything as before, or household only with option `unknown_caller: guest`. A note can be switched between personal and household on its card (`POST /api/item/{id}/scope`); option `personal_rule: household` shares everything. People suite: 1.000 (0.526 with nothing kept personal).
- Unknown voices (#78): codes and passwords are never read out to a voice the house doesn't recognise, and a request for every secret or for the whole memory from one gets nothing.
- Learning from known voices only (#79): once option `people` lists anyone, overnight learning skips turns from voices it can't recognise (a guest, the TV). Facts are marked with who said them, and first-person facts from a known speaker are personal.
- Extra search keys (#71): each note also files under broader words for what it names (a beagle under dog, pet and breed; a Civic under car; a dentist under doctor and appointment), from a built-in word list, and overnight the model adds other words for recent notes. The keys are searched but never shown. Existing memories are upgraded on start (the search index is rebuilt once). Keys suite: 0.942 (0.592 without keys); on the LongMemEval sample, "what breed is my new dog?" now finds "I just adopted a beagle".

## 0.24.1
- Protein today now counts what you log: each meal you log gets a rough protein and calorie estimate (from the pantry's nutrition data where it has it), and Today adds them up. Meals already logged today can be estimated with one call.
- Log a meal with when you ate it ("breakfast at 8am"); before, every meal was stamped with the time you logged it.

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
