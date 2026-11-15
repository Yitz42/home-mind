"""MCP (Model Context Protocol) server over HTTP: JSON-RPC in a POST, JSON back. This is what Claude connects to
(through the Home Mind Bridge webhook in Home Assistant)."""
import json

from . import suites
from .version import VERSION

PROTOCOLS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = (
    "Home Mind is a long-term memory that lives on Yitz's Home Assistant. Use mind_recall before answering anything "
    "that may depend on what was stored before, and mind_remember for things worth keeping. Give facts that can change "
    "a key ('subject|attribute') so a new value replaces the old one. Use shelves (paths like 'projects/pebble') to "
    "organize, and mind_browse to see how things are organized. Tests: mind_run_test, then mind_test_results.")


def S(props, req=()):
    return {"type": "object", "properties": props, "required": list(req)}


STR, INT, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}

TOOLS = [
    ("mind_remember", "Store something. A key ('subject|attribute') makes a new value supersede the old one (kept as "
     "history). Kinds: fact, note, task, procedure, episode.",
     S({"text": STR, "title": STR, "kind": STR, "shelf": STR, "tags": {"type": "array", "items": STR}, "key": STR,
        "meta": {"type": "object"}}, ["text"])),
    ("mind_recall", "Find stored items for a query, best first. Optional shelf and kind filters. include_old adds "
     "superseded versions.", S({"query": STR, "shelf": STR, "kind": STR, "limit": INT, "include_old": BOOL}, ["query"])),
    ("mind_get", "One item by id, with its version history.", S({"id": INT}, ["id"])),
    ("mind_update", "Write a new version of an item (title, text, shelf, tags, key, meta). The old one is kept.",
     S({"id": INT, "title": STR, "text": STR, "shelf": STR, "tags": {"type": "array", "items": STR}, "key": STR,
        "meta": {"type": "object"}}, ["id"])),
    ("mind_forget", "Archive an item (recoverable; nothing is deleted).", S({"id": INT}, ["id"])),
    ("mind_forget_about", "Forget everything about a name: archives every note that mentions it, including notes "
                          "linked to them (recoverable).", S({"name": STR}, ["name"])),
    ("mind_restore", "Bring an archived or faded item back.", S({"id": INT}, ["id"])),
    ("mind_confirm", "Mark a note learned overnight from a conversation as checked (it stops being UNCONFIRMED).",
     S({"id": INT}, ["id"])),
    ("mind_purge", "Delete an item for good, with its history, index entry and log rows (cannot be undone). Only when "
                   "the user clearly asks to delete it permanently; confirm must be true.",
     S({"id": INT, "confirm": BOOL}, ["id", "confirm"])),
    ("mind_purge_about", "Delete for good everything that mentions a name (cannot be undone). Only when the user clearly "
                         "asks; confirm must be true.", S({"name": STR, "confirm": BOOL}, ["name", "confirm"])),
    ("mind_browse", "The shelf tree under a shelf (counts), and the items on that shelf.", S({"shelf": STR})),
    ("mind_tasks", "Tasks (kind 'task', meta due/priority/project/done), soonest first. status: open, done, all.",
     S({"project": STR, "status": STR, "due_before": {"type": "number"}})),
    ("mind_feedback", "Say whether a recalled item was useful (it ranks higher or lower afterwards).",
     S({"id": INT, "helpful": BOOL, "note": STR}, ["id", "helpful"])),
    ("mind_ask", "Answer a question with one of the connected models, using the brain's memory (use_brain false: the "
     "model alone).", S({"question": STR, "model": STR, "use_brain": BOOL, "shelf": STR, "expand": BOOL, "voice": BOOL,
        "conversation_id": STR},
       ["question", "model"])),
    ("mind_organize", "Let a connected model choose title, shelf, tags and key for a note, then store it.",
     S({"text": STR, "model": STR}, ["text", "model"])),
    ("mind_models", "List connected models; check=true pings each one.", S({"check": BOOL})),
    ("mind_run_test", "Start a test. suite: " + ", ".join(f"{k} ({v})" for k, v in suites.SUITE_INFO.items())
     + ". arm: brain (no model), model+brain, model+context (model reads the whole log), model (alone). ingest: raw, "
     "or model (the model files each item itself). keyed: facts come with keys. expand: in model+brain, the model also "
     "suggests other search words. Returns a run id.",
     S({"suite": STR, "arm": STR, "model": STR, "seed": INT, "n": INT, "ingest": STR, "keyed": BOOL, "expand": BOOL},
       ["suite"])),
    ("mind_test_results", "A run's results by id (details=true for every question), or the latest runs.",
     S({"id": STR, "details": BOOL, "limit": INT, "suite": STR})),
    ("mind_trend", "How a test number moved over time. name: accuracy, hit1, hit5, mrr, stale_above, shelf_ok.",
     S({"suite": STR, "name": STR, "arm": STR}, ["suite"])),
    ("mind_comfort", "Bedroom comfort. action: status (learned preferences per situation, votes, which body/room "
     "signals are confirmed, recent actions), vote (feeling: cold, hot or ok — stores a snapshot and reacts), or cycle "
     "(run the controller now).", S({"action": STR, "feeling": STR}, ["action"])),
    ("mind_home", "The house as Home Assistant reports it. With q: the live state of the things a question names. "
                  "Without: every thing the brain watches, its state, and whether the brain may control it.",
     S({"q": STR})),
    ("mind_control", "Control one thing in the house (only things on the control list, only while "
                     "input_boolean.mind_control_enabled is on). action: on, off, toggle, brightness (1-100), color "
                     "(name), speed (fan %), humidity (%), run (script/scene). Logged in memory.",
     S({"entity_id": STR, "action": STR, "value": STR}, ["entity_id", "action"])),
    ("mind_do", "A request in words ('dim the desk light to 30%'): a connected model picks one action from the "
                "control list, then it is checked and done like mind_control.", S({"text": STR, "model": STR}, ["text"])),
    ("mind_automations", "Home Assistant automations run by Home Mind. action: list (who runs each, last run), "
                         "takeover (ids, or all=true), retire (stop running and leave the HA copy off), release (hand back to Home Assistant; ids or all), run (id, "
                         "optional trigger id), reload (re-read configs after editing in Home Assistant).",
     S({"action": STR, "ids": {"type": "array", "items": STR}, "all": BOOL, "id": STR, "trigger": STR}, ["action"])),
    ("mind_pantry", "The pantry. action: list (what's in the house with days left), add (text like 'chicken, 2 lb ground "
                    "beef and eggs', optional location fridge/freezer/pantry), used or tossed (text), goals (show), "
                    "today (tired day? weight trend).", S({"action": STR, "text": STR, "location": STR}, ["action"])),
    ("mind_meals", "Meal ideas from what's in the house: uses what expires first, fits today (tired days lower-carb) and "
                   "the goals (more protein, fewer calories). meal: breakfast/lunch/dinner/snack; request: anything extra.",
     S({"meal": STR, "request": STR, "model": STR}, [])),
    ("mind_status", "Counts, version, packs, models.", S({})),
    ("mind_updates", "Check for updates (apply=true installs available packs).", S({"apply": BOOL})),
]


class MCP:
    def __init__(self, app):
        self.app = app  # the App in main.py

    async def handle(self, msg):
        if isinstance(msg, list):
            out = [r for r in [await self.handle(m) for m in msg] if r is not None]
            return out or None
        mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
        if mid is None:  # a notification
            return None
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                res = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "home-mind", "title": "Home Mind", "version": VERSION},
                       "instructions": INSTRUCTIONS}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                try:
                    out = await self.call(name, args)
                    res = {"content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}],
                           "isError": False}
                except Exception as e:  # noqa: BLE001 - tool errors go back to the model
                    res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
            elif method in ("resources/list", "prompts/list"):
                res = {method.split("/")[0]: []}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": res}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}

    async def call(self, name, a):
        app, st = self.app, self.app.mind.store
        if name == "mind_remember":
            return st.remember(text=a["text"], title=a.get("title", ""), kind=a.get("kind", "note"),
                               shelf=a.get("shelf", "inbox"), tags=a.get("tags"), key=a.get("key"),
                               meta=a.get("meta"), source="mcp")
        if name == "mind_recall":
            from .mind import lookup
            return lookup(st, a["query"], int(a.get("limit") or 8), a.get("shelf"), a.get("kind"),
                          bool(a.get("include_old")))
        if name == "mind_get":
            return st.get(int(a["id"]), history=True)
        if name == "mind_update":
            return st.revise(int(a["id"]), title=a.get("title"), body=a.get("text"), shelf=a.get("shelf"),
                             tags=a.get("tags"), key=a.get("key"), meta=a.get("meta"))
        if name == "mind_forget":
            return st.set_status(int(a["id"]), "archived")
        if name == "mind_forget_about":
            return st.forget_about(str(a["name"]))
        if name == "mind_confirm":
            return st.confirm(int(a["id"]))
        if name in ("mind_purge", "mind_purge_about"):
            if a.get("confirm") is not True:
                return {"error": "not deleted: confirm must be true, and only when the user asked to delete for good"}
            return st.purge([int(a["id"])]) if name == "mind_purge" else st.purge_about(str(a["name"]))
        if name == "mind_restore":
            return st.set_status(int(a["id"]), "active")
        if name == "mind_browse":
            return st.browse(a.get("shelf"))
        if name == "mind_tasks":
            return st.tasks(project=a.get("project"), status=a.get("status") or "open", due_before=a.get("due_before"))
        if name == "mind_feedback":
            return st.feedback(int(a["id"]), bool(a["helpful"]), a.get("note", ""))
        if name == "mind_ask":
            r = await app.mind.ask(a["question"], a["model"], use_brain=a.get("use_brain", True), shelf=a.get("shelf"),
                                   expand=bool(a.get("expand")), voice=bool(a.get("voice")),
                                   conversation_id=a.get("conversation_id"))
            r["hits"] = [{"id": h["id"], "title": h["title"], "score": h["score"]} for h in r["hits"]]
            return r
        if name == "mind_organize":
            return await app.mind.organize(a["text"], a["model"], source="mcp")
        if name == "mind_models":
            return await app.mind.models.check_all() if a.get("check") else app.mind.models.list()
        if name == "mind_run_test":
            run = app.mind.start_test(a["suite"], a.get("arm") or "brain", a.get("model"), a.get("seed"), a.get("n"),
                                      a.get("ingest") or "raw", bool(a.get("keyed")),
                                      bool(a.get("expand")))
            return {"id": run["id"], "status": run["status"], "seed": run["seed"],
                    "note": "call mind_test_results with this id when it's done"}
        if name == "mind_test_results":
            if a.get("id"):
                r = st.run(a["id"])
                if r and not a.get("details"):
                    wrong = [d for d in r["details"] if not d.get("correct")]
                    r["details"] = {"wrong_examples": wrong[:8], "n_wrong": len(wrong)}
                return r
            return st.runs(limit=int(a.get("limit") or 20), suite=a.get("suite"))
        if name == "mind_trend":
            return st.trend(a["suite"], a.get("name") or "accuracy", a.get("arm"))
        if name == "mind_comfort":
            act = a.get("action", "status")
            if act == "vote":
                return await app.comfort.vote(a.get("feeling", ""), source="claude")
            if act == "cycle":
                return await app.comfort.cycle(reason="claude")
            return app.comfort.status()
        if name == "mind_home":
            if not app.home.catalog:
                await app.home.refresh()
            if a.get("q"):
                return {"now": app.home.now_lines(a["q"]) or "nothing in the house matches"}
            return {"things": [{"entity_id": e, "now": app.home.describe(e), "controllable": app.home.allowed(e)}
                               for e in sorted(app.home.catalog)]}
        if name == "mind_control":
            if not app.home.catalog:
                await app.home.refresh()
            return await app.home.act(a["entity_id"], a["action"], a.get("value"), source="claude")
        if name == "mind_do":
            return await app.do(a["text"], a.get("model"), source="claude")
        if name == "mind_automations":
            eng, act = app.engine, a.get("action", "list")
            if act == "list":
                return {"automations": eng.summary(), "recent": eng.log[-15:]}
            ids = a.get("ids") or []
            if a.get("all"):
                ids = [e for e, x in eng.autos.items() if x["ha_state"] == "on" or e in eng.owned]
            if act == "takeover":
                return await eng.takeover(ids)
            if act == "release":
                return await eng.release(ids or sorted(eng.owned))
            if act == "retire":
                return await eng.retire(ids)
            if act == "run":
                return await eng.run_now(a["id"], a.get("trigger"))
            if act == "reload":
                await eng.load()
                return {"loaded": len(eng.autos)}
            raise ValueError(f"unknown action {act}")
        if name == "mind_pantry":
            pa, act = app.pantry, a.get("action", "list")
            if act == "list":
                return {"items": pa.items(), "text": pa.lines()}
            if act == "add":
                return {"added": [pa.add(n, am, a.get("location"), source="claude") for n, am in pa.parse_list(a["text"])]}
            if act in ("used", "tossed"):
                return {"done": [pa.use(n, "used" if act == "used" else "tossed") for n, _ in pa.parse_list(a["text"])]}
            if act == "goals":
                return pa.goals()
            if act == "today":
                return await pa.today()
            raise ValueError(f"unknown action {act}")
        if name == "mind_meals":
            return await app.pantry.suggest(a.get("meal"), a.get("model"), 3, a.get("request", ""))
        if name == "mind_status":
            return app.status()
        if name == "mind_updates":
            return await (app.updater.apply() if a.get("apply") else app.updater.check())
        raise ValueError(f"no tool {name}")
