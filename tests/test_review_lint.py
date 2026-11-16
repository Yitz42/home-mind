"""Expert review (maintainability): a small static checker run over every Python file in the repo, so CI catches the
mistakes that only blow up on a rarely-run path. No installs needed (pyflakes isn't available everywhere).

  - undefined names (a NameError waiting on an error branch), including class-body names used from methods
  - two defs with the same name in one module or class (the first one is dead)
  - duplicate constant keys in a dict literal (the first value is silently lost)
  - statements after return/raise/continue/break in the same block (unreachable)
  - `from .mod import name` / `mod.name` on a sibling module that has no such name
  - bare `except:` (swallows KeyboardInterrupt and asyncio.CancelledError)

  python tests/test_review_lint.py
"""
import ast
import builtins
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PKG = os.path.join(ROOT, "home_mind", "rootfs", "app", "homemind")
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__package__", "__loader__",
                                 "__builtins__", "__path__", "__annotations__"}
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---- the checker ---------------------------------------------------------------------------------------------------
def _targets(node, out):
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            _targets(e, out)
    elif isinstance(node, ast.Starred):
        _targets(node.value, out)


def _bound(body, out):
    """Names bound directly in a scope (not inside nested functions, classes, lambdas or comprehensions)."""
    stack = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
            continue
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for sub in ast.walk(n):         # := in a comprehension binds in the enclosing scope
                if isinstance(sub, ast.NamedExpr):
                    _targets(sub.target, out)
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add("*" if a.name == "*" else (a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            out.add(n.rest)
        stack.extend(ast.iter_child_nodes(n))
    return out


def _args(a):
    s = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    s |= {x.arg for x in (a.vararg, a.kwarg) if x}
    return s


class _ClassScope(set):
    pass


class Checker:
    def __init__(self, path, src=None):
        self.path = path
        self.tree = ast.parse(src if src is not None else open(path, encoding="utf-8").read(), path)
        self.problems = []

    def say(self, line, msg):
        self.problems.append(f"{os.path.relpath(self.path, ROOT)}:{line}: {msg}")

    def run(self):
        mod = _bound(self.tree.body, set())
        mod |= {g for n in ast.walk(self.tree) if isinstance(n, ast.Global) for g in n.names}
        if "*" not in mod:
            for s in self.tree.body:
                self.visit(s, [mod])
        self.block(self.tree.body, "module")
        return self.problems

    def visit(self, n, envs):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for d in getattr(n, "decorator_list", []) + n.args.defaults + [x for x in n.args.kw_defaults if x]:
                self.visit(d, envs)
            for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs + [n.args.vararg, n.args.kwarg]:
                if a is not None and a.annotation is not None:
                    self.visit(a.annotation, envs)
            if getattr(n, "returns", None) is not None:
                self.visit(n.returns, envs)
            body = n.body if isinstance(n.body, list) else [n.body]
            inner = [e for e in envs if not isinstance(e, _ClassScope)] + [_args(n.args) | _bound(body, set())]
            for s in body:
                self.visit(s, inner)
            if isinstance(n.body, list):
                self.block(n.body, n.name)
            return
        if isinstance(n, ast.ClassDef):
            for d in n.decorator_list + n.bases + [k.value for k in n.keywords]:
                self.visit(d, envs)
            local = _ClassScope(_bound(n.body, set()))
            for s in n.body:
                self.visit(s, envs + [local])
            self.block(n.body, n.name)
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            local = set()
            for g in n.generators:
                _targets(g.target, local)
            inner = [e for e in envs if not isinstance(e, _ClassScope)] + [local]
            for i, g in enumerate(n.generators):
                self.visit(g.iter, envs if i == 0 else inner)     # the first iterable is evaluated outside
                for c in g.ifs:
                    self.visit(c, inner)
            for part in ((n.key, n.value) if isinstance(n, ast.DictComp) else (n.elt,)):
                self.visit(part, inner)
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id not in BUILTINS and not any(n.id in e for e in envs):
                self.say(n.lineno, f"undefined name {n.id!r}")
            return
        if isinstance(n, ast.Dict):
            seen = set()
            for k in n.keys:
                if isinstance(k, ast.Constant):
                    if k.value in seen:
                        self.say(k.lineno, f"duplicate dict key {k.value!r}")
                    seen.add(k.value)
        if isinstance(n, ast.ExceptHandler) and n.type is None:
            self.say(n.lineno, "bare except: (catches KeyboardInterrupt/CancelledError)")
        for field in ("body", "orelse", "finalbody", "handlers"):
            b = getattr(n, field, None)
            if isinstance(b, list) and b and isinstance(b[0], ast.stmt):
                self.block(b, type(n).__name__.lower())
        for c in ast.iter_child_nodes(n):
            self.visit(c, envs)

    def block(self, body, where):
        defs = {}
        for s in body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                decos = [ast.unparse(d) for d in getattr(s, "decorator_list", [])]
                if not any(d.endswith((".setter", ".deleter", ".register")) or "overload" in d for d in decos):
                    if s.name in defs:
                        self.say(s.lineno, f"{s.name!r} defined again in {where} (the one at line {defs[s.name]} is dead)")
                    defs[s.name] = s.lineno
        for i, s in enumerate(body[:-1]):
            if isinstance(s, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                self.say(body[i + 1].lineno, f"unreachable after {type(s).__name__.lower()} in {where}")
                break


def _top_names(tree):
    s = _bound(tree.body, set())
    s |= {g for n in ast.walk(tree) if isinstance(n, ast.Global) for g in n.names}
    return s


def sibling_problems(pkg):
    """`from .x import y` and `x.y` (x a sibling module imported with `from . import x`) where x has no y."""
    trees = {f[:-3]: ast.parse(open(os.path.join(pkg, f), encoding="utf-8").read())
             for f in os.listdir(pkg) if f.endswith(".py")}
    tops = {m: _top_names(t) for m, t in trees.items()}
    out = []
    for m, t in sorted(trees.items()):
        alias = {}
        for n in ast.walk(t):
            if isinstance(n, ast.ImportFrom) and n.level == 1:
                for a in n.names:
                    if n.module is None:
                        alias[a.asname or a.name] = a.name
                    elif n.module in tops and a.name != "*" and a.name not in tops[n.module] and a.name not in trees:
                        out.append(f"{m}.py:{n.lineno}: .{n.module} has no {a.name!r}")
        for n in ast.walk(t):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in alias
                    and alias[n.value.id] in tops and not n.attr.startswith("__")
                    and n.attr not in tops[alias[n.value.id]]):
                out.append(f"{m}.py:{n.lineno}: {alias[n.value.id]} has no {n.attr!r}")
    return out


# ---- the checker finds what it is meant to (so a clean run means something) ----------------------------------------
SAMPLE = '''
import os
def f(a):
    if a:
        y = 2
    return y + zz + os.sep + [q for q in range(3) if q > w][0]
class C:
    k = 3
    def m(self):
        return k
    def m(self):
        return 1
d = {"a": 1, "a": 2}
def g(x):
    for i in x:
        try:
            pass
        except:
            continue
            print(i)
    return 1
    print(2)
'''
got = "\n".join(Checker("sample.py", SAMPLE).run())
for want in ("undefined name 'zz'", "undefined name 'w'", "undefined name 'k'", "'m' defined again",
             "duplicate dict key 'a'", "unreachable after continue", "unreachable after return", "bare except"):
    check(f"checker finds: {want}", want in got, got)
check("checker: names bound later in a branch are not flagged", "undefined name 'y'" not in got, got)

# ---- the repo is clean -----------------------------------------------------------------------------------------------
files = []
for d, dirs, names in os.walk(ROOT):
    dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__")]
    files += [os.path.join(d, n) for n in names if n.endswith(".py")]
check(f"found the code ({len(files)} files)", len(files) > 50 and os.path.join(PKG, "store.py") in files)
problems = []
for p in sorted(files):
    try:
        problems += Checker(p).run()
    except SyntaxError as e:
        problems.append(f"{os.path.relpath(p, ROOT)}: syntax error {e}")
check("no undefined names, dead duplicate defs, duplicate keys, unreachable code or bare excepts",
      not problems, "\n  " + "\n  ".join(problems))
sib = sibling_problems(PKG)
check("every sibling-module name used exists", not sib, "\n  " + "\n  ".join(sib))

print(f"\n{len(FAILS)} failed" if FAILS else "\nALL OK")
sys.exit(1 if FAILS else 0)
