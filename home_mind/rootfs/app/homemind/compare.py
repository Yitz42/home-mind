"""Model comparison on a schedule (#34): every few days, the connected models answer the same questions from the same
memory (same suites, same seed), so you can see which is right most often, how fast, and whether a model got worse.

Options: `compare_models` (names; empty = every connected model except mock), `compare_days` (0 = off),
`compare_suites` (default recall, tricky, hard). Results: /api/compare, a sensor per model, and a note in the log
when a model drops by 5 points or more since the last comparison.
"""
import logging
import random
import time

log = logging.getLogger("homemind.compare")
KV = "model_compare"
KEEP = 26
DEFAULT_SUITES = ("recall", "tricky", "hard")
DROP = 0.05


class Compare:
    def __init__(self, mind, models=(), days=0, suites=DEFAULT_SUITES, n=None):
        self.mind, self.models, self.days = mind, list(models or []), float(days or 0)
        self.suites, self.n = list(suites or DEFAULT_SUITES), n
        self.running = False

    def history(self):
        return self.mind.store.kv_get(KV) or []

    def names(self):
        have = [m for m in self.mind.models.models if m != "mock"]
        return [m for m in self.models if m in have] if self.models else have

    def due(self, now=None):
        if self.days <= 0 or self.running or not self.names():
            return False
        h = self.history()
        return not h or (now or time.time()) - h[-1]["ts"] >= self.days * 86400

    async def run(self, seed=None):
        if self.running:
            return {"error": "a comparison is already running"}
        names = self.names()
        if not names:
            return {"error": "no models to compare"}
        self.running = True
        seed = int(seed) if seed is not None else random.randint(1, 99999)
        res = {"ts": time.time(), "seed": seed, "suites": self.suites, "models": {}}
        try:
            for m in names:
                row = {"suites": {}, "ms": [], "errors": 0}
                for s in self.suites:
                    try:
                        r = await self.mind.run_test_now(s, "model+brain", model=m, seed=seed, n=self.n)
                    except Exception as e:  # noqa: BLE001
                        log.warning("compare %s %s: %s", m, s, e)
                        row["errors"] += 1
                        continue
                    if r["status"] != "done":
                        row["errors"] += 1
                        continue
                    row["suites"][s] = r["summary"].get("accuracy", 0)
                    if r["summary"].get("ms_avg"):
                        row["ms"].append(r["summary"]["ms_avg"])
                accs = list(row["suites"].values())
                row["accuracy"] = round(sum(accs) / len(accs), 3) if accs else None
                row["ms_avg"] = int(sum(row["ms"]) / len(row["ms"])) if row["ms"] else None
                del row["ms"]
                res["models"][m] = row
        finally:
            self.running = False
        prev = self.history()[-1] if self.history() else None
        res["dropped"] = []
        if prev:
            for m, row in res["models"].items():
                was = (prev["models"].get(m) or {}).get("accuracy")
                if was is not None and row["accuracy"] is not None and was - row["accuracy"] >= DROP:
                    res["dropped"].append({"model": m, "was": was, "now": row["accuracy"]})
                    log.warning("model %s dropped from %.3f to %.3f in the comparison", m, was, row["accuracy"])
        ranked = sorted((m for m in res["models"] if res["models"][m]["accuracy"] is not None),
                        key=lambda m: (-res["models"][m]["accuracy"], res["models"][m]["ms_avg"] or 1e9))
        res["best"] = ranked[0] if ranked else None
        h = self.history() + [res]
        self.mind.store.kv_set(KV, h[-KEEP:])
        return res

    def table(self):
        """The latest comparison, with each model's change since the one before."""
        h = self.history()
        if not h:
            return {"latest": None, "runs": 0}
        last, prev = h[-1], (h[-2] if len(h) > 1 else None)
        rows = []
        for m, r in last["models"].items():
            was = (prev["models"].get(m) or {}).get("accuracy") if prev else None
            rows.append({"model": m, "accuracy": r["accuracy"], "ms_avg": r["ms_avg"], "suites": r["suites"],
                         "change": round(r["accuracy"] - was, 3) if was is not None and r["accuracy"] is not None else None})
        rows.sort(key=lambda x: -(x["accuracy"] or 0))
        return {"latest": {"ts": last["ts"], "seed": last["seed"], "suites": last["suites"], "best": last.get("best"),
                           "dropped": last.get("dropped", [])}, "models": rows, "runs": len(h),
                "trend": {m: [x["models"].get(m, {}).get("accuracy") for x in h] for m in last["models"]}}
