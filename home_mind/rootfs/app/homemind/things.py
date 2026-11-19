"""Things, and scenes drafted from a description (design spec: Records > Learned, "things" from Mem0, scenes from
Josh.ai; phase 12).

Things. The drill, the dishwasher, the red toolbox, the car: each is a note of kind 'thing' (an item type, not a new
store), linked to every note about it through the one links table, with a relation:
  about       a note is about this thing
  in          this thing is in that one (the drill is in the red toolbox; the toolbox is in the shed)
  part_of     a part of something (the filter is part of the dishwasher)
  belongs_to  whose it is (a person's thing)
Things come only from Home Assistant's devices or after a person says yes to one Home Mind noticed in notes ("the
drill is in the red toolbox": shall I keep track of the drill?). Look-alike names ("the lamp", "living room lamp")
are proposed for merging, never merged silently. A device renamed in Home Assistant keeps its thing (it is held by
the device, and the old name stays as another name). Following links is filtered by who is asking: a link never shows
a note its viewer may not see. "Where's the drill?" follows the 'in' links: "In the red toolbox, in the shed."

Scenes from a description. "Make the living room cozy" drafts a scene: the room's lights and fans with levels, from
fixed words first (cozy, bright, movie, reading, off) and a model's draft only where those don't fit. Heaters, locks,
doors, covers and anything that isn't open-level are never in a drafted scene, and every step still goes through
the usual checks. The first run is a preview: it undoes itself after 10 minutes unless you say keep, and only a kept
scene becomes one to run by name. Scenes are scored by how often they're edited and whether they're still kept a
week later.
"""
import json
import re
import time

from .store import words

RELATIONS = ("about", "in", "part_of", "belongs_to")
PREVIEW_SECONDS = 600
NEVER_IN_SCENE = ("lock", "alarm_control_panel", "cover", "climate", "water_heater", "valve", "camera", "siren")
MOODS = [  # (words, light level or "off", fan)
    (r"\b(?:cozy|cosy|relax\w*|calm|wind down|warm|evening)\b", 30, None),
    (r"\b(?:bright|work\w*|focus|clean\w*|study)\b", 100, None),
    (r"\b(?:movie|film|tv|cinema)\b", 10, None),
    (r"\b(?:read\w*)\b", 70, None),
    (r"\b(?:off|dark|goodnight|good night|sleep|bed)\b", "off", "off"),
    (r"\b(?:cool|fresh|breez\w*)\b", None, 60),
]


class Things:
    def __init__(self, store):
        self.store = store

    # ---- reading ------------------------------------------------------------------------------------------------
    def all(self):
        return [self._t(r) for r in self.store.db.execute(
            "SELECT * FROM items WHERE kind='thing' AND status='active' ORDER BY title")]

    def _t(self, r):
        d = dict(r)
        d["meta"] = json.loads(d.get("meta") or "{}")
        return {"id": d["id"], "name": d["title"], "aliases": d["meta"].get("aliases") or [],
                "device_id": d["meta"].get("device_id"), "entity_ids": d["meta"].get("entity_ids") or [],
                "type": d["meta"].get("type") or "thing", "meta": d["meta"]}

    def get(self, tid):
        r = self.store.db.execute("SELECT * FROM items WHERE id=? AND kind='thing'", (tid,)).fetchone()
        return self._t(r) if r else None

    def names(self, t):
        return [t["name"]] + list(t["aliases"])

    def find(self, text):
        """The thing a phrase names (longest name first), or None."""
        low = " " + re.sub(r"[^a-z0-9' ]", " ", (text or "").lower()) + " "
        best = None
        for t in self.all():
            for n in self.names(t):
                n2 = re.sub(r"^(?:the|my|our)\s+", "", n.lower()).strip()
                if n2 and f" {n2} " in low and (best is None or len(n2) > best[1]):
                    best = (t, len(n2))
        return best[0] if best else None

    def links(self, tid, relation=None, viewer=None):
        """Links from and to a thing, each only where the viewer may see the note at the other end."""
        from .speakers import visible
        out = []
        for a, b, kind in self.store.db.execute("SELECT a, b, kind FROM links WHERE (a=? OR b=?)", (tid, tid)):
            if kind not in RELATIONS or (relation and kind != relation):
                continue
            other = b if a == tid else a
            it = self.store.get(other)
            if not it or it.get("status") != "active" or not visible(it.get("meta"), viewer):
                continue
            out.append({"id": other, "relation": kind, "direction": "out" if a == tid else "in",
                        "title": it.get("title"), "kind": it.get("kind")})
        return out

    def where(self, tid, viewer=None, depth=4):
        """Follow 'in' links: ["red toolbox", "shed"]."""
        chain, seen, cur = [], {tid}, tid
        for _ in range(depth):
            nxt = [x for x in self.links(cur, "in", viewer) if x["direction"] == "out" and x["id"] not in seen]
            if not nxt:
                break
            cur = nxt[0]["id"]
            seen.add(cur)
            chain.append(nxt[0]["title"])
        return chain

    def answer_where(self, question, viewer=None):
        m = re.search(r"\bwhere(?:'s| is| are| did i (?:put|leave))\s+(?:the |my |our )?(.+?)[?.!]*$",
                      (question or "").replace("’", "'"), re.I)
        if not m:
            return None
        t = self.find(m.group(1))
        if not t:
            return None
        chain = self.where(t["id"], viewer)
        if not chain:
            return None
        return f"In the {chain[0]}" + "".join(f", in the {c}" for c in chain[1:]) + "."

    # ---- writing ------------------------------------------------------------------------------------------------
    def create(self, name, type_="thing", device_id=None, entity_ids=None, source="confirmed"):
        name = re.sub(r"^(?:the|my|our)\s+", "", (name or "").strip(), flags=re.I)
        if not name:
            raise ValueError("a thing needs a name")
        meta = {"type": type_, "aliases": [], "device_id": device_id, "entity_ids": entity_ids or []}
        r = self.store.remember(text=f"{name} (a thing Home Mind keeps track of)", title=name, kind="thing",
                                shelf="things", source=f"things:{source}", meta=meta)
        return r["id"]

    def _meta(self, tid, **kw):
        it = self.store.get(tid)
        meta = dict(it.get("meta") or {})
        meta.update(kw)
        with self.store.lock:
            self.store.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), tid))
            self.store.db.commit()

    def link(self, a, b, relation):
        if relation not in RELATIONS:
            raise ValueError(f"relation is one of {', '.join(RELATIONS)}")
        if relation == "in":       # a thing is in one place at a time
            with self.store.lock:
                self.store.db.execute("DELETE FROM links WHERE a=? AND kind='in'", (a,))
                self.store.db.commit()
        with self.store.lock:
            self.store.db.execute("INSERT OR IGNORE INTO links (a, b, kind) VALUES (?,?,?)", (a, b, relation))
            self.store.db.commit()

    def unlink(self, a, b, relation):
        with self.store.lock:
            self.store.db.execute("DELETE FROM links WHERE a=? AND b=? AND kind=?", (a, b, relation))
            self.store.db.commit()

    def from_devices(self, catalog, device_of=None):
        """Things from Home Assistant: one per device (or per entity without one). A renamed device keeps its thing
        and the old name becomes another name. Returns (created, renamed)."""
        have = {t["device_id"]: t for t in self.all() if t["device_id"]}
        created = renamed = 0
        for eid, c in (catalog or {}).items():
            if eid.split(".")[0] in ("person", "sun", "weather", "zone", "automation", "script", "sensor",
                                     "binary_sensor", "input_number", "input_boolean", "input_button"):
                continue
            dev = (device_of(eid) if device_of else None) or f"entity:{eid}"
            name = c.get("name") or eid
            t = have.get(dev)
            if t is None:
                tid = self.create(name, "device", device_id=dev, entity_ids=[eid], source="home_assistant")
                have[dev] = self.get(tid)
                created += 1
            elif t["name"].lower() != name.lower() and name.lower() not in [a.lower() for a in t["aliases"]]:
                old = t["name"]
                with self.store.lock:
                    self.store.db.execute("UPDATE items SET title=? WHERE id=?", (name, t["id"]))
                    self.store.db.commit()
                self._meta(t["id"], aliases=sorted(set(t["aliases"]) | {old}),
                           entity_ids=sorted(set(t["entity_ids"]) | {eid}))
                have[dev] = self.get(t["id"])
                renamed += 1
        return created, renamed

    def link_notes(self, limit=2000):
        """Notes that name a thing get an 'about' link; "X is in Y" between two things gets an 'in' link."""
        from .structure import place
        ts = self.all()
        if not ts:
            return 0
        n = 0
        cur = int(self.store.kv_get("things_cursor") or 0)
        rows = self.store.db.execute("SELECT id, title, body, source, meta FROM items WHERE id>? AND kind!='thing' "
                                     "AND status='active' ORDER BY id LIMIT ?", (cur, limit)).fetchall()
        for r in rows:
            cur = r["id"]
            if not household_note(r):
                continue        # a personal note, or one from email or a document, never makes a link (check 2, 3)
            text = f"{r['title'] or ''} {r['body'] or ''}"
            t = self.find(text)
            if t:
                self.link(r["id"], t["id"], "about")
                n += 1
            p = place(r["body"] or "")
            if p:
                a, b = self.find(p["thing"]), self.find(p["in"])
                if a and b and a["id"] != b["id"]:
                    self.link(a["id"], b["id"], "in")
                    n += 1
        self.store.kv_set("things_cursor", cur)
        return n

    def noticed(self, limit=500):
        """Things named in household notes that aren't things yet ("the drill is in the red toolbox"): proposals
        only. Personal notes and mail or documents are never read for this."""
        from .structure import place
        known = {n.lower() for t in self.all() for n in self.names(t)}
        dismissed = set(self.store.kv_get("things_dismissed") or [])
        out = {}
        for r in self.store.db.execute("SELECT body, source, meta FROM items WHERE kind!='thing' AND status='active' "
                                       "ORDER BY id DESC LIMIT ?", (limit,)):
            if not household_note(r):
                continue
            p = place(r["body"] or "")
            if not p:
                continue
            for nm in (p["thing"], p["in"]):
                nm = re.sub(r"^(?:the|my|our)\s+", "", nm).strip()
                if nm and nm not in known and nm not in dismissed and len(nm) <= 40:
                    out[nm] = out.get(nm, 0) + 1
        return sorted(out, key=lambda k: -out[k])

    def dismiss(self, name):
        d = set(self.store.kv_get("things_dismissed") or [])
        d.add(name.lower())
        self.store.kv_set("things_dismissed", sorted(d))

    def duplicates(self):
        """Pairs of things whose names look alike ("lamp" / "living room lamp"): proposed to merge, never merged."""
        ts = self.all()
        out = []
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                if a["device_id"] and b["device_id"] and a["device_id"] != b["device_id"]:
                    continue        # two real devices are two things
                wa, wb = set(words(a["name"])), set(words(b["name"]))
                if wa and wb and (wa <= wb or wb <= wa):
                    out.append({"keep": (b if len(b["name"]) > len(a["name"]) else a)["id"],
                                "merge": (a if len(b["name"]) > len(a["name"]) else b)["id"],
                                "names": [a["name"], b["name"]]})
        return out

    def merge(self, keep, gone):
        """A person said yes: links and names move to the kept thing; the other is archived."""
        k, g = self.get(keep), self.get(gone)
        if not k or not g:
            raise ValueError("no such thing")
        with self.store.lock:
            for a, b, kind in self.store.db.execute("SELECT a, b, kind FROM links WHERE a=? OR b=?", (gone, gone)).fetchall():
                na, nb = (keep if a == gone else a), (keep if b == gone else b)
                if na != nb:
                    self.store.db.execute("INSERT OR IGNORE INTO links (a, b, kind) VALUES (?,?,?)", (na, nb, kind))
            self.store.db.execute("DELETE FROM links WHERE a=? OR b=?", (gone, gone))
            self.store.db.commit()
        self._meta(keep, aliases=sorted(set(k["aliases"]) | {g["name"]} | set(g["aliases"])),
                   entity_ids=sorted(set(k["entity_ids"]) | set(g["entity_ids"])))
        with self.store.lock:
            self.store.db.execute("UPDATE items SET status='archived' WHERE id=?", (gone,))
            self.store.db.commit()
        return self.get(keep)


def household_note(r):
    """A note anyone in the house may see, not filed from email or a document."""
    try:
        meta = json.loads(r["meta"] or "{}") if isinstance(r["meta"], str) else (r["meta"] or {})
    except ValueError:
        meta = {}
    src = (r["source"] or "").lower()
    return meta.get("scope") != "personal" and not meta.get("sensitive") and \
        not src.startswith(("email", "mail", "doc", "pdf"))


# ---- scenes ----------------------------------------------------------------------------------------------------
class Scenes:
    KV = "scenes"

    def __init__(self, app, clock=None):
        self.app = app
        self.clock = clock or time.time
        self.previews = {}        # name -> {"scene", "before", "until"}; also in kv, so a restart still undoes them

    def _store(self):
        return self.app.mind.store

    def saved(self):
        return dict(self._store().kv_get(self.KV) or {})

    def _room_devices(self, room):
        home = self.app.home
        areas = getattr(home, "areas", {}) or {}
        r = (room or "").lower()
        return [d for d in home.controllable() if not r or areas.get(d["entity_id"]) == r]

    def allowed_step(self, s):
        home = self.app.home
        eid = s.get("entity_id") or ""
        if eid.split(".")[0] in NEVER_IN_SCENE:
            return False
        try:
            from .approval import heater
            if heater(eid, (home.catalog.get(eid) or {}).get("name") or ""):
                return False
            return home.level(eid, s.get("action"), s.get("value"))[0] == "open"
        except Exception:  # noqa: BLE001
            return False

    async def draft(self, text, room=None, model_draft=None):
        """Steps for a described mood: fixed words first, a model's draft (validated) only if none fit."""
        devs = self._room_devices(room)
        steps = []
        for rx, light, fan in MOODS:
            if re.search(rx, text or "", re.I):
                for d in devs:
                    dom = d["entity_id"].split(".")[0]
                    if dom == "light" and light is not None:
                        steps.append({"entity_id": d["entity_id"], "action": "off"} if light == "off" else
                                     {"entity_id": d["entity_id"], "action": "brightness", "value": light})
                    elif dom == "fan" and fan is not None:
                        steps.append({"entity_id": d["entity_id"], "action": "off"} if fan == "off" else
                                     {"entity_id": d["entity_id"], "action": "speed", "value": fan})
                break
        if not steps and model_draft is not None:
            try:
                got = await model_draft(text, devs)
            except Exception:  # noqa: BLE001
                got = []
            names = {d["entity_id"] for d in devs}
            steps = [s for s in (got or []) if isinstance(s, dict) and s.get("entity_id") in names]
        steps = [s for s in steps if self.allowed_step(s)]
        return {"name": _scene_name(text, room), "room": room, "steps": steps}

    async def preview(self, scene, source="scene"):
        """Run a drafted scene for 10 minutes: it undoes itself unless kept."""
        home = self.app.home
        before = {s["entity_id"]: {"state": (home.catalog.get(s["entity_id"]) or {}).get("state"),
                                   "attrs": dict((home.catalog.get(s["entity_id"]) or {}).get("attrs") or {})}
                  for s in scene["steps"]}
        done = []
        for s in scene["steps"]:
            r = await self.app._run_plan(dict(s, by="scene"), f"scene: {scene['name']}", source, None)
            done.append({"entity_id": s["entity_id"], "ok": bool(r.get("done")), "error": r.get("error")})
        self.previews[scene["name"]] = {"scene": scene, "before": before, "until": self.clock() + PREVIEW_SECONDS}
        self._save_previews()
        return {"name": scene["name"], "preview_until": self.clock() + PREVIEW_SECONDS, "steps": done}

    def _save_previews(self):
        try:
            self._store().kv_set("scene_previews", self.previews)
        except Exception:  # noqa: BLE001
            pass

    async def undo_due(self):
        """Previews nobody kept: put back as they were (including ones started before a restart)."""
        if not self.previews:
            self.previews.update(self._store().kv_get("scene_previews") or {})
        out = []
        for name, p in list(self.previews.items()):
            if p["until"] > self.clock():
                continue
            self.previews.pop(name)
            for eid, b in p["before"].items():
                st = b.get("state")
                try:
                    if st == "off":
                        await self.app.home.act(eid, "off", None, "scene undo")
                    elif st == "on":
                        bri = (b.get("attrs") or {}).get("brightness")
                        if bri is not None and eid.startswith("light."):
                            await self.app.home.act(eid, "brightness", max(1, round(bri / 2.55)), "scene undo")
                        else:
                            await self.app.home.act(eid, "on", None, "scene undo")
                except Exception:  # noqa: BLE001
                    pass
            out.append(name)
            self._score(name, "undone")
        if out:
            self._save_previews()
        return out

    def keep(self, name, steps=None):
        p = self.previews.pop(name, None)
        self._save_previews()
        if p is None and steps is None:
            raise ValueError("no preview by that name running (previews last 10 minutes)")
        scene = dict((p or {}).get("scene") or {"name": name, "steps": []})
        if steps is not None:
            scene["steps"] = [s for s in steps if self.allowed_step(s)]
        all_ = self.saved()
        old = all_.get(name)
        all_[name] = {"steps": scene["steps"], "room": scene.get("room"), "kept": self.clock(),
                      "edits": (old or {}).get("edits", 0) + (1 if old and old["steps"] != scene["steps"] else 0)}
        self._store().kv_set(self.KV, all_)
        self._score(name, "kept")
        return all_[name]

    async def run(self, name, source="scene"):
        sc = self.saved().get(name)
        if not sc:
            raise ValueError(f"no scene called {name}")
        done = []
        for s in sc["steps"]:
            r = await self.app._run_plan(dict(s, by="scene"), f"scene: {name}", source, None)
            done.append({"entity_id": s["entity_id"], "ok": bool(r.get("done"))})
        return {"name": name, "steps": done}

    def remove(self, name):
        all_ = self.saved()
        all_.pop(name, None)
        self._store().kv_set(self.KV, all_)
        self._score(name, "removed")

    def _score(self, name, what):
        sc = dict(self._store().kv_get("scene_scores") or {})
        r = sc.setdefault(name, {})
        r[what] = r.get(what, 0) + 1
        r["last"] = self.clock()
        self._store().kv_set("scene_scores", sc)

    def scores(self):
        """Per scene: edits, and whether it was still kept a week after it was first kept."""
        out = {}
        now = self.clock()
        for name, s in self.saved().items():
            out[name] = {"edits": s.get("edits", 0), "kept_a_week": now - s.get("kept", now) >= 7 * 86400}
        for name, s in (self._store().kv_get("scene_scores") or {}).items():
            out.setdefault(name, {}).update({k: v for k, v in s.items() if k != "last"})
        return out


def _scene_name(text, room):
    w = [x for x in re.findall(r"[a-z]+", (text or "").lower()) if x not in ("make", "the", "a", "it", "please", "set",
                                                                           "turn", "my", "room", "in", "for", "to")]
    return " ".join(([room.lower()] if room else []) + w[:3]) or "scene"
