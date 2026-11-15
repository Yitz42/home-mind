"""Corrections become test cases (#17). When you say "that's wrong" and then give the right value, the question, the
wrong answer and the right value are kept. The `corrections` suite asks every kept question again of your real memory
(read-only) and checks the right value now comes first and the wrong one doesn't, so a later change to the brain that
brings an old mistake back shows up.
"""
import re
import time

from .store import words


class Cases:
    def __init__(self, store):
        self.store = store
        store.db.execute("CREATE TABLE IF NOT EXISTS cases (id INTEGER PRIMARY KEY, ts REAL, question TEXT, wrong TEXT, "
                         "right TEXT, item_id INTEGER, fixed_id INTEGER)")
        store.db.commit()

    def add(self, question, right, wrong="", item_id=None, fixed_id=None):
        if not question or not right:
            raise ValueError("a case needs the question and the right value")
        with self.store.lock:
            cur = self.store.db.execute("INSERT INTO cases(ts, question, wrong, right, item_id, fixed_id) "
                                        "VALUES(?,?,?,?,?,?)", (time.time(), question, wrong or "", right, item_id, fixed_id))
            self.store.db.commit()
        return cur.lastrowid

    def all(self):
        return [dict(r) for r in self.store.db.execute("SELECT * FROM cases ORDER BY id")]

    def remove(self, case_id):
        with self.store.lock:
            self.store.db.execute("DELETE FROM cases WHERE id=?", (case_id,))
            self.store.db.commit()


def values(text, question):
    """What a value is made of: its numbers, else its words that the question doesn't already have."""
    nums = re.findall(r"\d+", text or "")
    if nums:
        return nums
    qw = set(words(question))
    return [w for w in words(text) if w not in qw][:4]


def check(case, top_text):
    """Right: every part of the right value is in the top note, and no part of the wrong one that isn't also right."""
    have = set(words(top_text)) | set(re.findall(r"\d+", top_text or ""))
    right = values(case["right"], case["question"])
    wrong = [w for w in values(case["wrong"], case["question"]) if w not in right]
    return bool(right) and all(v in have for v in right) and not any(w in have for w in wrong)
