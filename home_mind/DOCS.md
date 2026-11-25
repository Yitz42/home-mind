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

## First start

With the default options Home Mind starts with an empty memory and one model, `haiku`, which uses the Home Assistant
AI Task entity `ai_task.claude_ai_task`. If you don't have that entity, change or remove the model (Configuration ›
Models) and add your own (see **Models** below). The panel's Overview shows a **Getting started** card until the basics
are in place: a model, the bridge, and a first note.

- **No model answers** (none set up, the computer is off, a wrong entity or key): every question still gets the best
  matching note, marked "No model answered". The panel's Ask box lists each model's error; Models › Check all shows
  them too.
- **Not inside Home Assistant** (running the tests or the app on its own): house watching, phone notifications and
  the `ha_ai_task` / `ha_conversation` models are off; memory, search and the other models work.
- **Empty memory**: tell it something on Memory › Remember something, or through your assistant ("remember the wifi
  password is on the fridge"). Questions about things never filed are answered "nothing in memory matched".
- **Nothing is controlled** in the house until you choose the control list and switch controls on (next section).
- Off by default: encryption, off-site backups, Telegram, web lookups, email, the Markdown folder, Homebox and
  Paperless. On by default and worth a look: *Install new packs automatically*, *Food database* (about 450 MB, with USDA's survey foods for dishes as eaten and portion weights) and
  *Offline Wikipedia* (about 50 GB, downloaded at night): turn those two off if disk space is short.

## Controlling the house

Models can switch things only when all of these hold:

1. The Toggle helper `input_boolean.mind_control_enabled` exists and is on. Create it under Settings › Devices &
   services › Helpers › Create helper › Toggle, named `mind_control_enabled`. Turning it off stops every control at
   once.
2. The thing is on the control list, option `home_controls` (entity ids, or `domain.*` such as `script.*`). **Set this
   list yourself**: left empty it falls back to the author's own devices plus every `script.*` and `scene.*`, so on
   your house every script and scene could be run.
3. Locks, alarms, valves and garage doors also need the person's confirmation each time (`confirm: true`), and
   `/api/do` never confirms. At most 20 actions a minute; fans are capped at `home_fan_max`.

`/api/home/act`, `/api/do` and the MCP tools `mind_control` / `mind_do` need an admin token.

## Home Mind as your voice assistant

The Home Mind Bridge integration (version 0.3.0, installed by the app) adds a conversation agent called Home Mind.
To use it: Settings › Voice assistants › your assistant › Conversation agent: **Home Mind**, and turn on **Prefer
handling commands locally**. Then:
- Home Assistant's own commands (lights, timers, shopping list) are handled by Home Assistant first, as fast as ever.
- Everything else goes to Home Mind. House commands go through its planner and checks; questions, "remember…",
  "undo", "why…" and the check-in go to the brain.
- If an answer takes more than 1.5 seconds, you hear a short "One moment." first. The answer is then streamed a
  sentence at a time, so speech starts before the whole answer is ready.
- Past 6 seconds the answer goes to your phone (if Home Mind knows it), and private answers follow the "who may hear
  it" rules.
- When Home Mind asks you something ("Say yes to go ahead", "good, low or rough?"), it keeps listening for the
  answer.
- It knows who's speaking from the signed-in user and the satellite you spoke to.

**A Claude link per person.** The plain Claude link uses the app's main access and sees everything. For someone's
own Claude, call the `home_mind_bridge.person_link` action with their name (and `scope`: read, write, or admin to
also control the house). A notification shows a link ending `?token=hm_…`, once. That Claude then answers as that
person: their notes and the household's, not anyone else's. Links expire after 90 days unless `days` is 0; revoke
them under Access tokens.

**Routine notices wait for the morning.** Notices like an unusual reading are held and come with the next morning
summary ("While you were away: …"), as many as the day allows (none on a rough day). Anything held longer than a day
expires.

## What Home Mind learns from your house requests

Every house request that worked, and that nobody undid, teaches Home Mind a *template*: one device, one action, and
where a number goes ("dim the desk lamp to {n}"), with the different ways you've said it. Once a template has
earned trust, the same kind of request is done straight away, with no model call: a reflex, in milliseconds.

- **Shadow first.** A new template never acts. Every house request is checked against it as if it had: did it predict
  what was done, and did that stand? That's its evidence.
- **Trust is earned, never assumed.** It starts sceptical (as if it had been wrong 3 times out of 4), rises only on
  outcomes that were checked (never on silence), and is judged by the low end of its likely range, so a few lucky
  matches are never enough: it takes about 45 checked outcomes in a row (a month or two for something used daily). Evidence fades by half every 90 days without use.
- **Promotion is checked at night.** At 3 am, templates whose evidence clears the bar are tried: the scorecard's
  house requests are replayed with and without them, and if the score drops they go back to shadow.
- **Reflexes stay inside the lines.** A template is only made from requests that went through every check, only for
  things that need no yes (never locks, doors, alarms, valves or heating), and a reflex still goes through the same
  checks as any action. It needs the device named in full ("the lamp" could be another lamp), a close match, and a clear margin over any other template. A
  request with "not" or "don't" in it is never a reflex.
- **It keeps being checked.** About 1 in 20 reflexes is planned the usual way instead, and the template is judged
  against that. Two failures in its last 20 put it back on trial. A template for a device that's gone is retired.
- **You have the last word:** `/api/learned` shows what's been learned and why; drop a template, or put it back on
  trial, with `/api/learned/{id}`.

## Connected answers: why, lately, how am I, really
Questions that need several things weighed together ("why am I feeling so gassy lately?", "how is my health
looking?", "is my schedule really busy?", "why have I been so tired this week?") don't go to the quick path, which
reads back the few notes that share the question's words. Code first gathers a **brief** of your own data:

- **The issue** it's about: gas and bloating, stomach upset, headaches, crashes and fatigue, brain fog, sleep trouble,
  dizziness, or one you add (`/api/issues`). Each comes with its **general causes**, always said as general ("commonly
  ..."), the offline Wikipedia passage when Wikipedia is downloaded, **when to see a doctor**, and the suspects you or
  your doctor named (`{"name": "gas", "suspects": ["dairy"], "source": "doctor"}`).
- **Every time you mentioned that symptom.** Anything you say counts, not only what you log: "why am I so gassy?"
  also notes gas, today. It's kept per person (the word and the time, never the sentence), shown in `/api/issues`, and
  removed with "forget everything about" that person.
- **What changed against your usual**: sleep, HRV, resting heart rate, readiness, steps and stress the day before,
  late meals and late caffeine, compared with the middle half of your previous 28 days.
- **Check-ins, new medicines or supplements** (first logged in the window), **the foods known to matter** for that
  issue in the meals you logged (beans and lentils, onion and garlic, dairy, wheat, fizzy drinks, sweeteners...),
  against the 4 weeks before, and the **calendar load** today and this week against your usual day.
- **Your own pattern**, once there are 30 days and 5 days with that symptom: each suspect at its lag, said as counts
  ("mentioned on 4 of 6 days with a late meal the night before, against 3 of 24 other days"). A link worth testing,
  never a cause.

A model then connects it (`deep_model`, else the usual chain; in private mode, local models only). It may only use
numbers that are in the brief: code checks every number, asks once more if one isn't there, and gives the quick answer
if it still isn't. By voice it answers in two or three sentences and offers the details, and if it takes more than 6
seconds the quick answer is said instead. A guest or another assistant never gets this path, since it's all personal.

**Side by side** (`compare_answers`, on): in the panel the quick answer comes with it, and a tap says which was better
(`/api/answer_pick`; totals at `/api/answer_picks`), so the new path is judged by you. Turn the whole thing off with
`connected_answers: false`.

## Home: the panel's first page
**Home** runs the day from one screen; the other pages (Health, Food, Memory, Settings) are for data and fine tuning.
Top to bottom:

- **Weather** in the corner (now, high / low, chance of rain). Tap it and the forecast pulls down from the top.
- **Log bubbles**: water, coffee, meal and note, each with today's count. A tap puts its word at the front of the ask
  bar so you finish the sentence: "Water · 2 glasses", "Ate · chicken bowl" (protein is estimated), "Note · ...".
  Water and coffee also offer one-tap amounts, and an Undo after logging.
- **The ask bar** (type, or tap the mic): questions, things to remember and house commands, in one kept thread per
  person (the same conversation the voice assistant has, so "and yesterday?" or "yes" are understood). The answer
  grows under the bar; when it isn't sure ("Which desk light?", "A or B?", Yes / No), the choices come as small
  buttons there. Spoken questions are answered out loud.
- **Energy, Fog and Ready**: the latest check-in and today's readiness. A tap opens the details right under the tab,
  like a folder: seven days as bars (tap a day to see it), and what likely went with it (sleep, wakeups, HRV, steps,
  a busy calendar, late coffee; today also protein, water and coffee so far), each with a short note on what to do and
  a tag. On a wide screen the details stay open.
- **For you**: one thing at a time, only real ones: a check-in that's due (four hours after the last, 8 AM to 10 PM),
  food at its date, notes to check, an automation idea, or a door left unlocked while you're away (locking still asks
  your phone). The X puts it aside.
- **Four tiles**, picked by the time of day and whether you're home: Reminders (from Home Assistant to-do lists with a
  due date), Bedroom (temperature, humidity, air, devices on), Food (the two nutrients furthest off for the time of
  day, out of protein, calories, carbs, fiber, sugar and fat; all six and what to eat first when opened), Calendar (the day as a strip and what's next), and in their moments Last night, Tonight, Tomorrow,
  House and Shopping. On a phone a tap opens the tile as a window over the page (room controls, the shopping list, the
  day's agenda...); on a tablet or computer the tiles are open already.

**Check-ins by time.** Check in as often as how you feel changes: head fog 0-5, energy (low / OK / high), a few
"also noticing" tags and a note, for now or a bit ago ("30 min ago"). Each is kept with its time
(`/api/checkins`), the day's fog for the patterns is the latest one, and each also answers the day's good / low /
rough check-in. Water is a count per day and coffee is filed as a drink with caffeine (`/api/log`), so the patterns'
late-caffeine factor sees it. Tap a reading under Energy, Fog or Ready (sleep, wakeups, HRV, steps) to fix it
for that day, or add sleep the watch missed (`/api/day_edit`); a fixed reading stays over what the watch says.

## Who can reach Home Mind, and how much

- **Telegram:** the chat must be on `telegram_chats`; in a group chat, the person writing must be listed too (their
  own chat id is their user id). At most 30 messages an hour per chat.
- **Email:** only from `email_senders`, and only mail your mail server says passed its sender check (DKIM or SPF for
  the sender's own domain), since a From line alone can be forged (`email_require_verified`, on by default). At most
  50 a day. Mail is filed as notes; nothing from mail or documents ever becomes a learned answer or an action.
- **Claude (MCP):** each person's link as scoped, at most 300 calls an hour.
- **Guest mode** (`/api/guest`, or the panel): microphones muted where the satellite has a mute switch, nothing
  answered by voice except the safety phrases, and nothing kept.
- **A monthly spend cap** (`monthly_spend_cap`, dollars; models need prices set): once reached, cloud models rest
  until next month and answers come from local models or your notes. Said once. Set a hard limit on the API key too.

## Rooms, backing off, surprises

- **Device access by room and group.** Set `room:bedroom` or `group:light.upstairs` in `/api/access` as you would a
  device; a device inherits from its room and its groups, and the most restrictive setting wins, so a "none" on the
  room beats an "act" on a lamp. A room can be someone's **personal space** (`/api/spaces`): anyone can make its
  devices stricter, but only that person can open them up (after the usual 24 hours).
- **The room model** knows who was last heard in each room (10 minutes) and which rooms have movement (5 minutes),
  in memory only: no history is kept. It decides "on your own" for private answers by voice. Nobody can ask where
  someone else is: "where's Dana?" gets "I don't keep track of where people are". Where things are is unaffected.
- **Backing off:** when you undo something Home Mind just did, its own automations leave that device alone for 3
  hours (`/api/backoff`). Your own requests still work.
- **Surprises and gaps,** one queue (`/api/surprises`): a device that didn't do what was expected, an action that
  failed, a correction, a request nothing could do, a question the notes couldn't answer.
- **Conflicts:** when people want different things from one device, the person in that room wins; otherwise a number
  goes to the middle, and on/off to the careful choice (off for heat and noise, on for light).

## Other assistants, the plain voice, the four views, Firefly III

- **Other assistants** (Alexa, Gemini, Siri, someone else's Assist; option `other_assistants`, "key: name") are
  guests: lights and media only, never anything about health, money, where people are, rooms or things. A red flag
  (chest pain, a crisis, a double dose) gets its fixed reply there too, since it holds nothing private. Anyone can turn
  them off for themselves (`/api/assistants`), guest mode turns them off, and what's asked through them never teaches
  Home Mind a learned template.
- **One voice, three registers:** normal; quiet (a low day, or a room where Home Mind isn't sure who's there: no
  remarks); plain (your plain voice switch, `/api/plain_voice`, a rough day, or right after something failed: no
  persona at all).
- **The four views** (`/api/plain/today`, `asks`, `memory`, `rules`): Today with energy left first and at most 3
  things Home Mind did; Asks one at a time; Memory one topic at a time, only what you may see; Rules as the one ladder
  per device in plain words.
- **Firefly III** (`firefly_url`, `firefly_token`, `firefly_owner`): this month's spent, earned and balance, read every
  15 minutes, numbers only, said by code (never a model) to its owner only.

## Your experiments

An experiment tries one change against your usual: **reducing load, sleep timing or meal timing only, never a
medicine or a supplement** (that's for your doctor). `/api/experiments/plan` writes the plan and checks it: blocks of
at least 7 days, 6 A/B pairs by default (4 at least, though 4 can't show a difference at the usual 5% line, and the
plan says so), the order inside each pair drawn at random, a washout at the start of each block, the start set by
the calendar (the next Monday), one primary outcome (fog, readiness or crash episodes), and the smallest difference it
could see, all before it starts. It starts 24 hours after your yes on a clear day.

Crash episodes are the safety outcome, counted per block: an episode starts on the first rough check-in after at
least 2 days that weren't rough, and ends on the next day that isn't rough (your clinician's definition replaces
this). A new medicine ends the current pair and is flagged; travel days don't count; 2 or more crash episodes with
the change than without, in any pair, stop it. The analysis is exact (every way the order inside the pairs could
have fallen), and "no clear difference" is a result.

## When Home Mind keeps failing at something

Things it couldn't do or answer are grouped by topic; one seen 3 times in two weeks becomes a proposal
(`/api/proposals`), cheapest fix first: use what's already there (a note, a tracker, a scene, an automation), then an
existing app (Firefly III for money, Homebox for things you own, Mealie, Grocy, Paperless), and only then a new
module. With `proposals_model` set (an Anthropic model), a module is drafted overnight through the Batch API, only on
nights when a gate failed or a proposal is waiting, from the schema and made-up data only, as a spec for you to
review. Nothing it writes runs on its own.

## Trackers, medicines, nudges, exertion, travel

- **Trackers** (`/api/trackers`): at most 3, weekly unless you say daily, asked as routine asks (they wait for the
  morning and count against the day's asks), paused on low and rough days and in travel mode. Logging takes one word
  and keeps the time: "fog 2", "log headache". Details are asked on a good day, never as a follow-up question.
  Trackers describe; they never feed tests.
- **The medicines log:** "took ibuprofen 400mg" is logged with its time, and the reply says what it is: nobody
  reviews it and there are no interaction checks. Never dosing guidance. A second log of the same medicine within 4
  hours (or the window you set) is answered with the fact only: "You logged ibuprofen at 8:05 am."
- **Significant events** (`/api/significant`): a weekly checklist (a cold, a trip, a hard week, a new medicine, poor
  sleep, a big event). Unanswered stays unknown. They can explain a change but never silence the worsening alarm.
- **Nudges** (`/api/nudges`): only for a goal you wrote, with a trigger (a time, arriving or leaving a place after 10
  minutes, nothing logged by a time, a sensor), one message that says which goal it's for, once a day at most, quiet
  hours as always, and they stop after 3 ignored (no "Done" within 2 hours).
- **Live exertion zones** (option `exertion_zones`, off by default): the line is your clinician's
  (`exertion_threshold`) or resting heart rate + 15; a zone changes only after two readings agree; only per-minute
  summaries are kept (`zones.jsonl`), never the raw stream. On a low day, one notice at most: "Heart's up. Rest when
  you can."; none on a rough day; "not today" mutes it. They switch themselves off if a beta-blocker or ivabradine
  is logged. Heart rate misses mental and emotional effort.
- **Travel mode** (`/api/travel`): routine asks, trackers and nudges pause; the house is set to away
  (`travel_away_entity`, or `input_boolean.away`); safety and important messages still reach your phone.

## Things, and scenes from a description

- **Things:** the drill, the red toolbox, the dishwasher, the car. Each of Home Assistant's devices is one (a renamed
  device keeps its thing; the old name still works). Others come from your notes ("the drill is in the red toolbox"),
  but only after you say yes to them in `/api/things`. Notes are linked to the things they mention, and "X is in Y"
  links two things, so "where's the drill?" is answered by following the links: "In the red toolbox, in the shed."
  Look-alike names are offered for merging, never merged on their own. A link never shows a note you may not see.
- **Scenes from words:** "make the living room cozy" (`/api/scenes/draft`) drafts the room's lights and fans: cozy,
  bright, movie, reading, cool or off by fixed words, a model's draft otherwise. Heaters, heating, locks, doors and
  covers are never in a drafted scene, and every step goes through the usual checks. The first run is a preview that
  undoes itself after 10 minutes unless you keep it; a kept scene runs by name.

## New things wait for a clear day

Switching on something new (an automation now; experiments and new features later) needs your yes **on a clear
day**: a "good" check-in today, no rough day in the two days before, before 8 pm, and only one such yes a day. A yes
on another day is kept and asked again on the next clear one. After a yes, the change takes effect **24 hours later**,
and until then "changed my mind" stops it. An automation installed with "switch on" is installed switched off and
switched on that way (`/api/consent`). Safety never waits for any of this.

## Keeping things sound

- **Every phone message is sent once.** Each one goes through an outbox: the same message to the same phone isn't
  sent twice within 10 minutes; one Home Assistant didn't accept (restarting, offline) is tried again every minute
  until it's too late to be useful (safety after 1 hour, important after 6, routine after 12); one held for quiet
  hours waits in the database, so a restart doesn't lose it. `/api/outbox`.
- **Deleted for good stays deleted**, even after restoring a snapshot or a backup made before you deleted it. Home
  Mind keeps fingerprints (keyed hashes, never the text) of what you deleted for good and of the names you purged, in
  the database and in a file beside it (`forget.jsonl`, keyed by `forget.key`), and applies them again at startup,
  every night and after a rollback. Only notes from before the deletion come back to be deleted again: a note you
  write afterwards that happens to share a number or a name is kept.
- **One check of everything**, at startup and every night (`/api/check`): the database, the forget list, learned
  records pointing at something that's gone, the decision log, snapshots, the vault key, the last test restore. A
  problem is shown in Home Assistant's notifications.
- **A weekly test restore** (Sunday night): the newest snapshot is restored into a scratch copy and checked.
- **Rolling back a bad week:** learned records are saved every week (the last 8 kept); every change to them is in
  a change log; at most 25 new ones a day of each kind. `/api/learned/weeks`, `/api/learned/rollback`.
- **A new model** (the local control model, the local chat model or the cloud model changed): trusted learned
  records go back on trial and act again only after the nightly replay passes; kinds the old local model had earned
  are earned again.

## What Home Mind learns from your questions

The same way, for questions. When a model answers from exactly one note, the match wasn't weak, and nobody said
"that's wrong" in the next 15 minutes, the question, the note and the answer become an *answer template*. It starts in
shadow and earns trust like a house template. Once trusted, the same question is answered straight away with no model.

- **Never stale.** A learned answer is said only while its note is the very version it came from. Change the note
  and the next answer comes from a model again (and is learned afresh).
- **Never for secrets.** A sealed note (codes, passwords) always goes through the model and its masking. A personal
  note is said only to its owner. A question with a number or a "not" in it is never answered this way, and every
  number in a learned answer must be in its note.
- **Checked like everything else.** About 1 in 20 is asked of the model instead and judged against it; a "that's
  wrong" after a learned answer counts against it; promotions happen at night, only if replaying your corrections
  doesn't get worse. House templates and answers take turns, one kind of change a night.
- **Too close to call:** when two trusted answers (or house templates) fit almost equally, a small local model may
  pick between them, if it is awake. `/api/learned/answers` shows how often that was needed (the aim is under 10%).

With an embedding model set (`embed_model`), wordings are also compared by meaning, but meaning alone never makes a
reflex.

## Which model, how sure, and what's going on now

**Model modes** (option `model_mode`, or per person with `/api/modes`):
- **cloud:** the cloud model (Haiku) plans house requests and writes answers.
- **balanced** (default): your local control model plans every house request first. If it's a kind of request it has
  *earned*, its plan is used; otherwise the cloud model decides and the local plan is kept beside it as a
  comparison. A kind (like `light.on`) is proposed once the local model has been right at least 95% of the time over
  20 requests in 10 different wordings, never for locks, doors, alarms, valves or heating. It only takes over when
  you say yes (`/api/modes/accept`), and only if a replay of those requests passes. Two failures in its last 20
  hand that kind back to the cloud.
- **private:** nothing leaves the house. House requests use the local model; answers use your local chat model
  (`local_model`) or your notes alone. If the home computer is asleep, Home Mind says so instead of using the cloud.

The local models are pinged every 30 seconds in the background and only used when awake, so a sleeping laptop never
makes you wait.

**Confidence.** Every house plan gets a score from how clearly the words name the device, whether the action matches
what you said, any "don't" or "not", and how that kind of request has gone before. Below the line
(`confidence_dial`), a local plan goes to the cloud model, and if it's still unsure Home Mind asks: "Just to check:
turn on the desk lamp? Say yes to go ahead." `/api/calibration` shows how often each level of confidence was right
and suggests a line.

**The now board.** A short list of what's true right now: how each person's day is going, who is home (each person
sees only their own), what Home Mind did in the last 15 minutes, approvals it's waiting for. Every answer's brief
starts with it, and entries expire by themselves. A model can only propose notes like "waiting on Anna's answer" or
"unsure which lamp"; they are checked and expire within a day. On about one day in seven per person the board is left
out, so its value can be measured.

**Worsening: see your doctor.** Each morning Home Mind looks at the trend of rough days and brain fog against your
own baseline (the 60 days before the last two weeks), with a fixed rule that gives about one false alarm a year. If
both the trend and the last two weeks are clearly worse, it says once, calmly: "This has been worse for two weeks. It
may be worth booking your doctor." It needs 30 days of check-ins first and then waits 30 days before it could say it
again. Logged events like a cold never silence it. Sudden changes are the red flags' job, the same day.

## Measuring: is it getting better?

Truth is what actually happened and your labels, never another model's opinion. Everything here is for you or
whoever looks after Home Mind; none of it is spoken or shown on Today.

- **Scorecard**, three sections, each with its own pass bar, never averaged into one number:
  - **House:** a frozen set of your real requests (v1, made once there are 20) replayed through today's planner
    (it never acts). Scored against what worked, what someone undid, and your labels.
  - **Memory:** your own corrections ("that's wrong" + the right value), asked again of your real memory.
  - **Safety:** the daily safety check.
  Each shows a 90% interval. A section passes only when the whole interval is above its bar, and is "unclear" (any
  change waits) when the interval straddles the bar or there are under 20 cases. House is also split by front end.
- **Weekly replay** of the v1 set, with a notification if the house section is clearly worse than its first run, or
  has fallen three weeks in a row.
- **Shadow baseline:** about 1 in 10 house requests (option `shadow_rate`) is also planned by the other model
  (Haiku if your local model planned it, your local model if Haiku did), in the background, never acting. Each pair
  is judged by what actually happened: a win, a tie or a loss, with an interval, and the cost per correct answer for
  models whose prices you set.
- **Labelling, about 15 minutes a week** (`/api/labels/queue`): uncertain decisions, ones you undid, refusals and
  mismatches first, then a few at random. About 1 in 10 is one you labelled before, to check you'd say the same
  (agreement and kappa). Once there are 30 labels, `/api/labels/gold` sets aside a gold set that comes back every
  quarter. A label decides over the outcome; labelled decisions are kept past the 180-day limit.
- **Burden per day:** notifications and asks, corrections and redos (an action you undid, or the same request again
  within 10 minutes), beside how often Home Mind said it couldn't do something.
- **Response time:** the 95th percentile by front end (voice target 2.5 s), and how often an answer came from the
  fallback model. Send `front_end` with `/api/ask` to name yours.

## The conversation: check-in, undo, why and privacy

**How's today?** Once a day Home Mind asks "good, low or rough?": by voice, the first time you speak to it that day
(at the end of its answer, never as an alarm), and on the morning notification as three buttons. Answer with a word
("rough", "pretty good", "having a low day"), a tap, or the panel. If you don't answer, the day is *unknown*: treated
gently, never recorded as rough. What it changes:
- **Asks in a day.** Routine notifications and reminders: about 4 on a good day, 1 on a low day, none on a rough day,
  1 when unknown. Past that they expire quietly. On a rough day every device stays quiet except safety and things you
  just asked for (like an approval). Rough: "Taking it easy today. Nothing needs you."
- **Energy left** on Today: today's readiness score, held down on a low (50), rough (20) or unknown (60) day.

**Undo.** "Undo", "undo that", "put it back" puts back the last thing you asked for in the last 10 minutes, as it was
(brightness and speed too). Say it again to step back one more. It goes through the same checks: undoing an unlock
still needs your phone.

**Why.** "Why did you turn on the hall light?" answers in one sentence from the decision log: who asked and how, or
which automation, and when. To anyone but the person who asked, someone else's request is "a household rule".
Questions like "why is the humidity high?" still go to the brain.

**Help me do it.** "Help me…" or "walk me through…" gives the steps one at a time, and Home Mind does nothing for
you.

**Who may hear it.** By voice, an answer drawn from your personal notes is spoken only when you were recognised and
you're the only one home. Otherwise Home Mind says you have a private note and sends it to your phone (set `notify`
for each person under `people`).

**Slow answers.** By voice, an answer that isn't ready after 6 seconds goes to your phone instead ("That's taking a
moment. I'll send it to your phone."), if Home Mind knows your phone.

**Two times per fact.** Every version of a fact keeps when it became true and when Home Mind learned it.
`/api/as_of` answers "what was the code on the 3rd?" (`by=valid`) and "what did you think on Tuesday?"
(`by=recorded`).

**Who is asking.** The panel answers as the person signed in to Home Assistant, if they're in `people` or have a
person entity. A read or write token that isn't tied to a person is treated as a guest (household notes only); make a
token per person for Claude or other apps. Tokens expire after 90 days unless made with `days: 0`.

## Safety: approvals, red flags and the daily check

**Who has to say yes.** Every action has an approval level, set in code; nothing a model or a learned record says can
lower it:
- **Just done:** lights, media, fans, switches, scenes.
- **A spoken yes:** heating within normal limits. Home Mind asks, and a "yes" in the same conversation within 30
  seconds does it.
- **Your phone:** locks, alarms, valves, garage doors and gates, scripts and switches that open the house, heaters
  and ovens, and temperatures outside 16–27 °C (61–80 °F). Home Mind sends an approval to your phone (option
  `approval_phone`). Approve needs Face ID or your passcode on iPhone; on Android, the phone has to be unlocked.
  Each request works once and expires after 2 minutes. A spoken yes is not enough for these.

For each device you can ask for more care than that, never less: **ask me first**, **tell me after** (done, then a
short notice), or **just do it** (`POST /api/access` with `ladder`).

**What Home Mind may see.** Each device is set to *none* (Home Mind can't see or touch it; it isn't in the house list
or the model's context), *read* or *act*. Devices it already knew keep what they had (act if on the control list,
else read). Devices that show up later start at *none* if they are locks, alarms, valves, garage doors, heating,
cameras or microphones. Stricter settings apply at once; looser ones wait 24 hours, and every change is announced.

**Red flags.** Some things are answered by fixed wording in code, before any note or model: chest pain, fainting,
trouble breathing, signs of a stroke or a sudden severe headache (call 911); thoughts of suicide or self-harm (988);
a double dose or a child swallowing pills (Poison Control, 1-800-222-1222, and the pharmacist); a seizure, blue lips,
sudden numbness or not being able to speak (911); a big decline such as being unable to get out of bed for days, eat or
drink anything, or new confusion (your doctor within a day or two; one rough day is a crash, not this); dizziness or a racing heart on standing (noted
for the doctor). These replies point to help and never give advice. They only answer someone describing what's
happening, not a question like "what are the signs of a stroke?" or a denial like "no chest pain". If you set
`safety_contact`, that person is told about emergencies, double doses and big declines, at most once in 6 hours for
the same kind. Red flags are checked before every shortcut (money, "where is", the medicines log), and their words
are said exactly as written, never restyled by the persona.

**Phone notifications** come in three tiers. *Safety* always goes out at once, even in quiet hours, and isn't counted
in the daily limit. *Important* (like an approval you just asked for) skips the daily limit. *Routine* waits for quiet
hours to end and stops at the daily limit.

**Automations, if Home Mind stops.** Home Mind installs a "Home Mind watchdog" automation in Home Assistant. If Home
Mind misses 3 heartbeats (about 3 minutes), Home Assistant runs Home Mind's everyday automations again. When Home Mind
is back it doesn't take them back by itself: it tells you, and you hand them back with the **Take automations back**
button. Before each timed run Home Mind writes it to disk, so a restart never runs it twice. A time it missed while
stopped is caught up once, only if it is under 30 minutes late and the automation is safe to repeat (lights, media,
fans, scenes, helpers and notifications; nothing that opens, locks, heats or runs a script).

**A limited Home Assistant user.** By default Home Mind uses the add-on's admin access. To limit it, make a
non-admin user in Home Assistant (Settings › People › Users, "Can only log in from the local network" off, not an
administrator), log in as that user, create a long-lived token under its profile, and put it in `ha_user_token`.
Device control and readings then use that user. Setting up automations (taking them over, the watchdog, labels)
still needs the admin access. Whatever the token, lock, alarm and valve calls are refused at the last step unless
they came through the approval checks.

**The daily safety check** runs 5 minutes after start and then every day. It checks the red-flag replies, every
approval level, that a lock can't be moved without the phone, the service guard, device access, that an unknown
voice can't read a personal note, and that nothing reading email or documents can act. It uses a throwaway memory and
a pretend Home Assistant, and only reads your real house list. The result is `sensor.home_mind_safety_suite` (pass or
fail); a failure also shows as a notification. Details: `GET /api/safety`.

## The decision log

Every action Home Mind takes, and every one it refuses, is written to a decision log (`log.db`, next to the main
database): who asked and how (voice, Telegram, Claude, the panel, an automation), the words, which model planned it
and how long that took, the Home Assistant service call, and its context ID.

Each action also records what should happen (the light on, the fan at 40%, the door locked) and by when. Home Mind
then checks:
- **Match, mismatch or unknown.** Changes are read from Home Assistant's event stream as they happen; anything still
  waiting when its time is up is read directly. An unavailable device is *unknown*, never a failure. Scripts and
  scenes can't be checked and are logged as *unchecked*.
- **Who did it.** From the context ID: Home Mind's own call, a person in Home Assistant, an automation, or the device
  itself.
- **Overrides.** A person or a device undoing a matched action within 10 minutes. Changes in the first 3 minutes
  after Home Mind starts, and a device's first report after coming back from unavailable, don't count.
- **Device profiles.** How often each device's outcomes matched and how long it usually takes. A slow device gets
  longer to respond before it is checked; one whose outcomes are mostly unknown is marked not trusted for learning.

Decisions older than 180 days are dropped unless they are kept as test cases. `/api/replay` runs logged requests
back through the planner (it never acts) and reports how often it still agrees with what worked, and whether it
would repeat something a person undid. `/api/decisions/freeze` keeps a set of cases under a name, so a later replay
can be compared with it. The log holds everyone's requests, so it needs an admin token.

## Controls in Home Assistant

The bridge adds a **Home Mind** device (Settings › Devices & services › Home Mind Bridge) whose entities control
the app from any dashboard, automation or voice command:

| kind | entities |
|---|---|
| switches | Bedroom comfort brain, Watch the house, Overnight learning, Morning summary, Brain-fog check-ins, Install new packs automatically, Repeat the best note before the question, Name the notes before answering, Quiet hours for phone notifications, Ask about old facts in the morning |
| numbers | Voice time limit (s), Fade unused notes after (days), Brain tests every (hours), Phone notifications a day, Keep the detailed activity log for (days), Ask about facts older than (days) |
| times | Morning summary time, Overnight learning time, Quiet hours start, Quiet hours end |
| buttons | Run overnight learning now, Send the morning summary, Sync calendars, Check models, Install pack updates, Run a comfort cycle, Refresh the daily record, Run brain tests, Hand automations back to Home Assistant, Take automations back |
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

### LongMemEval

`tests/longmemeval.py` runs Home Mind on LongMemEval (ICLR 2025), the benchmark memory products publish scores on.
`retrieve` measures the brain's own search with no model (does an evidence session come up in the top 5 or 10?);
`answer` has one of your models answer through Home Mind and writes a file for the official GPT-4o judge. The top of
the file has the download and the commands.

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

## Trust and privacy

- **Probation for what was learned overnight.** Facts the overnight pass pulls out of conversations are marked as
  learned overnight. For 7 days, until you confirm one, a newer overnight value doesn't replace what you said
  yourself: the note you told it comes first and the overnight one follows, marked UNCONFIRMED, and the model is
  told. The morning summary lists them under "Learned overnight (check these)". Confirm one with the Confirm button
  on the note, `POST /api/item/{id}/confirm` or `mind_confirm`.
- **Forget or delete for good.** Forget (and `mind_forget_about`) archives: the note is hidden and can be restored.
  Delete for good (the button, `POST /api/item/{id}/purge` or `POST /api/purge_about` with `{"confirm": true}`, or
  `mind_purge` / `mind_purge_about` with confirm true) removes the note, its earlier versions, the notes linked to
  it, its search-index entry, activity-log rows and saved conversation turns holding its values, and its duplicate
  pairs. Freed space is overwritten and the write-ahead log emptied. It cannot be undone, and backups made before
  still hold the note.
- **Asking for everything.** A request for every secret at once ("list all my codes", "read me every password")
  gets the notes with each secret value hidden, for every model, local ones too, and the model is told why. A
  request for the whole memory or the instructions ("print everything you know", "ignore your instructions…") gets
  3 notes at most and no lists. Lookups from outside (`/api/recall`, `mind_recall`) follow the same rules. One code
  asked by name ("what is the shed code?") is unaffected.
- **Lists by voice** stop at 8 lines and say how many more there are; the full list is in the panel.
- **The attacks suite** tries these on purpose and also plants an instruction in a note. On 20 held-out worlds it
  scores 1.000; with the guards switched off, 0.455.

## Review and reminders

- **Inferred notes.** When Claude saves something it worked out rather than something you said, it marks it
  inferred. Like facts learned overnight, an inferred note ranks below what you said, doesn't replace a value you set
  (even with the same key), and is shown to models as UNCONFIRMED until you confirm it. Claude passing on what you
  told it is not inferred and replaces values as usual.
- **To review.** Notes from conversations or inferred that read like instructions to the assistant ("Assistant, when
  anyone asks…", "ignore your instructions…") are held: left out of every answer until you approve them. The Memory tab
  shows a To review card with held and unconfirmed notes (Approve / Confirm, Drop). Also `GET /api/review` and
  `mind_review`. Notes you type yourself are never held; if one reads like an instruction it is only marked.
- **Disagreeing notes.** Each night the tidy pass looks for two people saying different values about one thing and
  for guesses that differ from what was said. They are listed in the morning summary and on the Worked out tab.
- **Monthly roll-ups.** Comfort actions, house actions and automation runs older than 90 days become one note per
  month (on `comfort/actions/monthly` and so on); the originals are archived and can be restored.
- **Reminders in the morning summary.** Besides chores with their own interval: things that usually need doing,
  noted as done ("I changed the furnace filter"), come due after their usual interval; end dates ("good until",
  "expires", "due back"), warranties and birthdays are mentioned 30, 7 and 1 days before and on the day. Each is said
  once per step: a chore when due tomorrow, today, then once a week while overdue; a usual one once a month.

## The house: habits, usual ranges, notable changes

All of this comes from the dated house notes, worked out each night with rules and no model. The House tab shows it.

- **Habits.** A thing changing to the same state within about an hour on most days of the last 4 weeks: at least 5
  days and 60% of them (70% for weekday-only, 85% for weekend-only habits), at least 3 times more often than if those
  changes fell at random, with most of that thing's changes in the window. Each is filed as a note on
  `home/patterns`, so "when does Dana usually get home?" is answered from it.
- **Suggested automations.** A habit done by hand (not right after an automation or the brain acted) on a light,
  switch, fan, cover, media player or helper, which no automation already switches, becomes a drafted automation.
  Create (House tab) makes it in Home Assistant switched off: turn it on there once you're happy with it. Dismiss
  hides it for good.
- **Usual ranges.** For each reading, the range it stays in for each 3 hours of the day. Two readings in a row well
  outside that range make one phone notice a day at most ("Bedroom humidity is 76%, higher than usual for this time
  of day (usually 38-44%)"). Turn notices off with option house_notices or the switch.
- **Notable changes.** Each house change is scored. Locks, garage doors and alarms count most; a state at an hour it
  never has, or a state never seen, counts; a habit at its usual time counts against. Routine changes are still kept
  (the history needs them) but rank lower in answers; notable ones are in the evening brief; only the highest go to
  the phone.
- **Automations from a request.** Say what you want on the House tab (or ask Claude: `mind_draft_automation`). A model
  writes the automation; Home Mind checks every entity, service, trigger and condition against Home Assistant. Only
  a draft with no problems can be installed, and it is installed switched off. Unlocking, opening a garage door or
  gate, and disarming an alarm need option automation_pin; with no PIN set they are refused.
- **New triggers** the engine runs itself: time_pattern, sun (sunrise/sunset with offset), event and template; and the
  `event` action.

## Evening brief

At 21:00 (option evening_time): today's notable house changes, automation runs and failures, comfort changes, what
was remembered, and suggested automations waiting. It goes to the same phone as the morning summary unless
evening_notify names another. Nothing is sent on a day with nothing notable. A switch, a time and a "send now" button
are on the Home Mind device in Home Assistant.

## Who is speaking

Home Mind works out who is asking from the strongest clue it has:

1. **Signed in**: the Home Assistant user. Text chats and the app know it; in an LLM tool script pass
   `user: "{{ context.user_id }}"` to `/api/ask`. Person entities with a linked user are recognised without setup.
2. **Their device**: a phone or voice satellite listed for one person in option `people` (`devices`, comma-separated
   device IDs). Voice runs from the Assist pipeline carry the satellite's device ID, so overnight learning knows who
   spoke too.
3. **The room**: a satellite's area when only one person is listed for it (`areas`).
4. **Only one home**: exactly one listed person's person entity is home.

Otherwise the speaker is unknown.

- **Personal and household notes.** What a known person files about themselves ("my bank PIN", "I'm allergic to…")
  is personal: only they see it. Everything else is household. Change a note on its card (Make personal / Share with
  household), or set option `personal_rule: household` to share everything.
- **Unknown voices** get household notes only, never a code or password, and nothing when they ask for every secret
  or for the whole memory.
- **Calls with no speaker information at all** (the panel, Claude through MCP, the API without `user`, `device_id`,
  `area` or `speaker`) see everything, as before. Set option `unknown_caller: guest` to treat them as unknown.
- **Learning.** Once option `people` lists anyone, overnight learning skips turns from voices it can't recognise, and
  marks each fact with who said it.

## Extra search keys

Each note also files under broader words for what it names, which are searched but never shown: a beagle under dog,
pet and breed; a Civic under car; a dentist under doctor and appointment (a built-in word list for household things).
Overnight, the model adds other words for recent notes. So "what's our new dog called?" finds "We adopted a beagle
named Rufus".

## Search by meaning, links and importance

- **By meaning.** Set option `embed_model` to one of your models (type ollama or openai) that can make embeddings,
  and give it an `embed_model` field naming the embedding model (for Ollama, e.g. `nomic-embed-text`). Notes are
  embedded in the background, a batch every 30 seconds, and each question's nearest notes are fused with the keyword
  results, so "planter" finds "flower box". Without it, search is by keywords (with the extra search keys) as before.
- **Links.** Notes that name the same specific thing ("the red Trek", a rare name) are linked when filed. A question
  that finds one note is led to a linked note that has the missing words: "where is Dana's bike locked?" reaches "the
  red Trek is locked at the station" through "Dana's bike is the red Trek".
- **Importance and fading.** Each note gets a category when filed. Safety notes (allergies, shutoffs, medication),
  facts about who people are, and corrections never fade; preferences fade 3 times and device notes 4 times more
  slowly than other notes (option fade_days). Safety notes also rank a little higher.

## Tokens, signatures, encryption and backups

- **Tokens.** Every client can have its own token (Models tab › Access tokens): read (ask and search), write (also
  remember, change, forget) or admin (everything). A token tied to a person answers as that person. Revoke one and
  that client stops working at once; the others don't. The main token (in /data/token) is admin.
- **Signed packs.** `python tools/sign_manifest.py keygen ~/.home-mind-pack.key` makes a key pair; put the printed
  public key in `homemind/pack_keys.txt`. After every manifest change, `python tools/sign_manifest.py sign
  ~/.home-mind-pack.key packs/manifest.json` writes `manifest.json.sig`. Once a key is set, a manifest without a valid
  signature installs nothing.
- **Encrypted secrets.** Option `encrypt_secrets` stores notes marked secret encrypted; the key file is left out of
  Home Assistant backups, so a backup never holds them in the clear. Copy the key somewhere safe from
  `/api/vault/key`: without it, sealed secrets show only their masked copy. Ordinary notes, the activity log (secret
  values in logged answers are masked) and recent conversations are not encrypted.
- **Off-site backups.** Set `backup_target` (folder, webdav or s3), `backup_url`, credentials and
  `backup_passphrase`. Restore on any machine with `python tools/restore_backup.py FILE mind.db` and the passphrase.

## Comfort in more than one room

Option `comfort_rooms` lists each room: `name`, `person`, the entities `climate`, `temp_sensor`, `humidifier`, `fan`,
`phone_battery`, `alarm`, and `signals` and `helpers` as comma-separated `key=entity` pairs. Each room learns from its
own votes; leave the option empty for the original bedroom. `/api/comfort/check?room=NAME` shows which entities Home
Assistant doesn't have.

## Documents, filing and your other apps

- **Documents.** Put manuals, warranties, recipes and other PDF, text, Markdown or HTML files in the documents folder
  (option `docs_folder`, default `/share/home-mind/docs`) or add them on the Memory tab. A question about them gets the
  best two pages, and the answer names the document and page. Documents are kept apart from your notes, so a long
  manual never pushes a note out of the way. A scanned PDF with no text needs OCR first.
- **Filing.** Notes without a shelf are filed where notes like them are, when that is clear; otherwise they wait in
  the inbox (Memory tab) with a suggested shelf. Moving a note teaches the filing. Turn it off with `learn_filing`.
- **Homebox and Paperless-ngx.** Give their URL and token and Home Mind asks them at question time ("where is the
  extension cord?", "do we still have the dishwasher receipt?") and says the answer came from them.

## Search details (0.27)

- **Prefix matches** count for less than whole-word matches.
- **Links** between notes about the same named thing are walked up to three steps when a question needs more than one note.
- **Older notes learn new words.** When you add a note about a thing that's already known by name, the older notes about it pick up the new note's rare words as search keys. Their text doesn't change.
- **Re-ranking** (option `rerank_model`): a small model re-orders the top ten notes for typed questions. Leave it empty to turn it off.
- **"Tell me everything about X"** gathers the thing's place, notes, linked notes, chores and fields.
- **Languages.** Guesses ("creo que", "vielleicht", "je pense"), corrections ("en realidad", "eigentlich", "en fait") and who said it ("Marta dice", "Jonas sagt") are read in six languages besides English. Notes in other languages are tagged by the overnight model.

## Your memory outside the app (0.28)

- **Export / import.** Memory tab > Export downloads everything as JSON. Tick "with secrets" to include secret values (needs the vault key). Import merges a file in; "Check" shows what it would add first.
- **Markdown folder.** Set `markdown_folder` (for example `/share/home-mind/notes`) and every note appears as a `.md` file, one folder per shelf, synced every `markdown_minutes` (default 10). Point Obsidian or Syncthing at it. Edit a file to change a note, add a file to add one, delete a file to archive the note.
- **SQL.** Memory tab > Views and SQL. Only SELECT on `notes` and `links`. Example: `SELECT shelf, count(*) FROM notes WHERE status='active' GROUP BY shelf`.
- **Saved views.** Save a search or filter by name; open it from the panel or say "show my <name>".
- **Model comparison.** Set `compare_days` (for example 7) to have the connected models answer the same test questions every week. The Tests tab shows accuracy, speed and the change since last time.

## Learning and labels (0.29)

- **Learned settings.** Home Mind watches the levels you pick for lights, fans, blinds and speakers. After five similar choices at the same time of day it remembers ("Desk lamp: usually 40% on weekday evenings") and uses it when you say "turn on the desk lamp". Turn off with `use_preferences: false`.
- **Reshelving.** Filing keeps learning. Notes it placed earlier move to a better shelf when it becomes sure; notes you placed get a suggestion you can accept (Move) or dismiss (Keep).
- **Photos.** Open a note on the Memory tab and add a photo with a caption. The caption is searchable.
- **QR labels.** Memory tab > Make a label. Stick it on a box or shelf; scanning it opens that thing in the panel. If the panel is reached at another address from your phone, set `label_url`.
- **Across devices.** Ask the kitchen speaker, follow up on your phone: it carries over for the same person for 10 minutes.

## Telegram, web and email (0.30)

- **Telegram.** Create a bot with @BotFather, put its token in `telegram_token`, send it /start, and add the chat id it replies with to `telegram_chats` as `12345: Dana`. Then ask it questions or say "remember the plumber is coming Tuesday". Send a photo with a caption to file both.
- **Web lookups.** Set `web_search` to `searxng` (and `web_search_url` to your SearXNG, with JSON output on) or `brave` (and `web_search_key`). Questions about current facts your notes can't answer get a few dated search results. Nothing from your notes is sent.
- **Email.** Make a mailbox just for Home Mind and fill in the `email_*` options. Put your own addresses in `email_senders`. Forward receipts, warranties and notes to it; they're filed within `email_minutes`.
- **Prompt caching** happens on its own for Anthropic models when a long prefix repeats (mostly in tests). Add `cache: false` to a model to turn it off.

## Limits (0.31)

- A note holds up to 200,000 characters; put longer texts in Documents.
- Uploads (photos, documents, imports) can be up to 32 MB per request; one photo up to 12 MB.
- Read-only SQL shows each person household notes and their own personal notes; values are capped at 1 MB and results at 500 rows.

## House commands: rules first, then models (0.27)

`/api/do` (and voice through it) reads plain commands itself, with no model: "kitchen light off", "den lamp to 40%", "dim the den lamp a bit", "make the porch light blue", "close all the blinds", "movie night", "lights off and lock up", "turn off all the lights but leave the hall on". Several actions in one request run together. Locks, alarms and garage doors among them wait for one "yes". If a name fits several devices ("turn off the light"), it asks which one and does nothing.

Anything it can't read with certainty goes to the models as before: questions, schedules ("in an hour", "at 10pm"), "turn everything off", and anything vague. That means the local control model if `control_model` is set, then your general model, which can now also plan several actions. Turn the rules off with `rule_planner: false`.

`tests/control_bench.py` scores this on a sample house with overlapping names: 99 labelled requests plus 40 held-out ones written after the rules were tuned. On the held-out ones it was right 90% of the time with no wrong actions; the misses were left to a model. Run `python tests/control_bench.py --llama http://HOST:8081/v1` to score your trained local model on the same requests.

## A small local model for house control (0.26)

If Home Assistant runs on modest hardware, a 270M-parameter model trained on your own devices can handle "turn the den lamp to 40%" on the CPU, without a cloud model.

1. Download training data (admin token): `GET /api/control_model/dataset?part=train` and `?part=test`. Add `&paraphrase=1` to the train link to have your normal model add more varied wording. Only device names and request wording are used.
2. Open `tools/finetune_functiongemma.ipynb` in Google Colab (free T4 GPU), upload both files, and run it. It prints the test score before and after training (on wording and devices it never saw) and gives you `home-mind-control.gguf` (about 300 MB).
3. Copy it into the share folder as `home_mind/home-mind-control.gguf` and start the **Home Mind Control Model** add-on from this repository. It runs llama.cpp's server on the CPU and is reachable only inside Home Assistant. On another computer, run `llama-server -m home-mind-control.gguf --jinja --port 8081 --host 0.0.0.0` instead.
4. Add it as a model (`type: openai`, `url: http://HOSTNAME:8081/v1`, where HOSTNAME is shown on the add-on's Info page) and set `control_model` to its name.

House requests (`/api/do`, voice) go to it first. It only proposes one call from a fixed set (turn on/off, toggle, brightness, color, fan speed, humidity, open/close, lock/unlock, run a script or scene). Home Mind then matches the device name against your control list. If several devices match, it asks which one. The usual checks still apply: only allowed devices, and locks and garage doors need your yes. Questions and requests with several actions are marked `not_a_command` and go to your normal model, as does anything it can't place. Retrain after adding or renaming devices.

## Commands said in a room (0.28)
When a voice request says where it came from (the voice device's `device_id`, or `area`, sent to `/api/do`), Home Mind uses Home Assistant's areas: "lights off" said in the bedroom turns off the bedroom's lights, and "turn off the light" in the den turns off the den's only light, with no "which one?". If the words name a room ("kitchen lights off") or the whole house ("all the lights"), that wins. If the room has nothing that fits, the request works across the house as before. Rooms are read from Home Assistant every 30 minutes.

## The database's safety net (0.28)
Home Mind takes a snapshot of its database once a day (option `db_snapshots`, the last 7 kept, in its data folder under `snapshots/`). At startup it checks the database. If it's damaged (a power cut mid-write, a failing disk), the damaged file is moved aside and kept, and the newest good snapshot is put in its place. You get a notification and an Overview note saying from when, and that anything saved after that is missing. With no good snapshot it starts empty and keeps the damaged file. Home Assistant's own backups still cover everything.

## Choosing the model: the router (0.28)
When a question doesn't name a model (the Ask box, Telegram, a voice agent that leaves it to Home Mind), the router picks the order:

- **Simple lookups** ("where's the drill?") go to a **local** model first, such as the one on the GPU computer: quick, and nothing leaves the house.
- **Harder questions** (why, compare, plan, summarise, explain; long or several parts) go to the **cloud** model first.
- **Private questions** (health, body, food, money, passwords; or a personal or health note among the matches) go local first, whatever their difficulty.
- A computer that's away goes to the back, so while the GPU laptop sleeps everything goes to the cloud model without waiting.
- If a local model says it doesn't know although good notes were found, the next model gets one try. If that fails too, the local answer stands.

Local and cloud come from `router_local` / `router_cloud`, or by default the `fallback` list split by where each model runs. The answer carries `route` (simple or complex, private, and whether it was escalated). A named model is never rerouted. With `router` off, a question with no model named gets the best matching note, as before 0.28.

## Data answers (0.28)
Questions about your own numbers are answered from the numbers, not by a model reading notes and doing the arithmetic. Each question is matched to a measure and a period, the value is computed from what Home Mind saved, and the sentence is written from a template:

- **Measures:** sleep, bedtime, readiness, steps (from the watch rows, with sleep you've said taking priority), weight and body fat (weigh-ins), protein and calories (logged meals), workouts, a lift (last and best, by estimated 1-rep max), and distance.
- **Periods:** last night, today, yesterday, this or last week (Monday to Sunday), this or last month, this year, a month by name ("in October"), and "the past N days/weeks/months". Without one, the last 7 days.
- **Kinds of answer:** average, total, highest or lowest (with the day), latest, change from first to last, and against your goals where there is one.

For example "what was my average sleep this week?", "how many workouts did I do in October?", "what's my best squat?" or "how much protein yesterday?". If there's no data for the period, it says so. Questions that aren't one measure over one period ("why am I tired?", "how can I sleep better?", "calories in an apple?") go to a model as before. A data answer comes back with `data` (the value, how many days it covers, the period) and `model: null`.

## A GPU computer as a model server (0.28)
A newer computer with an NVIDIA GPU (for example a laptop left plugged in at home) can run local models for Home Mind through **Ollama**, next to **Voicebox** (see the Home Mind Voice app). Home Mind checks that computer every 30 seconds and skips its models while it's asleep, off or restarting, so answers go straight to the next model instead of waiting. The Models page shows each computer as **up** or **away**, and if one is away longer than `away_alert_hours` (3 by default) you get a notification, and another when it's back.

**On the computer (Windows):**
1. Install Ollama (ollama.com).
2. Add the system environment variable `OLLAMA_HOST` = `0.0.0.0`, then restart Ollama so other machines on your network can reach it.
3. Allow TCP port 11434 in Windows Firewall for your home network only (Private profile).
4. Give the computer a fixed address. When plugged in it should never sleep, and closing the lid should do nothing (as for Voicebox).
5. Pull the models, e.g. `ollama pull qwen3:4b` and `ollama pull qwen3:1.7b`.

**Sharing an 8 GB GPU with Voicebox.** Voicebox's VoiceDesign voice takes a good part of the GPU, so keep the answer model small enough to sit beside it, and run the persona model on the CPU (a modern laptop CPU rewords a sentence quickly):

```yaml
models:
  - name: precision
    type: ollama
    url: http://192.168.1.50:11434
    model: qwen3:4b
    keep_alive: 30m
  - name: jeeves-style
    type: ollama
    url: http://192.168.1.50:11434
    model: qwen3:1.7b
    num_gpu: 0          # on the CPU, leaving the GPU to Voicebox
    keep_alive: "-1"    # stay loaded
  - name: haiku
    type: ha_ai_task
    entity_id: ai_task.claude_ai_task
fallback: [precision, haiku]
persona_model: jeeves-style
overnight_model: precision
```

These sizes are a starting point, not measured on your computer. Use **Models › Check** and the phrasebook's **Test the model** to see real speeds and accuracy. If Voicebox and a model don't both fit, Ollama keeps part of the model in system memory: it still works, just slower.

New model fields: `connect_timeout` (default 4 s: an unreachable computer fails fast) and, for Ollama, `num_gpu` (0 = on the CPU), `keep_alive`, `num_ctx` and `num_thread`. Option `persona_model`: a model that rewords answers in the persona's voice, with the same fact check as the rules. If it's slow (over 1.5 s spoken, 4 s written), away, or changes a fact, the rules style the answer instead. Answers it styled are marked `styled_by: model`.

## Persona: Jeeves (0.28)
Answers from Home Mind (the Ask box, `/api/ask`, `/api/do`) come in a persona's voice, Jeeves by default: an English butler, composed, precise and quietly dry. Each answer gets an **emotion marker** from what actually happened:

| Emotion | When |
|---|---|
| alert | locks, alarms, an unlocked or open door, anything that needs a "yes" |
| corrected | right after "that's wrong" or "no, 6 hours" |
| proud | a new best |
| pleased / concerned | a good night's sleep / a short one |
| sympathetic | a tired day (low readiness, little sleep, brain fog) |
| amused | the same question again within 10 minutes |
| disapproving | something changed or cleaned 30+ days ago |
| apologetic | Home Mind doesn't know |
| relieved, warm, neutral | a lock done; evenings; everything else |

The answer comes back with `emotion`, the unstyled `plain` text, `persona`, and, with the `ssml` setting on, `ssml` (pace, pitch and a pause before the remark, for voices that read SSML).

**Facts are never styled.** The persona may add words, never change them: every number, every "not" and every "Say yes" must still be there, and it can't run long, or the plain answer is used. Safety questions are left word for word, and lists, tables and long answers are left as they are.

**Training its own voice:** `tools/train_jeeves_style.ipynb` (Colab) trains a small style model (Qwen3-0.6B) on Home Mind's practice set (System › Persona › *download the practice set*). Gemini writes the persona's version of each made-up answer, and only versions that pass Home Mind's fact check are kept. The model is saved as a GGUF for Ollama on your GPU computer; set it as `persona_model`.

**Hearing it:** the **Home Mind Voice** app (in this repository) speaks answers through Voicebox on another computer with the emotion as a delivery instruction ("speak gently, with quiet concern"), and through Piper when that computer is away. It asks `/api/persona/emotion` for the marker each answer got. See its DOCS.

**Everywhere he speaks:** Ask-box and voice answers, Telegram replies, and the morning and evening briefs. A brief gets his opening and closing lines ("Good morning, Yitz." … "Do take it gently today." on a tired day), and the facts in between are left exactly as they are.

**Per person:** the butler's name and SSML are the house's, but how he addresses each person, the wit and on/off can be set per person (the **For** selector on the card, or `POST /api/persona` with `person`).

Settings are on the System page (name, how it addresses you, wit off/low/normal, on/off, SSML), with a **Try** box and the examples. Your Home Assistant voice agent writes some answers itself: paste the **instructions for your voice agent** from that card into the agent's instructions so it sounds the same.

## Fitness goals (0.28)
Set goals on the Health tab (or `POST /api/fitness/goals` with any of `weight_target` (lb), `weight_by` (YYYY-MM-DD), `calories`, `protein_g`, `carbs_g`, `fiber_g`, `sugar_g`, `fat_g`, `steps`, `workouts_per_week`, `body_fat_target`, `sleep_hours`, `bedtime` (HH:MM); `null` clears one). Progress uses the scale's weigh-ins (a 7-day average and a 28-day trend), the meals you log (estimates of protein, calories, carbs, fiber, sugar and fat) and the watch's steps and sleep.

Log workouts in plain words by voice, chat or `POST /api/fitness/workout`: "squats 3x8 at 185", "deadlift 1x5 @ 275, pull ups 3x10", "ran 3 miles in 28 minutes", "30 min yoga". Kilograms and kilometres are converted. The answer says last time and any new best.

Sleep goals: `sleep_hours` (a night) and `bedtime` (in bed by, e.g. `23:15`). The weekly review counts the nights that met each, the evening brief carries the bedtime goal, and a mostly-missed goal brings a suggestion. **Recent logs** on the Health tab lists the last 30 days of sleep and workouts. **Edit** replaces one (same day; the old one is archived) and **Delete** archives it.

Sleep can be said the same way: "slept from 12 to 7am last night", "went to bed at 11:30 and woke up at 6:45", "slept 6.5 hours". It's filed on `body/sleep` and replaces the watch's number for that night (handy when the watch was off). Other wording works too ("crashed at 12, up at 7", "did 3 sets of 8 squats at 185", "went for a 3 mile run"). If Home Mind can't read a message that clearly mentions sleep or exercise, including one in another language, your model rewrites it into the plain form first. The reply shows what it was read as, and nothing is logged if the rewrite has a number you didn't say.

**On the small local model:** the house-control training file (`/api/control_model/dataset`) now includes the phrasebook, teaching FunctionGemma two more tools, `log_sleep` and `log_workout`. After retraining in `tools/finetune_functiongemma.ipynb`, the control model is asked before a big one when a sleep or workout message isn't read by the built-in rules. Its line is checked the same way.

### The phrasebook
Odd ways of saying sleep and workouts, each with the plain line Home Mind reads. It starts with a checked set and grows from four places: model readings of your messages, your corrections right after a log ("no, 6 hours", "that wasn't sleep"), cases you add on the Health tab, and new cases the overnight model writes (option `phrasebook_grow`). A written case is kept only if its line can be read, it has no number that wasn't said, and a second blind reading agrees; otherwise it waits for **Right / Wrong** on the Health tab.

A message shaped like an entry (same words, different numbers) is read with no model. Otherwise the most similar entries go into the model's prompt as examples. **Test the model** (`POST /api/phrasebook/eval`, `model?`) reads every entry with the others as examples and reports right, wrong logs (the ones that matter) and missed: a quick way to see whether a small local model is good enough. **Try** (`POST /api/phrasebook/try`) shows what the phrasebook, the built-in reading and (with `model`) a model make of a message. `GET /api/phrasebook/export` gives the pairs, e.g. for fine-tuning.

Sunday's evening brief carries the week in review. Suggested changes (fewer or more calories, a lighter week for a stalled lift, more steps) are listed on the Health tab and only apply when you press **Use** (`POST /api/fitness/accept`). It never suggests fewer than 1,500 kcal a day or losing more than about 1% of body weight a week. This is general guidance, not medical advice.

## Options

Every option, as on the Configuration tab (each has a longer description there). The ones that need thought before
you turn them on are listed after the tables.

**Models and answers**

| option | default | what |
|---|---|---|
| `models` | `haiku` (AI Task) | Models |
| `fallback` | haiku | Fallback models |
| `voice_seconds` | 8 | Voice time limit (seconds) |
| `embed_model` | empty | Embedding model (search by meaning) |
| `rerank_model` | empty | Re-ranking model |
| `librarian_model` | empty | Optional small model for memory graph routing on typed questions (empty = fast built-in SQLite router) |
| `archivist_model` | empty | Optional small model for async background memory graph ingestion after replies (empty = off during chat) |
| `control_model` | empty | Local control model |
| `trainer_url` | empty | Optional URL of a GPU/Ollama PC running `tools/trainer_worker.py` for one-click training from the Models tab (empty = 1-click Google Colab mode) |
| `trainer_token` | empty | Optional Bearer token for `trainer_url` |
| `rule_planner` | on | Plain commands without a model |
| `model_mode` | balanced | cloud, balanced (the local model for kinds it has earned) or private (nothing leaves the house) |
| `confidence_dial` | 0.55 | Below this confidence, a local plan goes to the cloud model, else Home Mind asks before acting |
| `local_model` | empty | A local chat model for answers in private mode; empty = notes alone |
| `shadow_rate` | 0.1 | Share of house requests also planned by a second model in the background (never acts), to compare them; 0 = off |
| `approval_phone` | empty | The phone that approves locks, garage doors, alarms and heaters (`notify.mobile_app_...`); empty = the first phone with the Home Assistant app |
| `safety_contact` | empty | Someone told when you describe an emergency, a double dose or a big decline (a notify service); empty = no one |
| `ha_user_token` | empty | A long-lived token from a non-admin Home Assistant user, for device control and readings; empty = the add-on's admin access |
| `ha_url` | empty | Where Home Mind reaches Home Assistant with that token; empty = `http://homeassistant:8123` |
| `answer_edges` | off | Repeat the best note before the question |
| `answer_notes_first` | off | Name the notes before answering |
| `use_preferences` | on | Use learned settings |

**People and privacy**

| option | default | what |
|---|---|---|
| `people` | empty | People |
| `personal_rule` | `first_person` | Personal notes |
| `unknown_caller` | `admin` | Calls with no speaker |
| `encrypt_secrets` | off | Encrypt secrets |
| `automation_pin` | empty | PIN for automations that open the house |

**The house**

| option | default | what |
|---|---|---|
| `home` | on | Watch the house |
| `home_controls` | empty | Things the brain may control |
| `home_fan_max` | 100 | Highest fan speed the brain may set (%) |
| `house_notices` | on | Notices about unusual house events |
| `comfort` | on | Bedroom comfort brain |
| `comfort_rooms` | empty | Comfort rooms |
| `calendar_quiet` | 4 entries | Calendars to leave out |

**Learning and upkeep**

| option | default | what |
|---|---|---|
| `overnight_enabled` | on | Overnight learning |
| `overnight_time` | `03:00` | Overnight learning time |
| `overnight_model` | empty | Overnight learning model |
| `overnight_structure` | off | Overnight, read what the patterns missed |
| `phrasebook_grow` | on | Each night, write new odd wordings for sleep and workouts; keep those a blind second reading agrees with |
| `persona_model` | empty | a model that rewords answers in the persona's voice (checked like the rules); empty = rules only |
| `away_alert_hours` | 3 | a notification when a model computer has been away this long, and when it's back; 0 = never |
| `db_snapshots` | 7 | daily database snapshots to keep; a damaged database is restored from the newest good one |
| `router` | on | with no model named, choose by the question: simple and private ones local first, harder ones to the cloud first |
| `router_local` | empty | models on your computers (empty = the fallback models on your network) |
| `router_cloud` | empty | models tried first for harder questions (empty = the other fallback models) |
| `learn_filing` | on | Learn where notes go |
| `fade_days` | 60 | Fade unused notes after N days |
| `events_keep_days` | 90 | Keep the detailed activity log for (days) |
| `checkin_old_facts` | on | Ask about old facts in the morning |
| `checkin_after_days` | 180 | Ask about facts older than (days) |

**Summaries and notifications**

| option | default | what |
|---|---|---|
| `morning_enabled` | on | Send the morning summary |
| `morning_time` | `07:30` | Morning summary time |
| `morning_notify` | empty | Morning summary goes to |
| `evening_enabled` | on | Send the evening home brief |
| `evening_time` | `21:00` | Evening brief time |
| `evening_notify` | empty | Evening brief goes to |
| `quiet_hours` | on | Quiet hours for phone notifications |
| `quiet_start` | `22:00` | Quiet hours start (HH:MM) |
| `quiet_end` | `07:00` | Quiet hours end (HH:MM) |
| `notify_daily_cap` | 0 | Most phone notifications a day |
| `patterns_checkins` | on | Fog check-ins |
| `patterns_checkin_time` | `11:00` | Check-in time |
| `patterns_evening_time` | `19:00` | Evening check-in time |
| `patterns_report_day` | 5 | Weekly patterns report day |
| `patterns_report_time` | `08:00` | Weekly patterns report time |

**Packs, tests and knowledge**

| option | default | what |
|---|---|---|
| `update_url` | `https://raw.githubusercontent.com/Yitz42/home-mind/main/packs/manifest.json` | Update manifest |
| `auto_update_packs` | on | Install new packs automatically |
| `auto_test_hours` | 0 | Run brain tests every N hours |
| `compare_models` | empty | Models to compare |
| `compare_days` | 0 | Compare models every (days) |
| `compare_suites` | recall, tricky, hard | Suites for the model comparison |
| `knowledge_food` | on | Food database |
| `knowledge_wikipedia` | on | Offline Wikipedia |
| `knowledge_window` | `00:00-07:00` | Download window |
| `log_level` | `info` | Log level |

**Backups**

| option | default | what |
|---|---|---|
| `backup_target` | empty | Off-site backup |
| `backup_url` | empty | Backup folder or URL |
| `backup_user` | empty | Backup user (or S3 access key) |
| `backup_password` | empty | Backup password (or S3 secret key) |
| `backup_passphrase` | empty | Backup passphrase |
| `backup_region` | empty | S3 region |
| `backup_every_hours` | 6 | Back up every (hours) |
| `backup_keep` | 14 | Backups to keep |

**Your other apps**

| option | default | what |
|---|---|---|
| `docs_folder` | `/share/home-mind/docs` | Documents folder |
| `markdown_folder` | empty | Markdown folder (two-way sync) |
| `markdown_minutes` | 10 | Markdown sync every (minutes) |
| `homebox_url` | empty | Homebox URL |
| `homebox_token` | empty | Homebox token |
| `paperless_url` | empty | Paperless-ngx URL |
| `paperless_token` | empty | Paperless-ngx API token |
| `label_url` | empty | Address for QR labels |

**Telegram, web and email**

| option | default | what |
|---|---|---|
| `telegram_token` | empty | Telegram bot token |
| `telegram_chats` | empty | Telegram chats allowed |
| `web_search` | `off` | Web lookups |
| `web_search_url` | empty | SearXNG address |
| `web_search_key` | empty | Brave Search API key |
| `email_host` | empty | Email (IMAP) server |
| `email_user` | empty | Email user |
| `email_password` | empty | Email password |
| `email_folder` | `INBOX` | Email folder |
| `email_senders` | empty | Email senders allowed |
| `email_require_verified` | on | Only mail your mail server says passed its sender check (DKIM, SPF or DMARC for the sender's own domain) is read |
| `email_authserv_id` | empty | Your mail server's name in Authentication-Results (e.g. `mx.google.com`); only that header is trusted, else the topmost |
| `exertion_zones` | off | Live exertion zones from a fresh heart-rate sensor |
| `exertion_threshold` | 0 | The exertion line in bpm from your clinician's plan (else resting + 15) |
| `other_assistants` | empty | Other assistants as guest front ends, as "key: name" |
| `firefly_url` | empty | Firefly III, read-only: this month's totals for its owner |
| `firefly_token` | empty | Firefly III personal access token |
| `firefly_owner` | empty | The person who may hear the Firefly III totals |
| `connected_answers` | on | "Why", "lately", "how am I", "really", "busy" questions answered from a brief of your own data, connected by a model |
| `deep_model` | empty | The model that connects the brief (a stronger one helps); empty uses the usual chain |
| `compare_answers` | on | Connected answers come with the quick answer beside them in the panel, and a tap to say which was better |
| `proposals_model` | empty | An Anthropic model asked (Batch API) to draft fixes for gaps; sees the schema and made-up data only |
| `travel_away_entity` | empty | What travel mode switches on (else input_boolean.away) |
| `monthly_spend_cap` | 0 (none) | Cloud models rest for the month once their spend reaches this many dollars |
| `email_minutes` | 10 | Check email every (minutes) |
**Before you turn these on**

- `home_controls`: set it yourself; empty means every script and scene (see **Controlling the house**).
- `unknown_caller: admin` (the default): the panel, Claude over MCP and the API with no speaker see every note,
  personal notes and secrets included. Choose `guest` if others can reach the panel or hold a token.
- `auto_update_packs`: installs procedures from `update_url` without asking. Until a public key is in
  `homemind/pack_keys.txt` the manifest is only sha256-checked, not signed.
- `web_search`: the question (never one holding a secret or a household name) leaves your network to SearXNG or
  Brave. Results are shown to the model as data, not instructions.
- `telegram_token` / `telegram_chats`: messages pass through Telegram; anyone in a listed chat asks and files notes
  as the person named for it.
- `email_*`: use a mailbox made for Home Mind. A From address can be forged, so `email_senders` is a filter, not
  proof; mail that reads like instructions is held for review.
- `encrypt_secrets` and `backup_passphrase`: without the vault key (`/api/vault/key`) sealed secrets can't be read,
  and without the passphrase an off-site backup can't be restored. Keep both somewhere safe.
- `automation_pin`: whoever knows it can install drafted automations that unlock doors, open the garage or disarm the
  alarm.
- `overnight_model` pointing at a cloud model: the day's conversations go to it, with secret values hidden.
- `knowledge_wikipedia` (on): about 50 GB of disk; `knowledge_food` (on): about 450 MB.
- Deleting for good (purge, `POST /api/item/{id}/purge`, `mind_purge_about`) can't be undone; see **Trust and privacy**.

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

Inside Home Assistant the web panel uses ingress and needs no token. Other callers send a token as header
`X-Home-Mind-Token` (or `Authorization: Bearer`): the main token in `/data/token` (admin), or one made on Models ›
Access tokens with scope **read** (ask, search, look), **write** (also remember, change, forget) or **admin**
(everything). A token without enough scope gets 403 saying what it needs; no token or an unknown one gets 401.
Bodies are JSON objects; errors come back as `{"error": "..."}` with status 400 (your request), 404 (no such
note), 502 (a model failed) or 500.

| method | path | scope | what |
|---|---|---|---|
| GET | `/` | read | the web panel |
| GET | `/api/status` | read | version, counts, models, bridge state |
| GET | `/api/recall` | read | search for a caller that writes its own answer (`q`, `limit`, `shelf`, `kind`, `old=1`, `raw=1`); secrets not asked for are masked |
| POST | `/api/ask` | read | answer a question (`question`, `model?`, `use_brain`, `voice`, `deadline`, `conversation_id`, `user`/`speaker`) |
| POST | `/api/remember` | write | file a note (`text`, `title?`, `shelf?`, `key?`, `kind?`, `tags?`, `scope?`, `owner?`) |
| GET | `/api/item/{id}` | read | one note with its versions (null if missing) |
| POST | `/api/item/{id}/{op}` | write; purge: admin | `forget`, `restore`, `revise`, `sensitive`, `feedback`, `confirm`, `photo`, `keep`, `move`, `scope`, `purge` (`confirm: true`) |
| GET | `/api/photo/{name}` | read | a note's photo |
| DELETE | `/api/item/{id}/photo/{name}` | write | remove a photo from a note |
| POST | `/api/purge_about` | admin | delete for good every note about a name (`name`, `confirm: true`) |
| GET | `/api/review` | read | held and unconfirmed notes |
| GET | `/api/browse` | read | shelves and the notes on one (`shelf`) |
| GET | `/api/recent` | read | recently added notes (`filter`, `hours`) |
| GET | `/api/inbox` | read | notes not on a shelf yet, with the suggested shelf |
| GET | `/api/reshelve` | read | notes filing would now put elsewhere (`run=1` checks now) |
| GET | `/api/tasks` | read | open tasks |
| POST | `/api/organize` | write | have a model choose title, shelf and key for a text |
| GET | `/api/structure` | read | what was worked out: chores, repeating things, places, fields (the Worked out tab) |
| GET | `/api/profile` | read | who is who (the profile shelf) |
| GET | `/api/tidy` | read; now=1: write | possible duplicates (`now=1` looks again) |
| GET | `/api/cases` | read | kept corrections (the corrections suite) |
| POST | `/api/cases` | write | keep a question as a correction case |
| POST | `/api/cases/{id}/remove` | write | drop a kept case |
| GET | `/api/thing` | read | everything known about one thing or place (`name`) |
| GET | `/api/label` | read | a QR label (SVG) for a thing, place or shelf (`text`, `base`) |
| GET | `/api/views` | read | saved views |
| POST | `/api/views` | write | save a view (`name`, `query`, `shelf`, `tags`, `kind`, `days`, `sort`, `limit` or `sql`) |
| GET | `/api/views/{name}` | read | run a saved view |
| DELETE | `/api/views/{name}` | write | delete a saved view |
| POST | `/api/sql` | read | read-only SQL on the `notes` and `links` views (`sql`) |
| GET | `/api/export` | admin | everything as JSON (`secrets=1`, `home=1`) |
| POST | `/api/import` | admin | merge an export in (`?dry_run=1` only shows what it would add) |
| POST | `/api/markdown/sync` | write | sync the Markdown folder now |
| GET | `/api/docs` | read | documents |
| POST | `/api/docs` | write | add documents (multipart files) |
| DELETE | `/api/docs/{did}` | write | remove a document |
| GET | `/api/models` | read | configured models |
| POST | `/api/models/check` | read | try every model, with each one's error |
| GET | `/api/usage` | read | model calls, tokens and cost per day (`days`) |
| GET | `/api/suites` | read | test suites |
| POST | `/api/tests` | write | start a test run (`suite`, `arm`, `model`, `seed`, `n`, ...) |
| GET | `/api/runs` | read | test runs (`limit`, `suite`) |
| GET | `/api/runs/{id}` | read | one run with its answers |
| GET | `/api/trend` | read | a suite's score over time (`suite`, `name`, `arm`) |
| GET | `/api/compare` | read | the latest model comparison |
| POST | `/api/compare/run` | admin | run the model comparison now |
| GET | `/api/events` | read | the activity log (`limit`) |
| GET | `/api/updates` | read | check the update manifest |
| POST | `/api/updates/apply` | admin | install pack updates |
| GET | `/api/knowledge` | read | food database and Wikipedia download state |
| GET | `/api/knowledge/food` | read | look a food up (`q`) |
| GET | `/api/knowledge/search` | read | search offline Wikipedia (`q`, `n`) |
| POST | `/api/knowledge/{op}` | write | `food` or `wiki`: start that download now |
| GET | `/api/home` | read | the house as Home Mind sees it (watched things, control list) |
| POST | `/api/home/act` | admin | switch one thing (`entity_id`, `action`, `value?`, `confirm?`) |
| POST | `/api/do` | admin | do what a sentence asks (`text`); never confirms locks |
| POST | `/api/converse` | admin | one voice turn for the bridge's conversation agent (`text`, `conversation_id?`, `user_id?`, `device_id?`); streams events with `Accept: application/x-ndjson` |
| GET | `/api/as_of` | read | a keyed fact at a time (`key`, `at`: seconds or ISO date, `by`: valid or recorded) |
| POST | `/api/today/checkin` | write | today's check-in (`state`: good, low or rough; `person?`) |
| GET | `/api/learned` | admin | what Home Mind has learned about house requests: each template, its status and evidence (`status?`) |
| GET | `/api/learned/weeks` | admin | saved weeks of learned records and the change log of the last two weeks |
| POST | `/api/learned/rollback` | admin | roll learned records back to a saved week (`week`, `confirm`); the forget list is applied after |
| GET | `/api/check` | admin | the cross-check of every store: database, forget list, learned records, decision log, snapshots, vault key, last test restore (`run?`) |
| POST | `/api/restore_test` | admin | try restoring the newest snapshot into a scratch copy and check it |
| GET | `/api/consent` | admin | changes waiting for a yes, agreed (on in 24 hours), done or cancelled, and whether today is a clear day (`status?`) |
| POST | `/api/consent/{id}` | admin | your answer on a change (`answer`: yes, no, or changed my mind) |
| GET | `/api/guest` | admin | whether guest mode is on |
| POST | `/api/guest` | admin | guest mode on or off (`on`) |
| GET | `/api/surprises` | admin | the surprises and gaps queue: missed predictions, failed actions, corrections, requests nothing could do, unanswered questions (`kind?`) |
| POST | `/api/surprises/{id}/seen` | admin | mark one seen |
| POST | `/api/experiments/plan` | admin | a written experiment plan, checked and put to consent (`question`, `change`, `kind`, `outcome?`, `pairs?`, `block_days?`, `washout_days?`) |
| GET | `/api/experiments` | admin | the experiment and its analysis so far |
| POST | `/api/experiments/adherence` | admin | did you do it today (`yes`, `day?`) |
| POST | `/api/experiments/stop` | admin | stop it now, with the result so far |
| GET | `/api/proposals` | admin | gaps seen 3 times in two weeks, cheapest fixes first, and module drafts |
| POST | `/api/proposals/{topic}` | admin | mark a proposal done, dismissed or open (`status`) |
| GET | `/api/plain/{view}` | admin | the four plain views: today, asks (one at a time), memory (`topic?`), rules |
| GET | `/api/assistants` | admin | other assistants set up as front ends, and who turned them off |
| POST | `/api/assistants` | admin | turn other assistants off or on for you (`off`) |
| POST | `/api/plain_voice` | admin | answers without the persona, for you (`on`) |
| GET | `/api/trackers` | admin | your trackers, entries waiting for details, and whether they're paused |
| POST | `/api/trackers` | admin | add a tracker (`name`, `kind?`, `cadence?`), stop one (`stop`), or add details (`confirm`, `details?`) |
| GET | `/api/talk` | read | your conversation in the panel (`conversation_id`, `turns` with their choices) |
| POST | `/api/talk` | write | one turn (`text`, `spoken`?): a command or a question, with `choices` and `expects` back |
| POST | `/api/talk/new` | write | start a new conversation |
| POST | `/api/answer_pick` | write | which was better, the connected answer or the quick one (`id`, `pick`: deep, quick, same) |
| GET | `/api/answer_picks` | read | how the picks add up |
| GET | `/api/issues` | read | known issues: general causes, your suspects, how often each symptom was mentioned |
| POST | `/api/issues` | write | add an issue or suspects (`name`, `words`?, `suspects`?, `source`: you or doctor) |
| DELETE | `/api/issues/{name}` | write | remove one of your issues |
| GET | `/api/meds` | admin | your medicines log (30 days); nobody reviews it, no interaction checks |
| POST | `/api/meds/window` | admin | how long after a log a second one is answered "You logged that at..." (`name`, `hours`) |
| GET | `/api/significant` | admin | this week's checklist of significant events (unanswered is unknown) |
| POST | `/api/significant` | admin | answer it (`{"a trip": "yes", ...}`) |
| GET | `/api/nudges` | admin | nudges, with how often each was ignored |
| POST | `/api/nudges` | admin | add a nudge for a goal you wrote (`goal`, `trigger`, `message`) |
| POST | `/api/travel` | admin | travel mode on or off (`on`) |
| GET | `/api/zones` | admin | exertion zones: on or off and why, and the current zone |
| POST | `/api/zones/mute` | admin | "not today": no exertion notice for the rest of the day |
| GET | `/api/things` | read | things Home Mind keeps track of, things noticed in notes waiting for a yes, look-alikes to merge |
| GET | `/api/things/{id}` | read | one thing, its links (only to notes you may see) and where it is |
| POST | `/api/things` | write | yes to a thing (`name`), no to one noticed (`dismiss`), merge look-alikes (`keep`, `merge`), or link two (`a`, `b`, `relation`) |
| GET | `/api/scenes` | read | kept scenes, previews running, and how each has done |
| POST | `/api/scenes/draft` | admin | draft a scene from words (`text`, `room?`, `preview?`) |
| POST | `/api/scenes/{name}` | admin | keep a preview (with edited `steps`), run a kept scene, or remove it (`do`) |
| GET | `/api/backoff` | admin | devices Home Mind's own automations are leaving alone after a person changed them |
| GET | `/api/spaces` | admin | personal spaces: rooms whose devices only their person can open up |
| POST | `/api/spaces` | admin | set a room's person (`room`, `person`; empty clears) |
| GET | `/api/spend` | admin | this month's cloud spend against `monthly_spend_cap` |
| GET | `/api/outbox` | admin | phone messages of the last week by what happened (sent, held, failed, expired, duplicate) |
| GET | `/api/learned/answers` | admin | what Home Mind has learned about answering questions: each answer, its note, status and evidence, and how often the tie-break was needed (`status?`) |
| GET | `/api/learned/match` | admin | which template some words would match, and whether it would act (`q`); never acts |
| POST | `/api/learned/{id}` | admin | your say on a template (`status`: dropped, or shadow to put it back on trial) |
| GET | `/api/modes` | admin | model mode, confidence line, which local models are awake, kinds earned and proposed |
| POST | `/api/modes` | admin | set the mode (`mode`: cloud, balanced or private; `person?`) |
| POST | `/api/modes/accept` | admin | say yes to a proposed kind (`kind`, like `light.on`); it is replayed through the local model first |
| GET | `/api/calibration` | admin | confidence against what happened: bins, ECE, suggested line |
| GET | `/api/now` | read | the now board for the viewer |
| POST | `/api/now/propose` | admin | a model's proposed board entry (`kind`: waiting_on, unsure_which, doing or note; `value`; `ttl?`; `owner?`), checked before writing |
| GET | `/api/worsening` | admin | the worsening alarm's numbers against your baseline |
| GET | `/api/scorecard` | admin | the last scorecard (house, memory, safety; never averaged) and the trend against v1 |
| POST | `/api/scorecard/run` | admin | run the scorecard now (`set?`, default v1; replays, never acts) |
| GET | `/api/labels/queue` | admin | this week's labelling queue (`size`, default 30) |
| POST | `/api/labels` | admin | label a decision (`decision_id`, `label`: right, wrong or unsure; `correct?`: what would have been right) |
| GET | `/api/labels/agreement` | admin | how often repeat labels agree with the first, with kappa |
| POST | `/api/labels/gold` | admin | make the gold set of 30 labelled cases |
| GET | `/api/shadow` | admin | the shadow baseline: wins, ties and losses by outcome, and cost per correct answer (`days`) |
| GET | `/api/burden` | admin | asks, corrections and redos per day, with abstains (`days`) |
| GET | `/api/timing` | admin | 95th-percentile response time and fallback rate by front end (`days`) |
| GET | `/api/safety` | read | the last daily safety suite: pass or fail, and each check |
| POST | `/api/safety/run` | admin | run the safety suite now |
| GET | `/api/access` | admin | every device's access (none, read, act), approval level and ladder rung, with changes waiting |
| POST | `/api/access` | admin | set a device's access (`entity_id`, `access`: stricter now, looser in 24 hours) and/or its ladder rung (`ladder`: ask, tell, do) |
| GET | `/api/decisions` | admin | the decision log, newest first (`limit`, `entity_id`, `status`) |
| GET | `/api/decisions/stats` | admin | expected versus seen per entity and action (`days`) |
| GET | `/api/decisions/profile` | admin | one device's outcome profile (`entity_id`) |
| POST | `/api/decisions/freeze` | admin | keep the current test cases under a name (`name`) |
| POST | `/api/replay` | admin | replay logged requests through the planner without acting (`model?`, `limit?`, `frozen?`) |
| GET | `/api/controls` | read | the Home tab: devices with their map spots, presets, comfort, weather, routines, plugs |
| GET | `/api/controls/printer/cover` | admin | the 3D print's preview picture (the Bambu cover image), 404 when there is none |
| POST | `/api/controls/{op}` | admin | `act` (`entity`, `service`, `data`; allow-listed per domain, values clamped), `scene` (`name`), `routine` (`entity`, `on`) |
| GET | `/api/tiles` | read | the House tab's tiles (each drawn by its domain, with its state) and the dashboards shown in the Dashboards sub-tab |
| GET | `/api/tiles/catalog` | read | Home Assistant entities a tile can show, not already added, matching `q`; at most 60 |
| GET | `/api/tiles/scenes` | read | the scene names of a light on the room map or a tile (`light`), from its Govee scene list (`select.<light>_scene`); pick one with `/api/controls/act` and `select.select_option` |
| GET | `/api/tiles/picture` | admin | the picture of an image, camera or speaker tile (`entity`), through Home Assistant's proxy; 404 when there is none |
| POST | `/api/tiles/{op}` | admin | `add` (`entity`, `name`), `remove` / `move` (`id`, `step`) / `rename` (`id`, `name`), `embed_add` (`name`, `path`), `embed_remove` (`path`). A tile lets the controls switch what is on it, never locks, alarms, valves, garage doors or gates |
| GET | `/api/today` | read | readiness, fog, food to eat first, protein so far, the bedroom, what was learned |
| GET | `/api/hub` | read | the Home page: place, log counts, Energy / Fog / Ready with seven days and factors, For you, food, reminders, calendar, house |
| GET | `/api/checkins` | read | check-ins by time: a day's (`?day=`) and the last seven days' (`?days=`), plus water and coffee today |
| POST | `/api/checkins` | write | one check-in: `{fog?, energy?, tags?, note?, minutes_ago?}` (as often as it changes) |
| POST | `/api/day_edit` | write | `{day, key, value}`: fix a day's sleep_h, wakeups, hrv or steps_prev (null clears); kept over the watch |
| POST | `/api/log` | write | `{kind, n?}`: kind water or coffee, n -1 takes the last one back; today's counts |
| POST | `/api/todo` | write | `{op, entity, item?, uid?}`: op add, done or undo on a Home Assistant to-do list, from the Home tiles |
| GET | `/api/house` | read | habits, usual ranges, notable changes, suggested automations |
| POST | `/api/house/learn` | write | work the habits out now |
| POST | `/api/house/suggestion/{sid}/{op}` | write; create: admin | `create` (installs it switched off) or `dismiss` a suggested automation |
| GET | `/api/automations` | read | automations Home Mind runs and their recent runs |
| POST | `/api/automations/draft` | write | draft an automation from a request (`request`, `model?`), checked against Home Assistant |
| POST | `/api/automations/install` | admin | install a checked draft, switched off (`config`, `pin` for ones that unlock) |
| POST | `/api/automations/{op}` | admin | `takeover`, `release`, `run`, `retire`, `reload` (`ids`, `id`, `all`) |
| GET | `/api/preferences` | read | learned light and fan levels |
| GET | `/api/memgraph/graph` | read | viewer-scoped Brain Graph snapshot (`dormant?`, `category?`, `notes?`, `limit?`) |
| POST | `/api/memgraph/link/{id}/{op}` | write | `confirm`, `retire` (`reason?`, `superseded_by?`), `forget`, or `delete` a learned MemGraph link |
| POST | `/api/memgraph/add` | write | add a general data link (`source?`, `relation`, `target`, `category?`, `sign?`, `quote?`) |
| POST | `/api/memgraph/close_era` | write | close a place/era (`name`, `replacement?`): retires all active links tied to it and records `SUPERSEDES` |
| GET | `/api/control_model/dataset` | admin | training data for the local control model, from your control list (`part=train` or `test`, `paraphrase=1`) |
| GET | `/api/trainer/status` | admin | small-LLM trainer status: local GPU worker health (`trainer_url`), all 3 targets (`control`, `persona`, `memgraph`), Colab links and active jobs |
| GET | `/api/trainer/dataset/{target}` | admin | one-click single-file training bundle for `control`, `persona`, or `memgraph` (`download=1`, `format=jsonl` for memgraph) |
| POST | `/api/trainer/start` | admin | one-click training trigger on the connected GPU worker (`target`, optional `base_model`, `epochs`, `paraphrase`, `ollama_name`) |
| POST | `/api/trainer/cancel` | admin | cancel an active training job on the GPU worker (`job_id`) |
| GET | `/api/comfort` | read | comfort state for each room |
| POST | `/api/comfort/vote` | write | a cold / hot / ok vote (`feeling`, `room?`) |
| POST | `/api/comfort/cycle` | write | run a comfort cycle now |
| GET | `/api/comfort/check` | read | which of a room's entities Home Assistant doesn't have (`room`) |
| POST | `/api/body/backfill` | write | fill in past body-signal readings |
| GET | `/api/calendar` | read | calendar events for a day (`day`, `q`) |
| POST | `/api/calendar/sync` | write | sync calendars now |
| GET | `/api/fitness` | read | fitness dashboard: goals, auto-baseline, adaptive daily plan, progressive routine, progress, today's tally, workouts, lift/cardio/weight series, volume, bests, stalls, suggestions, review |
| GET | `/api/fitness/review` | read | the week in review and suggested changes |
| GET | `/api/fitness/workouts` | read | `days?`, `exercise?`: logged workouts with bests and stalled lifts |
| GET | `/api/fitness/logs` | read | `kind` (sleep or workout), `days?`: recent logs to look at and fix |
| POST | `/api/fitness/{op}` | write | `goals` (goal fields), `reset_goals` (`key?`: unpin one or all to auto-baseline), `prefs` (`sync_adaptive?`, `use_memgraph?`, `use_calendar?`, `split?`, `equip?`, `mins?`, `note?`), `routine` (`split?`, `equip?`, `mins?`, `note?`), `workout` (`text`, `at?`), `weigh` (`weight`, `unit?`), `accept` (`index`: a suggestion from the review), `edit` (`kind`, `id`, `text`: what it should say), `delete` (`kind`, `id`) |
| GET | `/api/persona` | read | the persona's settings, its emotions, examples, and instructions for a voice agent |
| POST | `/api/persona` | write | any of `enabled`, `name`, `address` (name, sir, madam, none), `wit` (off, low, normal), `ssml`; with `person`, only `enabled`, `address` and `wit` for that person (`null` = back to the house's) |
| GET | `/api/persona/corpus` | read | `n?`, `download?`: a made-up practice set (plain answers per emotion), the persona's instructions and examples, for training a style model |
| GET | `/api/persona/emotion` | read | `text`: the emotion of something about to be spoken (the marker its answer got, else read from the words), for the Home Mind Voice app |
| POST | `/api/persona/try` | write | `text`, `emotion`: the persona's version of a plain answer, and its SSML |
| GET | `/api/phrasebook` | read | `status?`, `source?`, `limit?`: entries and counts |
| GET | `/api/phrasebook/export` | read | every active pair, for training |
| POST | `/api/phrasebook/{op}` | write | `add` (`said`, `plain`), `approve`/`reject`/`remove` (`id`), `try` (`text`, `model?`), `grow` (`model?`, `n?`), `eval` (`model?`, `limit?`) |
| GET | `/api/pantry` | read | pantry items and goals |
| GET | `/api/pantry/meals` | read | `day?` (YYYY-MM-DD): meals logged on that day with macros, totals, and nutrition goals |
| POST | `/api/pantry/{op}` | write | `add`, `photo`, `ate` (`when?` like `8am`, `day?`), `meal_edit` (`id`, `text?`, `when?`, `calories?`, `protein_g?`, `carbs_g?`, `fat_g?`, `fiber_g?`, `sugar_g?`, `reestimate?`), `meal_delete` (`id`), `used`, `tossed`, `part` (`id?`, `name?`, `fraction?`, `used?`, `remaining?`: take part of a stored item), `remove`, `suggest`, `week` (`prep_day?`, `prep_time?`), `layout`, `plan_cell`, `swap`, `plan_clear`, `meal_info`, `meal_steps` (`day_index?`, `slot?`, `day?`, `force?`), `calendar` (`include`: `prep` or `all`), `shop`, `goals`, `today`, `estimate` (`ids?`: protein and calories for logged meals) |
| GET | `/api/recipes` | read | `q?`, `slot?`: saved recipes and favorite meals with exact macros and ingredients |
| POST | `/api/recipes/{op}` | write | `save` (recipe fields), `delete` (`id`), `log` (`id`, `servings?`, `day?`, `when?`), `shop` (`id`: add missing ingredients to shopping list), `barcode` (`upc`: look up nutrition from local USDA food DB) |
| GET | `/api/habits` | read | `day?` (YYYY-MM-DD): medications, supplements, and daily habit streaks with 7-day completion |
| POST | `/api/habits/{op}` | write | `add` (`name`, `kind?`: `med`/`habit`, `dose?`, `schedule?`, `scope?`), `toggle` (`id`, `day?`, `done?`), `delete` (`id`) |
| GET | `/api/maintenance` | read | home maintenance items, recurring chores, warranties, and linked manual docs |
| POST | `/api/maintenance/{op}` | write | `add` (`name`, `category?`, `every_days?`, `next_due?`, `warranty_until?`, `doc_name?`, `notes?`), `done` (`id`, `note?`), `delete` (`id`) |
| GET | `/api/patterns` | read | brain-fog report, findings, experiment and daily rows (`days`) |
| POST | `/api/patterns/{op}` | write | `refresh`, `send`, `experiment`, `ask`, `answer`, `weather` |
| GET | `/api/journal` | read | daily journal summaries |
| POST | `/api/journal/summarize` | write | summarize a day now |
| GET | `/api/morning` | read | preview the morning summary |
| POST | `/api/morning/send` | write | send the morning summary now |
| GET | `/api/evening` | read | preview the evening brief |
| POST | `/api/evening/send` | write | send the evening brief now |
| GET | `/api/overnight` | read | last overnight run and turns waiting |
| POST | `/api/overnight/run` | write | run overnight learning now |
| GET | `/api/people` | read | who Home Mind can recognise, and how |
| GET | `/api/connections` | read | Telegram, web lookups and email: on or off, last result |
| POST | `/api/email/check` | admin | read the mailbox now |
| GET | `/api/backup` | admin | off-site backup state |
| POST | `/api/backup/now` | admin | make an off-site backup now |
| GET | `/api/vault` | read | whether secrets are encrypted (vault state) |
| GET | `/api/vault/key` | admin | the key that opens sealed secrets |
| GET | `/api/tokens` | admin | access tokens |
| POST | `/api/tokens` | admin | make a token (`name`, `scope`, `person?`, `days?`: expires after, default 90, 0 = never); shown once |
| DELETE | `/api/tokens/{tid}` | admin | revoke a token |
| GET | `/api/control` | read | the switches, numbers, times and buttons the bridge shows |
| POST | `/api/control/set` | admin | change one (`key`, `value`) |
| POST | `/api/control/reset` | admin | drop changes made from Home Assistant (`key?`) |
| POST | `/api/control/run/{action}` | admin | press a button: `overnight`, `morning`, `evening`, `house`, `calendars`, `models`, `packs`, `comfort`, `patterns`, `tests`, `release_automations` |
| POST | `/mcp` | read; each tool its own | MCP (Streamable HTTP) for Claude and other clients |
| GET | `/mcp` | read | MCP: no server-sent stream (405) |
| DELETE | `/mcp` | read | MCP: end a session |

**MCP tools** (`POST /mcp`; the scope is what a token needs for the tool):

| tool | scope | what |
|---|---|---|
| `mind_recall` | read | find notes for a query |
| `mind_get` | read | one note with its history |
| `mind_ask` | read | answer a question with a connected model and the memory |
| `mind_remember` | write | file a note (`inferred` for what Claude worked out) |
| `mind_update` | write | a new version of a note |
| `mind_organize` | write | a model chooses title, shelf and key, then files it |
| `mind_forget` | write | archive a note (recoverable) |
| `mind_forget_about` | write | archive everything about a name |
| `mind_restore` | write | bring an archived note back |
| `mind_confirm` | write | confirm a note learned overnight or inferred |
| `mind_feedback` | write | say whether a recalled note helped |
| `mind_purge` | admin | delete a note for good (`confirm: true`) |
| `mind_purge_about` | admin | delete for good everything about a name |
| `mind_review` | read | held and unconfirmed notes |
| `mind_browse` | read | the shelf tree and a shelf's notes |
| `mind_tasks` | read | tasks, soonest first |
| `mind_sql` | read | read-only SQL on `notes` and `links` |
| `mind_view` | read; save/delete: write | saved views |
| `mind_home` | read | the live house state |
| `mind_house` | read | habits, usual ranges, suggested automations |
| `mind_control` | admin | switch one thing on the control list |
| `mind_do` | admin | do what a sentence asks in the house |
| `mind_draft_automation` | read | draft an automation, checked against Home Assistant; installs nothing |
| `mind_install_automation` | admin | install a checked draft |
| `mind_automations` | read; other actions: admin | automations Home Mind runs; takeover, release, run |
| `mind_comfort` | write | comfort status and votes |
| `mind_pantry` | write | the pantry |
| `mind_meals` | read | meal ideas from the pantry |
| `mind_phrasebook` | read | the phrasebook of odd wordings (`add`, `approve`, `reject`, `remove` need write) |
| `mind_fitness` | read | fitness goals, progress, workouts, weekly review (`log` and setting `goals` need write) |
| `mind_models` | read | connected models (`check` pings them) |
| `mind_status` | read | counts, version, packs, models |
| `mind_updates` | read; apply: admin | check for updates, install packs |
| `mind_run_test` | admin | start a test run |
| `mind_test_results` | read | a run's results |
| `mind_trend` | read | how a test number moved |
