#!/usr/bin/env python3
"""Home Mind local GPU trainer worker.

Run this lightweight HTTP server on your GPU / Ollama computer so clicking "Train now" in
Home Mind's Models tab trains any of the three small models (control, persona, memgraph),
runs evaluation & safety gates, converts to GGUF, and registers the model in Ollama
automatically — with zero manual file uploads.

Quick start on your GPU PC:
    pip install torch transformers datasets accelerate trl peft sentencepiece protobuf
    python tools/trainer_worker.py --port 8090

Then set in Home Mind's configuration (config.yaml):
    trainer_url: http://<GPU_PC_IP>:8090
    trainer_token: ""   # optional shared secret matching --token / TRAINER_TOKEN
"""

import argparse
import collections
import hmac
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VALID_TARGETS = ("control", "persona", "memgraph")

NUM_RE = re.compile(r"\d+(?:[.:]\d+)?")
NEG_RE = re.compile(r"\b(?:not|no|never|none|nothing|nobody|nowhere|neither|nor|without)\b|n't\b", re.I)


def persona_check(plain, styled, limit=2.0):
    """Exact mirror of homemind.persona.check: every number and negation must survive."""
    a, b = collections.Counter(NUM_RE.findall(plain)), collections.Counter(NUM_RE.findall(styled))
    if any(b[n] < c for n, c in a.items()):
        return False
    if len(NEG_RE.findall(styled)) < len(NEG_RE.findall(plain)):
        return False
    if "Say yes" in plain and "Say yes" not in styled:
        return False
    return len(styled) <= limit * len(plain) + 60


def eval_regression_gate(candidate, baseline, *, min_delta=0.02, max_hallucination=0.05):
    """Exact mirror of homemind.memgraph.eval_regression_gate."""
    c_acc = float(candidate.get("accuracy", 0.0))
    b_acc = float(baseline.get("accuracy", 0.0))
    c_gold = float(candidate.get("golden_score", 1.0))
    b_gold = float(baseline.get("golden_score", 1.0))
    c_hall = float(candidate.get("hallucination_rate", 0.0))
    reasons = []
    if c_acc - b_acc < min_delta - 1e-9:
        reasons.append(f"accuracy gain {c_acc - b_acc:+.3f} < required +{min_delta:.3f}")
    if c_gold < b_gold - 1e-9:
        reasons.append(f"golden replay regressed ({c_gold:.3f} < {b_gold:.3f})")
    if c_hall > max_hallucination + 1e-9:
        reasons.append(f"hallucination rate {c_hall:.3f} > max {max_hallucination:.3f}")
    return {"promote": not reasons, "delta": round(c_acc - b_acc, 4), "reasons": reasons}


def detect_hardware():
    """Return GPU + Ollama availability without crashing if torch isn't installed yet."""
    gpu = None
    vram_gb = None
    torch_ok = False
    try:
        import torch  # type: ignore
        torch_ok = True
        if torch.cuda.is_available():
            gpu = torch.cuda.get_device_name(0)
            vram_gb = round(torch.cuda.get_device_properties(0).total_memory / (1024 ** 3), 1)
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            gpu = "Apple Silicon (MPS)"
    except Exception:  # noqa: BLE001
        pass
    ollama_bin = shutil.which("ollama")
    return {
        "torch": torch_ok,
        "gpu": gpu or "CPU only",
        "has_gpu": bool(gpu),
        "vram_gb": vram_gb,
        "ollama": bool(ollama_bin),
    }


def _extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


class WorkerState:
    """Thread-safe job registry & single-GPU training runner."""

    def __init__(self, work_dir, *, token="", dry_run=False, skip_ollama=False):
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.token = token or ""
        self.dry_run = bool(dry_run)
        self.skip_ollama = bool(skip_ollama)
        self.lock = threading.Lock()
        self.jobs = {}
        self.cancel_flags = {}
        self.active_job_id = None
        self.hw = detect_hardware()

    def health(self):
        with self.lock:
            active = self.active_job_id
            busy = active is not None and self.jobs.get(active, {}).get("state") in ("queued", "running")
            return {
                "ok": True,
                "busy": busy,
                "active_job": active if busy else None,
                "dry_run": self.dry_run,
                **self.hw,
            }

    def get_job(self, job_id):
        with self.lock:
            j = self.jobs.get(job_id)
            return dict(j) if j else None

    def _update(self, job_id, **fields):
        with self.lock:
            j = self.jobs.get(job_id)
            if not j:
                return
            log_line = fields.pop("log", None)
            j.update(fields)
            j["updated_at"] = time.time()
            if log_line:
                tail = list(j.get("log_tail") or [])
                tail.append(f"[{time.strftime('%H:%M:%S')}] {log_line}")
                j["log_tail"] = tail[-40:]

    def _cancelled(self, job_id):
        ev = self.cancel_flags.get(job_id)
        return bool(ev and ev.is_set())

    def submit(self, target, dataset, options=None):
        if target not in VALID_TARGETS:
            raise ValueError(f"unknown target '{target}'")
        if not isinstance(dataset, dict):
            raise ValueError("dataset must be a JSON object")
        with self.lock:
            if self.active_job_id and self.jobs[self.active_job_id]["state"] in ("queued", "running"):
                raise RuntimeError(f"another training job is already running ({self.active_job_id})")
            job_id = f"{target}-{uuid.uuid4().hex[:8]}"
            now = time.time()
            job = {
                "id": job_id,
                "target": target,
                "state": "queued",
                "stage": "queued",
                "progress": 0,
                "created_at": now,
                "updated_at": now,
                "options": dict(options or {}),
                "metrics": {},
                "log_tail": [f"[{time.strftime('%H:%M:%S')}] Accepted {target} training job"],
            }
            self.jobs[job_id] = job
            self.cancel_flags[job_id] = threading.Event()
            self.active_job_id = job_id

        t = threading.Thread(target=self._run_job, args=(job_id, target, dataset, dict(options or {})), daemon=True)
        t.start()
        return self.get_job(job_id)

    def cancel(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(f"unknown job '{job_id}'")
            ev = self.cancel_flags.get(job_id)
            if ev:
                ev.set()
            j = self.jobs[job_id]
            if j["state"] in ("queued", "running"):
                j["state"] = "cancelled"
                j["stage"] = "cancelled"
                j["updated_at"] = time.time()
                tail = list(j.get("log_tail") or [])
                tail.append(f"[{time.strftime('%H:%M:%S')}] Cancellation requested")
                j["log_tail"] = tail[-40:]
                if self.active_job_id == job_id:
                    self.active_job_id = None
            return dict(j)

    # ---- execution pipeline ----------------------------------------------------------------------------------
    def _run_job(self, job_id, target, dataset, opts):
        run_dir = self.work_dir / job_id
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._update(job_id, state="running", stage="preparing", progress=5,
                         log=f"Writing dataset to {run_dir}")
            (run_dir / "dataset.json").write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")

            if self.dry_run:
                self._run_dry(job_id, target, dataset, opts, run_dir)
                return

            if target == "memgraph":
                self._train_memgraph(job_id, dataset, opts, run_dir)
            elif target == "persona":
                self._train_persona(job_id, dataset, opts, run_dir)
            elif target == "control":
                self._train_control(job_id, dataset, opts, run_dir)
        except Exception as e:  # noqa: BLE001
            if not self._cancelled(job_id):
                self._update(job_id, state="failed", stage="failed", error=str(e)[:400],
                             log=f"ERROR: {e}")
        finally:
            with self.lock:
                if self.active_job_id == job_id:
                    self.active_job_id = None

    def _run_dry(self, job_id, target, dataset, opts, run_dir):
        """Fast deterministic dry-run for connection checks & automated tests (no GPU needed)."""
        stages = [
            ("preparing", 20, "Validated dataset bundle (dry-run)"),
            ("training", 55, f"Simulated LoRA/SFT training on {opts.get('base_model', 'base')}"),
            ("evaluating", 80, "Simulated held-out evaluation & gate check"),
            ("exporting", 95, "Simulated GGUF export & Ollama registration"),
        ]
        for stg, pct, msg in stages:
            if self._cancelled(job_id):
                return
            self._update(job_id, stage=stg, progress=pct, log=msg)
            time.sleep(0.05)

        if target == "memgraph":
            n = len(dataset.get("records") or [])
            baseline = {"accuracy": 0.72, "golden_score": 0.90, "hallucination_rate": 0.04}
            candidate = {"accuracy": 0.88, "golden_score": 0.95, "hallucination_rate": 0.01}
            gate = eval_regression_gate(candidate, baseline)
            metrics = {"examples": n, "baseline": baseline, "candidate": candidate, "gate": gate}
        elif target == "persona":
            n = len(dataset.get("items") or [])
            metrics = {"examples": n, "pass_rate": 0.94, "passed": True}
        else:
            n_tr = len(dataset.get("train") or [])
            n_te = len(dataset.get("test") or [])
            metrics = {"train": n_tr, "test": n_te, "before_accuracy": 0.35, "after_accuracy": 0.93}

        gguf = str(run_dir / (opts.get("gguf_name") or f"{target}.gguf"))
        Path(gguf).write_text("dry-run-gguf\n", encoding="utf-8")
        self._update(
            job_id,
            state="done",
            stage="done",
            progress=100,
            metrics=metrics,
            gguf_path=gguf,
            ollama_model=opts.get("ollama_name") or target,
            log=f"Dry-run complete -> {opts.get('ollama_name') or target}",
        )

    # ---- GGUF conversion + Ollama registration ---------------------------------------------------------------
    def _ensure_llama_cpp(self, job_id):
        repo = self.work_dir / "llama.cpp"
        converter = repo / "convert_hf_to_gguf.py"
        if converter.exists():
            return converter
        self._update(job_id, stage="exporting", progress=84, log="Cloning llama.cpp converter (one-time setup)...")
        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/ggml-org/llama.cpp", str(repo)],
            check=True, capture_output=True, text=True,
        )
        return converter

    def _export_gguf_and_ollama(self, job_id, merged_dir, gguf_name, ollama_name, *, temperature=0.1, num_predict=256):
        converter = self._ensure_llama_cpp(job_id)
        gguf_path = Path(merged_dir).parent / gguf_name
        self._update(job_id, stage="exporting", progress=88, log=f"Converting to GGUF ({gguf_name}, q8_0)...")
        subprocess.run(
            [sys.executable, str(converter), str(merged_dir), "--outtype", "q8_0", "--outfile", str(gguf_path)],
            check=True,
        )
        registered = None
        if not self.skip_ollama and shutil.which("ollama") and ollama_name:
            self._update(job_id, stage="exporting", progress=95, log=f"Registering Ollama model '{ollama_name}'...")
            modelfile = gguf_path.parent / "Modelfile"
            modelfile.write_text(
                f"FROM {gguf_path}\nPARAMETER temperature {temperature}\nPARAMETER num_predict {num_predict}\n",
                encoding="utf-8",
            )
            subprocess.run(["ollama", "create", ollama_name, "-f", str(modelfile)], check=True)
            registered = ollama_name
        return str(gguf_path), registered

    # ---- real GPU training: MemGraph LoRA --------------------------------------------------------------------
    def _train_memgraph(self, job_id, dataset, opts, run_dir):
        import torch  # type: ignore
        from datasets import Dataset  # type: ignore
        from peft import LoraConfig  # type: ignore
        from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore
        from trl import SFTConfig, SFTTrainer  # type: ignore

        records = list(dataset.get("records") or [])
        if len(records) < 4:
            raise ValueError("need at least 4 MemGraph training records")
        lib_sys = dataset.get("librarian_system") or "You are the memory graph Librarian. Reply with JSON only."
        arc_sys = dataset.get("archivist_system") or "You are the memory graph Archivist. Reply with JSON only."

        def to_row(rec):
            sys_p = lib_sys if rec.get("mode") == "[RETRIEVE]" else arc_sys
            return {
                "messages": [
                    {"role": "system", "content": sys_p},
                    {"role": "user", "content": f"{rec.get('mode', '[INGEST]')} {rec.get('input', '')}"},
                    {"role": "assistant", "content": json.dumps(rec.get("output", {}), ensure_ascii=False)},
                ],
                "golden": bool(rec.get("golden")),
                "expected": rec.get("output", {}),
            }

        rng = random.Random(7)
        rows = [to_row(r) for r in records]
        rng.shuffle(rows)
        cut = max(2, min(len(rows) // 5, 40))
        val_rows, train_rows = rows[:cut], rows[cut:]
        golden_rows = [r for r in rows if r["golden"]][:20]

        base = opts.get("base_model") or "Qwen/Qwen3-0.6B"
        epochs = int(opts.get("epochs") or 3)
        self._update(job_id, stage="loading", progress=12, log=f"Loading {base} ({len(train_rows)} train / {len(val_rows)} eval)...")
        dtype = torch.bfloat16 if (torch.cuda.is_available() and torch.cuda.is_bf16_supported()) else torch.float16
        tok = AutoTokenizer.from_pretrained(base)
        model = AutoModelForCausalLM.from_pretrained(base, torch_dtype=dtype, device_map="auto")

        def score_split(m, split):
            if not split:
                return {"accuracy": 1.0, "hallucination_rate": 0.0}
            ok = hall = 0
            for r in split:
                msgs = r["messages"][:2]
                ids = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt").to(m.device)
                with torch.no_grad():
                    out = m.generate(ids, max_new_tokens=200, do_sample=False)
                txt = tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True)
                parsed = _extract_json(txt)
                exp = r["expected"]
                if not isinstance(parsed, dict):
                    hall += 1
                    continue
                if "patches" in exp:
                    exp_p, got_p = exp.get("patches") or [], parsed.get("patches") or []
                    if not exp_p and not got_p:
                        ok += 1
                    elif not exp_p and got_p:
                        hall += 1
                    elif exp_p and got_p:
                        ok += int(str(exp_p[0].get("relation", "")).upper() == str(got_p[0].get("relation", "")).upper())
                else:
                    ok += int(bool(parsed.keys() & exp.keys()))
            return {"accuracy": round(ok / len(split), 4), "hallucination_rate": round(hall / len(split), 4)}

        self._update(job_id, stage="evaluating_baseline", progress=22, log="Scoring baseline before LoRA...")
        b_val = score_split(model, val_rows)
        b_gold = score_split(model, golden_rows)
        baseline = {"accuracy": b_val["accuracy"], "golden_score": b_gold["accuracy"],
                    "hallucination_rate": b_val["hallucination_rate"]}

        if self._cancelled(job_id):
            return
        self._update(job_id, stage="training", progress=35, log=f"Fine-tuning LoRA for {epochs} epochs...")
        ds = Dataset.from_list([{"messages": r["messages"]} for r in train_rows])
        args = SFTConfig(
            output_dir=str(run_dir / "ckpt"),
            num_train_epochs=epochs,
            per_device_train_batch_size=4,
            gradient_accumulation_steps=2,
            learning_rate=2e-4,
            lr_scheduler_type="cosine",
            warmup_ratio=0.05,
            logging_steps=10,
            save_strategy="no",
            max_length=1024,
            bf16=torch.cuda.is_available() and torch.cuda.is_bf16_supported(),
            fp16=torch.cuda.is_available() and not torch.cuda.is_bf16_supported(),
            report_to=[],
        )
        lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, target_modules="all-linear", task_type="CAUSAL_LM")
        trainer = SFTTrainer(model=model, args=args, train_dataset=ds, processing_class=tok, peft_config=lora)
        trainer.train()

        if self._cancelled(job_id):
            return
        self._update(job_id, stage="evaluating", progress=75, log="Evaluating candidate LoRA & regression gate...")
        merged = trainer.model.merge_and_unload()
        c_val = score_split(merged, val_rows)
        c_gold = score_split(merged, golden_rows)
        candidate = {"accuracy": c_val["accuracy"], "golden_score": c_gold["accuracy"],
                     "hallucination_rate": c_val["hallucination_rate"]}
        gate = eval_regression_gate(candidate, baseline)
        metrics = {"examples": len(records), "baseline": baseline, "candidate": candidate, "gate": gate}

        merged_dir = run_dir / "merged"
        merged.save_pretrained(str(merged_dir))
        tok.save_pretrained(str(merged_dir))
        gguf_path, ollama_model = self._export_gguf_and_ollama(
            job_id, merged_dir, opts.get("gguf_name") or "memgraph-lora.gguf",
            opts.get("ollama_name") or "memgraph-lora" if gate["promote"] else "",
            temperature=0.0, num_predict=256,
        )
        self._update(
            job_id, state="done", stage="done", progress=100, metrics=metrics,
            gguf_path=gguf_path, ollama_model=ollama_model,
            log=(f"Promoted to Ollama '{ollama_model}'" if ollama_model
                 else f"Saved GGUF ({gguf_path}); gate note: {'; '.join(gate['reasons']) or 'Ollama not found'}"),
        )

    # ---- real GPU training: Persona Voice LoRA ---------------------------------------------------------------
    def _train_persona(self, job_id, dataset, opts, run_dir):
        import torch  # type: ignore
        from datasets import Dataset  # type: ignore
        from peft import LoraConfig  # type: ignore
        from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore
        from trl import SFTConfig, SFTTrainer  # type: ignore

        prompt_sys = f"{dataset.get('prompt', '')}\n\n{dataset.get('style_system', '')}".strip()
        items = list(dataset.get("items") or [])
        pairs = list(dataset.get("pairs") or [])
        if not pairs:
            api_key = os.environ.get("GEMINI_API_KEY") or ""
            if not api_key:
                raise ValueError("Set GEMINI_API_KEY on the trainer worker to generate persona teacher rewrites")
            from google import genai  # type: ignore
            client = genai.Client(api_key=api_key)
            shots = "\n".join(f"[{e['emotion']}] {e['plain']}\n=> {e['styled']}" for e in (dataset.get("examples") or []))
            self._update(job_id, stage="teacher", progress=15, log=f"Generating teacher rewrites for {len(items)} items...")
            for idx, it in enumerate(items):
                if self._cancelled(job_id):
                    return
                ask = f"{prompt_sys}\n\nExamples:\n{shots}\n\nNow:\n[{it['emotion']}] {it['plain']}\n=>"
                try:
                    out = client.models.generate_content(model="gemini-flash-latest", contents=ask).text.strip().splitlines()[0].strip()
                    out = re.sub(r"^=>\s*", "", out).strip().strip('"')
                    if out and out != it["plain"] and persona_check(it["plain"], out):
                        pairs.append({"emotion": it["emotion"], "plain": it["plain"], "styled": out})
                except Exception:  # noqa: BLE001
                    time.sleep(1.0)
                if idx and idx % 50 == 0:
                    self._update(job_id, progress=15 + int(25 * idx / max(1, len(items))),
                                 log=f"Teacher {idx}/{len(items)}: kept {len(pairs)}")
        if len(pairs) < 20:
            raise ValueError(f"only {len(pairs)} styled pairs passed fact checks (need >= 20)")

        base = opts.get("base_model") or "Qwen/Qwen3-0.6B"
        epochs = int(opts.get("epochs") or 3)
        self._update(job_id, stage="training", progress=45, log=f"Training {base} LoRA on {len(pairs)} pairs...")
        dtype = torch.bfloat16 if (torch.cuda.is_available() and torch.cuda.is_bf16_supported()) else torch.float16
        tok = AutoTokenizer.from_pretrained(base)
        model = AutoModelForCausalLM.from_pretrained(base, torch_dtype=dtype, device_map="auto")
        random.Random(7).shuffle(pairs)
        cut = max(20, len(pairs) // 10)
        test, train = pairs[:cut], pairs[cut:]
        ds = Dataset.from_list([
            {"messages": [
                {"role": "system", "content": prompt_sys},
                {"role": "user", "content": f"[{p['emotion']}] {p['plain']}"},
                {"role": "assistant", "content": p["styled"]},
            ]}
            for p in train
        ])
        args = SFTConfig(
            output_dir=str(run_dir / "ckpt"), num_train_epochs=epochs, per_device_train_batch_size=8,
            gradient_accumulation_steps=2, learning_rate=2e-4, lr_scheduler_type="cosine", warmup_ratio=0.05,
            logging_steps=20, save_strategy="no", max_length=1024,
            bf16=torch.cuda.is_available() and torch.cuda.is_bf16_supported(),
            fp16=torch.cuda.is_available() and not torch.cuda.is_bf16_supported(), report_to=[],
        )
        lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, target_modules="all-linear", task_type="CAUSAL_LM")
        trainer = SFTTrainer(model=model, args=args, train_dataset=ds, processing_class=tok, peft_config=lora)
        trainer.train()

        self._update(job_id, stage="evaluating", progress=78, log="Scoring persona pass rate on held-out answers...")
        merged = trainer.model.merge_and_unload()
        ok = 0
        for p in test:
            msgs = [{"role": "system", "content": prompt_sys}, {"role": "user", "content": f"[{p['emotion']}] {p['plain']}"}]
            ids = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt").to(merged.device)
            with torch.no_grad():
                out = merged.generate(ids, max_new_tokens=120, do_sample=False)
            styled = tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip().splitlines()[0].strip()
            ok += int(persona_check(p["plain"], styled) and styled != p["plain"])
        pass_rate = round(ok / len(test), 4)

        merged_dir = run_dir / "merged"
        merged.save_pretrained(str(merged_dir))
        tok.save_pretrained(str(merged_dir))
        gguf_path, ollama_model = self._export_gguf_and_ollama(
            job_id, merged_dir, opts.get("gguf_name") or "jeeves-style.gguf",
            opts.get("ollama_name") or "jeeves-style", temperature=0.3, num_predict=120,
        )
        self._update(
            job_id, state="done", stage="done", progress=100,
            metrics={"examples": len(pairs), "pass_rate": pass_rate, "passed": pass_rate >= 0.85},
            gguf_path=gguf_path, ollama_model=ollama_model,
            log=f"Done: pass_rate={pass_rate:.0%}, GGUF={gguf_path}",
        )

    # ---- real GPU training: Home Control FunctionGemma -------------------------------------------------------
    def _train_control(self, job_id, dataset, opts, run_dir):
        import torch  # type: ignore
        from datasets import Dataset  # type: ignore
        from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore
        from trl import SFTConfig, SFTTrainer  # type: ignore

        train_rows = list(dataset.get("train") or [])
        test_rows = list(dataset.get("test") or [])
        if not train_rows:
            raise ValueError("control dataset has 0 training rows (refresh Home Assistant devices first)")

        base = opts.get("base_model") or "google/functiongemma-270m-it"
        epochs = int(opts.get("epochs") or 4)
        self._update(job_id, stage="loading", progress=15, log=f"Loading {base} ({len(train_rows)} train / {len(test_rows)} test)...")
        tok = AutoTokenizer.from_pretrained(base)
        model = AutoModelForCausalLM.from_pretrained(base, dtype="auto", device_map="auto", attn_implementation="eager")
        bf16 = torch.cuda.is_available() and torch.cuda.is_bf16_supported()
        self._update(job_id, stage="training", progress=35, log=f"Fine-tuning FunctionGemma for {epochs} epochs...")
        args = SFTConfig(
            output_dir=str(run_dir / "ckpt"), max_length=2048, packing=False, num_train_epochs=epochs,
            per_device_train_batch_size=8, gradient_accumulation_steps=1, learning_rate=5e-5,
            lr_scheduler_type="constant", optim="adamw_torch_fused", logging_steps=10,
            eval_strategy="no", save_strategy="no", bf16=bf16, fp16=not bf16, report_to=[],
        )
        trainer = SFTTrainer(model=model, args=args, train_dataset=Dataset.from_list(train_rows), processing_class=tok)
        trainer.train()

        merged_dir = run_dir / "merged"
        trainer.save_model(str(merged_dir))
        tok.save_pretrained(str(merged_dir))
        gguf_path, ollama_model = self._export_gguf_and_ollama(
            job_id, merged_dir, opts.get("gguf_name") or "home-mind-control.gguf",
            opts.get("ollama_name") or "home-mind-control", temperature=0.0, num_predict=128,
        )
        self._update(
            job_id, state="done", stage="done", progress=100,
            metrics={"train": len(train_rows), "test": len(test_rows)},
            gguf_path=gguf_path, ollama_model=ollama_model,
            log=f"Done: GGUF={gguf_path} (copy to /share/home_mind/home-mind-control.gguf or use Ollama)",
        )


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A002
            pass  # keep stdout clean

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if not state.token:
                return True
            hdr = self.headers.get("Authorization") or ""
            got = hdr[7:].strip() if hdr.startswith("Bearer ") else ""
            return hmac.compare_digest(got, state.token)

        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self):  # noqa: N802
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            if self.path == "/health":
                return self._send(200, state.health())
            if self.path.startswith("/jobs/"):
                jid = self.path[len("/jobs/"):].split("?")[0].strip("/")
                j = state.get_job(jid)
                if not j:
                    return self._send(404, {"error": "job not found"})
                return self._send(200, j)
            return self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            try:
                body = self._read_json()
            except Exception:  # noqa: BLE001
                return self._send(400, {"error": "invalid JSON"})
            if self.path == "/jobs":
                try:
                    j = state.submit(body.get("target"), body.get("dataset"), body.get("options"))
                    return self._send(202, j)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except RuntimeError as e:
                    return self._send(409, {"error": str(e)})
            if self.path.startswith("/jobs/") and self.path.endswith("/cancel"):
                jid = self.path[len("/jobs/"):-len("/cancel")].strip("/")
                try:
                    return self._send(200, state.cancel(jid))
                except KeyError:
                    return self._send(404, {"error": "job not found"})
            return self._send(404, {"error": "not found"})

    return Handler


def start_server(host="0.0.0.0", port=8090, *, work_dir="./home_mind_training", token="",
                 dry_run=False, skip_ollama=False):
    state = WorkerState(work_dir, token=token, dry_run=dry_run, skip_ollama=skip_ollama)
    srv = ThreadingHTTPServer((host, port), make_handler(state))
    return srv, state


def main():
    p = argparse.ArgumentParser(description="Home Mind local GPU trainer worker")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--work-dir", default="./home_mind_training")
    p.add_argument("--token", default=os.environ.get("TRAINER_TOKEN", ""))
    p.add_argument("--dry-run", action="store_true", help="Simulate training without a GPU (connection test)")
    p.add_argument("--skip-ollama", action="store_true", help="Export GGUF only, do not call ollama create")
    a = p.parse_args()
    srv, state = start_server(a.host, a.port, work_dir=a.work_dir, token=a.token,
                              dry_run=a.dry_run, skip_ollama=a.skip_ollama)
    h = state.health()
    print(f"Home Mind Trainer Worker listening on http://{a.host}:{a.port}")
    print(f"  GPU: {h['gpu']}  |  Ollama: {'yes' if h['ollama'] else 'no'}  |  Auth: {'token' if a.token else 'open'}")
    print(f"  Set trainer_url: http://<THIS_PC_IP>:{a.port} in Home Mind config.yaml")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping trainer worker.")
        srv.shutdown()


if __name__ == "__main__":
    main()
