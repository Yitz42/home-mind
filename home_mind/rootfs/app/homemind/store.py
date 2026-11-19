"""Home Mind storage: one SQLite file holding what the mind knows and everything that happened to it.

Stores
  items    what is kept: facts, notes, tasks, procedures, episodes. Each has a shelf (a path like
           "projects/pebble") and tags, so it can be found by browsing as well as by searching.
           Changing an item never overwrites it: a new version is written that supersedes the old one,
           so the mind can say what it believed before and when that changed.
  events   every store, recall, update, feedback and test step, with the time (the episodic log).
  runs / metrics   test runs and the numbers they produced, kept for trends.

Search is SQLite FTS5 (BM25) plus small boosts for recent and often-used items. If FTS5 is missing it
falls back to word overlap so the mind still works.
"""
import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata

STOP = set("""a an the of to in on at for and or is are was were be been it its this that these those with
from by as what which who whom whose when where why how do does did can could should would will i me my we our you
your he she they them his her their there here about into over under than then so if not no yes any some all""".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'note',
  key TEXT,
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  shelf TEXT NOT NULL DEFAULT 'inbox',
  status TEXT NOT NULL DEFAULT 'active',
  supersedes INTEGER,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  last_used REAL,
  use_count INTEGER NOT NULL DEFAULT 0,
  strength REAL NOT NULL DEFAULT 1.0,
  source TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS items_key ON items(key, status);
CREATE INDEX IF NOT EXISTS items_shelf ON items(shelf, status);
CREATE INDEX IF NOT EXISTS items_kind ON items(kind, status);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, type TEXT NOT NULL, item_id INTEGER, data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, suite TEXT, arm TEXT, model TEXT, seed INTEGER,
  status TEXT, config TEXT, summary TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
  run_id TEXT, ts REAL, suite TEXT, arm TEXT, model TEXT, name TEXT, value REAL
);
CREATE INDEX IF NOT EXISTS metrics_suite ON metrics(suite, arm, name, ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS links (a INTEGER, b INTEGER, kind TEXT, PRIMARY KEY (a, b, kind));
"""

FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, body, tags, shelf, keys, content='items', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, body, tags, shelf, keys) VALUES (new.id, new.title, new.body, new.tags, new.shelf, new.keys);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, body, tags, shelf, keys) VALUES('delete', old.id, old.title, old.body, old.tags, old.shelf, old.keys);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF title, body, tags, shelf, keys ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, body, tags, shelf, keys) VALUES('delete', old.id, old.title, old.body, old.tags, old.shelf, old.keys);
  INSERT INTO items_fts(rowid, title, body, tags, shelf, keys) VALUES (new.id, new.title, new.body, new.tags, new.shelf, new.keys);
END;
"""


MODEL_SOURCES = ("overnight",)   # sources where a model, not a person, decided what the note says
TRUST_SOURCES = True             # off: only overnight facts are on probation and nothing is held (how 0.20 worked)
# sources whose text was written by someone outside the house (a forwarded email: the vendor wrote it, and a From
# header can be forged): a note from one that reads like an instruction to the assistant is held for review too
OUTSIDE_SOURCES = ("email",)


def _inferred(m, source=""):
    """Written by a model from what it guessed or pulled out of a conversation, not what someone said to keep (#6):
    learned overnight, or marked inferred by whoever filed it (Claude through mind_remember, a script)."""
    if not TRUST_SOURCES:
        return bool(m.get("learned_overnight"))
    return bool(m.get("learned_overnight") or m.get("inferred") or source in MODEL_SOURCES)


def _on_probation(r, now, days):
    """Inferred, not confirmed, and less than `days` old."""
    try:
        m = r["meta"]
        m = json.loads(m or "{}") if isinstance(m, str) else (m or {})
        src = r["source"] if "source" in r.keys() else ""
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False
    return _inferred(m, src) and not m.get("confirmed") and now - r["created"] < days * 86400


def _eff(r):
    """When a row's fact became true: the note's own date ("last Tuesday ...") if it gave one, else when it was filed."""
    try:
        m = r["meta"]
        m = json.loads(m or "{}") if isinstance(m, str) else (m or {})
    except (ValueError, TypeError, KeyError, IndexError):
        m = {}
    vf = m.get("valid_from")
    # never later than it was filed: a date in the future (a forged email's Date header, a model's tag, an import)
    # would make a note "the newest" for good, so no later correction could replace it
    return min(float(vf), float(r["created"])) if vf else float(r["created"])


def fold(text):
    """Accents dropped ("Müller" -> "Muller", "Zoë" -> "Zoe"), as the search index (unicode61) does: without it a
    name with an accent split into pieces ("m", "ller") that match nothing."""
    text = text or ""
    if text.isascii():
        return text
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def words(text):
    return [w for w in re.findall(r"[a-z0-9]+", fold(text).lower()) if w not in STOP and len(w) > 1]


CALENDAR = set("monday tuesday wednesday thursday friday saturday sunday january february march april may june july "
               "august september october november december mon tue wed thu fri sat sun jan feb mar apr jun jul aug sep "
               "sept oct nov dec today yesterday tomorrow".split())


def names_in(text):
    """Lower-cased words written with a capital letter anywhere but at the start of a sentence: likely names.
    Days and months are capitalized but aren't names."""
    out = set()
    for sent in re.split(r"[.!?]\s+", fold(text)):
        toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9']*", sent)
        for t in toks[1:]:
            if t[0].isupper() and t.lower() not in STOP and t.lower().split("'")[0] not in CALENDAR:
                out.add(t.lower().split("'")[0])
    return out


# a pronoun that points outside the note: an owner word ("its code"), or a sentence that starts with it ("It's in the
# shed now."). "Finish it with lemon" points inside its own sentence and doesn't count.
SPEAKER = re.compile(r"\b([A-Z][a-z]+)\s+(?:says|said|thinks|thought|told me|mentioned|claims|reckons|"
                     # #21: es, de, fr, nl, pt, it
                     r"dice|dijo|cree|sagt|sagte|meint|glaubt|dit|pense|zegt|zei|denkt|diz|disse|acha|ha detto|pensa)\b")
FOLLOW = re.compile(r"\b(whoever|whose|of the (?:person|one|guy|woman|man)|(?:person|one|guy|woman|man) who)\b", re.I)


ATTR_THINGS = [({"code", "combination", "pin", "passcode"},
                {"safe", "locker", "padlock", "lock", "keypad", "gate", "shed", "garage", "alarm", "door", "lockbox"}),
               ({"password", "passphrase"}, {"router", "wifi", "laptop", "account", "network", "computer", "tablet"})]
ATTR_SAME = [{"code", "combination", "pin", "passcode", "open", "opens", "number"},
             {"password", "passphrase", "passcode"}]      # attribute words that name the same thing (#13)
MOD_STOP = STOP | {"new", "old", "same", "other", "next", "last", "first", "whole", "only", "own", "main", "current"}


def modified_things(text):
    """Things named by a modifier and a noun with no capital ("the blue toolbox", "our garage freezer"):
    {noun: {modifiers}}. A name ("the Nimsk fern") is left to names_in."""
    out = {}
    for mod, noun in re.findall(r"\b(?i:the|my|our|your|his|her|their)\s+([a-z]+)\s+([a-z]{3,})\b", text or ""):
        if mod in MOD_STOP or noun in STOP or noun.endswith("ly"):
            continue
        out.setdefault(singular(noun) if "singular" in globals() else noun, set()).add(mod)
    return out


def thing_word(text, anchor):
    """The word right after a name ("Bexbel locker", "Nimsk fern's", "Kate's bike"): what the name is attached to.
    Notes that share the name and this word are about the same thing, however they're worded."""
    m = re.search(rf"\b{re.escape(anchor)}(?:['\u2019]s)?\s+([A-Za-z]+)", fold(text), re.I)   # Kofi’s: a phone's apostrophe
    if not m:
        return None
    w = m.group(1).lower()
    return None if w in STOP else w


def _said_by(r):
    """Who said a note, when a model tagged it (#21) or a known speaker filed it (#3)."""
    m = re.search(r'"(?:said_by|speaker)": "([^"]+)"', r["meta"] or "")
    return m.group(1).lower() if m else None


def speaker(text):
    m = SPEAKER.search(text or "")
    return m.group(1).lower() if m else None


def sound_key(w):
    try:
        from metaphone import doublemetaphone
        return [k for k in doublemetaphone(w) if k]
    except ImportError:
        return []


GATHER = re.compile(r"^\s*(?:how many|which|list|what (?:did|have) i (?:change|update|set|move)|what are all)\b"
                    r"|\beverything (?:i know )?about\b|\ball (?:of )?(?:my|the)\s+\w+s\b", re.I)
ABOUT = re.compile(r"\b(?:everything|all) (?:i know |you know )?about\b", re.I)
UNIVERSAL_NOUN = re.compile(r"\b(?:all|every|each)\s+(?:of\s+)?(?:the\s+|my\s+)?([a-z]+)", re.I)
UNIVERSAL_PLACE = re.compile(r"\bthe\s+([a-z]+(?:\s[a-z]+)?)\s+(?:was|got|has been)\s+(?:cleared out|emptied|cleaned out)"
                             r"|\beverything in the\s+([a-z]+(?:\s[a-z]+)?)\s+(?:is|was) gone", re.I)
HEDGE = re.compile(r"\b(guess|guessed|guesses|probably|maybe|might be|i think|not sure|possibly|perhaps|"
                   # #21: Spanish, German, French, Dutch, Portuguese, Italian
                   r"creo que|quiz[aá]s?|tal vez|probablemente|ich glaube|vielleicht|wahrscheinlich|"
                   r"je (?:crois|pense)|peut-[eê]tre|probablement|ik denk|misschien|waarschijnlijk|acho que|talvez|"
                   r"provavelmente|penso che|forse|probabilmente)\b", re.I)
CORRECTION = re.compile(r"\b(actually|that was a typo|typo|i meant|correction|scratch that|en realidad|"
                        r"me equivoqu[eé]|eigentlich|korrektur|en fait|je voulais dire|eigenlijk|correctie|na verdade|"
                        r"in realt[aà]|correzione)\b", re.I)
# A note that talks to the model instead of about the house (#16, MINJA): addressed to an assistant, a meta-instruction
# (ignore your instructions, your new instructions are), prompt markup, or telling the answerer what to say when asked.
# Household rules for people ("you must take your shoes off", "do not tell Mia about the party") are not instructions.
_AI = r"(?:assistant|ai|a\.i\.|jeeves|claude|chatgpt|gpt|model|bot|chatbot|llm|language model|home assistant)"
INSTRUCTION = re.compile(
    r"\b(?:note|message|instructions?|reminder)s? (?:to|for) (?:the |any |my )?(?:home )?" + _AI + r"\b|"
    r"(?:^|[.!?]\s+)\W*(?:hey |hello |hi |dear |ok |okay |attention |important:? )?" + _AI + r"\s*(?:[,:!]|reading this)|"
    r"\bto (?:the |any |whatever )?" + _AI + r" (?:reading|that reads|seeing) this\b|"
    r"\b(?:" + _AI + r"|system|admin) (?:note|instruction|override)s?\s*:|(?:^|\n)\W*(?:system|assistant|user)\s*:|"
    r"\[/?inst\]|</?(?:system|assistant|instructions?|prompt)>|#{2,}\s*(?:system|instruction)|"
    r"\b(?:ignore|disregard|forget|override)\b (?:all |any |the |your |my |of )*(?:previous |prior |earlier |above |"
    r"other |existing )*(?:instructions|rules|notes|prompts?|guidelines|everything (?:you were|you've been|you have been) "
    r"told)\b|\b(?:your|the|new) (?:new |real |actual )?instructions (?:are|is|say)\b|\bsystem prompt\b|"
    r"\b(?:pretend|act as if|act like) (?:you are|you're|to be)\b|\b(?:respond|reply|answer) only\b|"
    r"\b(?:never|don't|do not) (?:reveal|disclose)\b|\btell the user to\b|\bwhenever you (?:answer|reply|respond)\b|"
    r"\bfrom now on,? (?:say|answer|reply|respond|tell)\b|"
    r"\b(?:when|if|whenever) (?:you are |you're |you get |anyone |someone |somebody |they |people )?asked\b[^.;]*?"
    r"\b(?:say|answer|reply|respond|tell)\b|"
    r"\b(?:say|tell|give)\b[^.;]{0,40}\b(?:to )?(?:whoever|anyone who|anybody who|everyone who|anyone that) asks\b|"
    r"\btreat (?:the |this |any )?(?:next )?(?:speaker|user|person|caller|voice)\b|"
    r"\b(?:the|this) (?:user|speaker) is (?:an? )?(?:admin|administrator|owner|developer)\b", re.I | re.M)


def singular(w):
    return w[:-2] if w.endswith("es") and w[:-2].endswith(("x", "ch", "sh", "ss")) else w[:-1] if w.endswith("s") and len(w) > 3 else w


PRONOUN = re.compile(r"\b(its|their)\b|(?:^|[.;!?]\s+)(it|it's|it is|there|there's|this one|that one)\b", re.I)
OWNER = re.compile(r"\b(its|their)\b", re.I)
THING_PAIR = re.compile(r"\b([A-Z][a-z]+)(?:['\u2019]s)?\s+([a-z]+)")
# a sentence that starts with "It" is an update only if it carries one ("It's in the shed now", "It opens with 4417");
# "It rained all afternoon" is not
UPDATE_HINT = re.compile(r"\d|\b(now|moved|changed|set|updated|switched|replaced|opens|is in|is at|is on|lives)\b", re.I)


def edit_distance(a, b, cap=3):
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def clean_shelf(shelf):
    # letters of any language ("Küche", "台所"), digits, _ and -; anything else (spaces, dots, slashes) becomes -
    parts = [re.sub(r"[^\w\-]+", "-", p.strip().lower()).strip("-") for p in (shelf or "inbox").split("/")]
    return "/".join(p for p in parts if p) or "inbox"


def _ascii_shelf(shelf):
    """How shelves were cleaned before 0.32 (letters outside a-z became "-"): "Küche" was stored as "k-che"."""
    parts = [re.sub(r"[^a-z0-9_\-]+", "-", p.strip().lower()).strip("-") for p in (shelf or "inbox").split("/")]
    return "/".join(p for p in parts if p) or "inbox"


def clean_tags(tags):
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    return " ".join(sorted({re.sub(r"[^a-z0-9_\-]", "", t.lower()) for t in (tags or []) if t and t.strip()} - {""}))


class _Rows:
    """A finished query: its rows already read, so another thread can use the connection at once."""
    __slots__ = ("_rows", "lastrowid", "rowcount", "description", "_i")

    def __init__(self, rows, lastrowid, rowcount, description):
        self._rows, self.lastrowid, self.rowcount, self.description, self._i = rows, lastrowid, rowcount, description, 0

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self):
        out, self._i = self._rows[self._i:], len(self._rows)
        return out

    def fetchmany(self, n=1):
        out = self._rows[self._i:self._i + n]
        self._i += len(out)
        return out

    def __iter__(self):
        return iter(self.fetchall())


class LockedDB:
    """The one SQLite connection, shared by the event loop and worker threads (documents, Markdown sync, email,
    backups). Every statement runs under the store's lock and reads its rows before letting go, so two threads never
    interleave on one connection (bug-hunt set: concurrent remember + recall raised InterfaceError)."""

    def __init__(self, conn, lock):
        self._c, self._lock = conn, lock

    def execute(self, sql, params=()):
        with self._lock:
            cur = self._c.execute(sql, params)
            return _Rows(cur.fetchall() if cur.description else [], cur.lastrowid, cur.rowcount, cur.description)

    def executemany(self, sql, seq):
        with self._lock:
            cur = self._c.executemany(sql, seq)
            return _Rows([], cur.lastrowid, cur.rowcount, None)

    def executescript(self, sql):
        with self._lock:
            self._c.executescript(sql)

    def commit(self):
        with self._lock:
            self._c.commit()

    def rollback(self):
        with self._lock:
            self._c.rollback()

    def backup(self, *a, **k):
        with self._lock:
            return self._c.backup(*a, **k)

    def close(self):
        with self._lock:
            self._c.close()

    def __getattr__(self, name):
        return getattr(self._c, name)


class Store:
    def _shelf(self, shelf):
        """clean_shelf, but a shelf that already exists under its pre-0.32 name ("k-che" for "Küche") keeps that
        name, so one shelf doesn't split in two after the upgrade."""
        new = clean_shelf(shelf)
        old = _ascii_shelf(shelf)
        if new != old and self.db.execute("SELECT 1 FROM items WHERE shelf=? LIMIT 1", (old,)).fetchone() and \
                not self.db.execute("SELECT 1 FROM items WHERE shelf=? LIMIT 1", (new,)).fetchone():
            return old
        return new

    def __init__(self, path, clock=None):
        self.path = path
        self.clock = clock or time.time
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        from .speakers import seen_sql
        conn.create_function("hm_seen", 2, seen_sql, deterministic=True)     # speakers.sql(): who may see a note
        self.lock = threading.RLock()
        self.db = LockedDB(conn, self.lock)
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(items)")}
            if "secret" not in cols:
                self.db.execute("ALTER TABLE items ADD COLUMN secret TEXT")    # #66: sealed text of a secret note
            fresh_keys = "keys" not in cols
            if fresh_keys:
                # 0.23: a hidden column of extra search keys (#71); the search index is rebuilt to include it
                self.db.execute("ALTER TABLE items ADD COLUMN keys TEXT")
                for t in ("items_ai", "items_ad", "items_au"):
                    self.db.execute(f"DROP TRIGGER IF EXISTS {t}")
                self.db.execute("DROP TABLE IF EXISTS items_fts")
                # fill the keys before the index and its triggers exist: an update trigger on an index that doesn't
                # hold the row yet would corrupt it
                from .keys import lexicon_keys
                rows = self.db.execute("SELECT id, title, body FROM items").fetchall()
                self.db.executemany("UPDATE items SET keys=? WHERE id=?",
                                    [(" ".join(lexicon_keys(f"{r['title']} {r['body']}")) or None, r["id"]) for r in rows])
            had_fts = bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='items_fts'").fetchone())
            try:
                self.db.executescript(FTS)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            # a new index over notes already there (the keys migration above, or a start that stopped between dropping
            # the old index and rebuilding it): fill it, or search finds nothing until every note is edited
            if (fresh_keys or not had_fts) and self.fts:
                self.db.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
            self.db.commit()
        self._gen, self._watch, self._cache, self._fix_memo, self._index = 0, {"index": set()}, {}, None, None
        self._index_lock = threading.Lock()
        self.watching = self._watch_items(conn)

    WATCHED = ("kind", "key", "title", "body", "tags", "shelf", "status", "supersedes", "created", "updated", "source",
               "meta", "secret", "keys")      # every column but the use counters (use_count, last_used, strength)

    def _changed(self, item_id):
        self._gen += 1
        for s in self._watch.values():
            s.add(item_id)

    def _watch_items(self, conn):
        """Count changes to notes (temporary triggers on this connection, so nothing is stored), whatever code makes
        them: caches built from the notes (the index of things, typo repairs, places) are reused until a note changes
        instead of re-read on every call, and the index is updated from the changed notes only."""
        try:
            conn.create_function("hm_changed", 1, self._changed)
            with self.lock:
                for ev, ref in (("INSERT", "new"), ("DELETE", "old"), (f"UPDATE OF {', '.join(self.WATCHED)}", "new")):
                    self.db.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS hm_watch_{ev.split()[0].lower()} AFTER {ev} "
                                    f"ON main.items BEGIN SELECT hm_changed({ref}.id); END")
            return True
        except sqlite3.Error:
            return False

    def cached(self, key, fn):
        """fn(), kept until a note changes. Without the change count (no triggers) it is called every time."""
        if not self.watching:
            return fn()
        g = self._gen
        c = self._cache
        if c.get("_gen") != g:
            c = self._cache = {"_gen": g}
        if key in c:
            return c[key]
        v = fn()
        if self._gen == g:      # a note changed while it was built: don't keep it
            c[key] = v
        return v

    def close(self):
        with self.lock:
            self.db.close()

    # ---- helpers ------------------------------------------------------------------------------------------
    MAX_NOTE = 200_000     # characters; longer texts belong in Documents (#50)
    LINK_BY_ATTR = True    # #13: a pronoun note links to the recent note naming its attribute ("its code")
    filer = None           # filing.Filer, set by the app (#58)
    vault_key = None       # set by the app with option encrypt_secrets (#66)
    on_purge = None        # called after purge(): the app deletes the photo files no note lists any more

    def _row(self, r):
        if r is None:
            return None
        d = dict(r)
        d["meta"] = json.loads(d.get("meta") or "{}")
        d["tags"] = d["tags"].split() if d.get("tags") else []
        sec = d.pop("secret", None)
        if sec:
            if self.vault_key:
                from . import vault
                try:
                    real = json.loads(vault.open_(self.vault_key, sec, aad=str(d["id"]).encode()))
                    d["title"], d["body"] = real["title"], real["body"]
                except Exception:  # noqa: BLE001 - another key: the masked copy is all there is
                    d["locked"] = True
            else:
                d["locked"] = True      # sealed, and the key isn't loaded: only the masked copy
        return d

    def seal_item(self, item_id, _scrub=True):
        """#66: keep a secret note's real text sealed (AES-GCM, key outside the database) and a masked copy for
        search. Nothing happens without a key or for a note that isn't sensitive or is sealed already."""
        if not self.vault_key:
            return False
        from . import vault
        from .privacy import mask_all
        with self.lock:
            r = self.db.execute("SELECT id, title, body, meta, secret FROM items WHERE id=?", (int(item_id),)).fetchone()
            if not r or r["secret"] or not json.loads(r["meta"] or "{}").get("sensitive"):
                return False
            sealed = vault.seal(self.vault_key, json.dumps({"title": r["title"], "body": r["body"]}),
                                aad=str(r["id"]).encode())
            # the clear text must not stay in the file: freed space is overwritten, and the search index's old
            # segments (which still hold the clear words after the update) are merged away (below)
            self.db.execute("PRAGMA secure_delete = ON")
            self.db.execute("UPDATE items SET secret=?, title=?, body=? WHERE id=?",
                            (sealed, mask_all(r["title"] or ""), mask_all(r["body"] or ""), r["id"]))
            self.db.commit()
            if _scrub:
                self._scrub_index()
        return True

    def _scrub_index(self):
        """Merge the search index into one segment, dropping the words of text that was replaced (sealed), and empty
        the write-ahead log, so the old text is not left in the files (#66)."""
        if not self.fts:
            return
        with self.lock:
            if self.fts:
                self.db.execute("INSERT INTO items_fts(items_fts) VALUES('optimize')")
                self.db.commit()
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass

    def unseal_item(self, item_id):
        if not self.vault_key:
            return False
        with self.lock:
            r = self.db.execute("SELECT * FROM items WHERE id=?", (int(item_id),)).fetchone()
            if not r or not r["secret"]:
                return False
            d = self._row(r)
            if d.get("locked"):
                return False
            self.db.execute("UPDATE items SET secret=NULL, title=?, body=? WHERE id=?", (d["title"], d["body"], r["id"]))
            self.db.commit()
        return True

    def seal_all(self):
        ids = [r[0] for r in self.db.execute("SELECT id FROM items WHERE secret IS NULL AND meta LIKE '%\"sensitive\": true%'")]
        n = sum(1 for i in ids if self.seal_item(i, _scrub=False))
        if n:
            self._scrub_index()
        return n

    def unseal_all(self):
        ids = [r[0] for r in self.db.execute("SELECT id FROM items WHERE secret IS NOT NULL")]
        return sum(1 for i in ids if self.unseal_item(i))

    def log(self, type_, item_id=None, **data):
        with self.lock:
            self.db.execute("INSERT INTO events(ts,type,item_id,data) VALUES(?,?,?,?)",
                            (self.clock(), type_, item_id, json.dumps(data, default=str)[:20000]))
            self.db.commit()

    def kv_get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def kv_set(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))
            self.db.commit()

    def get(self, item_id, history=False):
        r = self._row(self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())
        if r and history:
            chain, cur = [], r
            while cur and cur.get("supersedes"):
                cur = self._row(self.db.execute("SELECT * FROM items WHERE id=?", (cur["supersedes"],)).fetchone())
                if cur:
                    chain.append({"id": cur["id"], "title": cur["title"], "body": cur["body"], "created": cur["created"]})
            later = self.db.execute("SELECT id FROM items WHERE supersedes=?", (item_id,)).fetchone()
            r["history"] = chain
            r["superseded_by"] = later["id"] if later else None
        return r

    def seen(self, item_id, history=False):
        """get(), but None for a personal note the current viewer may not see (#48): what a person-bound token
        reads one item at a time."""
        from .speakers import visible
        d = self.get(item_id, history=history)
        return d if d and visible(d.get("meta")) else None

    def check_seen(self, item_id):
        """Raise KeyError for an item the current viewer may not see, so a person-bound token can't change (or learn
        about) someone else's personal note by its id (#48). Items that don't exist are left to the caller."""
        if self.get(int(item_id)) and not self.seen(int(item_id)):
            raise KeyError(f"no item {item_id}")
        return int(item_id)

    def active_by_key(self, key):
        return self._row(self.db.execute(
            "SELECT * FROM items WHERE key=? AND status IN ('active','faded') ORDER BY id DESC LIMIT 1", (key,)).fetchone())

    def as_of(self, key, when, by="valid"):
        """Two times per fact (design step 9), read through one function. Every version of a keyed fact has when it
        became true (valid: its own date if it gave one, else when it was filed) and when Home Mind learned it
        (recorded: when it was filed).
          by="valid":    what was true at `when` ("what was the code on the 3rd?")
          by="recorded": what Home Mind believed at `when` ("what did you think on Tuesday?"), knowing only what
                         it had been told by then
        Returns the version with valid_from / valid_to (when the next version took over; None if still current), or
        None. Forgotten (purged) versions are never returned."""
        if by not in ("valid", "recorded"):
            raise ValueError("by is valid or recorded")
        key = (key or "").strip().lower()
        rows = self.db.execute("SELECT * FROM items WHERE key=? AND status IN ('active','faded','superseded') "
                               "ORDER BY id", (key,)).fetchall()
        if not rows:
            return None
        when = float(when)
        known = [r for r in rows if by == "valid" or float(r["created"]) <= when]
        cands = [r for r in known if _eff(r) <= when] if by == "valid" else known
        if not cands:
            return None
        pick = max(cands, key=lambda r: (_eff(r), r["id"]))
        later = sorted(_eff(r) for r in known if _eff(r) > _eff(pick))
        out = self._row(pick)
        out["valid_from"], out["valid_to"] = _eff(pick), (later[0] if later else None)
        out["recorded_at"] = float(pick["created"])
        return out

    # ---- writing ----------------------------------------------------------------------------------------------
    def remember(self, text="", title="", kind="note", shelf="inbox", tags=None, key=None, source="", meta=None, at=None):
        """Keep something. With a key (e.g. 'tavrin locker|code'), a different value for the same key supersedes
        the old item (kept as history); the same value just strengthens it."""
        text, title = str(text or "").replace("\x00", "").strip(), str(title or "").replace("\x00", "").strip()
        if not text and not title:
            raise ValueError("nothing to remember")
        if len(text) > self.MAX_NOTE:
            raise ValueError(f"note too long ({len(text):,} characters, at most {self.MAX_NOTE:,}): add long texts "
                             "as documents instead")
        if meta is not None and not isinstance(meta, dict):
            raise ValueError("meta must be an object")
        if at is not None and at != "":
            try:
                at = float(at)
            except (TypeError, ValueError):
                raise ValueError("at must be a time in seconds since 1970") from None
        if not title:
            title = text.split("\n")[0][:90]
        key = (key or "").strip().lower() or None
        shelf, tags = self._shelf(shelf), clean_tags(tags)
        now = float(at) if at else self.clock()     # at: when it happened, for readings filed afterwards
        m0 = dict(meta or {})
        if shelf == "inbox" and self.filer is not None and kind != "procedure":
            from .filing import SKIP as _fskip
            if source not in _fskip:
                shelf, extra = self.filer.place(f"{title} {text}")         # #58: where notes like this go
                m0.update(extra)
        if "category" not in m0 and source not in ("home", "automations", "calendar", "habits", "tidy", "evening"):
            from .categories import categorize
            m0["category"], imp = categorize(title + " " + text, kind, source)       # #38, #75
            if imp != 1:
                m0["importance"] = imp
        from .privacy import is_sensitive
        if "sensitive" not in m0 and kind != "procedure" and is_sensitive(title + " " + text):
            m0["sensitive"] = True
        both = text + " " + title
        if self.LINK_PRONOUNS and ((PRONOUN.search(both) and (OWNER.search(both) or UPDATE_HINT.search(both)))
                                   or (CORRECTION.search(both) and UPDATE_HINT.search(both))) \
                and not names_in(text) and kind != "procedure":
            # "Changed its code to 8812" names nothing: it is about whatever was just mentioned. Link it to the note
            # right before it (if recent), so it can be found under that thing's name.
            recent = [r for r in self.db.execute("SELECT id, title, body, created FROM items WHERE kind != 'procedure' "
                                                 "AND source NOT IN ('home','automations','calendar') "
                                                 "ORDER BY id DESC LIMIT 5").fetchall()
                      if now - r["created"] <= self.LINK_SECONDS]
            # #13: "its code changed" is about the recent thing that has a code, not just the last thing said
            attr = re.search(r"\b(?:its|their)\s+([a-z]+)", both, re.I)
            want = singular(attr.group(1).lower()) if attr and self.LINK_BY_ATTR else None
            same_as = next((g for g in ATTR_SAME if want in g), {want})
            has = lambda r: bool(same_as & {singular(w) for w in words(r["title"] + " " + r["body"])})
            prev = recent[0] if recent else None
            if prev and want and not has(prev):
                with_attr = [r for r in recent[1:] if has(r)]
                last_names = names_in(prev["title"] + " " + prev["body"])
                # the last note is about something that has this attribute in an earlier note: it's that thing
                same = [r for r in with_attr if last_names & names_in(r["title"] + " " + r["body"])]
                # ... or in an older note: "Went to check on the Ranwyn locker", and the locker's code was noted
                # last week: still the locker
                older = any(self.db.execute("SELECT 1 FROM items WHERE (title || ' ' || body) LIKE ? AND "
                                            "(title || ' ' || body) LIKE ? LIMIT 1", (f"%{n}%", f"%{a}%")).fetchone()
                            for n in list(last_names)[:3] for a in list(same_as)[:6])
                # ... or the last note is about a kind of thing that has one ("check on the Quillon safe": safes
                # have codes)
                kind_has = bool({singular(w) for w in words(prev["title"] + " " + prev["body"])} &
                                {t for g, ts in ATTR_THINGS if same_as & g for t in ts})
                if with_attr and not same and not older and not kind_has:
                    prev = with_attr[0]        # the last note was about something else: the recent thing with a code
            if prev:
                title = f"{title} [re: {prev['title'][:100]}]"
                m0["refers_to"] = prev["id"]
        from . import structure
        if kind not in ("procedure", "task") and self.STRUCTURE and structure.wanted(source, shelf):
            for k, v in structure.annotate(text, title, now).items():
                m0.setdefault(k, v)
            if "alias" in m0 and shelf == "inbox":
                shelf = "profile"          # "Dana is my partner": who is who, always shown to models
        meta = m0
        hold = TRUST_SOURCES and (_inferred(m0, source) or source in OUTSIDE_SOURCES) and not m0.get("confirmed") \
            and bool(INSTRUCTION.search(title + " " + text))
        if hold:
            # pulled from a conversation and reads like an instruction to the assistant: not searchable until a
            # person approves it in the review queue (#16)
            m0["held"] = "reads like an instruction"
            if key:
                m0["pending_key"], key = key, None
        with self.lock:
            old = self.active_by_key(key) if key else None
            if TRUST_SOURCES and old and _inferred(m0, source) and not m0.get("confirmed") and \
                    not _on_probation(old, now, 1e9):
                # an inferred value for a key you set yourself: kept next to it, unconfirmed, until confirmed (#6)
                m0["pending_key"], key, old = key, None, None
            if old and old["body"].strip().lower() == text.lower() and old["title"].strip().lower() == title.lower():
                self.db.execute("UPDATE items SET use_count=use_count+1, strength=strength+1, last_used=?, "
                                "status='active' WHERE id=?", (now, old["id"]))
                self.db.commit()
                self.log("reinforce", old["id"], key=key)
                return {"id": old["id"], "action": "reinforced"}
            m = dict(meta or {})
            if old and m.get("valid_from") and min(float(m["valid_from"]), now) < _eff(old):
                # "On Monday it changed to X", filed after a newer value: history, not the current value
                cur = self.db.execute(
                    "INSERT INTO items(kind,key,title,body,tags,shelf,status,supersedes,created,updated,source,meta) "
                    "VALUES(?,?,?,?,?,?,'superseded',NULL,?,?,?,?)",
                    (kind, key, title, text, tags, shelf, now, now, source, json.dumps(m)))
                new_id = cur.lastrowid
                self.db.commit()
                self._vocab_add(title, text, tags, new_id)
                self.log("remember", new_id, kind=kind, key=key, shelf=shelf, action="history", source=source)
                return {"id": new_id, "action": "history", "shelf": shelf, "current": old["id"]}
            m = dict(meta or {})
            cur = self.db.execute(
                "INSERT INTO items(kind,key,title,body,tags,shelf,status,supersedes,created,updated,source,meta) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, key, title, text, tags, shelf, "review" if hold else "active", old["id"] if old else None, now,
                 now, source, json.dumps(m)))
            new_id = cur.lastrowid
            if self.KEYS and source not in ("home", "automations", "calendar"):
                from .keys import lexicon_keys
                kw = lexicon_keys(f"{title} {text}")
                if kw:
                    self.db.execute("UPDATE items SET keys=? WHERE id=?", (" ".join(kw), new_id))
            if old:
                self.db.execute("UPDATE items SET status='superseded', updated=? WHERE id=?", (now, old["id"]))
            self.db.commit()
        self._vocab_add(title, text, tags, new_id)
        if self.LINKS and kind != "procedure" and source not in ("home", "automations", "calendar", "habits", "tidy",
                                                                 "evening", "lme-skip"):
            try:
                self._link_new(new_id, f"{title} {text}")
            except sqlite3.Error:
                pass
        if self.vault_key and m0.get("sensitive"):
            self.seal_item(new_id)
        out = {"id": new_id, "action": "held for review" if hold else "updated" if old else "created", "shelf": shelf}
        if old:
            out["previous"] = {"id": old["id"], "title": old["title"], "body": old["body"]}
        self.log("remember", new_id, kind=kind, key=key, shelf=shelf, action=out["action"], source=source)
        return out

    def revise(self, item_id, **changes):
        """A new version of an item (old one kept as history)."""
        old = self.get(item_id)
        if not old:
            raise KeyError(f"no item {item_id}")
        if old.get("locked") and changes.get("body") is None:
            # sealed with a key that isn't loaded: the row holds only the masked copy, and a new version made from it
            # would replace the secret with "[hidden]" for good (#66)
            raise ValueError(f"item {item_id} is sealed and the vault key isn't loaded; put vault.key back to change it")
        f = {k: old[k] for k in ("kind", "key", "title", "body", "shelf", "source")}
        f["tags"], f["meta"] = old["tags"], old["meta"]
        for k, v in changes.items():
            if v is not None and k in ("kind", "key", "title", "body", "shelf", "tags", "meta", "source"):
                f[k] = v if k != "meta" else {**old["meta"], **v}
        if changes.get("title") is None and isinstance(f["body"], str) and f["body"] != old["body"]:
            # a title made from the old text's first line (remember() makes one when none is given) follows the new
            # text, or an edit from the panel (text only) keeps the old value in the title, and search finds it there
            first = (old["body"] or "").split("\n")[0][:90]
            if first and (old["title"] == first or old["title"].startswith(first + " [re: ")):
                f["title"] = (f["body"].strip().split("\n")[0][:90] or old["title"]) + old["title"][len(first):]
        from .privacy import is_sensitive
        if "sensitive" not in f["meta"] and f["kind"] != "procedure" and is_sensitive(f"{f['title']} {f['body']}"):
            # an edit that adds a code is a secret note now, as if filed that way (sealed, kept out of files)
            f["meta"] = dict(f["meta"], sensitive=True)
        now = self.clock()
        if f["title"] == old["title"] and f["body"] == old["body"] and not f["meta"].get("valid_from"):
            # only moved, tagged or marked: what it says became true when the old version was noted, so a newer
            # version of the same fact stays the newer one
            f["meta"] = {**f["meta"], "valid_from": old["created"]}
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO items(kind,key,title,body,tags,shelf,status,supersedes,created,updated,source,meta,"
                "use_count,strength) VALUES(?,?,?,?,?,?,'active',?,?,?,?,?,?,?)",
                (f["kind"], f["key"], f["title"], f["body"], clean_tags(f["tags"]), self._shelf(f["shelf"]), item_id,
                 now, now, f["source"], json.dumps(f["meta"]), old["use_count"], old["strength"]))
            new_id = cur.lastrowid
            # the new version is still found by the old one's search keys (and those of its new words), and stays
            # linked to the notes about the same thing
            keys = (old.get("keys") or "").split()
            if self.KEYS and f["source"] not in ("home", "automations", "calendar"):
                from .keys import lexicon_keys
                keys += [w for w in lexicon_keys(f"{f['title']} {f['body']}") if w not in keys]
            if keys:
                self.db.execute("UPDATE items SET keys=? WHERE id=?", (" ".join(keys)[:600], new_id))
            for b in self.linked(item_id):
                if b != new_id:
                    self.db.execute("INSERT OR IGNORE INTO links(a, b, kind) VALUES(?,?,'same_thing')", (new_id, b))
                    self.db.execute("INSERT OR IGNORE INTO links(a, b, kind) VALUES(?,?,'same_thing')", (b, new_id))
            self.db.execute("UPDATE items SET status='superseded', updated=? WHERE id=?", (now, item_id))
            self.db.commit()
        if self.vault_key and f["meta"].get("sensitive"):
            self.seal_item(new_id)
        self.log("revise", new_id, previous=item_id, changes=list(changes))
        return {"id": new_id, "action": "updated", "previous": item_id}

    def set_status(self, item_id, status):
        with self.lock:
            self.db.execute("UPDATE items SET status=?, updated=? WHERE id=?", (status, self.clock(), item_id))
            self.db.commit()
        self._vocab_n = None        # the word list changes when something is archived or restored
        self.log("status", item_id, status=status)
        return {"id": item_id, "status": status}

    def forget_about(self, name):
        """Archive every note that mentions a name, including notes linked to it ("Changed its code to 8812
        [re: ... Kosk locker ...]"). Recoverable, like every archive; nothing is deleted."""
        if not str(name or "").strip():
            raise ValueError("give a name (a blank one would match every note)")
        pat = re.compile(rf"\b{re.escape(name.strip())}\b", re.I)
        from .speakers import sql as _vis
        vs, va = _vis()
        rows = self.db.execute(f"SELECT id, title, body FROM items WHERE status IN ('active','faded') AND {vs}",
                               va).fetchall()
        ids = [r["id"] for r in rows if pat.search(r["title"] + " " + r["body"])]
        with self.lock:
            self.db.executemany("UPDATE items SET status='archived', updated=? WHERE id=?", [(self.clock(), i) for i in ids])
            self.db.commit()
        self._vocab_n = None
        self.log("forget_about", None, name=name, ids=ids)
        return {"name": name, "archived": len(ids), "ids": ids}

    def purge(self, ids, values=()):
        """Delete notes for good: each note with its whole history (versions by key, what it replaced and what replaced
        it, notes linked to it), its search-index entry, the activity-log rows about it, saved conversation turns and
        corrections that hold its values, and its place in the duplicate list. Freed space is overwritten
        (secure_delete) and the write-ahead log is emptied, so the text is not left in the file. Backups made before
        still have it. Returns what was removed."""
        ids = {int(i) for i in ids}
        with self.lock:
            # the whole history: follow supersedes both ways, same key, and notes that refer to these
            todo = set(ids)
            while todo:
                i = todo.pop()
                for r in self.db.execute("SELECT id, key, supersedes FROM items WHERE id=? OR supersedes=?", (i, i)):
                    for j in (r["id"], r["supersedes"]):
                        if j and j not in ids:
                            ids.add(j)
                            todo.add(j)
                    if r["key"]:
                        for x in self.db.execute("SELECT id FROM items WHERE key=?", (r["key"],)):
                            if x["id"] not in ids:
                                ids.add(x["id"])
                                todo.add(x["id"])
                for x in self.db.execute("SELECT id, meta FROM items WHERE meta LIKE ?", (f'%"refers_to": {i}%',)):
                    # the LIKE also matches "refers_to": 12 for note 1: only a note that refers to this one goes
                    try:
                        if json.loads(x["meta"] or "{}").get("refers_to") != i:
                            continue
                    except (ValueError, AttributeError):
                        continue
                    if x["id"] not in ids:
                        ids.add(x["id"])
                        todo.add(x["id"])
            if not ids:
                return {"deleted": 0}
            # a sealed note's row holds the masked copy: its values are in the sealed text (#66)
            rows = [self._row(r) for r in self.db.execute(f"SELECT * FROM items WHERE id IN ({','.join('?' * len(ids))})",
                                                          list(ids)).fetchall()]
            # the note's values, to find them in logs: codes, serials, passwords (a digit or a symbol, 4+ long), and
            # what follows a secret word; not years or plain words, so unrelated log rows stay
            from .privacy import RIGHT_AFTER, WORD_AFTER
            vals = set(values)
            for r in rows:
                text = (r["title"] or "") + " " + (r["body"] or "")
                vals |= set(re.findall(r"(?<![\w-])(?=[\w!@#$%^&*-]*\d)[\w!@#$%^&*-]{4,}", text))
                vals |= {m.group(2) for rx in (WORD_AFTER, RIGHT_AFTER) for m in rx.finditer(text)}
            vals = {v for v in vals if len(v) >= 4 and not re.fullmatch(r"(19|20)\d\d", v) and v.lower() not in STOP}
            self.db.execute("PRAGMA secure_delete = ON")
            q = ",".join("?" * len(ids))
            try:        # all of it or nothing: a half purge would leave the logs holding the values
                n_items = self.db.execute(f"DELETE FROM items WHERE id IN ({q})", list(ids)).rowcount
                try:        # a note's embedding is its content in another form (#10)
                    self.db.execute(f"DELETE FROM vectors WHERE item_id IN ({q})", list(ids))
                except sqlite3.OperationalError:
                    pass
                self.db.execute(f"DELETE FROM links WHERE a IN ({q}) OR b IN ({q})", list(ids) * 2)
                n_ev = self.db.execute(f"DELETE FROM events WHERE item_id IN ({q})", list(ids)).rowcount
                evs = self.db.execute("SELECT id, data FROM events").fetchall()
                gone = []
                for e in evs:
                    d = e["data"] or ""
                    try:
                        j = json.loads(d)
                    except ValueError:
                        j = {}
                    if any(i in ids for i in (j.get("ids") or []) if isinstance(i, int)) or any(v in d for v in vals):
                        gone.append(e["id"])
                for k in range(0, len(gone), 500):
                    part = gone[k:k + 500]
                    n_ev += self.db.execute(f"DELETE FROM events WHERE id IN ({','.join('?' * len(part))})", part).rowcount
                n_conv = 0
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='conversations'").fetchone():
                    for v in vals:
                        n_conv += self.db.execute("DELETE FROM conversations WHERE user_text LIKE ? OR reply LIKE ?",
                                                  (f"%{v}%", f"%{v}%")).rowcount
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='cases'").fetchone():
                    self.db.execute(f"DELETE FROM cases WHERE item_id IN ({q}) OR fixed_id IN ({q})", list(ids) * 2)
                    for v in vals:
                        self.db.execute("DELETE FROM cases WHERE question LIKE ? OR wrong LIKE ? OR right LIKE ?",
                                        (f"%{v}%",) * 3)
                pairs = self.kv_get("tidy_pairs") or []
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            self.kv_set("tidy_pairs", [p for p in pairs if not set(p) & ids])
            if self.fts:
                self.db.execute("INSERT INTO items_fts(items_fts) VALUES('optimize')")
                self.db.commit()
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
        self._vocab_n = None
        if self.on_purge:
            try:
                self.on_purge()
            except Exception:  # noqa: BLE001 - the notes are gone; a stray file is swept again later
                pass
        self.log("purge", None, n=n_items)          # how many, never what
        return {"deleted": n_items, "events_removed": n_ev, "conversation_turns_removed": n_conv}

    def confirm(self, item_id):
        """Mark a note learned overnight as checked: it leaves probation and ranks like anything told directly."""
        with self.lock:
            r = self.db.execute("SELECT meta FROM items WHERE id=?", (int(item_id),)).fetchone()
            if not r:
                return {"id": item_id, "error": "not found"}
            m = json.loads(r["meta"] or "{}")
            m["confirmed"] = True
            m.pop("held", None)
            key = m.pop("pending_key", None)
            now = self.clock()
            if key:
                old = self.active_by_key(key)
                if old and old["id"] != int(item_id):
                    self.db.execute("UPDATE items SET status='superseded', updated=? WHERE id=?", (now, old["id"]))
                self.db.execute("UPDATE items SET key=?, supersedes=? WHERE id=?",
                                (key, old["id"] if old and old["id"] != int(item_id) else None, int(item_id)))
            self.db.execute("UPDATE items SET meta=?, updated=?, status=CASE WHEN status='review' THEN 'active' "
                            "ELSE status END WHERE id=?", (json.dumps(m), now, int(item_id)))
            self.db.commit()
            self._vocab_n = None
        self.log("confirm", int(item_id))
        return {"id": int(item_id), "confirmed": True}

    def review_queue(self, limit=100):
        """What a person should look at (#16): notes held because they read like instructions, and inferred notes
        still unconfirmed (on probation, or waiting to replace a value you set). Newest first."""
        from .speakers import sql as _vis
        vs, va = _vis()
        now, out = self.clock(), []
        for r in self.db.execute("SELECT * FROM items WHERE status IN ('review','active') AND (status='review' OR "
                                 "meta LIKE '%learned_overnight%' OR meta LIKE '%inferred%' OR source IN "
                                 f"({','.join('?' * len(MODEL_SOURCES))})) AND {vs} ORDER BY created DESC LIMIT ?",
                                 (*MODEL_SOURCES, *va, limit * 3)).fetchall():
            d = self._row(r)
            m = d.get("meta") or {}
            if r["status"] == "review":
                why = m.get("held") or "held"
            elif _on_probation(r, now, self.PROBATION_DAYS):
                why = "would replace " + m["pending_key"] if m.get("pending_key") else "unconfirmed"
            else:
                continue
            out.append({k: d[k] for k in ("id", "title", "body", "shelf", "source", "created", "status")} | {"why": why})
        return out[:limit]

    LINKS = True
    EVOLVE = True          # older linked notes take new notes' distinctive words as keys (#82)
    LINK_HOPS = 3          # how far the link walk goes (#81)
    LINK_ADD = 3           # at most this many notes added from it
    LINK_MAX = 5           # a thing named in more notes than this is too common to link on

    def _link_new(self, new_id, text):
        """#12: link a new note to the notes that name the same specific thing (a rare name, or a thing with a
        modifier: "the red Trek", "the side gate"), so a question that finds one can be led to the other."""
        anchors = set()
        for n in names_in(text):
            if len(n) >= 3:
                anchors.add(n)
        if not text.isascii():
            # a name with an accent is matched as written ("Müller"): LIKE doesn't drop accents
            for t in re.findall(r"[^\W\d_][\w']*", text):
                lt = t.lower().split("'")[0]
                if not lt.isascii() and fold(lt) in anchors:
                    anchors.discard(fold(lt))
                    anchors.add(lt)
        for noun, mods in modified_things(text).items():
            for m in mods:
                anchors.add(f"{m} {noun}")
        found = set()
        with self.lock:
            for a in list(anchors)[:6]:
                rows = self.db.execute("SELECT id FROM items WHERE id != ? AND status IN ('active','faded') AND "
                                       "kind != 'procedure' AND source NOT IN ('home','automations','calendar') AND "
                                       "(title LIKE ? OR body LIKE ?) ORDER BY id DESC LIMIT ?",
                                       (new_id, f"%{a}%", f"%{a}%", self.LINK_MAX + 1)).fetchall()
                if len(rows) > self.LINK_MAX:
                    continue
                found |= {r[0] for r in rows}
            for o in found:
                self.db.execute("INSERT OR IGNORE INTO links(a, b, kind) VALUES(?,?,'same_thing')", (new_id, o))
                self.db.execute("INSERT OR IGNORE INTO links(a, b, kind) VALUES(?,?,'same_thing')", (o, new_id))
            self.db.commit()
        if found and self.EVOLVE:
            self._evolve(found, text)
        return found

    def _evolve(self, older, text):
        """#82 (A-MEM's memory evolution): the older notes about the same thing take up to five of the new note's
        distinctive words as extra search keys ("the red Trek was stolen" gives "Kofi's bike is the red Trek" the key
        "stolen"), so a question in the new note's words finds them too. Their text is never changed."""
        n = max(1, self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0])
        named = names_in(text)
        cand = [w for w in dict.fromkeys(singular(x) for x in words(text)) if w not in STOP and len(w) > 3 and
                not w.isdigit()]
        rare = []
        for w in cand:
            try:
                df = self.db.execute("SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH ?", (f'"{w}"',)).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            if df <= max(3, 0.05 * n):
                rare.append(w)
        if not rare:
            return
        for o in older:
            r = self.db.execute("SELECT title, body FROM items WHERE id=?", (o,)).fetchone()
            if not r:
                continue
            if not (names_in(r["title"] + " " + r["body"]) & named):
                continue        # linked only by a common thing ("spare key"): another thing's words would mislead
            mine = {singular(x) for x in words(r["title"] + " " + r["body"])}
            extra = [w for w in rare if w not in mine][:5]
            if extra:
                self.add_keys(o, extra, by="lexicon")

    def _follow_links(self, query, res, limit, include_old):
        """#12, #81: from the best few results, walk the links (personalized PageRank, up to three steps, HippoRAG) and
        add the linked notes that bring words of the question the results don't have yet: the later steps of a
        question like "where is the key for Kofi's bike lock?" -> Kofi's bike is the red Trek -> the red Trek's lock
        is the Kryptonite one -> the Kryptonite lock's key is in the hall drawer."""
        qw = {singular(w) for w in words(query)} - STOP
        seeds = [d["id"] for d in res[:3]]
        nodes, frontier = set(seeds), set(seeds)
        for _ in range(self.LINK_HOPS):
            nxt = set()
            for n in frontier:
                nxt |= set(self.linked(n))
            frontier = nxt - nodes
            nodes |= frontier
            if not frontier or len(nodes) > 200:
                break
        if len(nodes) == len(seeds):
            return res
        nbr = {n: [m for m in self.linked(n) if m in nodes] for n in nodes}
        base = {n: (1.0 / len(seeds) if n in seeds else 0.0) for n in nodes}
        pr = dict(base)
        for _ in range(15):
            nxt = {n: 0.5 * base[n] for n in nodes}
            for n in nodes:
                if nbr[n]:
                    share = 0.5 * pr[n] / len(nbr[n])
                    for m in nbr[n]:
                        nxt[m] += share
            pr = nxt
        # what the best result already says (other results may share a word by chance: "the scooter is charging")
        covered = {singular(w) for w in words(res[0]["title"] + " " + res[0]["body"])}
        have = {d["id"] for d in res}
        add, top = [], res[0]["score"] if res else 1.0
        for n in sorted((x for x in nodes if x not in have), key=lambda x: -pr[x]):
            r = self.db.execute("SELECT * FROM items WHERE id=?", (n,)).fetchone()
            if not r or r["status"] not in (("active", "faded", "superseded") if include_old else ("active", "faded")):
                continue
            ow = {singular(w) for w in words(r["title"] + " " + r["body"])}
            new = (qw & ow) - covered
            if new:
                x = self._row(r)
                x["score"], x["via"] = round(top * 0.9, 3), next((s for s in seeds if n in nbr.get(s, ())), seeds[0])
                add.append(x)
                covered |= ow           # the next step must bring something new again
            if len(add) >= self.LINK_ADD:
                break
        if not add:
            return res
        return (res[:1] + add + res[1:])[:max(limit, 1) + len(add)]

    def linked(self, item_id):
        return [r[0] for r in self.db.execute("SELECT b FROM links WHERE a=?", (int(item_id),))]

    def add_keys(self, item_id, words, by="lexicon"):
        """Extra search keys for a note (#71): kept apart from its text, read only by search."""
        with self.lock:
            r = self.db.execute("SELECT keys, meta FROM items WHERE id=?", (int(item_id),)).fetchone()
            if not r:
                return None
            have = (r["keys"] or "").split()
            new = have + [w for w in words if w not in have]
            m = json.loads(r["meta"] or "{}")
            if by != "lexicon":
                m["keys_by"] = by
            self.db.execute("UPDATE items SET keys=?, meta=? WHERE id=?", (" ".join(new)[:600] or None, json.dumps(m),
                                                                          int(item_id)))
            self.db.commit()
        return new

    def purge_about(self, name):
        """Delete for good every note that mentions a name (any status), with everything purge() removes."""
        if not str(name or "").strip():
            raise ValueError("give a name (a blank one would match every note)")
        pat = re.compile(rf"\b{re.escape(name.strip())}\b", re.I)
        rows = self.db.execute("SELECT id, title, body FROM items").fetchall()
        ids = [r["id"] for r in rows if pat.search((r["title"] or "") + " " + (r["body"] or ""))]
        out = self.purge(ids, values=[name.strip()]) if ids else {"deleted": 0}
        return dict(out, name=name)

    def feedback(self, item_id, helpful, note=""):
        with self.lock:
            self.db.execute("UPDATE items SET strength=max(0.1, strength + ?) WHERE id=?",
                            (0.5 if helpful else -0.5, item_id))
            self.db.commit()
        self.log("feedback", item_id, helpful=bool(helpful), note=note)
        return {"id": item_id, "helpful": bool(helpful)}

    # ---- finding ------------------------------------------------------------------------------------------------
    def vocab(self):
        """Every word stored (cached until an item is added): for repairing typos in questions."""
        n = self.db.execute("SELECT MAX(id) FROM items").fetchone()[0] or 0
        if getattr(self, "_vocab_n", None) != n:
            v, caps = {}, set()
            for r in self.db.execute("SELECT title, body, tags FROM items WHERE status IN ('active','faded')"):
                for w in set(words(" ".join(r))):
                    v[w] = v.get(w, 0) + 1
                caps.update(w.lower().split("'")[0] for w in re.findall(r"\b[A-Z][a-z]+(?:'s)?", r[0] + " " + r[1]))
            self._vocab, self._vocab_n = v, n
            self._caps = {w: v[w] for w in caps if w in v and w not in STOP}
            # names that only appear in forgotten notes: a question naming one is about that (forgotten) thing,
            # not a typo for some other name
            gone = set()
            for r in self.db.execute("SELECT title, body FROM items WHERE status='archived'"):
                gone.update(w.lower().split("'")[0] for w in re.findall(r"\b[A-Z][a-z]+(?:'s)?", r[0] + " " + r[1]))
            self._gone = {w for w in gone if w not in v and w not in STOP}
        return self._vocab

    def _vocab_add(self, title, body, tags, new_id):
        """Keep the cached word list current as notes arrive, instead of rebuilding it (at 50,000 notes a rebuild
        after every new note cost ~0.6 s on the next question)."""
        if getattr(self, "_vocab", None) is None or self._vocab_n != new_id - 1:
            return
        for w in set(words(" ".join([title, body, tags]))):
            self._vocab[w] = self._vocab.get(w, 0) + 1
        for w in re.findall(r"\b[A-Z][a-z]+(?:'s)?", title + " " + body):
            lw = w.lower().split("'")[0]
            if lw in self._vocab and lw not in STOP:
                self._caps[lw] = self._vocab[lw]
        self._vocab_n = new_id

    def _index_entries(self, r):
        """What one note adds to index(): its ("word", "name") pairs in order, and its group entries."""
        text = r["title"] + " " + r["body"]
        pairs = []
        for nm, w in THING_PAIR.findall(text):
            n, w = nm.lower(), w.lower()
            if n in STOP or n in CALENDAR or w in STOP:
                continue
            pairs.append((w, n))
        universal = []
        if r["kind"] == "fact":
            m = UNIVERSAL_NOUN.search(r["body"])
            p = UNIVERSAL_PLACE.search(r["body"])
            if m and m.group(1).lower() not in STOP:
                universal.append({"id": r["id"], "noun": singular(m.group(1).lower()), "created": r["created"]})
            if p:
                universal.append({"id": r["id"], "place": (p.group(1) or p.group(2)).lower(), "created": r["created"]})
        return list(dict.fromkeys(pairs)), universal

    def index(self):
        """Things in memory: for every '<Name> <word>' ("Harlix crate", "Kosk locker"), which notes mention it; plus
        notes about a whole group ("all the cabinet codes", "the shed was cleared out"). Kept between calls and updated
        from the notes that changed (at 30,000 notes a rebuild took ~0.3 s, after every new note)."""
        if not self.watching:
            key = self.db.execute("SELECT MAX(id), COUNT(*) FROM items WHERE status IN ('active','faded')").fetchone()
            key = (key[0], key[1], self.db.execute("SELECT MAX(updated) FROM items").fetchone()[0])
            if getattr(self, "_index_key", None) == key:
                return self._index
            self._index_key = key
        with self._index_lock:
            if self.watching and self._index is not None:
                with self.lock:
                    dirty, self._watch["index"] = self._watch["index"], set()
                if len(dirty) <= 1000:       # (more, as after fading: a fresh build is quicker)
                    if dirty:
                        try:
                            self._index_update(dirty)
                        except BaseException:
                            self._index = None      # half updated: built afresh next time
                            raise
                    return self._index
            with self.lock:
                self._watch["index"] = set()
                rows = self.db.execute("SELECT id, kind, title, body, created FROM items WHERE status IN "
                                       "('active','faded') ORDER BY id").fetchall()
            things, by_id, unis = {}, {}, {}
            for r in rows:
                pairs, uni = self._index_entries(r)
                if pairs:
                    by_id[r["id"]] = pairs
                if uni:
                    unis[r["id"]] = uni
                for w, n in pairs:
                    things.setdefault(w, {}).setdefault(n, set()).add(r["id"])
            self._index_by_id, self._index_uni = by_id, unis
            self._index = {"things": things, "universal": [u for i in unis for u in unis[i]]}
            return self._index

    def _index_update(self, ids):
        """index() after the notes `ids` changed: their old entries out, their current ones in, in the order a full
        rebuild gives (by note id, then by place in the note). The published index is never changed in place (another
        thread may be reading it): the words touched get new entries."""
        by_id, unis = self._index_by_id, dict(self._index_uni)
        old_pairs = {}
        for i in ids:
            unis.pop(i, None)
            old_pairs[i] = by_id.pop(i, [])
        new_pairs = {}
        ids = sorted(ids)
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            for r in self.db.execute(f"SELECT id, kind, title, body, created, status FROM items WHERE id IN "
                                     f"({','.join('?' * len(part))})", part).fetchall():
                if r["status"] not in ("active", "faded"):
                    continue
                pairs, uni = self._index_entries(r)
                if pairs:
                    by_id[r["id"]] = new_pairs[r["id"]] = pairs
                if uni:
                    unis[r["id"]] = uni
        things = dict(self._index["things"])
        touched = {w for ps in (*old_pairs.values(), *new_pairs.values()) for w, _ in ps}
        for w in touched:
            inner = {n: set(s) for n, s in things.get(w, {}).items()}
            for i, ps in old_pairs.items():
                for w2, n in ps:
                    if w2 == w and n in inner:
                        inner[n].discard(i)
            for i, ps in new_pairs.items():
                for w2, n in ps:
                    if w2 == w:
                        inner.setdefault(n, set()).add(i)
            inner = {n: s for n, s in inner.items() if s}
            if not inner:
                things.pop(w, None)
                continue
            first = {n: min((i, by_id[i].index((w, n))) for i in s) for n, s in inner.items()}
            things[w] = dict(sorted(inner.items(), key=lambda x: first[x[0]]))
        self._index_uni = unis = dict(sorted(unis.items()))
        self._index = {"things": things, "universal": [u for i in unis for u in unis[i]]}

    def gather(self, query, limit=60):
        """For questions that need every match ("how many safes", "which crates are in the garage", "everything about
        the Quodor project"): all the things of that kind, each with its notes newest first. None if the question
        doesn't ask for that."""
        if not GATHER.search(query or ""):
            return None
        from .speakers import visible        # another person's personal notes are never gathered (#48)
        idx = self.index()
        if ABOUT.search(query):
            nms = names_in(self.fix_typos(query))
            if nms:
                pat = re.compile(r"\b(" + "|".join(map(re.escape, nms)) + r")\b", re.I)
                rows = [self._row(r) for r in self.db.execute(
                    "SELECT * FROM items WHERE status IN ('active','faded') ORDER BY created DESC")
                    if pat.search(fold(r["title"] + " " + r["body"])) and visible(json.loads(r["meta"] or "{}"))][:limit]
                return {"kind": "about", "name": " ".join(sorted(nms)), "items": rows} if rows else None
        for w in words(query):
            for cand in (w, singular(w)):
                group = idx["things"].get(cand)
                if group and len(group) >= 2:
                    ids = sorted({i for s in group.values() for i in s})
                    rows = {r["id"]: self._row(r) for r in self.db.execute(
                        f"SELECT * FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)
                        if visible(json.loads(r["meta"] or "{}"))}
                    out = {}
                    for nm, s in group.items():
                        notes = sorted((rows[i] for i in s if i in rows), key=lambda d: d["created"], reverse=True)
                        if notes:
                            out[nm] = notes
                    return {"kind": "list", "noun": cand, "things": out}
        return None

    def _series(self, res):
        """Readings of one series (meta "series", e.g. weigh-ins from the scale) are versions of one thing: the newest
        goes where the best of them was, older ones follow, marked with the newest id."""
        groups = {}
        for d in res:
            s = (d.get("meta") or {}).get("series")
            if s:
                groups.setdefault(s, []).append(d)
        if not groups:
            return res
        for g in groups.values():
            g.sort(key=lambda d: d["created"], reverse=True)
            for d in g[1:]:
                d["newer"] = g[0]["id"]
        out, placed = [], set()
        for d in res:
            s = (d.get("meta") or {}).get("series")
            if not s:
                out.append(d)
            elif s not in placed:
                placed.add(s)
                out += groups[s]
        return out

    def _apply_universal(self, query, res):
        """A note about a whole group applies to each member: "Changed all the cabinet codes to 2468" is the newest
        code of the Fiven cabinet; "The shed was cleared out" is the newest note about a box that was in the shed."""
        nms = names_in(query)
        if not nms or not res:
            return res
        idx = self.index()
        if not idx["universal"]:
            return res
        anchor = next(iter(nms))
        q_thing = thing_word(query, anchor)
        about = [d for d in res if anchor in words(d["title"] + " " + d["body"])]
        add = []
        for u in idx["universal"]:
            if u.get("noun") and q_thing and u["noun"] in (q_thing, singular(q_thing)) and about \
                    and u["created"] > max(d["created"] for d in about):
                add.append(u)
            elif u.get("place") and about:
                latest = max(about, key=lambda d: d["created"])
                if u["place"] in (latest["title"] + " " + latest["body"]).lower() and u["created"] > latest["created"]:
                    add.append(u)
        if not add:
            return res
        out = []
        for u in sorted(add, key=lambda x: x["created"], reverse=True)[:2]:
            d = self.get(u["id"])
            from .speakers import visible
            if d and d["status"] in ("active", "faded") and visible(d.get("meta")):
                d["universal"] = True
                d["score"] = res[0]["score"] if res else 1.0
                out.append(d)
        newest = max(out, key=lambda d: d["created"]) if out else None
        for d in res:
            if newest and d["id"] != newest["id"] and d["created"] < newest["created"] and d in about \
                    and not d.get("newer"):
                d["newer"] = newest["id"]
        ids = {d["id"] for d in out}
        return out + [d for d in res if d["id"] not in ids]

    def fix_typos(self, query):
        """Words in the question that appear nowhere in memory, replaced by the stored word one letter away (two for
        long words), if there is one. Capitals are kept so names stay names. 'Ranvn toolbox' -> 'Ranven toolbox'."""
        if not self.FIX_TYPOS or not query:
            return query
        if not self.watching:
            return self._fix_typos(query)
        # one question repairs its words four or five times (search, context, checks): the same answer until a note
        # changes, without reading the word list, the index and the search keys again
        key = (self._gen, self.FIX_SOUNDS, self.STRICT_THING)
        memo = self._fix_memo
        if memo is None or memo[0] != key:
            memo = self._fix_memo = (key, {})
        if query not in memo[1]:
            out = self._fix_typos(query)
            if self._gen != key[0] or len(memo[1]) > 500:
                return out
            memo[1][query] = out
        return memo[1][query]

    def _fix_typos(self, query):
        vocab = self.vocab()
        names = names_in(query)      # capitalized words that don't start a sentence

        def fix(m):
            w = m.group(0)
            lw = fold(w).lower()
            short = 3 if lw in names else 4         # names can be short ("Lsk" for "Losk")
            if lw in STOP or len(lw) < short or lw in vocab or not lw.isalpha() or lw in getattr(self, "_gone", ()):
                return w
            if lw not in names and any(v.startswith(lw[:4]) for v in vocab):
                return w        # an ordinary word with a known start ("changes", "changed"): search matches endings
            if self._is_key(lw):
                return w        # a note's search key ("bike" for the ebike note): found as it is, not a typo
            cap = 1 if len(lw) < 7 else 2

            pool = self._caps if lw in names and self._caps else vocab   # a name is repaired to a stored name

            def by_sound():
                keys = sound_key(lw)
                if not (self.FIX_SOUNDS and lw in names and keys):
                    return None
                cands = []
                for v, n in pool.items():
                    if abs(len(v) - len(lw)) > 3 or not v.isalpha():
                        continue
                    vk = sound_key(v)
                    dist = min((0 if x == y else edit_distance(x, y, 1)) for x in keys for y in vk) if vk else 9
                    if dist == 0 or (dist == 1 and min(len(keys[0]), len(vk[0])) >= 2):
                        cands.append((dist, edit_distance(lw, v, 9), -n, v))
                return min(cands)[3] if cands else None

            # a name that has been used with the thing the question names ("Harvk locker": Harvok has a locker,
            # Harsk only a key) wins a tie
            nxt = re.search(rf"\b{re.escape(w)}(?:['\u2019]s)?\s+([a-z]+)", query)
            owners = self.index()["things"].get(nxt.group(1), {}) if (nxt and lw in names) else {}

            def by_spelling():
                best = sorted(((edit_distance(lw, v, cap), v not in owners, -n, v) for v, n in pool.items()
                               if v[0] == lw[0] and abs(len(v) - len(lw)) <= cap))
                return best[0][3] if best and best[0][0] <= cap else None

            # spelling first (a dropped letter changes the sound), then sound ("Shawn" -> "Sean")
            v = by_spelling() or by_sound()
            if v and lw in names and nxt and owners and v not in owners and self.STRICT_THING:
                # the question says what the thing is ("Orvok fern") and the candidate has no such thing (Harvok
                # has a locker): an unknown name, not a typo
                return w
            if v:
                return v.capitalize() if w[0].isupper() else v
            return w
        return re.sub(r"[^\W\d_]+", fix, query)

    def _is_key(self, w):
        """Is a word one of some note's search keys (#71, #82)? Keys aren't in the word list typos are repaired to."""
        if not self.fts or not w.isascii():
            return False
        try:
            return self.db.execute("SELECT 1 FROM items_fts WHERE items_fts MATCH ? LIMIT 1",
                                   (f'keys : "{w}"',)).fetchone() is not None
        except sqlite3.OperationalError:
            return False

    def recall(self, query, shelf=None, kind=None, limit=5, include_old=False, touch=True, newest_first_ties=True,
               quiet=False):
        """Ranked items for a query. Old versions and archived items are left out unless include_old."""
        query = re.sub(r"(?<=\d)[,_](?=\d{3}\b)", "", self.fix_typos(query or ""))   # 4,417 -> 4417
        self._query = query
        ws = words(query)
        statuses = ("active", "faded", "superseded") if include_old else ("active", "faded")
        where = [f"i.status IN ({','.join('?' * len(statuses))})"]
        args = list(statuses)
        from .speakers import sql as _vis
        vs, va = _vis("i.")
        where.append(vs)            # personal notes only for their owner (#48)
        args += va
        if shelf:
            where.append("(i.shelf = ? OR i.shelf LIKE ?)")
            args += [self._shelf(shelf), self._shelf(shelf) + "/%"]
        if kind:
            where.append("i.kind = ?")
            args.append(kind)
        rows = []
        if ws and self.fts:
            q = " OR ".join(f'"{w}"*' for w in dict.fromkeys(ws))
            sql = (f"SELECT i.*, bm25(items_fts, {self.BM25}) AS rank FROM items_fts JOIN items i ON i.id = items_fts.rowid "
                   f"WHERE items_fts MATCH ? AND {' AND '.join(where)} ORDER BY rank LIMIT ?")
            try:
                rows = self.db.execute(sql, [q] + args + [max(limit * 6, 30)]).fetchall()
            except sqlite3.OperationalError:
                rows = []
            scored = [(-r["rank"], r) for r in rows]
        elif ws:
            cand = self.db.execute(f"SELECT i.* FROM items i WHERE {' AND '.join(where)}", args).fetchall()
            scored = []
            for r in cand:
                text = set(words(" ".join([r["title"], r["title"], r["body"], r["tags"], r["shelf"]])))
                s = sum(1 for w in ws if w in text)
                if s:
                    scored.append((float(s), r))
        else:
            cand = self.db.execute(f"SELECT i.* FROM items i WHERE {' AND '.join(where)} ORDER BY i.updated DESC LIMIT ?",
                                   args + [limit]).fetchall()
            scored = [(1.0, r) for r in cand]
        now = self.clock()
        idf, matched = self._term_stats(ws, [r for _, r in scored]) if ws else ({}, {})
        total = sum(idf.values()) or 1.0
        qnames = names_in(query) & set(idf)
        qmods = modified_things(query)
        out = []
        for base, r in scored:
            age_days = max(0.0, (now - r["created"]) / 86400.0)
            boost = 1 + 0.05 * math.log1p(r["use_count"]) + 0.05 * math.log1p(max(0.0, r["strength"] - 1))
            if self.PREFIX_ONLY_W != 1 and idf and not matched.get(r["id"]):
                boost *= self.PREFIX_ONLY_W      # #54: matched the question only by word starts ("called" in "Calla")
            if self.IMPORTANCE_W and '"importance": ' in (r["meta"] or ""):
                m_imp = re.search(r'"importance": (\d)', r["meta"])
                if m_imp:
                    boost *= 1 + self.IMPORTANCE_W * (int(m_imp.group(1)) - 1)      # #75
            if qmods and self.OTHER_THING_W != 1:
                # #11: "the garage freezer" asked; a note about "the chest freezer" is about another freezer
                nm = modified_things(r["title"] + " " + r["body"])
                for noun, mods in qmods.items():
                    if noun in nm and nm[noun] and not (mods & nm[noun]):
                        boost *= self.OTHER_THING_W
                        break
            boost *= 1 + 0.02 / (1 + age_days)          # a slight lean toward newer
            if r["status"] == "faded":
                boost *= 0.7
            if r["status"] == "superseded":
                boost *= 0.5
            if self.ROUTINE_W != 1 and r["source"] == "home" and '"routine": true' in (r["meta"] or ""):
                boost *= self.ROUTINE_W            # a house change that happens every day: below what matters (#40)
            if self.PROBATION_W != 1 and _on_probation(r, now, self.PROBATION_DAYS):
                boost *= self.PROBATION_W          # learned overnight, not confirmed yet: below what you said
            if qnames and self.NAME_BOOST != 1:
                # the question names something: notes that contain every name come first
                if qnames <= matched.get(r["id"], set()):
                    boost *= self.NAME_BOOST
            if idf and self.COVER_POW:
                # how much of the query's distinctive weight this item matches (rare words count for more)
                cover = sum(idf[w] for w in matched.get(r["id"], ())) / total
                boost *= max(cover, 0.05) ** self.COVER_POW
            out.append([base * boost, r["created"] if newest_first_ties else 0, r])
        out.sort(key=lambda x: (x[0], x[1]), reverse=True)
        older, self._disagree, self._guess, self._unconfirmed = {}, {}, set(), {}
        if self.CLUSTER and idf:
            out, older = self._newest_in_cluster(out, idf, matched, names_in(query))
        res = []
        for score, _, r in out[:limit]:
            d = self._row(r)
            d["score"] = round(score, 4)
            d.pop("rank", None)
            if r["id"] in older:
                d["newer"] = older[r["id"]]
            if INSTRUCTION.search(r["body"]):
                d["instruction"] = True
            if r["id"] in self._guess:
                d["guess"] = True
            if r["id"] in self._unconfirmed:
                d["unconfirmed"] = self._unconfirmed[r["id"]]
                d.pop("newer", None)
            if r["id"] in self._disagree:
                d["disagrees"] = self._disagree[r["id"]]
            res.append(d)
        res = self._apply_universal(query, res)
        res = self._series(res)
        if self.FOLLOW_NAMES and FOLLOW.search(query) and res:
            seen = names_in(query) | set(ws)
            found = []
            for d in res[:1]:        # only the best answer to the first part leads on
                for nm in names_in(d["title"] + " " + d["body"]) | {w.lower() for w in re.findall(r"\b[A-Z][a-z]+", d["body"])[:1]}:
                    if nm not in seen and nm not in found and nm not in STOP:
                        found.append(nm)
            if found:
                rest = " ".join(w for w in ws if w not in ("whoever", "runs", "run", "person", "one"))
                self._inner = True          # the hop's own search doesn't follow links (#12)
                try:
                    more = self.recall(" ".join(n.capitalize() for n in found[:2]) + " " + rest, shelf=shelf,
                                       kind=kind, limit=3, include_old=include_old, touch=False)
                finally:
                    self._inner = False
                hop = [dict(d, hop=True) for d in more[:2] if d["id"] != res[0]["id"]]
                hop_ids = {d["id"] for d in hop}
                # the first answer, then what its name leads to, then the rest
                res = ([res[0]] + hop + [d for d in res[1:] if d["id"] not in hop_ids])[:limit]
        if touch and res:
            with self.lock:
                # (the status only where it changes: a plain use count is not a change to the notes, see WATCHED)
                self.db.executemany("UPDATE items SET use_count=use_count+1, last_used=? WHERE id=?",
                                    [(now, d["id"]) for d in res])
                self.db.executemany("UPDATE items SET status='active' WHERE id=? AND status='faded'",
                                    [(d["id"],) for d in res])
                self.db.commit()
        if self.LINKS and res and len(res) < limit + 3 and not getattr(self, "_inner", False) and \
                not (self.FOLLOW_NAMES and FOLLOW.search(query)):
            # (a "whoever runs it" question already has its own hop to the person; links would push it down)
            res = self._follow_links(query, res, limit, include_old)
        from .speakers import visible as _visible
        res = [d for d in res if _visible(d.get("meta"))]      # and whatever was reached through links or hops
        if shelf or kind:
            # notes reached through links or a group note keep to the shelf and kind asked for, like the search
            sh = self._shelf(shelf) if shelf else None
            res = [d for d in res if (not kind or d.get("kind") == kind) and
                   (not sh or d.get("shelf") == sh or (d.get("shelf") or "").startswith(sh + "/"))]
        if not quiet:          # the nightly checks search a lot; they don't fill the activity log
            self.log("recall", None, query=query, shelf=shelf, kind=kind, ids=[d["id"] for d in res])
        return res

    # recall settings (class-level so tests can compare with and without)
    PREFIX_ONLY_W = 0.25 # a note sharing no whole word with the question, only word starts (#54)
    IMPORTANCE_W = 0.03  # per importance step above 1: a slight lean toward what matters (#75)
    OTHER_THING_W = 0.5  # a note about the same kind of thing with a different modifier (#11)
    KEYS = True          # extra search keys from the built-in word list when filing (#71)
    ROUTINE_W = 0.7      # routine house changes (a light going off at its usual time) rank this much lower
    PROBATION_W = 0.85   # facts learned overnight rank this much lower until confirmed (or PROBATION_DAYS pass)
    PROBATION_DAYS = 7
    STRUCTURE = True     # read recurrence, chores, places, fields, aliases and "since when" from notes as they are filed
    BM25 = "4.0, 1.0, 2.0, 1.5, 0.6"  # weights for title, body, tags, shelf, keys (other words: count less)
    LINK_PRONOUNS = True # a note that says "its"/"it" and names nothing is linked to the note just before it
    LINK_SECONDS = 7200  # ...if that note is at most this old
    FIX_SOUNDS = True    # ...or to a stored word that sounds the same
    FOLLOW_NAMES = True  # "whoever runs X": the name found in the first result is searched too
    FIX_TYPOS = True     # a question word found nowhere is matched to a stored word one or two letters away
    NAME_BOOST = 1.5     # how much a note containing every name in the question is lifted
    COVER_POW = 0.0      # weight by distinctive-word coverage (tested: no gain over BM25, so off)
    CLUSTER = True       # newest first among results that match the query on the same distinctive words
    STRICT_THING = True  # repair a name only to one that has the thing the question names
    CLUSTER_KINDS = ("fact",)  # only facts are replaced by newer ones; notes and ideas add up
    CLUSTER_OVERLAP = 0.6      # share of matched words two facts must have in common to be versions of one thing
    RARE_SHARE = 0.1     # a word is distinctive if it is in at most this share of items (or at most 3)
    COMMON_SHARE = 0.25  # words in more than this share of items are left out of the cluster key

    def _term_stats(self, ws, rows):
        """For each query word: how rare it is (IDF over all items) and which candidate items contain it."""
        n = max(1, self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0])
        ids = {r["id"] for r in rows}
        idf, matched, dfs = {}, {}, {}
        for w in dict.fromkeys(ws):
            if self.fts:
                try:
                    # whole words only: a prefix ("log" in "Logor") must not look like a rare match.
                    # Count all notes with the word, but fetch ids only among the candidates (fast at 50,000 notes).
                    df_all = self.db.execute("SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH ?",
                                             (f'"{w}"',)).fetchone()[0]
                    hit = {x[0] for x in self.db.execute(
                        f"SELECT rowid FROM items_fts WHERE items_fts MATCH ? AND rowid IN ({','.join('?' * len(ids))})",
                        [f'"{w}"'] + list(ids))} if ids and df_all else set()
                except sqlite3.OperationalError:
                    continue
            else:
                hit = {r["id"] for r in rows if w in words(" ".join([r["title"], r["body"], r["tags"], r["shelf"]]))}
            df = df_all if self.fts else len(hit)
            if not df:
                continue
            dfs[w] = df
            idf[w] = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i in hit & ids:
                matched.setdefault(i, set()).add(w)
        self._rare = {w for w, df in dfs.items() if df <= max(3, self.RARE_SHARE * n)}
        self._common = {w for w, df in dfs.items() if df > self.COMMON_SHARE * n}
        return idf, matched

    def _newest_in_cluster(self, out, idf, matched, names=()):
        """Facts that are versions of the same thing should come newest first. The thing is pinned by the query's most
        distinctive word that any result contains (usually a name): facts containing it whose matched words overlap
        the best such fact's by at least CLUSTER_OVERLAP are taken as versions of it. The newest takes the best place;
        older ones follow, marked with the newer id. Other results keep their order. Nothing is merged or hidden."""
        present = [w for w in idf if any(w in matched.get(r[2]["id"], ()) for r in out)]
        # a name in the question (a capitalized word that doesn't start it) is what the question is about; a rare
        # word that is part of a value ("laundry room") is not. Names first, then the rarest word.
        named = [w for w in present if w in names]
        if names and not named:
            return out, {}      # the question names something no result mentions: nothing here is a version of it
        anchor = max(named or present, key=lambda w: idf[w], default=None)
        if anchor is None:
            return out, {}
        group, ref, ref_speaker, ref_thing, ref_hedged = [], None, None, None, False
        q_thing = thing_word(getattr(self, "_query", ""), anchor)
        for row in out:
            r = row[2]
            m = matched.get(r["id"], set()) - self._common
            if r["kind"] not in self.CLUSTER_KINDS or anchor not in m:
                continue
            thing = thing_word(r["title"] + " " + r["body"], anchor)
            if q_thing and thing and thing != q_thing:
                continue            # the same name on a different thing (the Ranven toolbox, not the locker)
            if INSTRUCTION.search(r["body"]):
                continue            # an instruction hidden in a note is never a version of a fact
            if HEDGE.search(r["body"]) or '"hedge": true' in (r["meta"] or ""):   # #21: or tagged by the model
                # a guess never replaces, and is never replaced by, what was stated: it stands aside, marked
                self._guess.add(r["id"])
                continue
            if ref is None:
                ref, ref_speaker, ref_thing = m, speaker(r["title"] + " " + r["body"]) or _said_by(r), thing
                group.append(row)
                continue
            if (thing and ref_thing and thing == ref_thing) or \
                    len(m & ref) / max(len(m), len(ref)) >= self.CLUSTER_OVERLAP:
                sp = speaker(r["title"] + " " + r["body"]) or _said_by(r)
                if sp and ref_speaker and sp != ref_speaker:
                    # what two different people said: both stand, neither replaces the other
                    self._disagree[r["id"]] = group[0][2]["id"]
                    self._disagree.setdefault(group[0][2]["id"], r["id"])
                    continue
                group.append(row)
        if len(group) < 2:
            return out, {}
        best = group[0][0]
        ordered = sorted(group, key=lambda x: (_eff(x[2]), x[2]["id"]), reverse=True)   # when it became true; then filed
        now = self.clock()
        stated = [x for x in ordered if not _on_probation(x[2], now, self.PROBATION_DAYS)]
        if stated and stated[0] is not ordered[0] and self.PROBATION_W != 1:
            # newer versions learned overnight and not confirmed don't replace what you said: they stand right after
            # it, marked unconfirmed (the model is told; the morning summary asks you to check them)
            lead = stated[0]
            newer_unconf = [x for x in ordered if (_eff(x[2]), x[2]["id"]) > (_eff(lead[2]), lead[2]["id"])]
            for x in newer_unconf:
                self._unconfirmed[x[2]["id"]] = lead[2]["id"]
            ordered = [lead] + newer_unconf + [x for x in ordered if x is not lead and x not in newer_unconf]
        older = {row[2]["id"]: ordered[0][2]["id"] for row in ordered[1:]
                 if row[2]["id"] not in self._unconfirmed}
        for k, row in enumerate(ordered):
            row[0] = best - 1e-6 * k
        ids = {id(x) for x in group}
        rest = [x for x in out if id(x) not in ids]
        first = out.index(group[0])
        res = [x for x in out[:first] if id(x) not in ids] + ordered + [x for x in rest if out.index(x) > first]
        return res, older

    def browse(self, shelf=None, limit=50):
        """The shelf tree (with counts) under a shelf, and the items kept directly on it."""
        from .speakers import sql as _vis
        vs, va = _vis()
        base = self._shelf(shelf) if shelf else ""
        rows = self.db.execute(f"SELECT shelf, COUNT(*) n FROM items WHERE status IN ('active','faded') AND {vs} "
                               "GROUP BY shelf", va).fetchall()
        tree = {}
        for r in rows:
            s = r["shelf"]
            if base and not (s == base or s.startswith(base + "/")):
                continue
            rest = s[len(base):].lstrip("/") if base else s
            child = rest.split("/")[0] if rest else ""
            if child:
                tree[child] = tree.get(child, 0) + r["n"]
        items = []
        if base:
            items = [self._row(r) for r in self.db.execute(
                f"SELECT * FROM items WHERE shelf=? AND status IN ('active','faded') AND {vs} ORDER BY updated DESC "
                "LIMIT ?", (base, *va, limit)).fetchall()]
        self.log("browse", None, shelf=base)
        return {"shelf": base or "/", "shelves": [{"name": k, "path": (base + "/" + k).strip("/"), "items": v}
                                                    for k, v in sorted(tree.items())],
                "items": [{"id": i["id"], "kind": i["kind"], "title": i["title"], "tags": i["tags"]} for i in items]}

    def tasks(self, project=None, status="open", due_before=None, limit=100):
        """Tasks are items of kind 'task' with meta {due, priority, project, done}. Sorted by due, then priority."""
        from .speakers import sql as _vis
        vs, va = _vis()
        rows = [self._row(r) for r in self.db.execute(
            f"SELECT * FROM items WHERE kind='task' AND status='active' AND {vs}", va).fetchall()]
        out = []
        for t in rows:
            m = t["meta"]
            done = bool(m.get("done"))
            if status == "open" and done or status == "done" and not done:
                continue
            if project and (m.get("project") or "").lower() != project.lower():
                continue
            if due_before is not None and (m.get("due") is None or m["due"] >= due_before):
                continue
            out.append(t)
        out.sort(key=lambda t: (t["meta"].get("due") if t["meta"].get("due") is not None else 1e18,
                                -int(t["meta"].get("priority") or 0), t["id"]))
        return out[:limit]

    # ---- upkeep ---------------------------------------------------------------------------------------------------
    def fade(self, days, kinds=("episode", "note")):
        """Items not used for `days` move to the recycle bin ('faded'): left out of nothing, just ranked lower, and
        brought back the moment they are found again. Nothing is deleted."""
        if not days:
            return 0
        from .categories import FADE_X
        now, total = self.clock(), 0
        with self.lock:
            # each category at its own pace (#38); safety, identity and corrections never, importance 3 never (#75)
            for cat, x in FADE_X.items():
                if x is None:
                    continue
                cut = now - days * x * 86400
                cond = "json_extract(meta, '$.category') = ?" if cat != "note" else \
                    "(json_extract(meta, '$.category') IS NULL OR json_extract(meta, '$.category') = ?)"
                cur = self.db.execute(
                    f"UPDATE items SET status='faded' WHERE status='active' AND kind IN ({','.join('?' * len(kinds))}) "
                    f"AND COALESCE(last_used, created) < ? AND strength < 3 AND {cond} AND "
                    "COALESCE(json_extract(meta, '$.importance'), 1) < 3", list(kinds) + [cut, cat])
                total += cur.rowcount
            self.db.commit()
        if total:
            self.log("fade", None, n=total, days=days)
        return total

    def prune_events(self, keep_days):
        """The event log keeps every search, answer and change. Past `keep_days` the rows are rolled up into daily
        counts per type (event_days) and removed; notes themselves are never touched. 0 = keep everything. SQLite reuses
        the freed space, so the file stops growing rather than shrinking."""
        if not keep_days:
            return 0
        cut = self.clock() - keep_days * 86400
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS event_days (day TEXT, type TEXT, n INTEGER, "
                            "PRIMARY KEY (day, type))")
            self.db.execute("INSERT INTO event_days(day, type, n) SELECT date(ts, 'unixepoch', 'localtime'), type, COUNT(*) "
                            "FROM events WHERE ts < ? GROUP BY 1, 2 "
                            "ON CONFLICT(day, type) DO UPDATE SET n = n + excluded.n", (cut,))
            cur = self.db.execute("DELETE FROM events WHERE ts < ?", (cut,))
            self.db.commit()
        if cur.rowcount:
            self.log("prune_events", None, n=cur.rowcount, days=keep_days)
        return cur.rowcount

    def roll_up_house_log(self, keep_days):
        """The house log (home/readings: a note per sensor every half hour it moves; home/devices: a note per switch)
        is the bulk of what the database gains each day, kept forever. Past `keep_days` each thing's notes for a day
        become one daily note on home/daily (a reading: average, low, high, as the history backfill files them; a
        device: its changes with their times) and the raw notes are deleted. Unusual ones and notes someone marked
        important stay. 0 = keep everything. Returns how many raw notes were removed."""
        from datetime import datetime
        if not keep_days:
            return 0
        keep_days = max(int(keep_days), 30)        # the house learner (habits, baselines) reads the last 28 days
        cut = datetime.fromtimestamp(self.clock() - keep_days * 86400).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()          # whole days only
        days, ids, most = {}, [], 50000
        rows = self.db.execute("SELECT id, created, title, meta, shelf FROM items WHERE source='home' AND "
                               "shelf IN ('home/readings','home/devices') AND created < ? ORDER BY created LIMIT ?", (cut, most)).fetchall()
        if len(rows) == most:       # a long backlog: whole days now, the rest on the next run
            last = datetime.fromtimestamp(rows[-1]["created"]).date()
            rows = [r for r in rows if datetime.fromtimestamp(r["created"]).date() < last]
        for r in rows:
            try:
                m = json.loads(r["meta"] or "{}")
            except ValueError:
                continue
            eid = m.get("entity_id")
            if not eid or m.get("daily") or m.get("unusual") or (m.get("importance") or 0) >= 3:
                continue
            ids.append(r["id"])
            day = datetime.fromtimestamp(r["created"]).date()
            name, _, rest = (r["title"] or "").partition(": ")
            d = days.setdefault((eid, day), {"name": name or eid, "vals": [], "states": [], "unit": ""})
            if r["shelf"] == "home/devices":
                d["states"].append(f"{m.get('state')} at {datetime.fromtimestamp(r['created']):%I:%M %p}".replace(
                    " 0", " "))
                continue
            try:
                v = float(m.get("state"))
                if math.isfinite(v):
                    d["vals"].append(v)
                    d["unit"] = re.sub(r"^-?[\d.]+\s*", "", rest.split(" (")[0]).strip()
                    continue
            except (TypeError, ValueError):
                pass
            if str(m.get("state")) not in d["states"]:
                d["states"].append(str(m.get("state")))
        if not ids:
            return 0
        fmt = lambda x: f"{x:.1f}".rstrip("0").rstrip(".")
        for (eid, day), d in days.items():
            start = datetime(day.year, day.month, day.day).timestamp()
            if self.db.execute("SELECT 1 FROM items WHERE shelf='home/daily' AND created >= ? AND created < ? AND "
                               "json_extract(meta, '$.entity_id') = ?", (start, start + 86400, eid)).fetchone():
                continue        # the backfill already filed this day
            u = f" {d['unit']}" if d["unit"] else ""
            head = f"{day:%A %b %d %Y}".replace(" 0", " ") + f": {d['name']}"
            if d["vals"]:
                vs = d["vals"]
                avg = sum(vs) / len(vs)
                text = f"{head} averaged {fmt(avg)}{u} (low {fmt(min(vs))}, high {fmt(max(vs))})."
                title = f"{d['name']} on {day:%a %b %d}: avg {fmt(avg)}{u}"
            elif any(" at " in x for x in d["states"]):
                text = f"{head} changed {len(d['states'])} times: {', '.join(d['states'][:48])}."
                title = f"{d['name']} on {day:%a %b %d}: {len(d['states'])} changes"
            else:
                text = f"{head} was {', '.join(d['states'][:10])}."
                title = f"{d['name']} on {day:%a %b %d}: {', '.join(d['states'][:3])}"
            self.remember(text=text, title=title, kind="fact", shelf="home/daily", tags=["home", "daily"],
                          source="home", at=start + 86399,
                          meta={"series": "home:" + eid, "entity_id": eid, "daily": True,
                                "rolled_up": len(d["vals"]) or len(d["states"])})
        with self.lock:
            for k in range(0, len(ids), 500):
                part = ids[k:k + 500]
                q = ",".join("?" * len(part))
                self.db.execute(f"DELETE FROM items WHERE id IN ({q})", part)
                try:        # a note's embedding goes with it
                    self.db.execute(f"DELETE FROM vectors WHERE item_id IN ({q})", part)
                except sqlite3.OperationalError:
                    pass
            self.db.commit()
            # each deleted note leaves a delete marker in the search index until its segments are merged: without
            # this the index kept growing (~60 KiB a day) though the notes were gone
            if self.fts:
                self.db.execute("INSERT INTO items_fts(items_fts) VALUES('optimize')")
                self.db.commit()
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
        self._vocab_n = None
        self.log("roll_up_house_log", None, n=len(ids), days=keep_days)
        return len(ids)

    def stats(self):
        q = lambda s, a=(): self.db.execute(s, a).fetchone()[0]
        kinds = {r[0]: r[1] for r in self.db.execute(
            "SELECT kind, COUNT(*) FROM items WHERE status IN ('active','faded') GROUP BY kind")}
        return {"items": q("SELECT COUNT(*) FROM items WHERE status IN ('active','faded')"),
                "versions": q("SELECT COUNT(*) FROM items"),
                "faded": q("SELECT COUNT(*) FROM items WHERE status='faded'"),
                "shelves": q("SELECT COUNT(DISTINCT shelf) FROM items WHERE status IN ('active','faded')"),
                "events": q("SELECT COUNT(*) FROM events"), "runs": q("SELECT COUNT(*) FROM runs"),
                "by_kind": kinds, "fts": self.fts}

    # ---- test runs ------------------------------------------------------------------------------------------------
    def save_run(self, run):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO runs(id,ts,suite,arm,model,seed,status,config,summary,details) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (run["id"], run["ts"], run["suite"], run["arm"], run.get("model"), run.get("seed"),
                             run["status"], json.dumps(run.get("config", {})), json.dumps(run.get("summary", {})),
                             json.dumps(run.get("details", []))[:2_000_000]))
            if run["status"] == "done":
                self.db.execute("DELETE FROM metrics WHERE run_id=?", (run["id"],))
                self.db.executemany(
                    "INSERT INTO metrics(run_id,ts,suite,arm,model,name,value) VALUES(?,?,?,?,?,?,?)",
                    [(run["id"], run["ts"], run["suite"], run["arm"], run.get("model"), k, float(v))
                     for k, v in run.get("summary", {}).items() if isinstance(v, (int, float))])
            self.db.commit()

    def runs(self, limit=50, suite=None):
        sql, a = "SELECT id,ts,suite,arm,model,seed,status,config,summary FROM runs", []
        if suite:
            sql += " WHERE suite=?"
            a.append(suite)
        out = []
        for r in self.db.execute(sql + " ORDER BY ts DESC LIMIT ?", a + [limit]):
            d = dict(r)
            d["config"], d["summary"] = json.loads(d["config"] or "{}"), json.loads(d["summary"] or "{}")
            out.append(d)
        return out

    def run(self, run_id):
        r = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("config", "summary", "details"):
            d[k] = json.loads(d[k] or ("[]" if k == "details" else "{}"))
        return d

    def trend(self, suite, name, arm=None, limit=200):
        sql, a = "SELECT ts, arm, model, value, run_id FROM metrics WHERE suite=? AND name=?", [suite, name]
        if arm:
            sql += " AND arm=?"
            a.append(arm)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY ts DESC LIMIT ?", a + [limit])][::-1]
