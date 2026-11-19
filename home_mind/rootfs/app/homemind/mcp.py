"""MCP (Model Context Protocol) server over HTTP: JSON-RPC in a POST, JSON back. This is what Claude connects to
(through the Home Mind Bridge webhook in Home Assistant)."""
import json

from . import suites
from .version import VERSION

PROTOCOLS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = (
    "Home Mind is a long-term memory that lives on this home's Home Assistant. Use mind_recall before answering anything "
    "that may depend on what was stored before, and mind_remember for things worth keeping. Give facts that can change "
    "a key ('subject|attribute') so a new value replaces the old one. Use shelves (paths like 'projects/pebble') to "
    "organize, and mind_browse to see how things are organized. Tests: mind_run_test, then mind_test_results.")


def S(props, req=()):
    return {"type": "object", "properties": props, "required": list(req)}


STR, INT, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}

TOOLS = [
    ("mind_remember", "Store something. A key ('subject|attribute') makes a new value supersede the old one (kept as "
     "history). Kinds: fact, note, task, procedure, episode. Set inferred true when you worked it out or guessed it "
     "rather than the user telling you: it then ranks below what they said, and doesn't replace it, until they "
     "confirm it.",
     S({"text": STR, "title": STR, "kind": STR, "shelf": STR, "tags": {"type": "array", "items": STR}, "key": STR,
        "meta": {"type": "object"}, "inferred": BOOL, "scope": STR, "owner": STR}, ["text"])),
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
    ("mind_review", "Notes waiting for a person: held because they read like instructions, or inferred and not "
                    "confirmed. Confirm with mind_confirm, drop with mind_forget.", S({})),
    ("mind_house", "What the house usually does: habits found in its history, suggested automations for things done "
                   "by hand, usual ranges of readings, and today's notable changes.", S({})),
    ("mind_draft_automation", "Draft a Home Assistant automation from a request with a connected model. Nothing is "
                              "installed; the result lists any problems (unknown entities or services, actions that "
                              "open the house without the PIN).", S({"request": STR, "model": STR}, ["request"])),
    ("mind_install_automation", "Install a drafted automation in Home Assistant (switched off unless enable). Only "
                                "when the user asked for it; it is checked again first.",
     S({"config": {"type": "object"}, "enable": BOOL, "pin": STR}, ["config"])),
    ("mind_confirm", "Mark a note learned overnight from a conversation as checked (it stops being UNCONFIRMED).",
     S({"id": INT}, ["id"])),
    ("mind_purge", "Delete an item for good, with its history, index entry and log rows (cannot be undone). Only when "
                   "the user clearly asks to delete it permanently; confirm must be true.",
     S({"id": INT, "confirm": BOOL}, ["id", "confirm"])),
    ("mind_purge_about", "Delete for good everything that mentions a name (cannot be undone). Only when the user clearly "
                         "asks; confirm must be true.", S({"name": STR, "confirm": BOOL}, ["name", "confirm"])),
    ("mind_browse", "The shelf tree under a shelf (counts), and the items on that shelf.", S({"shelf": STR})),
    ("mind_sql", "Read-only SQL over the memory: SELECT on the views notes(id, kind, key, shelf, status, replaces, "
     "source, title, body, tags, created, updated, created_ts, use_count, strength, category, owner, scope) and "
     "links(a, b, kind). Secret text shows as [secret]. Up to 500 rows.", S({"q": STR}, ["q"])),
    ("mind_view", "Saved views (named searches). action: list (default), run (name), save (name with query/shelf/kind/tags/"
     "days/sort/limit or sql), remove (name).",
     S({"action": {"type": "string", "enum": ["list", "run", "save", "remove"]}, "name": STR, "query": STR, "shelf": STR,
        "kind": STR, "tags": {"type": "array", "items": STR},
        "days": {"type": "number"}, "sort": STR, "limit": INT, "sql": STR})),
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
    ("mind_comfort", "Bedroom comfort. action: status (default; learned preferences per situation, votes, which body/room "
     "signals are confirmed, recent actions), vote (feeling: cold, hot or ok — stores a snapshot and reacts), or cycle "
     "(run the controller now).",
     S({"action": {"type": "string", "enum": ["status", "vote", "cycle"]},
        "feeling": {"type": "string", "enum": ["cold", "hot", "ok"]}})),
    ("mind_home", "The house as Home Assistant reports it. With q: the live state of the things a question names. "
                  "Without: every thing the brain watches, its state, and whether the brain may control it.",
     S({"q": STR})),
    ("mind_control", "Control one thing in the house (only things on the control list, only while "
                     "input_boolean.mind_control_enabled is on). action: on, off, toggle, brightness (1-100), color "
                     "(name), speed (fan %), humidity (%), run (script/scene); covers open/close; locks lock/unlock. Locks, alarms "
                     "and garage doors need confirm=true, only after the user said yes. Logged in memory.",
     S({"entity_id": STR, "action": STR, "value": STR, "confirm": BOOL}, ["entity_id", "action"])),
    ("mind_do", "A request in words ('dim the desk light to 30%'): a connected model picks one action from the "
                "control list, then it is checked and done like mind_control. A lock, alarm or garage door is only "
                "asked about (confirm_needed): pass the user's reply (\"yes\") as text with the same conversation_id "
                "within 30 seconds to do it.", S({"text": STR, "model": STR, "conversation_id": STR}, ["text"])),
    ("mind_automations", "Home Assistant automations run by Home Mind. action: list (default; who runs each, last run), "
                         "takeover (ids, or all=true), retire (stop running and leave the HA copy off), release (hand back to Home Assistant; ids or all), run (id, "
                         "optional trigger id), reload (re-read configs after editing in Home Assistant).",
     S({"action": {"type": "string", "enum": ["list", "takeover", "retire", "release", "run", "reload"]},
        "ids": {"type": "array", "items": STR}, "all": BOOL, "id": STR, "trigger": STR})),
    ("mind_pantry", "The pantry. action: list (default; what's in the house with days left), add (text like 'chicken, 2 lb ground "
                    "beef and eggs', optional location fridge/freezer/pantry), used or tossed (text), goals (show), "
                    "today (tired day? weight trend).",
     S({"action": {"type": "string", "enum": ["list", "add", "used", "tossed", "goals", "today"]}, "text": STR,
        "location": STR})),
    ("mind_fitness", "Fitness goals and progress. action: status (default; weight trend against the goal, today's protein "
                     "and calories, workouts this week), review (the week, with what to change), goals (show, or set "
                     "with goals={weight_target, weight_by, protein_g, calories, steps, workouts_per_week, "
                     "body_fat_target}), log (text like 'squats 3x8 at 185, ran 2 miles in 18 min'), workouts "
                     "(recent ones, bests and stalled lifts; optional exercise).",
     S({"action": {"type": "string", "enum": ["status", "review", "goals", "log", "workouts"]}, "text": STR,
        "goals": {"type": "object"}, "exercise": STR})),
    ("mind_phrasebook", "The phrasebook: odd ways people say sleep and workouts, each with the plain log line Home Mind "
                        "reads (\"zonked at 1, kid woke me at 5:40\" -> \"slept from 1 am to 5:40 am\"; NONE for "
                        "look-alikes that aren't logs). action: list (default; status active|pending|rejected), add (said, "
                        "plain), approve/reject/remove (id), try (text: what each reader makes of it).",
     S({"action": {"type": "string", "enum": ["list", "add", "approve", "reject", "remove", "try"]}, "said": STR,
        "plain": STR, "id": {"type": "integer"}, "status": STR, "text": STR})),
    ("mind_meals", "Meal ideas from what's in the house: uses what expires first, fits today (tired days lower-carb) and "
                   "the goals (more protein, fewer calories). meal: breakfast/lunch/dinner/snack; request: anything extra.",
     S({"meal": STR, "request": STR, "model": STR}, [])),
    ("mind_status", "Counts, version, packs, models.", S({})),
    ("mind_updates", "Check for updates (apply=true installs available packs).", S({"apply": BOOL})),
]


# Tool annotations (MCP 2025-03-26+): clients use these to decide what to ask the user before a call. readOnlyHint for
# tools that never change anything; destructiveHint for those that delete, archive in bulk, act on the house or
# install things; openWorldHint for those that reach Home Assistant, models or the internet.
_READ_ONLY = {"mind_recall", "mind_get", "mind_review", "mind_house", "mind_browse", "mind_sql", "mind_tasks",
              "mind_models", "mind_test_results", "mind_trend", "mind_home", "mind_status", "mind_draft_automation",
              "mind_meals"}
_DESTRUCTIVE = {"mind_purge", "mind_purge_about", "mind_forget_about", "mind_install_automation", "mind_control",
                "mind_do", "mind_automations", "mind_updates", "mind_comfort"}
_OPEN_WORLD = {"mind_draft_automation", "mind_install_automation", "mind_ask", "mind_organize", "mind_models",
               "mind_comfort", "mind_home", "mind_control", "mind_do", "mind_automations", "mind_meals",
               "mind_updates", "mind_run_test"}
MAX_TEXT = 100_000      # characters in one tool result: Claude Code and others refuse or cut much bigger outputs


def annotations(name):
    return {"title": name.replace("mind_", "").replace("_", " ").capitalize(),
            "readOnlyHint": name in _READ_ONLY, "destructiveHint": name in _DESTRUCTIVE,
            "idempotentHint": name in _READ_ONLY, "openWorldHint": name in _OPEN_WORLD}


def _text(out):
    t = json.dumps(out, default=str, indent=1)
    if len(t) > MAX_TEXT:
        t = t[:MAX_TEXT] + (f"\n... [cut: {len(t) - MAX_TEXT:,} more characters. Ask for less: a lower limit, a shelf "
                            "or kind filter, or a narrower query]")
    return t


class MCP:
    def __init__(self, app):
        self.app = app  # the App in main.py

    async def handle(self, msg, scope="admin"):
        if isinstance(msg, list):
            out = [r for r in [await self.handle(m, scope) for m in msg] if r is not None]
            return out or None
        if not isinstance(msg, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request: not an object"}}
        mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
        if mid is None or ("method" not in msg and ("result" in msg or "error" in msg)):
            return None     # a notification, or the client's answer to a request: neither gets a reply
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params should be an object"}}
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
                from .tokens import tool_needs, allows
                res = {"tools": [{"name": n, "title": annotations(n)["title"], "description": d, "inputSchema": s,
                                  "annotations": annotations(n)} for n, d, s in TOOLS if allows(scope, tool_needs(n))]}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                if not isinstance(args, dict):
                    return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text":
                            "arguments should be an object"}], "isError": True}}
                from .tokens import tool_needs, allows
                schema = next((s for n, _, s in TOOLS if n == name), None)
                if schema is None:
                    return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text":
                            f"no tool {name}; tools/list has the ones this token can use"}], "isError": True}}
                if not allows(scope, tool_needs(name, args)):
                    return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text":
                            f"not allowed: this token can {scope}; {name} needs {tool_needs(name, args)}"}],
                            "isError": True}}
                missing = [k for k in schema["required"] if args.get(k) in (None, "")]
                if missing:
                    return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text":
                            f"{name} needs {', '.join(missing)}"}], "isError": True}}
                try:
                    out = await self.call(name, args)
                    res = {"content": [{"type": "text", "text": _text(out)}],
                           "isError": bool(isinstance(out, dict) and out.get("error"))}
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
        from .speakers import VIEWER
        bound = VIEWER.get()            # a person-bound token (#48): only their own and household notes
        if name in ("mind_update", "mind_forget", "mind_restore", "mind_confirm", "mind_feedback", "mind_purge"):
            st.check_seen(int(a["id"]))
            if not st.get(int(a["id"])):        # not a silent "archived" for an item that isn't there
                raise KeyError(f"no item {a['id']}")
        if name in ("mind_remember", "mind_update") and bound and isinstance(a.get("meta"), dict):
            from .speakers import own_meta
            a = dict(a, meta=own_meta(a["meta"]))
        if name == "mind_remember" and bound and a.get("scope") == "personal":
            a = dict(a, owner=bound)    # files personal notes as its own person, never someone else's
        if name == "mind_remember":
            return st.remember(text=a["text"], title=a.get("title", ""), kind=a.get("kind", "note"),
                               shelf=a.get("shelf", "inbox"), tags=a.get("tags"), key=a.get("key"),
                               meta=dict(a.get("meta") or {}, **({"inferred": True} if a.get("inferred") else {}),
                                         **({"scope": a["scope"], "owner": a.get("owner") or ""}
                                            if a.get("scope") == "personal" and a.get("owner") else {})),
                               source="mcp")
        if name == "mind_recall":
            from .mind import lookup
            return lookup(st, a["query"], max(1, min(200, int(a.get("limit") or 8))), a.get("shelf"), a.get("kind"),
                          bool(a.get("include_old")))
        if name == "mind_get":
            return st.seen(int(a["id"]), history=True)
        if name == "mind_update":
            return st.revise(int(a["id"]), title=a.get("title"), body=a.get("text"), shelf=a.get("shelf"),
                             tags=a.get("tags"), key=a.get("key"), meta=a.get("meta"))
        if name == "mind_forget":
            return st.set_status(int(a["id"]), "archived")
        if name == "mind_forget_about":
            return st.forget_about(str(a["name"]))
        if name == "mind_house":
            return app.house_overview()
        if name == "mind_draft_automation":
            return await app.draft_automation(a.get("request") or "", a.get("model"))
        if name == "mind_install_automation":
            return await app.install_automation(a.get("config"), bool(a.get("enable")), a.get("pin") or "")
        if name == "mind_review":
            return st.review_queue()
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
        if name == "mind_sql":
            from . import sqlview
            return sqlview.query(st.path, a["q"])
        if name == "mind_view":
            v, act = app.views, a.get("action") or "list"
            if act == "list":
                return v.all()
            if act == "run":
                return v.run(a["name"])
            if act == "save":
                return v.save(a["name"], a.get("query"), a.get("shelf"), a.get("kind"), a.get("tags"), a.get("days"),
                              a.get("sort"), a.get("limit") or 20, a.get("sql"))
            if act == "remove":
                return {"removed": v.remove(a["name"])}
            raise ValueError(f"unknown action {act}")
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
            return st.runs(limit=max(1, min(500, int(a.get("limit") or 20))), suite=a.get("suite"))
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
            return await app.home.act(a["entity_id"], a["action"], a.get("value"), source="claude",
                                      confirm=a.get("confirm") is True)
        if name == "mind_do":
            return await app.do(a["text"], a.get("model"), source="claude",
                                conversation_id=str(a["conversation_id"]) if a.get("conversation_id") else None)
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
        if name == "mind_fitness":
            fi, act = app.fitness, a.get("action", "status")
            if act == "status":
                return fi.status()
            if act == "review":
                return {"lines": fi.review(), "suggestions": fi.suggestions()}
            if act == "goals":
                return fi.set_goals(a["goals"]) if a.get("goals") else fi.goals()
            if act == "log":
                return fi.log(a.get("text") or "", source="claude")
            if act == "workouts":
                from .fitness import exercise_name
                rows = fi.workouts(90)
                if a.get("exercise"):
                    rows = [w for w in rows if w["entry"]["exercise"] == exercise_name(a["exercise"])]
                return {"workouts": rows, "records": fi.records(), "stalls": fi.stalls()}
            raise ValueError(f"unknown action {act}")
        if name == "mind_phrasebook":
            pb, act = app.phrasebook, a.get("action", "list")
            if act == "list":
                return {"stats": pb.stats(), "entries": pb.entries(a.get("status") or None, limit=200)}
            if act == "add":
                return pb.add(a.get("said") or "", a.get("plain") or "", "added")
            if act in ("approve", "reject", "remove"):
                if a.get("id") is None:
                    raise ValueError("give id")
                rid = int(a["id"])
                return pb.remove(rid) if act == "remove" else pb.set_status(rid, "active" if act == "approve" else "rejected")
            if act == "try":
                hit = pb.match(a.get("text") or "")
                return {"phrasebook": {"entry": hit[0]["id"], "plain": hit[1]} if hit else None,
                        "similar": pb.similar(a.get("text") or "", 5)}
            raise ValueError(f"unknown action {act}")
        if name == "mind_meals":
            return await app.pantry.suggest(a.get("meal"), a.get("model"), 3, a.get("request", ""))
        if name == "mind_status":
            return app.status()
        if name == "mind_updates":
            return await (app.updater.apply() if a.get("apply") else app.updater.check())
        raise ValueError(f"no tool {name}")
