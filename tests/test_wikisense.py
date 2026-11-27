"""Wikipedia by meaning, read with numpy alone: the WordPiece tokenizer, the BERT encoder and the Faiss IVF,SQ8 reader
against outputs made with transformers, torch and txtai on a tiny index (tests/data/wikisense, built by txtai with a
small random BERT standing in for e5-base)."""
import json
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "home_mind", "rootfs", "app"))
from homemind.wikisense import Encoder, IvfSq8, WikiSense, WordPiece  # noqa: E402

DATA = os.path.join(HERE, "data", "wikisense")
EXP = json.load(open(os.path.join(DATA, "expected.json")))


def test_tokenizer():
    wp = WordPiece(os.path.join(DATA, "model", "vocab.txt"))
    v = wp.vocab
    ids = wp.encode("query: Café, salmonella!")
    assert ids[0] == v["[CLS]"] and ids[-1] == v["[SEP]"]
    toks = [k for i in ids for k, j in v.items() if j == i]
    # lowercased, accent stripped, punctuation split, words in pieces (what BertTokenizer gives with this vocab)
    assert toks == ["[CLS]", "query", ":", "c", "##a", "##f", "##e", ",", "salmon", "##ella", "!", "[SEP]"], toks
    assert wp.encode("zzzz")[1] in (v["[UNK]"], v["z"])


def test_encoder_matches_transformers():
    enc = Encoder(os.path.join(DATA, "model"))
    for q, want in zip(EXP["queries"], EXP["vectors"]):
        got = enc.embed(q)
        assert np.abs(got - np.array(want, dtype=np.float32)).max() < 1e-4, q
        assert abs(np.linalg.norm(got) - 1) < 1e-5


def test_search_matches_txtai():
    ws = WikiSense(os.path.join(DATA, "index"), os.path.join(DATA, "model"))
    ws.nprobe = EXP["nprobe"]
    for q, want in EXP["search"].items():
        got = ws.search(q, k=4)
        assert [h["title"] for h in got] == want, (q, got)
        assert all(h["text"] for h in got)
        assert got[0]["score"] >= got[-1]["score"]


def test_ivf_header_and_bad_file():
    ix = IvfSq8(os.path.join(DATA, "index", "embeddings"))
    assert ix.nlist == 12 and ix.ntotal == 600 and ix.by_residual and ix.code_size == ix.d
    assert int(ix.sizes.sum()) == 600
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"nope" + b"\0" * 64)
    try:
        IvfSq8(f.name)
        raise AssertionError("bad file accepted")
    except ValueError:
        pass
    finally:
        os.remove(f.name)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
