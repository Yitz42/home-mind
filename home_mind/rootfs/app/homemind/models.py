"""Connections to models: small ones on other computers (Ollama, LM Studio, anything OpenAI-compatible), Claude
directly (Anthropic API), and the models Home Assistant already has set up (AI Task / conversation agents), so
Haiku can be used with no second API key.

Every model is used the same way: chat(system, messages) -> {"text", "ms", "tokens", "tokens_in", "tokens_out"}.
"""
import asyncio
import os
import re
import logging
import time

import aiohttp

from .ha import SUPERVISOR  # noqa: E402  (overridable for local testing)

log = logging.getLogger("homemind.models")
USAGE = None        # set by the app: f(model, ok, tokens_in, tokens_out, ms) records each call (see usage.py)


class ModelError(Exception):
    pass


def _use(*a):
    if USAGE:
        try:
            USAGE(*a)
        except Exception:  # noqa: BLE001  (counting must never break a call)
            pass


class _ThinkFilter:
    """Strips <think>...</think> blocks from a token stream, even when tags split across chunks."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, chunk):
        if not chunk:
            return ""
        self.buf += chunk
        out = []
        while self.buf:
            if self.inside:
                i = self.buf.find(self.CLOSE)
                if i >= 0:
                    self.buf = self.buf[i + len(self.CLOSE):]
                    self.inside = False
                    continue
                self.buf = self.buf[-(len(self.CLOSE) - 1):]
                break
            i = self.buf.find(self.OPEN)
            if i >= 0:
                if i:
                    out.append(self.buf[:i])
                self.buf = self.buf[i + len(self.OPEN):]
                self.inside = True
                continue
            hold = 0
            for k in range(min(len(self.buf), len(self.OPEN) - 1), 0, -1):
                if self.OPEN.startswith(self.buf[-k:]):
                    hold = k
                    break
            if hold:
                if len(self.buf) > hold:
                    out.append(self.buf[:-hold])
                    self.buf = self.buf[-hold:]
            else:
                out.append(self.buf)
                self.buf = ""
            break
        return "".join(out)

    def flush(self):
        if self.inside or not self.buf:
            self.buf = ""
            return ""
        rem, self.buf = self.buf, ""
        return rem


class Model:
    def __init__(self, cfg, session):
        self.cfg = dict(cfg)
        self.name = cfg["name"]
        self.type = cfg.get("type", "openai")
        self.session = session
        # a computer that's asleep or off fails in seconds (connect), not after the whole answer's time (total)
        self.timeout = aiohttp.ClientTimeout(total=float(cfg.get("timeout") or 120),
                                             sock_connect=float(cfg.get("connect_timeout") or 4))

    def endpoint(self):
        """(host, port) of the computer this model runs on, for the up/asleep check; None for services in
        Home Assistant and for cloud APIs without a url."""
        from urllib.parse import urlsplit
        url = self.cfg.get("url") or ("http://localhost:11434" if self.type == "ollama" else "")
        if not url or self.type not in ("ollama", "openai"):
            return None
        u = urlsplit(url if "//" in url else "http://" + url)
        if not u.hostname:
            return None
        return u.hostname, u.port or (443 if u.scheme == "https" else 80)

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

    async def chat_stream(self, system, messages, on_delta, max_tokens=800, temperature=0.2, cache_until=None):
        """Stream text deltas to on_delta(chunk) as they arrive (for openai, ollama, anthropic), returning the same
        result dict as chat(). Types without streaming fall back cleanly to chat()."""
        fn = getattr(self, f"_stream_{self.type}", None)
        if fn is None or on_delta is None:
            r = await self.chat(system, messages, max_tokens, temperature, cache_until)
            if on_delta is not None and r.get("text"):
                on_delta(r["text"])
            return r
        t0 = time.monotonic()
        messages = self._with_cache(messages, cache_until)
        self._cached = None
        filt = _ThinkFilter()
        parts = []

        def emit(chunk):
            clean = filt.feed(chunk)
            if clean:
                parts.append(clean)
                on_delta(clean)

        try:
            tokens = await fn(system, messages, max_tokens, temperature, emit)
            tail = filt.flush()
            if tail:
                parts.append(tail)
                on_delta(tail)
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
        text = "".join(parts).strip()
        out = {"text": text, "ms": ms, "tokens": total if total else None,
               "tokens_in": tin, "tokens_out": tout, "model": self.name}
        if getattr(self, "_cached", None):
            out["tokens_cached"] = self._cached
        return out

    async def _post_lines(self, url, payload, headers=None):
        """POST a streaming JSON request and yield decoded lines, raising ModelError on HTTP >= 400."""
        async with self.session.post(url, json=payload, headers=headers or {}, timeout=self.timeout) as r:
            if r.status >= 400:
                body = await r.text()
                raise ModelError(self._clean(f"{self.name}: HTTP {r.status}: {body[:300]}"))
            buf = b""
            async for chunk in r.content.iter_any():
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.decode("utf-8", "replace").strip()
            if buf.strip():
                yield buf.decode("utf-8", "replace").strip()

    async def _stream_openai(self, system, messages, max_tokens, temperature, emit):
        import json
        url = (self.cfg.get("url") or "http://localhost:11434/v1").rstrip("/")
        if not url.endswith("/chat/completions"):
            url = url + ("/chat/completions" if url.endswith("/v1") else "/v1/chat/completions")
        headers = {"Authorization": f"Bearer {self.cfg['api_key']}"} if self.cfg.get("api_key") else {}
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        payload = {"model": self.cfg.get("model", ""), "messages": msgs, "max_tokens": max_tokens,
                   "temperature": temperature, "stream": True}
        tin, tout = 0, 0
        async for line in self._post_lines(url, payload, headers):
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                j = json.loads(data)
            except ValueError:
                continue
            u = j.get("usage") or {}
            if u:
                tin, tout = u.get("prompt_tokens") or tin, u.get("completion_tokens") or tout
            ch = (j.get("choices") or [{}])[0]
            delta = (ch.get("delta") or {}).get("content")
            if isinstance(delta, str) and delta:
                emit(delta)
        return (tin, tout) if (tin or tout) else None

    async def _stream_ollama(self, system, messages, max_tokens, temperature, emit):
        import json
        url = (self.cfg.get("url") or "http://localhost:11434").rstrip("/")
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        opts = {"num_predict": max_tokens, "temperature": temperature}
        for k in ("num_gpu", "num_ctx", "num_thread"):
            if self.cfg.get(k) is not None:
                opts[k] = int(self.cfg[k])
        body = {"model": self.cfg.get("model", ""), "messages": msgs, "stream": True, "think": False, "options": opts}
        if self.cfg.get("keep_alive"):
            body["keep_alive"] = self.cfg["keep_alive"]
        tin, tout = 0, 0
        async for line in self._post_lines(url + "/api/chat", body):
            if not line:
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            txt = (j.get("message") or {}).get("content")
            if isinstance(txt, str) and txt:
                emit(txt)
            if j.get("done"):
                tin = j.get("prompt_eval_count") or tin
                tout = j.get("eval_count") or tout
        return (tin, tout) if (tin or tout) else None

    async def _stream_anthropic(self, system, messages, max_tokens, temperature, emit):
        import json
        if not self.cfg.get("api_key"):
            raise ModelError(f"{self.name}: needs api_key")
        url = (self.cfg.get("url") or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        sysp = system or ""
        if sysp and self.cfg.get("cache") is not False and self._cacheable(sysp):
            sysp = [{"type": "text", "text": sysp, "cache_control": {"type": "ephemeral"}}]
        payload = {"model": self.cfg.get("model") or "claude-haiku-5-5", "max_tokens": max_tokens,
                   "temperature": temperature, "system": sysp, "messages": messages, "stream": True}
        headers = {"x-api-key": self.cfg["api_key"], "anthropic-version": "2023-06-01"}
        tin, tout = 0, 0
        async for line in self._post_lines(url, payload, headers):
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                j = json.loads(data)
            except ValueError:
                continue
            t = j.get("type")
            if t == "content_block_delta":
                d = j.get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    emit(d["text"])
            elif t == "message_start":
                u = (j.get("message") or {}).get("usage") or {}
                tin = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (
                    u.get("cache_creation_input_tokens") or 0)
                self._cached = u.get("cache_read_input_tokens") or None
            elif t == "message_delta":
                u = j.get("usage") or {}
                tout = u.get("output_tokens") or tout
        return (tin, tout) if (tin or tout) else None

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

    async def call_tool(self, messages, tools, system=None, max_tokens=96):
        """One tool call from a small function-calling model (localctl: FunctionGemma fine-tuned on this house).
        Ollama's /api/chat or an OpenAI-compatible server (llama.cpp's llama-server --jinja) with tools=. Returns
        {"tool_calls": [...] or None, "text": raw reply, "ms"}: localctl.parse_call reads either form, because a server
        that doesn't know FunctionGemma's call format hands back the raw "call:name{...}" text instead."""
        t0 = time.monotonic()
        msgs = ([{"role": "developer", "content": system}] if system else []) + messages
        if self.type == "ollama":
            url = (self.cfg.get("url") or "http://localhost:11434").rstrip("/")
            j = await self._post(url + "/api/chat", {"model": self.cfg.get("model", ""), "messages": msgs, "tools": tools,
                                                     "stream": False, "options": {"num_predict": max_tokens,
                                                                                  "temperature": 0}})
            m = j.get("message") or {}
        elif self.type == "openai":
            url = (self.cfg.get("url") or "http://localhost:8080/v1").rstrip("/")
            if not url.endswith("/chat/completions"):
                url = url + ("/chat/completions" if url.endswith("/v1") else "/v1/chat/completions")
            headers = {"Authorization": f"Bearer {self.cfg['api_key']}"} if self.cfg.get("api_key") else {}
            j = await self._post(url, {"model": self.cfg.get("model", ""), "messages": msgs, "tools": tools,
                                       "max_tokens": max_tokens, "temperature": 0, "stream": False}, headers)
            m = (j.get("choices") or [{}])[0].get("message") or {}
        else:
            raise ModelError(f"{self.name}: tool calls need an ollama or openai model (llama-server), not {self.type}")
        ms = int((time.monotonic() - t0) * 1000)
        _use(self.name, True, 0, 0, ms)
        return {"tool_calls": m.get("tool_calls"), "text": m.get("content") or "", "ms": ms}

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
        opts = {"num_predict": max_tokens, "temperature": temperature}
        for k in ("num_gpu", "num_ctx", "num_thread"):      # num_gpu 0: on that computer's CPU, leaving its GPU free
            if self.cfg.get(k) is not None:
                opts[k] = int(self.cfg[k])
        body = {"model": self.cfg.get("model", ""), "messages": msgs, "stream": False, "think": False, "options": opts}
        if self.cfg.get("keep_alive"):
            body["keep_alive"] = self.cfg["keep_alive"]          # how long it stays loaded ("5m", "0", "-1")
        j = await self._post(url + "/api/chat", body)
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
    WATCH_SECONDS = 30          # how often the computers models run on are checked (a quick connect, no model call)

    def __init__(self, configs):
        self.session = None
        self.configs = configs or []
        self.models = {}
        self.hosts = {}         # "host:port" -> {"up", "since", "checked", "ms"}
        self._watch = None

    async def start(self, watch=True):
        self.session = aiohttp.ClientSession()
        self.models = {c["name"]: Model(c, self.session) for c in self.configs if c.get("name")}
        if "mock" not in self.models:
            self.models["mock"] = Model({"name": "mock", "type": "mock"}, self.session)
        if watch and any(m.endpoint() for m in self.models.values()):
            await self.probe_all()
            self._watch = asyncio.create_task(self._watch_loop())

    async def stop(self):
        if self._watch:
            self._watch.cancel()
        if self.session:
            await self.session.close()

    async def probe(self, host, port, timeout=2.0):
        """Is the computer there? A TCP connect only: cheap, and it doesn't load a model."""
        key = f"{host}:{port}"
        t0 = time.monotonic()
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
            w.close()
            up = True
        except Exception:  # noqa: BLE001 - asleep, off, restarting, wrong address
            up = False
        now = time.time()
        old = self.hosts.get(key)
        if old is None or old["up"] != up:
            if old is not None:
                log.info("model computer %s is %s", key, "back" if up else "unreachable (asleep, off or restarting)")
            self.hosts[key] = {"up": up, "since": now}
        self.hosts[key].update(checked=now, ms=int((time.monotonic() - t0) * 1000) if up else None)
        return up

    async def probe_all(self):
        eps = {m.endpoint() for m in self.models.values()} - {None}
        await asyncio.gather(*(self.probe(h, p) for h, p in eps))

    async def _watch_loop(self):
        while True:
            await asyncio.sleep(self.WATCH_SECONDS)
            try:
                await self.probe_all()
                if self.on_probe is not None:
                    await self.on_probe()
            except Exception as e:  # noqa: BLE001
                log.warning("model watch: %s", e)

    on_probe = None             # called after each round of checks (the away alert)

    def away_changes(self, sent, hours, now=None):
        """Which computers to tell about: ('away', key, since) once a computer has been away `hours`, ('back', key,
        since) when one that was told about answers again. `sent` (key -> since) is kept by the caller."""
        now = now or time.time()
        out = []
        for key, h in list(self.hosts.items()):
            if not h["up"] and now - h["since"] >= hours * 3600 and sent.get(key) != h["since"]:
                sent[key] = h["since"]
                out.append(("away", key, h["since"]))
            elif h["up"] and key in sent:
                sent.pop(key)
                out.append(("back", key, h["since"]))
        return out

    def on_host(self, key):
        """The model names on one computer ('host:port')."""
        return [m.name for m in self.models.values() if m.endpoint() and f"{m.endpoint()[0]}:{m.endpoint()[1]}" == key]

    def up(self, name):
        """True/False for a model whose computer is checked; None when it isn't (Home Assistant, cloud)."""
        m = self.models.get(name)
        ep = m.endpoint() if m else None
        h = self.hosts.get(f"{ep[0]}:{ep[1]}") if ep else None
        return None if h is None else h["up"]

    def mark(self, name, up):
        """A call just failed to connect (or worked): the computer's state now, without waiting for the next check."""
        m = self.models.get(name)
        ep = m.endpoint() if m else None
        if ep:
            key = f"{ep[0]}:{ep[1]}"
            if (self.hosts.get(key) or {}).get("up") != up:
                self.hosts[key] = {"up": up, "since": time.time(), "checked": time.time(), "ms": None}

    def get(self, name):
        if name not in self.models:
            raise ModelError(f"no model named {name!r}; have: {', '.join(self.models)}")
        return self.models[name]

    def list(self):
        out = []
        for m in self.models.values():
            d = m.describe()
            ep = m.endpoint()
            if ep:
                h = self.hosts.get(f"{ep[0]}:{ep[1]}") or {}
                d.update(host=f"{ep[0]}:{ep[1]}", up=h.get("up"), up_since=h.get("since"), checked=h.get("checked"))
            out.append(d)
        return out

    async def check_all(self):
        return await asyncio.gather(*(m.check() for m in self.models.values()))
