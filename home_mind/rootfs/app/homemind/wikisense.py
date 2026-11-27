"""Meaning-based search over Wikipedia, with numpy only (no faiss, torch or onnx in the image).

The index is NeuML's txtai-wikipedia: the first paragraph of every English article, embedded with intfloat/e5-base
into a Faiss IVF,SQ8 index (inner product on normalised vectors), plus a SQLite file of the paragraphs. This module
reads those files directly:

  Encoder     e5-base (a BERT-base model) run in numpy from model.safetensors, with BERT's WordPiece tokenizer from
              vocab.txt; mean pooling over the tokens, then normalised. A query is prefixed "query: " as e5 expects.
  IvfSq8      the Faiss file: the coarse centroids and the 8-bit scalar quantizer are read into memory, the inverted
              lists stay on disk (np.memmap) and only the probed lists are read for a search.
  Sections    the txtai SQLite store: indexid -> article title and its first paragraph.

A search embeds the question, takes the nprobe nearest cells, decodes their codes and returns the best article titles
with a score. Knowledge uses the titles to pull the full article from the Kiwix ZIM it already has.
"""
import json
import os
import time
import re
import sqlite3
import struct
import unicodedata

import numpy as np

# ---- WordPiece (BERT uncased) -------------------------------------------------------------------------------------

def _is_punct(ch):
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(cp):
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2A6DF or 0x2A700 <= cp <= 0x2B73F
            or 0x2B740 <= cp <= 0x2B81F or 0x2B820 <= cp <= 0x2CEAF or 0xF900 <= cp <= 0xFAFF or 0x2F800 <= cp <= 0x2FA1F)


class WordPiece:
    def __init__(self, vocab_path):
        with open(vocab_path, encoding="utf-8") as f:
            self.vocab = {line.rstrip("\n"): i for i, line in enumerate(f)}
        self.unk = self.vocab["[UNK]"]
        self.cls = self.vocab["[CLS]"]
        self.sep = self.vocab["[SEP]"]

    def _basic(self, text):
        out = []
        for ch in text:
            cp = ord(ch)
            if cp == 0 or cp == 0xFFFD or (unicodedata.category(ch).startswith("C") and ch not in "\t\n\r"):
                continue
            if _is_cjk(cp):
                out.append(f" {ch} ")
            elif ch in " \t\n\r" or unicodedata.category(ch) == "Zs":
                out.append(" ")
            else:
                out.append(ch)
        words = []
        for tok in "".join(out).lower().split():
            tok = "".join(c for c in unicodedata.normalize("NFD", tok) if unicodedata.category(c) != "Mn")
            cur = ""
            for ch in tok:
                if _is_punct(ch):
                    if cur:
                        words.append(cur)
                    words.append(ch)
                    cur = ""
                else:
                    cur += ch
            if cur:
                words.append(cur)
        return words

    def _piece(self, word):
        if len(word) > 100:
            return [self.unk]
        ids, start = [], 0
        while start < len(word):
            end, cur = len(word), None
            while start < end:
                sub = word[start:end] if start == 0 else "##" + word[start:end]
                if sub in self.vocab:
                    cur = self.vocab[sub]
                    break
                end -= 1
            if cur is None:
                return [self.unk]
            ids.append(cur)
            start = end
        return ids

    def encode(self, text, max_len=128):
        ids = [i for w in self._basic(text) for i in self._piece(w)]
        return [self.cls] + ids[:max_len - 2] + [self.sep]


# ---- safetensors ---------------------------------------------------------------------------------------------------

def read_safetensors(path):
    """name -> float32 array, memory-mapped (only F32/F16/BF16 tensors)."""
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        head = json.loads(f.read(n))
    base = 8 + n
    out = {}
    for name, meta in head.items():
        if name == "__metadata__":
            continue
        a, b = meta["data_offsets"]
        dt = meta["dtype"]
        shape = tuple(meta["shape"])
        if dt == "F32":
            out[name] = np.memmap(path, dtype="<f4", mode="r", offset=base + a, shape=shape)
        elif dt == "F16":
            out[name] = np.memmap(path, dtype="<f2", mode="r", offset=base + a, shape=shape).astype(np.float32)
        elif dt == "BF16":
            raw = np.memmap(path, dtype="<u2", mode="r", offset=base + a, shape=shape)
            out[name] = (raw.astype(np.uint32) << 16).view(np.float32)
    return out


# ---- BERT in numpy -------------------------------------------------------------------------------------------------

def _gelu(x):
    # BERT's exact gelu (erf); numpy has no erf, so the Abramowitz-Stegun 7.1.26 form (error < 1.5e-7)
    z = x / np.sqrt(2.0)
    s = np.sign(z)
    a = np.abs(z)
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * \
        np.exp(-a * a)
    return 0.5 * x * (1.0 + s * y)


def _ln(x, g, b, eps=1e-12):
    m = x.mean(-1, keepdims=True)
    v = ((x - m) ** 2).mean(-1, keepdims=True)
    return (x - m) / np.sqrt(v + eps) * g + b


class Encoder:
    """e5-base (BertModel) forward pass for one text at a time; returns the normalised mean-pooled vector."""

    def __init__(self, model_dir, heads=12, layers=12):
        try:
            with open(os.path.join(model_dir, "config.json")) as f:
                cfg = json.load(f)
            heads, layers = cfg.get("num_attention_heads", heads), cfg.get("num_hidden_layers", layers)
        except (OSError, ValueError):
            pass
        self.tok = WordPiece(os.path.join(model_dir, "vocab.txt"))
        w = read_safetensors(os.path.join(model_dir, "model.safetensors"))
        p = "bert." if any(k.startswith("bert.") for k in w) else ""
        self.w = {k[len(p):] if k.startswith(p) else k: v for k, v in w.items()}
        self.heads = heads
        self.layers = layers

    def _g(self, name):
        return np.asarray(self.w[name], dtype=np.float32)

    def embed(self, text):
        ids = np.array(self.tok.encode(text), dtype=np.int64)
        n = len(ids)
        g = self._g
        x = np.asarray(self.w["embeddings.word_embeddings.weight"][ids], dtype=np.float32)
        x = x + g("embeddings.position_embeddings.weight")[:n] + g("embeddings.token_type_embeddings.weight")[0]
        x = _ln(x, g("embeddings.LayerNorm.weight"), g("embeddings.LayerNorm.bias"))
        h = self.heads
        dk = x.shape[1] // h
        for i in range(self.layers):
            L = f"encoder.layer.{i}."
            q = x @ g(L + "attention.self.query.weight").T + g(L + "attention.self.query.bias")
            k = x @ g(L + "attention.self.key.weight").T + g(L + "attention.self.key.bias")
            v = x @ g(L + "attention.self.value.weight").T + g(L + "attention.self.value.bias")
            q, k, v = (t.reshape(n, h, dk).transpose(1, 0, 2) for t in (q, k, v))
            s = q @ k.transpose(0, 2, 1) / np.sqrt(dk)
            s = np.exp(s - s.max(-1, keepdims=True))
            s /= s.sum(-1, keepdims=True)
            a = (s @ v).transpose(1, 0, 2).reshape(n, h * dk)
            a = a @ g(L + "attention.output.dense.weight").T + g(L + "attention.output.dense.bias")
            x = _ln(a + x, g(L + "attention.output.LayerNorm.weight"), g(L + "attention.output.LayerNorm.bias"))
            m = _gelu(x @ g(L + "intermediate.dense.weight").T + g(L + "intermediate.dense.bias"))
            m = m @ g(L + "output.dense.weight").T + g(L + "output.dense.bias")
            x = _ln(m + x, g(L + "output.LayerNorm.weight"), g(L + "output.LayerNorm.bias"))
        v = x.mean(0)
        return (v / (np.linalg.norm(v) or 1.0)).astype(np.float32)


# ---- the Faiss IVF,SQ8 file ----------------------------------------------------------------------------------------

class IvfSq8:
    """IndexIVFScalarQuantizer (fourcc IwSq) with QT_8bit codes and an IndexFlatIP / IndexFlatL2 quantizer."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self._parse(f)

    @staticmethod
    def _vec(f, dtype):
        n = struct.unpack("<Q", f.read(8))[0]
        return np.frombuffer(f.read(n * np.dtype(dtype).itemsize), dtype=dtype)

    def _header(self, f):
        d, ntotal, _a, _b, trained, metric = struct.unpack("<iqqqBi", f.read(33))
        if metric > 1:
            f.read(4)                                   # metric_arg
        return d, ntotal, metric

    def _parse(self, f):
        if f.read(4) != b"IwSq":
            raise ValueError("not a Faiss IVF scalar-quantizer index")
        self.d, self.ntotal, self.metric = self._header(f)
        self.nlist, self.nprobe_saved = struct.unpack("<QQ", f.read(16))
        qf = f.read(4)
        if qf not in (b"IxFI", b"IxF2"):
            raise ValueError(f"unexpected quantizer {qf!r}")
        qd, qn, _m = self._header(f)
        n = struct.unpack("<Q", f.read(8))[0]           # Faiss writes the flat codes' length in floats
        if n == qn * qd:
            raw = f.read(n * 4)
        elif n == qn * qd * 4:                          # (or in bytes)
            raw = f.read(n)
        else:
            raise ValueError("quantizer size mismatch")
        self.centroids = np.frombuffer(raw, dtype="<f4").reshape(qn, qd).copy()
        self.l2_quantizer = qf == b"IxF2"
        # direct map
        f.read(1)
        self._vec(f, np.int64)
        # ScalarQuantizer
        qtype, _rangestat, _arg = struct.unpack("<iif", f.read(12))
        sq_d, sq_cs = struct.unpack("<QQ", f.read(16))
        trained = self._vec(f, "<f4")
        if qtype != 0 or sq_cs != self.d:
            raise ValueError(f"only 8-bit scalar quantizers are read (qtype {qtype})")
        self.vmin = trained[:self.d].astype(np.float32)
        self.vdiff = trained[self.d:2 * self.d].astype(np.float32)
        self.code_size, self.by_residual = struct.unpack("<QB", f.read(9))
        # inverted lists
        if f.read(4) != b"ilar":
            raise ValueError("unexpected inverted-list format")
        nlist, cs = struct.unpack("<QQ", f.read(16))
        kind = f.read(4)
        if kind == b"full":
            sizes = self._vec(f, np.uint64).astype(np.int64)
        elif kind == b"sprs":
            pairs = self._vec(f, np.uint64).astype(np.int64)
            sizes = np.zeros(nlist, dtype=np.int64)
            sizes[pairs[0::2]] = pairs[1::2]
        else:
            raise ValueError(f"unexpected list sizes {kind!r}")
        pos = f.tell()
        self.lists = []                                  # (codes offset, ids offset, n)
        for n in sizes:
            n = int(n)
            if n:
                self.lists.append((pos, pos + n * cs, n))
                pos += n * cs + n * 8
            else:
                self.lists.append((pos, pos, 0))
        self.sizes = sizes

    def search(self, q, k=10, nprobe=48):
        q = np.asarray(q, dtype=np.float32)
        if self.l2_quantizer:
            cs = -((self.centroids - q) ** 2).sum(1)
        else:
            cs = self.centroids @ q
        probe = np.argsort(-cs)[:min(nprobe, self.nlist)]
        scale = self.vdiff / 255.0
        base_q = float(q @ (self.vmin + 0.5 * scale))
        qs = q * scale
        best_s, best_i = [], []
        mm = np.memmap(self.path, dtype=np.uint8, mode="r")
        for c in probe:
            off, ioff, n = self.lists[c]
            if not n:
                continue
            codes = np.asarray(mm[off:off + n * self.code_size]).reshape(n, self.code_size)
            ids = np.asarray(mm[ioff:ioff + n * 8]).view("<i8")
            s = codes.astype(np.float32) @ qs + base_q
            if self.by_residual:
                s += cs[c]
            if n > k:
                top = np.argpartition(-s, k)[:k]
                s, ids = s[top], ids[top]
            best_s.append(s)
            best_i.append(ids)
        if not best_s:
            return []
        s = np.concatenate(best_s)
        i = np.concatenate(best_i)
        order = np.argsort(-s)[:k]
        return [(int(i[j]), float(s[j])) for j in order]


# ---- everything together -------------------------------------------------------------------------------------------

class WikiSense:
    """index_dir holds the txtai index (config.json, embeddings, documents); model_dir holds e5-base (vocab.txt,
    model.safetensors)."""

    def __init__(self, index_dir, model_dir):
        with open(os.path.join(index_dir, "config.json")) as f:
            self.config = json.load(f)
        self.prefix = (self.config.get("instructions") or {}).get("query", "query: ")
        self.ann = IvfSq8(os.path.join(index_dir, "embeddings"))
        self.db_path = os.path.join(index_dir, "documents")
        self.encoder = Encoder(model_dir)
        nlist = self.ann.nlist
        self.nprobe = max(6, min(64, round(nlist / 32)))

    def search(self, question, k=5, nprobe=None):
        t0 = time.time()
        q = self.encoder.embed(self.prefix + (question or ""))
        t1 = time.time()
        hits = self.ann.search(q, k=k, nprobe=nprobe or self.nprobe)
        t2 = time.time()
        out = []
        if hits:
            db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                for idx, score in hits:
                    row = db.execute("SELECT id, text FROM sections WHERE indexid = ?", (idx,)).fetchone()
                    if row:
                        out.append({"title": row[0], "text": re.sub(r"\s+", " ", row[1] or "").strip(), "score": score})
            finally:
                db.close()
        self.last_ms = {"encode": round((t1 - t0) * 1000), "index": round((t2 - t1) * 1000),
                        "text": round((time.time() - t2) * 1000)}
        return out
