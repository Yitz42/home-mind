"""Sign the pack manifest (#28).

  python tools/sign_manifest.py keygen ~/.home-mind-pack.key     # once: makes a secret key file (keep it off the
                                                                 # repo) and prints the public key
  # put the public key in home_mind/rootfs/app/homemind/pack_keys.txt and release
  python tools/sign_manifest.py sign ~/.home-mind-pack.key packs/manifest.json    # writes packs/manifest.json.sig

Sign after every change to the manifest; Home Mind won't install packs from a manifest without a valid signature
once a key is in pack_keys.txt.
"""
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import ed25519  # noqa: E402


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "keygen":
        path = sys.argv[2]
        if os.path.exists(path):
            sys.exit(f"{path} exists; not overwriting a key")
        sk = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(sk.hex() + "\n")
        print("public key (put it in homemind/pack_keys.txt):", ed25519.public_key(sk).hex())
    elif len(sys.argv) >= 4 and sys.argv[1] == "sign":
        sk = bytes.fromhex(open(sys.argv[2]).read().strip())
        data = open(sys.argv[3], "rb").read()
        sig = ed25519.sign(sk, data)
        with open(sys.argv[3] + ".sig", "w") as f:
            f.write(sig.hex() + "\n")
        print("signed", sys.argv[3], "with", ed25519.public_key(sk).hex()[:16], "->", sys.argv[3] + ".sig")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
