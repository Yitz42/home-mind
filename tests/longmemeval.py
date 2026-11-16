"""Run Home Mind on LongMemEval (Wu et al., ICLR 2025): 500 questions, each over its own chat history of ~40 sessions
(LongMemEval_S, ~115k tokens) or ~500 (LongMemEval_M). It tests information extraction, multi-session reasoning,
temporal reasoning, knowledge updates and abstention.

For each question, a fresh memory is filled with that question's history (each user turn and the reply after it is
one note, dated with its session's date), the clock is set to the question's date, and then:

  retrieve   the brain's own search (no model, free): is an evidence session among the top k? recall_any@k and
             recall_all@k by session, the measures the paper reports for its retrievers. Abstention questions
             (ids ending _abs) have no evidence and are left out, as in the paper.
  answer     Home Mind answers with one of your models (the same path as a real answer: search, page, model), and
             writes a hypothesis file in the format the official judge reads.

Get the data (about 264 MB for S):
  mkdir -p data && cd data
  wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
  wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json

Run (from the repo root; answer needs aiohttp, as the add-on does):
  python tests/longmemeval.py retrieve data/longmemeval_s_cleaned.json --limit 50       # a quick look
  python tests/longmemeval.py retrieve data/longmemeval_s_cleaned.json                  # all 500
  python tests/longmemeval.py answer data/longmemeval_s_cleaned.json --models models.json --model qwen \\
      --out hyp_qwen.jsonl --limit 50
  python tests/longmemeval.py selftest                                                  # no download needed

models.json is a list in the add-on's "models" format, e.g.
  [{"name": "qwen", "type": "ollama", "url": "http://192.168.1.20:11434", "model": "qwen2.5:14b"}]

Score answers with the official judge (GPT-4o, as every published number uses), from a clone of
github.com/xiaowu0162/LongMemEval:
  cd src/evaluation && python3 evaluate_qa.py gpt-4o /path/to/hyp_qwen.jsonl ../../data/longmemeval_oracle.json
A judge other than GPT-4o gives numbers that aren't comparable with published ones.
"""
import argparse
import asyncio
import collections
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))


def parse_date(s):
    """LongMemEval dates look like '2023/05/20 (Sat) 02:21'."""
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})(?: \(\w+\))?(?: (\d{1,2}):(\d{2}))?", s or "")
    if not m:
        return None
    y, mo, d, h, mi = (int(x) if x else 0 for x in m.groups())
    return datetime(y, mo, d, h, mi).timestamp()


def fill(store, inst, user_only=False):
    """One note per round (a user turn and the reply after it), dated with its session; meta names the session."""
    n = 0
    for sid, date, turns in zip(inst["haystack_session_ids"], inst["haystack_dates"], inst["haystack_sessions"]):
        t0 = parse_date(date) or time.time()
        i = 0
        while i < len(turns):
            t = turns[i]
            if t.get("role") != "user":
                i += 1
                continue
            text = (t.get("content") or "").strip()
            reply = ""
            if i + 1 < len(turns) and turns[i + 1].get("role") == "assistant":
                reply = (turns[i + 1].get("content") or "").strip()
                i += 1
            i += 1
            if not text:
                continue
            body = text if user_only or not reply else f"{text}\n\n(Assistant replied: {reply[:1500]})"
            store.remember(text=body, title=text.split("\n")[0][:90], kind="note", source="lme", at=t0 + n,
                           meta={"session": sid})
            n += 1
    return n


def sessions_ranked(hits):
    out = []
    for h in hits:
        s = (h.get("meta") or {}).get("session")
        if s and s not in out:
            out.append(s)
    return out


def load(path, limit, types):
    data = json.load(open(path))
    if types:
        data = [x for x in data if x["question_type"] in types]
    return data[:limit] if limit else data


def retrieve(args):
    from homemind.store import Store
    from homemind.mind import find
    ks = [int(k) for k in args.k.split(",")]
    by = collections.defaultdict(lambda: collections.defaultdict(list))
    t0 = time.time()
    data = load(args.data, args.limit, args.type)
    for n, inst in enumerate(data, 1):
        if inst["question_id"].endswith("_abs"):
            continue
        with tempfile.TemporaryDirectory() as d:
            qt = parse_date(inst.get("question_date")) or time.time()
            st = Store(os.path.join(d, "m.db"), clock=lambda: qt)
            fill(st, inst, args.user_only)
            hits = find(st, inst["question"], max(ks) * 4, touch=False)
            ranked = sessions_ranked(hits)
            gold = set(inst["answer_session_ids"])
            for k in ks:
                top = set(ranked[:k])
                by[inst["question_type"]][f"any@{k}"].append(bool(top & gold))
                by[inst["question_type"]][f"all@{k}"].append(gold <= top)
            st.close()
        if n % 25 == 0:
            print(f"  {n}/{len(data)} ({time.time() - t0:.0f}s)", file=sys.stderr)
    cols = [f"{m}@{k}" for k in ks for m in ("any", "all")]
    print(f"\nRetrieval by session, Home Mind's own search ({len(data)} questions, {time.time() - t0:.0f}s)")
    print(f"{'type':28s}" + "".join(f"{c:>9s}" for c in cols) + "      n")
    tot = collections.defaultdict(list)
    for t in sorted(by):
        for c in cols:
            tot[c] += by[t][c]
        print(f"{t:28s}" + "".join(f"{sum(by[t][c]) / len(by[t][c]):9.3f}" for c in cols) + f"{len(by[t][cols[0]]):7d}")
    if tot:
        print(f"{'all':28s}" + "".join(f"{sum(tot[c]) / len(tot[c]):9.3f}" for c in cols) + f"{len(tot[cols[0]]):7d}")
    return tot


async def answer(args):
    from homemind.store import Store
    from homemind.mind import Mind
    from homemind.models import Models
    models = Models(json.load(open(args.models)) if args.models else [])
    await models.start()
    done = set()
    if os.path.exists(args.out):
        done = {json.loads(x)["question_id"] for x in open(args.out) if x.strip()}
    out = open(args.out, "a")
    data = load(args.data, args.limit, args.type)
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory() as root:
            mind = Mind(root, models)
            mind.FALLBACK = []
            for n, inst in enumerate(data, 1):
                if inst["question_id"] in done:
                    continue
                with tempfile.TemporaryDirectory() as d:
                    qt = parse_date(inst.get("question_date")) or time.time()
                    st = Store(os.path.join(d, "m.db"), clock=lambda: qt)
                    fill(st, inst, args.user_only)
                    q = inst["question"]
                    if inst.get("question_date"):
                        q = f"{q} (Today is {inst['question_date']}.)"
                    try:
                        r = await mind.ask(q, args.model, store=st, fallback=False, k=args.notes)
                        hyp = r.get("answer") or "I don't know."
                    except Exception as e:  # noqa: BLE001 - recorded, the run goes on
                        hyp = "I don't know."
                        print(f"  {inst['question_id']}: {e}", file=sys.stderr)
                    st.close()
                out.write(json.dumps({"question_id": inst["question_id"], "hypothesis": hyp}) + "\n")
                out.flush()
                if n % 10 == 0:
                    print(f"  {n}/{len(data)} ({time.time() - t0:.0f}s)", file=sys.stderr)
    finally:
        out.close()
        await models.stop()
    print(f"wrote {args.out}; score it with the official evaluate_qa.py (see the top of this file)")


def sample():
    """Three questions in LongMemEval's format, for checking the harness without the download."""
    sess = lambda *pairs: [t for u, a in pairs for t in ({"role": "user", "content": u}, {"role": "assistant", "content": a})]
    filler = [sess(("Can you suggest a pasta recipe?", "Try cacio e pepe."), ("What wine goes with it?", "A crisp white.")),
              sess(("How do I fix a squeaky door?", "Oil the hinges."), ("Any brand?", "WD-40 works.")),
              sess(("Tips for running in the cold?", "Layer up."), ("What about gloves?", "Thin wool ones."))]
    base = {"haystack_session_ids": ["s1", "s2", "s3", "e1"],
            "haystack_dates": ["2023/05/01 (Mon) 10:00", "2023/05/03 (Wed) 11:00", "2023/05/07 (Sun) 09:30",
                               "2023/05/10 (Wed) 18:00"]}
    return [
        dict(base, question_id="q1", question_type="single-session-user", question="What breed is my new dog?",
             answer="a beagle", question_date="2023/05/20 (Sat) 12:00", answer_session_ids=["e1"],
             haystack_sessions=filler + [sess(("I just adopted a beagle named Rufus!", "Congratulations!"))]),
        dict(base, question_id="q2", question_type="knowledge-update", question="Where do I park my bike now?",
             answer="in the garage", question_date="2023/05/20 (Sat) 12:00", answer_session_ids=["s2", "e1"],
             haystack_sessions=[filler[0], sess(("I keep my bike in the hallway.", "Handy.")), filler[2],
                                sess(("I moved my bike to the garage, the hallway was too cramped.", "Good call."))]),
        dict(base, question_id="q4", question_type="single-session-user",
             question="What's the name of the beagle I adopted?", answer="Rufus",
             question_date="2023/05/20 (Sat) 12:00", answer_session_ids=["e1"],
             haystack_sessions=filler + [sess(("I just adopted a beagle named Rufus!", "Congratulations!"))]),
        dict(base, question_id="q3_abs", question_type="single-session-user", question="What is my cat's name?",
             answer="You did not mention a cat.", question_date="2023/05/20 (Sat) 12:00", answer_session_ids=[],
             haystack_sessions=filler + [sess(("I just adopted a beagle named Rufus!", "Congratulations!"))]),
    ]


def selftest():
    path = os.path.join(tempfile.mkdtemp(), "lme_sample.json")
    json.dump(sample(), open(path, "w"))
    a = argparse.Namespace(data=path, limit=0, type=None, k="1,5", user_only=False)
    tot = retrieve(a)
    # q1 ("what breed is my new dog?" over "I just adopted a beagle") shares no word with its evidence: it is found
    # through the extra search keys (#71: beagle files under dog, breed). All three come first.
    ok = len(tot["any@1"]) == 3 and sum(tot["any@1"]) == 3 and sum(tot["all@5"]) == 3
    print("\nselftest", "passed" if ok else "FAILED")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("retrieve", "answer"):
        s = sub.add_parser(name)
        s.add_argument("data", help="longmemeval_s_cleaned.json (or _m / _oracle)")
        s.add_argument("--limit", type=int, default=0, help="only the first N questions")
        s.add_argument("--type", action="append", help="only this question_type (repeatable)")
        s.add_argument("--user-only", action="store_true", help="file only what the user said, not the replies")
        if name == "retrieve":
            s.add_argument("--k", default="5,10")
        else:
            s.add_argument("--models", help="models.json, a list in the add-on's models format")
            s.add_argument("--model", required=True)
            s.add_argument("--out", required=True, help="hypothesis file (jsonl); an existing one is resumed")
            s.add_argument("--notes", type=int, default=8, help="notes on the page the model reads")
    sub.add_parser("selftest")
    a = p.parse_args()
    if a.cmd == "selftest":
        sys.exit(selftest())
    if a.cmd == "retrieve":
        retrieve(a)
    else:
        asyncio.run(answer(a))


if __name__ == "__main__":
    main()
