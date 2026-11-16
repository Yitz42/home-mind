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
        try:
            blob = vault.open_file(os.environ.get("HOME_MIND_BACKUP_PASSPHRASE") or getpass.getpass("passphrase: "), blob)
        except Exception:  # noqa: BLE001 - InvalidTag: the passphrase, or the file, isn't the one it was made with
            sys.exit("can't open it: wrong passphrase, or the file was changed or cut short")
    data = gzip.decompress(blob)
    if not data.startswith(b"SQLite format 3\0"):
        sys.exit("not a Home Mind database")
    with open(dst, "wb") as f:
        f.write(data)
    # the newest note's time comes from inside the (authenticated) backup, not the file's name: an older backup put
    # in place of a newer one shows here
    import sqlite3
    import time
    try:
        db = sqlite3.connect(dst)
        newest = db.execute("SELECT MAX(updated) FROM items").fetchone()[0]
        db.close()
    except sqlite3.Error:
        newest = None
    print("restored", dst, "- newest note", time.strftime("%Y-%m-%d %H:%M", time.localtime(newest)) if newest else "?")


if __name__ == "__main__":
    main()
