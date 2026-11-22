"""Tests for turnkey small-LLM training (homemind.trainer + tools/trainer_worker.py + Colab notebooks)."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "home_mind/rootfs/app"))
sys.path.insert(0, str(ROOT / "tools"))

from homemind.memgraph import MemGraph  # noqa: E402
from homemind.persona import Persona  # noqa: E402
from homemind.phrasebook import Phrasebook  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.tokens import needed  # noqa: E402
from homemind.trainer import TARGETS, Trainer  # noqa: E402
import trainer_worker  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def make_app(opts=None):
    tmp = tempfile.mkdtemp(prefix="hm-trainer-")
    st = Store(os.path.join(tmp, "mind.db"))
    mg = MemGraph(st)
    # Seed one approved staging row so MemGraph export has fresh + golden replay data
    res = mg.observe("user", "LIKES", "ramen", category="FOOD", source_ref="turn:1")
    lid = res.get("link_id") or (res.get("link") or {}).get("id") or res.get("id")
    with st.lock:
        st.db.execute(
            "INSERT INTO mg_staging(ts, op, patch, before_state, link_id, confidence, flagged, audit_status, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), "CREATE_LINK",
             json.dumps({"quote": "I love spicy ramen; pin 4417", "source": "user", "relation": "LIKES", "target": "ramen"}),
             None, lid, 0.9, 0, "approved", "ok"),
        )
        st.db.commit()

    class FakeHome:
        catalog = {
            "light.den": {"domain": "light", "name": "Den Lamp", "state": "off", "attrs": {"supported_color_modes": ["brightness"]}},
            "switch.fan": {"domain": "switch", "name": "Office Fan", "state": "off", "attrs": {}},
        }

        async def refresh(self):
            return self.catalog

        def controllable(self):
            return [
                {"entity_id": "light.den", "name": "Den Lamp", "domain": "light", "area": "Den", "caps": ["on_off", "brightness"]},
                {"entity_id": "switch.fan", "name": "Office Fan", "domain": "switch", "area": "Office", "caps": ["on_off"]},
            ]

    app = types.SimpleNamespace(
        mind=types.SimpleNamespace(store=st),
        memgraph=mg,
        persona=Persona(st),
        phrasebook=Phrasebook(st),
        home=FakeHome(),
        models=types.SimpleNamespace(models={"mock": None}),
    )
    return app, tmp


async def run_all():
    # 1. Admin scope check on all /api/trainer/* routes
    for m, p in (
        ("GET", "/api/trainer/status"),
        ("GET", "/api/trainer/dataset/memgraph"),
        ("POST", "/api/trainer/start"),
        ("POST", "/api/trainer/cancel"),
    ):
        check(f"tokens: {m} {p} requires admin", needed(m, p) == "admin")

    # 2. All 3 Colab notebooks exist and are valid JSON with expected upload handlers
    for tid, meta in TARGETS.items():
        nb_path = ROOT / "tools" / meta["notebook"]
        nb = json.loads(nb_path.read_text(encoding="utf-8"))
        check(f"notebook {meta['notebook']} valid JSON with cells", isinstance(nb.get("cells"), list) and len(nb["cells"]) >= 5)

    # 3. Unconfigured worker: status returns 1-click Colab fallback + dataset summaries for all 3 targets
    app, tmp = make_app()
    tr = Trainer(app, {})
    st0 = await tr.status()
    check("unconfigured trainer reports worker.online=False and 3 targets",
          st0["worker"]["configured"] is False and st0["worker"]["online"] is False
          and [t["id"] for t in st0["targets"]] == ["control", "persona", "memgraph"], st0)
    check("every target includes colab_url and relative dataset_url",
          all(t["colab_url"].startswith("https://colab.research.google.com/") and t["dataset_url"].startswith("api/trainer/dataset/")
              for t in st0["targets"]))

    # Dataset builds for all 3 targets
    ds_ctl = await tr.build_dataset("control")
    check("control dataset bundle has train and test rows", len(ds_ctl["train"]) > 0 and len(ds_ctl["test"]) > 0, ds_ctl["stats"])
    ds_per = await tr.build_dataset("persona", n=24)
    check("persona dataset bundle has items, prompt, style_system", len(ds_per["items"]) == 24 and bool(ds_per["prompt"]))
    ds_mg = await tr.build_dataset("memgraph")
    raw_mg = json.dumps(ds_mg)
    check("memgraph dataset bundle embeds system prompts and scrubs secrets",
          ds_mg["stats"]["fresh"] >= 1 and ds_mg["stats"]["replay"] >= 1
          and "memory graph" in ds_mg["librarian_system"] and "Archivist" in ds_mg["archivist_system"]
          and "4417" not in raw_mg, ds_mg["stats"])

    # Unconfigured start gives ValueError
    try:
        await tr.start("memgraph")
        check("start without trainer_url raises ValueError", False)
    except ValueError as e:
        check("start without trainer_url raises ValueError", "trainer_url" in str(e))

    # 4. Spin up real local GPU trainer_worker in dry-run mode on ephemeral port with Bearer token
    work_dir = os.path.join(tmp, "worker")
    srv, _wstate = trainer_worker.start_server("127.0.0.1", 0, work_dir=work_dir, token="secret-tok", dry_run=True)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        # Wrong token -> online=False with unauthorized error
        tr_bad = Trainer(app, {"trainer_url": f"http://127.0.0.1:{port}", "trainer_token": "wrong"})
        h_bad = await tr_bad.worker_health()
        check("worker rejects bad Bearer token", h_bad["online"] is False and "unauthorized" in (h_bad.get("error") or ""), h_bad)

        # Good token -> online=True
        tr_ok = Trainer(app, {"trainer_url": f"http://127.0.0.1:{port}", "trainer_token": "secret-tok"})
        h_ok = await tr_ok.worker_health()
        check("worker health succeeds with valid token", h_ok["online"] is True and h_ok.get("dry_run") is True, h_ok)

        # Validation: bad target and bad epochs
        for bad_call, label in (
            (lambda: tr_ok.start("unknown"), "unknown target"),
            (lambda: tr_ok.start("memgraph", epochs=0), "epochs=0"),
            (lambda: tr_ok.start("memgraph", epochs=99), "epochs=99"),
        ):
            try:
                await bad_call()
                check(f"rejects {label}", False)
            except ValueError:
                check(f"rejects {label}", True)

        # End-to-end 1-click training for all 3 targets!
        for target in ("memgraph", "persona", "control"):
            job = await tr_ok.start(target)
            check(f"1-click start({target}) accepted", job["target"] == target and job["id"].startswith(f"{target}-"), job)
            final = job
            for _ in range(40):
                await asyncio.sleep(0.05)
                final = await tr_ok.refresh_job(job["id"]) or final
                if final.get("state") in ("done", "failed", "cancelled"):
                    break
            check(f"1-click {target} completes with gguf + ollama_model",
                  final.get("state") == "done" and final.get("progress") == 100
                  and Path(final.get("gguf_path") or "").exists() and bool(final.get("ollama_model")), final)
            if target == "memgraph":
                check("memgraph job reports regression gate promotion",
                      final["metrics"]["gate"]["promote"] is True, final["metrics"])

        # Persisted in status()
        st_after = await tr_ok.status()
        check("status() persists completed jobs for all 3 targets",
              all((t.get("job") or {}).get("state") == "done" for t in st_after["targets"]))
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    asyncio.run(run_all())
    print()
    print("TRAINER OK" if not FAILS else f"FAIL ({len(FAILS)}): {', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
