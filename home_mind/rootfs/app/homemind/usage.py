"""How much each model is used: calls, failures, tokens in and out, and time, one row per model per day. Cost is
worked out only for models whose configuration gives prices (price_in / price_out, dollars per million tokens):
prices change, so none are built in. Home Assistant's own models (AI Task, conversation) don't report tokens, so
only their calls and time are counted.
"""
import time
from datetime import date, timedelta


class Usage:
    def __init__(self, store, prices=None):
        self.store = store
        self.prices = prices or {}         # model name -> (price_in, price_out) per million tokens
        store.db.execute("CREATE TABLE IF NOT EXISTS model_use (day TEXT, model TEXT, calls INTEGER, failures INTEGER, "
                         "tokens_in INTEGER, tokens_out INTEGER, ms INTEGER, PRIMARY KEY (day, model))")
        store.db.commit()

    def record(self, model, ok=True, tokens_in=None, tokens_out=None, ms=0):
        day = date.today().isoformat()
        with self.store.lock:
            self.store.db.execute(
                "INSERT INTO model_use(day, model, calls, failures, tokens_in, tokens_out, ms) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(day, model) DO UPDATE SET calls=calls+excluded.calls, failures=failures+excluded.failures, "
                "tokens_in=tokens_in+excluded.tokens_in, tokens_out=tokens_out+excluded.tokens_out, ms=ms+excluded.ms",
                (day, model, 1, 0 if ok else 1, int(tokens_in or 0), int(tokens_out or 0), int(ms or 0)))
            self.store.db.commit()

    def cost(self, model, tokens_in, tokens_out):
        p = self.prices.get(model)
        if not p:
            return None
        return round((tokens_in * p[0] + tokens_out * p[1]) / 1e6, 4)

    def rows(self, days=30):
        since = (date.today() - timedelta(days=days - 1)).isoformat()
        out = []
        for r in self.store.db.execute("SELECT * FROM model_use WHERE day >= ? ORDER BY day DESC, model", (since,)):
            d = dict(r)
            d["cost"] = self.cost(d["model"], d["tokens_in"], d["tokens_out"])
            d["ms_avg"] = int(d["ms"] / d["calls"]) if d["calls"] else 0
            out.append(d)
        return out

    def summary(self, days=30):
        """Totals per model over the last `days`, and today's totals across models."""
        per = {}
        for d in self.rows(days):
            t = per.setdefault(d["model"], {"model": d["model"], "calls": 0, "failures": 0, "tokens_in": 0,
                                            "tokens_out": 0, "ms": 0, "cost": None})
            for k in ("calls", "failures", "tokens_in", "tokens_out", "ms"):
                t[k] += d[k]
            if d["cost"] is not None:
                t["cost"] = round((t["cost"] or 0) + d["cost"], 4)
        today = [d for d in self.rows(1)]
        costs = [d["cost"] for d in today if d["cost"] is not None]
        return {"days": days, "models": sorted(per.values(), key=lambda t: -t["calls"]),
                "today": {"calls": sum(d["calls"] for d in today),
                          "tokens": sum(d["tokens_in"] + d["tokens_out"] for d in today),
                          "cost": round(sum(costs), 4) if costs else None},
                "at": time.time()}
