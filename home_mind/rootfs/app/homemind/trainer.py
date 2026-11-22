"""Turnkey small-LLM training coordinator for Home Mind.

Covers all three small local models from one unified interface:
- ``control``:  FunctionGemma-270M house-control & phrasebook tool caller (``control_model``)
- ``persona``:  Qwen3-0.6B LoRA voice/emotion restyler (``persona_model``)
- ``memgraph``: Qwen3-0.6B/1.7B LoRA dual-mode [RETRIEVE]/[INGEST] router & archivist
                (``librarian_model`` / ``archivist_model``)

Supports two turnkey paths for every model:
1. Local GPU worker (``tools/trainer_worker.py`` running on your Ollama/GPU PC, configured via
   ``trainer_url``): clicking Train exports the dataset, uploads it, trains + evaluates + runs
   safety/regression gates, converts to GGUF, and registers the model into Ollama automatically.
2. Google Colab fallback (zero local GPU needed): one click downloads the single training bundle
   and opens the matching Colab notebook ready for Runtime -> Run all.
"""

import json
import logging
import time
import aiohttp

log = logging.getLogger("homemind.trainer")

REPO_BLOB_BASE = "https://colab.research.google.com/github/Yitz42/home-mind/blob/main/tools"

TARGETS = {
    "control": {
        "id": "control",
        "name": "Home Control",
        "slot": "control_model",
        "base_model": "google/functiongemma-270m-it",
        "ollama_name": "home-mind-control",
        "gguf_name": "home-mind-control.gguf",
        "notebook": "finetune_functiongemma.ipynb",
        "colab_url": f"{REPO_BLOB_BASE}/finetune_functiongemma.ipynb",
        "filename": "home_mind_control_bundle.json",
        "description": "Turns voice/typed house requests and phrasebook logs into tool calls on CPU.",
        "default_epochs": 4,
    },
    "persona": {
        "id": "persona",
        "name": "Persona Voice",
        "slot": "persona_model",
        "base_model": "Qwen/Qwen3-0.6B",
        "ollama_name": "jeeves-style",
        "gguf_name": "jeeves-style.gguf",
        "notebook": "train_jeeves_style.ipynb",
        "colab_url": f"{REPO_BLOB_BASE}/train_jeeves_style.ipynb",
        "filename": "jeeves_corpus.json",
        "description": "Rewords plain answers in your persona's voice & emotion with fact-check guards.",
        "default_epochs": 3,
    },
    "memgraph": {
        "id": "memgraph",
        "name": "MemGraph Brain",
        "slot": "librarian_model / archivist_model",
        "base_model": "Qwen/Qwen3-0.6B",
        "ollama_name": "memgraph-lora",
        "gguf_name": "memgraph-lora.gguf",
        "notebook": "train_memgraph_lora.ipynb",
        "colab_url": f"{REPO_BLOB_BASE}/train_memgraph_lora.ipynb",
        "filename": "memgraph_training.json",
        "description": "Fast local [RETRIEVE] router + [INGEST] graph archivist trained on audited corrections (70/30 replay).",
        "default_epochs": 3,
    },
}

TARGET_ORDER = ("control", "persona", "memgraph")


class Trainer:
    """Builds training bundles and orchestrates one-click local GPU or Colab training."""

    KV_JOBS = "trainer_jobs"

    def __init__(self, app, opts=None, session=None):
        self.app = app
        self.opts = opts or {}
        self._session = session

    @property
    def store(self):
        return self.app.mind.store

    @property
    def worker_url(self):
        return (self.opts.get("trainer_url") or "").strip().rstrip("/")

    @property
    def worker_token(self):
        return (self.opts.get("trainer_token") or "").strip()

    def _headers(self):
        h = {"Accept": "application/json"}
        if self.worker_token:
            h["Authorization"] = f"Bearer {self.worker_token}"
        return h

    def _saved_jobs(self):
        raw = self.store.kv_get(self.KV_JOBS)
        return dict(raw) if isinstance(raw, dict) else {}

    def _save_job(self, target, job):
        jobs = self._saved_jobs()
        jobs[target] = job
        self.store.kv_set(self.KV_JOBS, jobs)

    # ---- dataset summaries & builders -------------------------------------------------------------------------
    def _target_summary(self, target):
        """Lightweight readiness summary for UI display without running heavy paraphrasing."""
        meta = dict(TARGETS[target])
        if target == "control":
            devices = len(self.app.home.controllable()) if getattr(self.app, "home", None) else 0
            pb = len(self.app.phrasebook.export()) if getattr(self.app, "phrasebook", None) else 0
            ready = devices > 0 or pb > 0
            detail = f"{devices} controllable devices" + (f" · {pb} phrasebook entries" if pb else "")
            if not ready:
                detail = "No controllable devices loaded yet (refresh Home Assistant first)"
            return {**meta, "ready": ready, "examples": devices, "detail": detail}

        if target == "persona":
            persona_name = self.app.persona.settings()["name"] if getattr(self.app, "persona", None) else "Jeeves"
            return {**meta, "ready": True, "examples": 600,
                    "detail": f"600 synthetic practice prompts for {persona_name} (nothing personal)"}

        # memgraph
        mg = getattr(self.app, "memgraph", None)
        if mg is None:
            return {**meta, "ready": False, "examples": 0, "fresh": 0, "replay": 0,
                    "detail": "MemGraph not enabled"}
        from .memgraph import export_training_dataset
        exp = export_training_dataset(mg, max_examples=500, replay_ratio=0.30)
        fresh, replay, total = exp["fresh"], exp["replay"], exp["total"]
        if fresh == 0:
            detail = f"{replay} golden replay anchors (0 audited turns yet — can still bootstrap a baseline LoRA)"
        elif fresh < 30:
            detail = f"{total} examples ({fresh} fresh audited + {replay} golden replay; 50+ fresh recommended)"
        else:
            detail = f"{total} examples ({fresh} fresh audited + {replay} golden replay, 70/30 mix)"
        return {**meta, "ready": total > 0, "examples": total, "fresh": fresh, "replay": replay, "detail": detail}

    async def build_dataset(self, target, *, paraphrase=False, n=600, max_examples=500):
        """Build the single-file training bundle for ``target`` ('control' | 'persona' | 'memgraph')."""
        if target not in TARGETS:
            raise ValueError(f"unknown target '{target}' (use control, persona, or memgraph)")

        if target == "control":
            from . import localctl
            if not self.app.home.catalog:
                try:
                    await self.app.home.refresh()
                except Exception as e:  # noqa: BLE001 - still build from phrasebook/existing catalog if HA is down
                    log.warning("trainer control refresh: %s", e)
            d = localctl.build_dataset(self.app.home.controllable(), phrasebook=self.app.phrasebook.export())
            train_rows = list(d["train"])
            test_rows = list(d["test"])
            if paraphrase and train_rows:
                names = [m for m in self.app.models.models if m not in ("mock", self.opts.get("control_model"))]
                if names:
                    model = self.app.models.get("haiku" if "haiku" in names else names[0])
                    train_rows = train_rows + await localctl.paraphrase(model, train_rows)
            return {
                "target": "control",
                "created_at": time.time(),
                "base_model": TARGETS["control"]["base_model"],
                "train": train_rows,
                "test": test_rows,
                "stats": {"train": len(train_rows), "test": len(test_rows)},
            }

        if target == "persona":
            count = max(12, min(5000, int(n or 600)))
            return {
                "target": "persona",
                "created_at": time.time(),
                "base_model": TARGETS["persona"]["base_model"],
                "persona": self.app.persona.settings()["name"],
                "prompt": self.app.persona.prompt(),
                "style_system": self.app.persona.MODEL_SYSTEM,
                "examples": self.app.persona.examples(),
                "items": self.app.persona.corpus(count),
            }

        # memgraph
        mg = getattr(self.app, "memgraph", None)
        if mg is None:
            raise ValueError("memgraph is not enabled")
        from .memgraph import ARCHIVIST_SYSTEM, LIBRARIAN_SYSTEM, export_training_dataset
        limit = max(20, min(5000, int(max_examples or 500)))
        exp = export_training_dataset(mg, max_examples=limit, replay_ratio=0.30)
        return {
            "target": "memgraph",
            "created_at": time.time(),
            "base_model": TARGETS["memgraph"]["base_model"],
            "librarian_system": LIBRARIAN_SYSTEM,
            "archivist_system": ARCHIVIST_SYSTEM,
            "stats": {
                "total": exp["total"],
                "fresh": exp["fresh"],
                "replay": exp["replay"],
                "replay_share": exp["replay_share"],
            },
            "records": exp["records"],
        }

    # ---- local GPU worker HTTP helpers ------------------------------------------------------------------------
    async def _request(self, method, path, *, json_body=None, timeout=4.0):
        url = f"{self.worker_url}{path}"
        if self._session is not None:
            async with self._session.request(
                method, url, json=json_body, headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                return resp.status, await resp.json(content_type=None)
        async with aiohttp.ClientSession() as sess:
            async with sess.request(
                method, url, json=json_body, headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                return resp.status, await resp.json(content_type=None)

    async def worker_health(self):
        """Check whether the local GPU trainer worker is configured and reachable."""
        if not self.worker_url:
            return {"configured": False, "online": False}
        try:
            code, data = await self._request("GET", "/health", timeout=3.0)
            if code == 200 and isinstance(data, dict):
                return {"configured": True, "online": True, "url": self.worker_url, **data}
            return {"configured": True, "online": False, "url": self.worker_url,
                    "error": (data or {}).get("error") if isinstance(data, dict) else f"HTTP {code}"}
        except Exception as e:  # noqa: BLE001
            return {"configured": True, "online": False, "url": self.worker_url, "error": str(e)[:160]}

    async def refresh_job(self, job_id):
        """Poll the worker for a specific job's live state and persist updates."""
        if not self.worker_url or not job_id:
            return None
        try:
            code, data = await self._request("GET", f"/jobs/{job_id}", timeout=3.5)
            if code == 200 and isinstance(data, dict) and data.get("target") in TARGETS:
                self._save_job(data["target"], data)
                return data
        except Exception as e:  # noqa: BLE001
            log.debug("trainer poll %s: %s", job_id, e)
        return None

    async def status(self):
        """Unified status for the Models tab: worker health, all 3 targets, and live/last jobs."""
        worker = await self.worker_health()
        jobs = self._saved_jobs()
        if worker.get("online"):
            # Refresh any active/recent jobs from the worker so the UI gets live progress
            seen_ids = set()
            active_id = worker.get("active_job")
            if active_id:
                seen_ids.add(active_id)
                updated = await self.refresh_job(active_id)
                if updated:
                    jobs[updated["target"]] = updated
            for tgt, j in list(jobs.items()):
                jid = (j or {}).get("id")
                if jid and jid not in seen_ids and j.get("state") in ("queued", "running"):
                    seen_ids.add(jid)
                    updated = await self.refresh_job(jid)
                    if updated:
                        jobs[tgt] = updated

        targets = []
        for t in TARGET_ORDER:
            info = self._target_summary(t)
            info["dataset_url"] = f"api/trainer/dataset/{t}?download=1"
            info["job"] = jobs.get(t)
            targets.append(info)
        return {"worker": worker, "targets": targets}

    async def start(self, target, *, base_model=None, epochs=None, paraphrase=False, ollama_name=None):
        """One-click training trigger: build dataset and push job to the local GPU worker."""
        if target not in TARGETS:
            raise ValueError(f"unknown target '{target}' (use control, persona, or memgraph)")
        if not self.worker_url:
            raise ValueError("trainer_url is not configured — start tools/trainer_worker.py on your GPU PC or use Train in Colab")
        if epochs is not None:
            epochs = int(epochs)
            if not (1 <= epochs <= 20):
                raise ValueError("epochs must be between 1 and 20")

        dataset = await self.build_dataset(target, paraphrase=bool(paraphrase))
        meta = TARGETS[target]
        payload = {
            "target": target,
            "dataset": dataset,
            "options": {
                "base_model": (base_model or meta["base_model"]).strip(),
                "epochs": epochs if epochs is not None else meta["default_epochs"],
                "ollama_name": (ollama_name or meta["ollama_name"]).strip(),
                "gguf_name": meta["gguf_name"],
            },
        }
        try:
            code, data = await self._request("POST", "/jobs", json_body=payload, timeout=60.0)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"could not reach GPU trainer at {self.worker_url}: {e}") from e
        if code >= 400:
            err = data.get("error") if isinstance(data, dict) else f"trainer returned HTTP {code}"
            if code in (400, 409):
                raise ValueError(str(err))
            raise RuntimeError(str(err))
        self._save_job(target, data)
        return data

    async def cancel(self, job_id):
        """Request cancellation of a running job on the GPU worker."""
        if not job_id or not isinstance(job_id, str):
            raise ValueError("missing job_id")
        if not self.worker_url:
            raise ValueError("trainer_url is not configured")
        try:
            code, data = await self._request("POST", f"/jobs/{job_id}/cancel", json_body={}, timeout=5.0)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"could not reach GPU trainer at {self.worker_url}: {e}") from e
        if code >= 400:
            err = data.get("error") if isinstance(data, dict) else f"trainer returned HTTP {code}"
            raise ValueError(str(err))
        if isinstance(data, dict) and data.get("target") in TARGETS:
            self._save_job(data["target"], data)
        return data
