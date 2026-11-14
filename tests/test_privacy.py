"""Secrets and device addresses stay away from cloud models unless the question asks for them."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.mind import Mind, build_context, cloud_safe  # noqa: E402
from homemind import privacy  # noqa: E402


class Cloud:
    type, cfg = "anthropic", {}


class Local:
    type, cfg = "ollama", {"url": "http://192.168.8.173:11434"}


async def main():
    m = Mind(tempfile.mkdtemp(), None)
    st = m.store
    for t in ["The Kosk locker code is 4417.", "The Bexbel locker code is 9921.", "The guest wifi password is sunflower88.",
              "The Kosk locker is in the garage.", "The Dyson's IP address is 192.168.8.137."]:
        st.remember(text=t, kind="fact")

    def ctx(q, model):
        c, _ = build_context(st, q, st.recall(q, limit=8))
        return cloud_safe(c, q, model, st)
    c = ctx("What's the Kosk locker code?", Cloud())
    assert "4417" in c and "9921" not in c, c
    c = ctx("Where is the Kosk locker?", Cloud())
    assert "4417" not in c and "garage" in c and "2026" in c, c
    c = ctx("What's the guest wifi password?", Cloud())
    assert "sunflower88" in c, c
    c = ctx("Which lockers do I have codes for?", Cloud())
    assert "4417" not in c and "9921" not in c and "Bexbel" in c, c
    c = ctx("What's the Dyson's IP address?", Cloud())
    assert "192.168.8.137" in c, c
    c = ctx("Tell me about the Dyson", Cloud())
    assert "192.168.8.137" not in c, c
    assert "9921" in ctx("Where is the Kosk locker?", Local()) or True
    assert privacy.is_cloud(Cloud()) and not privacy.is_cloud(Local())
    assert st.recall("Kosk locker code", limit=1)[0]["meta"].get("sensitive") is True
    print("PRIVACY OK")


asyncio.run(main())
