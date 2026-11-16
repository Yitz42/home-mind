"""Restore an off-site Home Mind backup (#60).

  python tools/restore_backup.py home-mind-20261116-030000.hmbk mind.db       # asks for the passphrase
  python tools/restore_backup.py home-mind-20261116-030000.db.gz mind.db      # a plain folder copy

Then stop the add-on, put mind.db in its /data folder (replacing the old one), and start it again. Needs the
cryptography package (pip install cryptography) for encrypted backups.
"""
import getpass
import gzip
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, dst = sys.argv[1:]
    if os.path.exists(dst):
        sys.exit(f"{dst} exists; not overwriting")
    blob = open(src, "rb").read()
    if blob[:5] == b"HMBK1":
        from homemind import vault
        blob = vault.open_file(os.environ.get("HOME_MIND_BACKUP_PASSPHRASE") or getpass.getpass("passphrase: "), blob)
    with open(dst, "wb") as f:
        f.write(gzip.decompress(blob))
    print("restored", dst)


if __name__ == "__main__":
    main()
