"""Connections to models: small ones on other computers (Ollama, LM Studio, anything OpenAI-compatible), Claude
directly (Anthropic API), and the models Home Assistant already has set up (AI Task / conversation agents), so
Haiku can be used with no second API key.

Every model is used the same way: chat(system, messages) -> {"text", "ms", "tokens", "tokens_in", "tokens_out"}.
"""
import asyncio
import os
import re
import time

import aiohttp

SUPERVISOR = "http://supervisor"
USAGE = None        # set by the app: f(model, ok, tokens_in, tokens_out, ms) records each call (see usage.py)


class ModelError(Exception):
    pass


def _use(*a):
    if USAGE:
        try:
            USAGE(*a)
        except Exception:  # noqa: BLE001  (counting must never break a call)
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

    # #45: Anthropic prompt caching. Claude caches a prefix only when it is long enough (1,024 to 4,096 tokens by
    # model), so only long, repeated prefixes are marked: a big system prompt, and the part of a message before
    # cache_until (the whole log in model+context tests, asked again for every question).
    CACHE_MIN_TOKENS = 4096

    def _cacheable(self, text):
        return len(text or "") / 4 >= int(self.cfg.get("cache_min_tokens") or self.CACHE_MIN_TOKENS)

    def _with_cache(self, messages, cache_until):
        if self.type != "anthropic" or not cache_until or not messages or self.cfg.get("cache") is False:
            return messages
        m0 = messages[0]
        c = m0.get("content")
        if not isinstance(c, str) or cache_until not in c:
            return messages
        head, tail = c.split(cache_until, 1)
        if not self._cacheable(head):
            return messages
        blocks = [{"type": "text", "text": head, "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": cache_until + tail}]
        return [dict(m0, content=blocks)] + list(messages[1:])

    async def chat(self, system, messages, max_tokens=800, temperature=0.2, cache_until=None):
        t0 = time.monotonic()
        fn = getattr(self, f"_chat_{self.type}", None)
        if fn is None:
            raise ModelError(f"unknown model type {self.type}")
        messages = self._with_cache(messages, cache_until)
        self._cached = None
        try:
            text, tokens = await fn(system, messages, max_tokens, temperature)
        except Exception as e:
            _use(self.name, False, None, None, int((time.monotonic() - t0) * 1000))
            if isinstance(e, asyncio.TimeoutError):
                raise ModelError(f"{self.name}: timed out after {self.timeout.total:g}s") from e
            if isinstance(e, aiohttp.ClientError):
                raise ModelError(self._clean(f"{self.name}: {type(e).__name__}: {e}")) from e
            raise
        tin, tout = tokens if isinstance(tokens, tuple) else (None, None)
        total = (tin or 0) + (tout or 0) if isinstance(tokens, tuple) else tokens
        ms = int((time.monotonic() - t0) * 1000)
        if self.type != "mock":
            _use(self.name, True, tin, tout, ms)
        if isinstance(text, list):          # content as parts ([{"type": "text", "text": ...}]), as some servers send
            text = "".join(str(p.get("text") or "") for p in text if isinstance(p, dict))
        text = text if isinstance(text, str) else ""
        # a thinking model (qwen3, deepseek-r1) through any server: its reasoning is not the answer, and a reply cut
        # off while still thinking has no answer at all
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        text = re.sub(r"^\s*<think>.*\Z", "", text, flags=re.S)
        out = {"text": text.strip(), "ms": ms, "tokens": total if total else None,
               "tokens_in": tin, "tokens_out": tout, "model": self.name}
        if getattr(self, "_cached", None):
            out["tokens_cached"] = self._cached
        return out

    async def check(self):
        try:
            r = await self.chat("Reply with the single word: ok", [{"role": "user", "content": "ping"}], max_tokens=10)
            return {"name": self.name, "ok": True, "ms": r["ms"], "reply": r["text"][:60]}
        except Exception as e:  # noqa: BLE001 - report any failure
            return {"name": self.name, "ok": False, "error": str(e)[:300]}

    # busy or briefly down (Anthropic's 529 "overloaded", a 429 rate limit, a proxy's 502/503): asked once more after
    # a short wait, so one blip doesn't fail a question or a night's pass. Never more than once, never a long wait.
    RETRY_STATUS = (429, 500, 502, 503, 504, 529)
    RETRY_WAIT_MAX = 20.0

    def _clean(self, text):
        """An error text without the API key (some servers echo the key they were given)."""
        k = self.cfg.get("api_key")
        return text.replace(k, "<api_key>") if k and isinstance(k, str) and len(k) >= 4 else text

    def _retry_wait(self, r):
        try:
            w = float((r.headers or {}).get("retry-after") or 2)
        except (TypeError, ValueError, AttributeError):
            w = 2.0
        return max(0.5, w) if w <= self.RETRY_WAIT_MAX else None

    async def _post(self, url, payload, headers=None):
        for attempt in (1, 2):
            async with self.session.post(url, json=payload, headers=headers or {}, timeout=self.timeout) as r:
                body = await r.text()
                status = r.status
                wait = self._retry_wait(r) if status in self.RETRY_STATUS and attempt == 1 else None
            if wait is not None:
                await asyncio.sleep(wait)
                continue
            break
        if status >= 400:
            raise ModelError(self._clean(f"{self.name}: HTTP {status}: {body[:300]}"))
        try:
            import json
            return json.loads(body)
        except ValueError as e:
            raise ModelError(self._clean(f"{self.name}: not JSON: {body[:200]}")) from e

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
        return j["choices"][0]["message"].get("content", ""), (u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0)

    async def embed(self, texts):
        """Vectors for texts (#10): Ollama's /api/embed, or an OpenAI-compatible /v1/embeddings. Option embed_model
        names the model; its "embed_model" field (or "model") is the embedding model to ask for."""
        name = self.cfg.get("embed_model") or self.cfg.get("model", "")
        if self.type == "mock":
            return [[float(len(t) % 7 + 1), float(len(t.split()) % 5 + 1), 1.0] for t in texts]
        if self.type == "ollama":
            url = (self.cfg.get("url") or "http://localhost:11434").rstrip("/")
            j = await self._post(url + "/api/embed", {"model": name, "input": texts})
            return j["embeddings"]
        if self.type == "openai":
            url = (self.cfg.get("url") or "http://localhost:11434/v1").rstrip("/")
            url = url + ("/embeddings" if url.endswith("/v1") else "/v1/embeddings")
            headers = {"Authorization": f"Bearer {self.cfg['api_key']}"} if self.cfg.get("api_key") else {}
            j = await self._post(url, {"model": name, "input": texts}, headers)
            return [d["embedding"] for d in sorted(j["data"], key=lambda d: d.get("index", 0))]
        raise ModelError(f"{self.name}: type {self.type} can't make embeddings (use ollama or openai)")

    # ---- Ollama native ---------------------------------------------------------------------------------------------
    async def _chat_ollama(self, system, messages, max_tokens, temperature):
        url = (self.cfg.get("url") or "http://localhost:11434").rstrip("/")
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        j = await self._post(url + "/api/chat", {"model": self.cfg.get("model", ""), "messages": msgs, "stream": False,
                                                 "think": False,
                                                 "options": {"num_predict": max_tokens, "temperature": temperature}})
        text = (j.get("message") or {}).get("content", "")
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        return text, (j.get("prompt_eval_count") or 0, j.get("eval_count") or 0)

    # ---- Anthropic API -----------------------------------------------------------------------------------------------
    async def _chat_anthropic(self, system, messages, max_tokens, temperature):
        if not self.cfg.get("api_key"):
            raise ModelError(f"{self.name}: needs api_key")
        url = (self.cfg.get("url") or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        sysp = system or ""
        if sysp and self.cfg.get("cache") is not False and self._cacheable(sysp):
            sysp = [{"type": "text", "text": sysp, "cache_control": {"type": "ephemeral"}}]
        j = await self._post(url, {"model": self.cfg.get("model") or "claude-haiku-5-5", "max_tokens": max_tokens,
                                   "temperature": temperature, "system": sysp, "messages": messages},
                             {"x-api-key": self.cfg["api_key"], "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        u = j.get("usage") or {}
        tin = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        self._cached = u.get("cache_read_input_tokens") or None
        return text, (tin, u.get("output_tokens") or 0)

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
