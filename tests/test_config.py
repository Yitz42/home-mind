"""The app's options and schema in config.yaml, checked the way the Supervisor checks them before an update: every
default must be valid for its type. A `url` option can't default to "" (the Supervisor refuses to install the update:
0.25.0 did that with homebox_url, paperless_url and web_search_url), and every option needs a schema entry."""
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
APPS = sorted(d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "config.yaml")))
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if not cond and extra != "" else ""))
    if not cond:
        FAILS.append(name)


def valid(kind, v):
    """Is v a valid value for a Supervisor schema type (the parts this app uses)?"""
    if isinstance(kind, list):
        return isinstance(v, list) and all(valid(kind[0], x) for x in v)
    if isinstance(kind, dict):
        return isinstance(v, dict) and all(valid(kind[k], x) for k, x in v.items() if k in kind)
    k = str(kind).rstrip("?")
    if k == "url":
        return isinstance(v, str) and re.match(r"^[a-z][a-z0-9+.-]*://\S+$", v) is not None
    if k in ("str", "password", "email") or k.startswith("match("):
        return isinstance(v, str)
    if k == "bool":
        return isinstance(v, bool)
    if k.startswith("int"):
        return isinstance(v, int) and not isinstance(v, bool)
    if k.startswith("float"):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if k.startswith("list("):
        return v in k[5:-1].split("|")
    return True


check("found the apps in the repository", "home_mind" in APPS, APPS)
for app in APPS:
    cfg = yaml.safe_load(open(os.path.join(ROOT, app, "config.yaml")))
    opts, schema = cfg.get("options") or {}, cfg.get("schema") or {}
    missing = [k for k in opts if k not in schema]
    check(f"{app}: every option has a schema entry", not missing, missing)
    bad = [(k, opts[k], schema[k]) for k in opts if k in schema and not valid(schema[k], opts[k])]
    check(f"{app}: every default is valid for its type (the Supervisor checks this before an update)", not bad, bad)
    url_empty = [k for k, t in schema.items() if str(t).rstrip("?") == "url" and opts.get(k, None) == ""]
    check(f"{app}: no url option defaults to an empty string", not url_empty, url_empty)
    for k in ("name", "version", "slug", "description", "arch"):
        check(f"{app}: has {k}", k in cfg)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("CONFIG OK")
