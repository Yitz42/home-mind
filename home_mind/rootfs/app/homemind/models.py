"""Connections to models: small ones on other computers (Ollama, LM Studio, anything OpenAI-compatible), Claude
directly (Anthropic API), and the models Home Assistant already has set up (AI Task / conversation agents), so
Haiku can be used with no second API key.

Every model is used the same way: chat(system, messages) -> {"text", "ms", "tokens"}.
"""
import asyncio
import os
import re
import time

import aiohttp

SUPERVISOR = "http://supervisor"


class ModelError(Exception):
    pass


class Model:
    def __init__(self, cfg, session):
        self.cfg = dict(cfg)
        self.name = cfg["name"]
        self.type = cfg.get("type", "openai")
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=float(cfg.get("timeout") or 120))

    def describe(self):
        d = {k: v for k, v in self.cfg.items() if k != "api_key"}
        d["has_key"] = bool(self.cfg.get("api_key"))
        return d

    async def chat(self, system, messages, max_tokens=800, temperature=0.2):
        t0 = time.monotonic()
        fn = getattr(self, f"_chat_{self.type}", None)
        if fn is None:
            raise ModelError(f"unknown model type {self.type}")
        try:
            text, tokens = await fn(system, messages, max_tokens, temperature)
        except asyncio.TimeoutError as e:
            raise ModelError(f"{self.name}: timed out after {self.timeout.total:g}s") from e
        except aiohttp.ClientError as e:
            raise ModelError(f"{self.name}: {type(e).__name__}: {e}") from e
        return {"text": (text or "").strip(), "ms": int((time.monotonic() - t0) * 1000), "tokens": tokens,
                "model": self.name}

    async def check(self):
        try:
            r = await self.chat("Reply with the single word: ok", [{"role": "user", "content": "ping"}], max_tokens=10)
            return {"name": self.name, "ok": True, "ms": r["ms"], "reply": r["text"][:60]}
        except Exception as e:  # noqa: BLE001 - report any failure
            return {"name": self.name, "ok": False, "error": str(e)[:300]}

    async def _post(self, url, payload, headers=None):
        async with self.session.post(url, json=payload, headers=headers or {}, timeout=self.timeout) as r:
            body = await r.text()
            if r.status >= 400:
                raise ModelError(f"{self.name}: HTTP {r.status}: {body[:300]}")
            try:
                import json
                return json.loads(body)
            except ValueError as e:
                raise ModelError(f"{self.name}: not JSON: {body[:200]}") from e

    # ---- OpenAI-compatible (Ollama /v1, LM Studio, llama.cpp server, vLLM, OpenAI) ------------------------------
    async def _chat_openai(self, system, messages, max_tokens, temperature):
        url = (self.cfg.get("url") or "http://localhost:11434/v1").rstrip("/")
        if not url.endswith("/chat/completions"):
            url = url + ("/chat/completions" if url.endswith("/v1") else "/v1/chat/completions")
        headers = {"Authorization": f"Bearer {self.cfg['api_key']}"} if self.cfg.get("api_key") else {}
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        j = await self._post(url, {"model": self.cfg.get("model", ""), "messages": msgs, "max_tokens": max_tokens,
                                   "temperature": temperature, "stream": False}, headers)
        u = j.get("usage") or {}
        return j["choices"][0]["message"].get("content", ""), u.get("total_tokens")

    # ---- Ollama native ---------------------------------------------------------------------------------------------
    async def _chat_ollama(self, system, messages, max_tokens, temperature):
        url = (self.cfg.get("url") or "http://localhost:11434").rstrip("/")
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        j = await self._post(url + "/api/chat", {"model": self.cfg.get("model", ""), "messages": msgs, "stream": False,
                                                 "think": False,
                                                 "options": {"num_predict": max_tokens, "temperature": temperature}})
        text = (j.get("message") or {}).get("content", "")
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        return text, (j.get("prompt_eval_count") or 0) + (j.get("eval_count") or 0)

    # ---- Anthropic API -----------------------------------------------------------------------------------------------
    async def _chat_anthropic(self, system, messages, max_tokens, temperature):
        if not self.cfg.get("api_key"):
            raise ModelError(f"{self.name}: needs api_key")
        url = (self.cfg.get("url") or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        j = await self._post(url, {"model": self.cfg.get("model") or "claude-haiku-5-5", "max_tokens": max_tokens,
                                   "temperature": temperature, "system": system or "", "messages": messages},
                             {"x-api-key": self.cfg["api_key"], "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        u = j.get("usage") or {}
        return text, (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)

    # ---- Home Assistant's own models -----------------------------------------------------------------------------
    def _ha_headers(self):
        tok = os.environ.get("SUPERVISOR_TOKEN")
        if not tok:
            raise ModelError(f"{self.name}: not running inside Home Assistant (no SUPERVISOR_TOKEN)")
        return {"Authorization": f"Bearer {tok}"}

    @staticmethod
    def _flatten(system, messages):
        parts = [system] if system else []
        for m in messages:
            parts.append(("" if m["role"] == "user" else "Assistant: ") + m["content"])
        return "\n\n".join(parts)

    async def _chat_ha_ai_task(self, system, messages, max_tokens, temperature):
        j = await self._post(f"{SUPERVISOR}/core/api/services/ai_task/generate_data?return_response",
                             {"task_name": "home_mind", "entity_id": self.cfg.get("entity_id") or "ai_task.claude_ai_task",
                              "instructions": self._flatten(system, messages)}, self._ha_headers())
        data = (j.get("service_response") or {}).get("data")
        return (data if isinstance(data, str) else str(data or "")), None

    async def _chat_ha_conversation(self, system, messages, max_tokens, temperature):
        j = await self._post(f"{SUPERVISOR}/core/api/conversation/process",
                             {"text": self._flatten(system, messages),
                              "agent_id": self.cfg.get("entity_id") or "conversation.claude_conversation"},
                             self._ha_headers())
        speech = ((j.get("response") or {}).get("speech") or {}).get("plain") or {}
        return speech.get("speech", ""), None

    # ---- mock: no model at all, for checking the plumbing ---------------------------------------------------------
    async def _chat_mock(self, system, messages, max_tokens, temperature):
        """Answers by copying the context line that shares the most words with the question (newest wins ties).
        It is a floor, not a model: anything a real model scores below this is a model problem."""
        q = messages[-1]["content"]
        ask = q.split("QUESTION:")[-1]
        lines = [ln for ln in q.split("\n") if ln.strip().startswith("[")]
        qw = set(re.findall(r"[a-z0-9]+", ask.lower()))
        best, bs = "", 0
        for i, ln in enumerate(lines):
            s = len(qw & set(re.findall(r"[a-z0-9]+", ln.lower())))
            if s >= bs and s > 0:
                best, bs = ln, s
        return (best or "I don't know."), 0


class Models:
    def __init__(self, configs):
        self.session = None
        self.configs = configs or []
        self.models = {}

    async def start(self):
        self.session = aiohttp.ClientSession()
        self.models = {c["name"]: Model(c, self.session) for c in self.configs if c.get("name")}
        if "mock" not in self.models:
            self.models["mock"] = Model({"name": "mock", "type": "mock"}, self.session)

    async def stop(self):
        if self.session:
            await self.session.close()

    def get(self, name):
        if name not in self.models:
            raise ModelError(f"no model named {name!r}; have: {', '.join(self.models)}")
        return self.models[name]

    def list(self):
        return [m.describe() for m in self.models.values()]

    async def check_all(self):
        return await asyncio.gather(*(m.check() for m in self.models.values()))
