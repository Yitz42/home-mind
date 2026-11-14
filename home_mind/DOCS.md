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

Arms: **brain** (no model: the top search result is the answer), **model+brain** (model gets what the brain
found), **model+context** (model reads the whole log, no brain), **model** (alone). Ingest **model** lets the
model choose title, shelf and key for each item itself, so `locate` then measures how well it organized.

Numbers recorded: accuracy, found first (hit@1), in top 5, MRR, how often an old version ranked above the newest
(stale on top), shelf found, time per answer. Results are kept for trends and pushed to Home Assistant as
`sensor.home_mind_test_<suite>` and `sensor.home_mind_items`.

## Updates

- App code: Settings › Apps › Home Mind › Update (or turn on auto-update).
- Packs: installed automatically every 12 hours when "Install new packs automatically" is on, or from the
  Updates tab.

## API

Inside Home Assistant the web panel uses ingress. Other callers need the token in `/data/token` as header
`X-Home-Mind-Token`. MCP is at `POST /mcp`.
