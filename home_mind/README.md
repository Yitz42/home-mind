# Home Mind

A brain for small models, as a Home Assistant app: memory with versions and shelves, connections to local
models (Ollama, OpenAI-compatible), Claude (Home Assistant AI Task or the API), an MCP endpoint for Claude,
knowledge packs that update, and random tests that measure how well it adapts to new information and finds what
it has organized.

## Features

**Home, the panel's first page.** One screen that runs the day: weather that pulls down from the top, log bubbles
(water, coffee, meal, note) above an ask bar that answers right under itself, Energy / Fog / Ready tabs with seven
days of history and the readings behind them (tap one to fix it), a "For you" card with one thing at a time, and
four tiles picked by the time of day and whether you're home (Reminders, Bedroom, Food, Calendar, Shopping, House,
Tonight, Last night, Tomorrow). On a phone the tiles fill the screen and open as windows.

**Memory.** Notes, facts, tasks and procedures on shelves, with every change kept as a version. A new value for the
same thing replaces the old one, and questions about a past day get what was true then. Search by words, by meaning
(an embedding model), by the things a note names, and by links between notes. Documents (PDF, text, Markdown) are
searched with the page named. Export / import, a two-way Markdown folder (Obsidian, Syncthing), photos on notes, and
read-only SQL.

**Models.** Local models (Ollama, OpenAI-compatible, a GPU computer at home), Claude through Home Assistant or the
API, and an MCP endpoint so Claude chats share the same memory. A router sends simple and private questions to a
local model and harder ones to the cloud; private mode keeps everything in the house. Spend caps, usage per model,
and fallbacks when a model is asleep or slow.

**Answers.** Quick answers from your notes, "connected" answers for why / lately / how-am-I questions built from a
brief of your own data, and number questions (sleep, steps, weight, protein, lifts) computed from the data rather
than by a model. Answers it keeps getting right become instant templates.

**Voice and the house.** A Home Assistant conversation agent, with the Jeeves persona and an optional Voicebox voice.
Plain commands ("den lamp to 40%", "lights off and lock up") are read by rules with no model, then a small local
control model, then your general model. Requests it has learned become instant reflexes. Commands said in a room act
on that room. Locks, alarms and garage doors need approval on your phone, and every action is in a decision log.

**Health and the day.** Check-ins by time (fog, energy, tags, a note), water and coffee logs, sleep, HRV and steps
from your watch, meals and the pantry with meal ideas from what's in stock, fitness goals and workouts in plain
words, trackers and medicines, experiments against your usual, and a weekly patterns report on what goes with foggy
days.

**The house over time.** Habits, usual ranges and notable changes worked out each night, suggested automations, a
morning summary and an evening brief, bedroom comfort that learns from cold / hot votes (one or more rooms), and
learned levels for lights and fans.

**Reaching it.** The panel, voice, Claude (MCP), Telegram, a mailbox for receipts and notes, and web lookups for
current facts. Each person is recognised and keeps their own notes; guests and other assistants get household notes
only.

**Keeping it sound.** Tokens per client, encrypted secrets, off-site encrypted backups, daily database snapshots,
quiet hours and caps on phone messages, knowledge packs that update, and random tests that measure how well it adapts
to new information and finds what it has filed.

## Install

Settings › Apps › App store › ⋮ › Repositories, add `https://github.com/Yitz42/home-mind`, then install
**Home Mind**. See [DOCS](home_mind/DOCS.md).

## Layout

- `home_mind/` the app (add-on): `rootfs/app/homemind` is the Python service, `rootfs/app/bridge` the
  Home Assistant integration it installs
- `packs/` knowledge packs and the update manifest
- `tests/` tests that run without Home Assistant
