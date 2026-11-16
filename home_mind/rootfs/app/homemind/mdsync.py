"""Two-way Markdown sync (#67): every note as a Markdown file in a folder (one folder per shelf), so you can read and
edit your memory in Obsidian, a text editor or a synced folder.

- A note becomes `<shelf>/<id>-<slug>.md` with a small header (id, kind, key, tags, updated) and its text.
- Editing a file makes a new version of the note (the old one kept as history, like any change).
- A new file without an id becomes a new note on the shelf its folder names.
- Deleting a file archives the note (restorable); archiving a note deletes its file.
- If the note and the file both changed since the last sync, the note wins and the file's text is kept next to it as
  `<name>.conflict.md` for you to merge.
Secret notes and house readings are never written out.
"""
import hashlib
import json
import os
import re
import threading

STATE = "mdsync"
MAX_FILE = 200_000
SKIP_SOURCES = ("home", "automations", "calendar")
HEADER = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def slug(t):
    return (re.sub(r"[^a-z0-9]+", "-", (t or "").lower()).strip("-")[:48]) or "note"


def render(d):
    head = {"id": d["id"], "kind": d["kind"]}
    if d.get("key"):
        head["key"] = d["key"]
    if d.get("tags"):
        head["tags"] = " ".join(d["tags"])
    lines = [f"{k}: {v}" for k, v in head.items()]
    body = d["body"] or ""
    title = d["title"] or ""
    text = body if not title or body.startswith(title) else f"# {title}\n\n{body}"
    return "---\n" + "\n".join(lines) + "\n---\n" + text.rstrip() + "\n"


def parse(text):
    head, body = {}, text
    m = HEADER.match(text)
    if m:
        for ln in m.group(1).splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                head[k.strip()] = v.strip()
        body = text[m.end():]
    body = body.strip()
    title = ""
    t = re.match(r"#\s+(.+)\n+", body)
    if t:
        title, body = t.group(1).strip(), body[t.end():].strip()
    return head, title, body


class MdSync:
    def __init__(self, store, folder):
        self.store, self.folder = store, folder
        self.last = None
        self._running = threading.Lock()

    def _state(self):
        return self.store.kv_get(STATE) or {}

    def _notes(self):
        rows = self.store.db.execute(
            "SELECT * FROM items WHERE status IN ('active','faded') AND kind != 'procedure' AND source NOT IN (%s)"
            % ",".join("?" * len(SKIP_SOURCES)), SKIP_SOURCES).fetchall()
        out = {}
        for r in rows:
            d = self.store._row(r)
            if d["meta"].get("sensitive") or r["secret"] is not None:
                continue
            out[d["id"]] = d
        return out

    def _path(self, d):
        return os.path.join(self.folder, *d["shelf"].split("/"), f"{d['id']}-{slug(d['title'] or d['body'])}.md")

    def _successor(self, iid):
        """A note revised elsewhere has a new id: follow the chain forward to the active version."""
        seen = set()
        while iid and iid not in seen:
            seen.add(iid)
            r = self.store.db.execute("SELECT id, status FROM items WHERE supersedes=? ORDER BY id DESC LIMIT 1",
                                      (iid,)).fetchone()
            if not r:
                return None
            if r["status"] in ("active", "faded"):
                return r["id"]
            iid = r["id"]
        return None

    def _write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    def run(self):
        if not self.folder:
            return {"enabled": False}
        with self._running:         # the upkeep loop and "Sync now" run in threads: one at a time, or a new file
            return self._run()      # is filed twice and one run's state overwrites the other's

    def _run(self):
        os.makedirs(self.folder, exist_ok=True)
        st = self.store
        state = {int(k): v for k, v in self._state().items()}
        notes = self._notes()
        files = {}
        for root, _dirs, names in os.walk(self.folder):
            for n in names:
                if n.endswith(".md") and not n.endswith(".conflict.md"):
                    files[os.path.join(root, n)] = None
        by_id, new_files = {}, []
        by_path = {v.get("path"): k for k, v in state.items()}
        for p in files:
            try:
                if os.path.getsize(p) > MAX_FILE:
                    continue            # too big for a note: left alone (put long texts in Documents)
                text = open(p, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            head, title, body = parse(text)
            try:
                iid = int(head.get("id")) if head.get("id") else None
            except ValueError:
                iid = None
            if iid is None and by_path.get(os.path.relpath(p, self.folder)) is not None:
                iid = by_path[os.path.relpath(p, self.folder)]     # its header was removed: still that note's file
                if not title and not body:
                    text = ""                                       # emptied: written again from the note, not applied
            if iid is None:
                new_files.append((p, title, body, head))
            elif iid in by_id:
                new_files.append((p, title, body, {k: v for k, v in head.items() if k != "id"}))   # a copied file
            else:
                by_id[iid] = (p, text, title, body, head)
        stats = {"written": 0, "updated_notes": 0, "new_notes": 0, "archived": 0, "removed_files": 0, "conflicts": 0}
        new_state = {}
        # files that name a note
        for iid, (p, text, title, body, head) in by_id.items():
            s = state.get(iid)
            cur = iid if iid in notes else None
            if cur is None:
                succ = self._successor(iid)
                if succ in notes:
                    cur = succ
            if cur is None:
                if s is not None:
                    # the note was archived or forgotten here: its file goes
                    os.remove(p)
                    stats["removed_files"] += 1
                elif st.get(iid) is None:
                    # names a note that was never here (copied from another memory): a new note
                    new_files.append((p, title, body, {k: v for k, v in head.items() if k != "id"}))
                continue
            d = notes[cur]
            file_changed = s is None or _h(text) != s.get("file")
            note_changed = s is None or cur != iid or _h(render(d)) != s.get("note")
            if not text:
                file_changed = False
            if file_changed and s is not None and not note_changed:
                if (title or d["title"]) != d["title"] or body != (d["body"] or "").strip() or \
                        head.get("tags", "") != " ".join(d["tags"]):
                    r = st.revise(cur, body=body, title=title or d["title"],
                                  tags=head.get("tags", "").split() if "tags" in head else None)
                    r_id = r["id"] if isinstance(r, dict) else r
                    st.log("mdsync_edit", r_id, previous=cur)
                    d = st.get(r_id)
                    stats["updated_notes"] += 1
                    notes.pop(cur, None)
                    notes[d["id"]] = d
                    cur = d["id"]
            elif file_changed and s is not None and note_changed:
                self._write(p[:-3] + ".conflict.md", text)
                stats["conflicts"] += 1
            want = self._path(d)
            out = render(d)
            if p != want:
                os.remove(p)
            if p != want or _h(text) != _h(out):
                self._write(want, out)
                stats["written"] += 1
            new_state[cur] = {"file": _h(out), "note": _h(render(d)), "path": os.path.relpath(want, self.folder)}
        # files without an id: new notes
        for p, title, body, head in new_files:
            if not body and not title:
                continue
            rel = os.path.relpath(os.path.dirname(p), self.folder)
            shelf = "inbox" if rel in (".", "") else rel.replace(os.sep, "/")
            r = st.remember(text=body or title, title=title, shelf=shelf, kind=head.get("kind") or "note",
                            tags=head.get("tags", "").split(), source="markdown", meta={"from_file": os.path.basename(p)})
            d = st.get(r["id"])
            stats["new_notes"] += 1
            os.remove(p)
            if d["meta"].get("sensitive") or d.get("secret"):
                continue        # a secret written in a file: kept, not written back out
            want = self._path(d)
            out = render(d)
            self._write(want, out)
            notes[d["id"]] = d
            new_state[d["id"]] = {"file": _h(out), "note": _h(out), "path": os.path.relpath(want, self.folder)}
        # notes whose file is gone, or never written
        for iid, d in notes.items():
            if iid in new_state:
                continue
            s = state.get(iid)
            if s is not None and iid not in by_id:
                st.set_status(iid, "archived")         # the file was deleted: archive (restorable)
                stats["archived"] += 1
                continue
            want = self._path(d)
            out = render(d)
            self._write(want, out)
            stats["written"] += 1
            new_state[iid] = {"file": _h(out), "note": _h(out), "path": os.path.relpath(want, self.folder)}
        # empty shelf folders left behind
        for root, dirs, names in os.walk(self.folder, topdown=False):
            if root != self.folder and not os.listdir(root):
                os.rmdir(root)
        st.kv_set(STATE, {str(k): v for k, v in new_state.items()})
        self.last = stats
        return stats
