"""Tools for the think path (think.py): exact work a small model should never do in its head.

  calc(expr)       arithmetic, percentages and a few functions, by a checked expression tree (never eval)
  date_tool(text)  days between dates, a date plus or minus days/weeks/months, the weekday of a date, days until or
                   since a date, a clock time plus or minus minutes or hours, minutes between two times
  convert(text)    "3.4 lb to g": length, mass, volume (kitchen too), temperature, time, speed, area, data
  direct(q)        a whole question that one of the tools can answer by itself ("what's 17.5% of 368?", "how many
                   grams in 3.4 pounds?", "what day of the week is March 14 2027?"), answered by code with no model.
                   None unless the whole question is read with certainty: a word problem is never guessed at here.

Every tool returns text (a result or a short "can't read that: ..."), never raises, so the loop can hand the model
back whatever happened.
"""
import ast
import math
import operator
import re
import statistics
from datetime import date, datetime, timedelta

# ---- calc ------------------------------------------------------------------------------------------------------

MAX_EXPR = 300
MAX_NODES = 200
MAX_POW = 1000
MAX_ABS = 1e100


def _mean(*a):
    a = _flat(a)
    return statistics.fmean(a)


def _median(*a):
    return statistics.median(_flat(a))


def _flat(a):
    out = []
    for x in a:
        out += list(x) if isinstance(x, (list, tuple)) else [x]
    if not out:
        raise ValueError("needs at least one number")
    return out


def _fact(n):
    if n != int(n) or n < 0 or n > 170:
        raise ValueError("factorial needs a whole number from 0 to 170")
    return math.factorial(int(n))


def _log(x, b=10):
    return math.log(x, b)


def _round(x, n=0):
    n = int(n)
    r = round(x + (1e-12 if x >= 0 else -1e-12), n)       # 2.675 -> 2.68, as people round, not as floats do
    return int(r) if n <= 0 else r


FUNCS = {
    "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x), "abs": abs, "round": _round,
    "floor": math.floor, "ceil": math.ceil, "ln": math.log, "log": _log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
    "atan": math.atan, "sind": lambda d: math.sin(math.radians(d)), "cosd": lambda d: math.cos(math.radians(d)),
    "tand": lambda d: math.tan(math.radians(d)), "radians": math.radians, "degrees": math.degrees,
    "min": lambda *a: min(_flat(a)), "max": lambda *a: max(_flat(a)), "sum": lambda *a: sum(_flat(a)),
    "mean": _mean, "avg": _mean, "average": _mean, "median": _median, "factorial": _fact,
    "comb": lambda n, k: math.comb(int(n), int(k)), "perm": lambda n, k: math.perm(int(n), int(k)),
    "gcd": lambda *a: math.gcd(*(int(x) for x in _flat(a))), "lcm": lambda *a: math.lcm(*(int(x) for x in _flat(a))),
    "hypot": math.hypot,
}
CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
          ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
WORD_OPS = [(r"\bmultiplied by\b|\btimes\b", "*"), (r"\bdivided by\b|\bover\b", "/"), (r"\bplus\b", "+"),
            (r"\bminus\b", "-"), (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"), (r"\bmod(?:ulo)?\b", "%")]


def _prep(expr):
    s = (expr or "").strip()
    s = re.sub(r"\s*=\s*[^=]*$", "", s) if s.count("=") == 1 else s        # "14*23 = 322": the model's guess goes
    s = s.replace("×", "*").replace("·", "*").replace("÷", "/").replace("−", "-").replace("–", "-").replace("^", "**")
    s = s.replace("$", "").replace("€", "").replace("£", "").rstrip("?.! ")
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)                       # 1,234 -> 1234
    s = re.sub(r"\bsquare root of\s+", "sqrt ", s, flags=re.I)
    s = re.sub(r"\bcube root of\s+", "cbrt ", s, flags=re.I)
    s = re.sub(r"\b(sqrt|cbrt)\s+(\d+(?:\.\d+)?)", r"\1(\2)", s)
    for pat, rep in WORD_OPS:
        s = re.sub(pat, rep, s, flags=re.I)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s+", r"(\1/100)*", s, flags=re.I)   # 15% of 80
    s = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", s)                      # 15% alone
    s = re.sub(r"(?<=[\d)])\s*[xX]\s*(?=[\d(])", "*", s)                  # 3 x 4
    s = re.sub(r"(\d)\s*\(", r"\1*(", s)                                   # 2(3+4)
    s = re.sub(r"\)\s*(?=[\d(])", ")*", s)                                 # (2)(3)
    return s.strip()


def _ev(node, count):
    count[0] += 1
    if count[0] > MAX_NODES:
        raise ValueError("expression too long")
    if isinstance(node, ast.Expression):
        return _ev(node.body, count)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        if node.id.lower() in CONSTS:
            return CONSTS[node.id.lower()]
        raise ValueError(f"unknown name {node.id}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _ev(node.operand, count)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
        a, b = _ev(node.left, count), _ev(node.right, count)
        if isinstance(node.op, ast.Pow) and (abs(b) > MAX_POW or (abs(a) > 1e6 and abs(b) > 50)):
            raise ValueError("power too large")
        return BINOPS[type(node.op)](a, b)
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_ev(e, count) for e in node.elts]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        fn = FUNCS.get(node.func.id.lower())
        if fn is None:
            raise ValueError(f"unknown function {node.func.id}")
        return fn(*[_ev(a, count) for a in node.args])
    raise ValueError("only numbers, + - * / ** %, brackets and functions like sqrt(), round(), mean() can be used")


def fmt(v):
    """A number as people write it: 322, 64.4, 0.3333333333 (not 64.40000000000001)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        s = f"{v:.10g}"
        return s if "e" in s else s.rstrip("0").rstrip(".")
    return str(v)


def calc_value(expr):
    """The value of an expression, or raises ValueError with a short reason."""
    s = _prep(expr)
    if not s or len(s) > MAX_EXPR:
        raise ValueError("give a short arithmetic expression")
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError:
        raise ValueError(f"can't read {s!r} as arithmetic") from None
    try:
        v = _ev(tree, [0])
    except ZeroDivisionError:
        raise ValueError("division by zero") from None
    except (OverflowError, RecursionError):
        raise ValueError("number too large") from None
    except TypeError as e:
        raise ValueError(f"wrong use of a function: {e}") from None
    if isinstance(v, list):
        raise ValueError("that's a list, not one number")
    if isinstance(v, (int, float)) and abs(v) > MAX_ABS:
        raise ValueError("number too large")
    return v


def calc(expr):
    try:
        return fmt(calc_value(expr))
    except ValueError as e:
        return f"can't calculate that: {e}"
    except Exception as e:  # noqa: BLE001 - math domain errors and the like
        return f"can't calculate that: {e}"


# ---- dates and times --------------------------------------------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                           "september", "october", "november", "december"))}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
HOLIDAYS = {"christmas": (12, 25), "christmas day": (12, 25), "christmas eve": (12, 24), "new year's day": (1, 1),
            "new years day": (1, 1), "new year": (1, 1), "new year's": (1, 1), "new years": (1, 1),
            "new year's eve": (12, 31), "halloween": (10, 31), "valentine's day": (2, 14), "valentines day": (2, 14),
            "independence day": (7, 4), "the fourth of july": (7, 4), "fourth of july": (7, 4), "july 4th": (7, 4)}
NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
            "fifty": 50, "sixty": 60, "ninety": 90, "hundred": 100}
_D = (r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|(?:[a-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)|"
      r"(?:\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?[a-z]{3,9}\.?(?:,?\s+\d{4})?)|today|tomorrow|yesterday|"
      r"(?:next|this|last)\s+[a-z]+day|[a-z]+day|christmas(?: eve| day)?|new year'?s?(?: eve| day)?|halloween|"
      r"valentine'?s day|independence day|(?:the )?fourth of july|july 4th)")


def _num(s):
    s = (s or "").strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    return float(NUMWORDS[s]) if s in NUMWORDS else None


def _add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    last = [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(y, m, min(d.day, last[m - 1]))


def parse_date(s, today, future=False):
    """A date from words; future=True reads a date without a year as its next coming (for "until")."""
    t = (s or "").strip().lower().rstrip("?.!,").strip()
    t = re.sub(r"^(?:on|the)\s+", "", t)
    if t in ("today", "now"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "yesterday":
        return today - timedelta(days=1)
    if t in HOLIDAYS:
        m, d = HOLIDAYS[t]
        x = date(today.year, m, d)
        return x if not future or x >= today else date(today.year + 1, m, d)
    m = re.fullmatch(r"(next|this|last)?\s*([a-z]+day)", t)
    if m and m.group(2) in WEEKDAYS:
        wd = WEEKDAYS.index(m.group(2))
        if m.group(1) == "last":
            back = (today.weekday() - wd) % 7 or 7
            return today - timedelta(days=back)
        ahead = (wd - today.weekday()) % 7
        if m.group(1) == "next":
            ahead = ahead or 7
        return today + timedelta(days=ahead)
    try:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", t)               # US order: month/day/year
        if m:
            y = int(m.group(3))
            return date(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))
        m = re.fullmatch(r"([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", t) or \
            re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,9})\.?(?:,?\s+(\d{4}))?", t)
        if m:
            a, b, y = m.group(1), m.group(2), m.group(3)
            mon, day = (a, b) if not a.isdigit() else (b, a)
            if mon not in MONTHS:
                return None
            x = date(int(y) if y else today.year, MONTHS[mon], int(day))
            if not y and future and x < today:
                x = date(today.year + 1, MONTHS[mon], int(day))
            return x
    except ValueError:
        return None
    return None


def _weeks(n):
    w, d = divmod(abs(n), 7)
    if w and d:
        return f" ({w} week{'s' * (w != 1)} and {d} day{'s' * (d != 1)})"
    return f" ({w} week{'s' * (w != 1)})" if w and n >= 14 else ""


def _say(d):
    return f"{d.isoformat()} ({WEEKDAYS[d.weekday()].title()}, {d.strftime('%B')} {d.day}, {d.year})"


def _clock(s):
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", (s or "").strip().lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if not m.group(2) and not ap:
        return None
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


def _say_clock(mins, ampm=True):
    days, mins = divmod(int(round(mins)), 1440)
    h, m = divmod(mins, 60)
    s = f"{h:02d}:{m:02d}"
    if ampm:
        s += f" ({(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'})"
    if days:
        s += f", {abs(days)} day{'s' * (abs(days) != 1)} {'later' if days > 0 else 'earlier'}"
    return s


_UNIT = r"(minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)"
_N = r"(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|" \
     r"forty|fifty|sixty|ninety|hundred)"


def _shift(d, n, unit):
    u = unit.rstrip("s")
    if u in ("day",):
        return d + timedelta(days=n)
    if u == "week":
        return d + timedelta(weeks=n)
    if u == "month":
        return _add_months(d, int(n))
    if u == "year":
        return _add_months(d, int(n) * 12)
    return None


def date_tool(text, today=None):
    today = today or date.today()
    t = (text or "").strip().lower().replace("’", "'").rstrip("?.! ")
    t = re.sub(r"^(?:what(?:'s| is| was| will be)?|how many|calculate|compute|tell me|find)\s+", "", t)
    t = re.sub(r"^(?:the )?time (?:is it|will it be|was it|is|be)?\s*(?:at\s+)?", "", t)
    try:
        # clock times first ("9:40 + 135 minutes", "minutes between 9:40 and 11:55")
        m = re.fullmatch(r"(?:the time\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)\s*(\+|-|plus|minus)\s*" + _N +
                         r"\s*(minutes?|mins?|hours?|hrs?)", t) or \
            re.fullmatch(_N + r"\s*(minutes?|mins?|hours?|hrs?)\s+(after|before|from)\s+(\d{1,2}(?::\d{2})?\s*"
                                r"(?:am|pm|a\.m\.|p\.m\.)?)", t)
        if m:
            g = m.groups()
            if g[0] and re.match(r"\d", g[0]) and g[1] in ("+", "-", "plus", "minus"):
                c, sign, n, unit = _clock(g[0]), (-1 if g[1] in ("-", "minus") else 1), _num(g[2]), g[3]
            else:
                n, unit, sign, c = _num(g[0]), g[1], (-1 if g[2] == "before" else 1), _clock(g[3])
            if c is not None and n is not None:
                mins = n * (60 if unit.startswith("h") else 1)
                return _say_clock(c + sign * mins)
        m = re.fullmatch(r"(?:minutes|hours|time)\s+(?:between|from)\s+(.+?)\s+(?:and|to|until|till)\s+(.+)", t)
        if m and _clock(m.group(1)) is not None and _clock(m.group(2)) is not None:
            a, b = _clock(m.group(1)), _clock(m.group(2))
            diff = (b - a) % 1440
            h, mi = divmod(diff, 60)
            return f"{diff} minutes ({h} h {mi} min)"
        # how far apart two dates are
        m = re.fullmatch(r"(?:the\s+)?(?:number of\s+)?(days|weeks|months)?\s*(?:are there\s+)?(?:is it\s+)?(?:between|from)\s+"
                         r"(" + _D + r")\s+(?:and|to|until|till)\s+(" + _D + r")(?:\s+inclusive)?", t)
        if m:
            a, b = parse_date(m.group(2), today), parse_date(m.group(3), today)
            if a and b:
                n = (b - a).days
                incl = " (count both ends: " + str(abs(n) + 1) + ")" if "inclusive" in t else ""
                if m.group(1) == "weeks":
                    return f"{fmt(n / 7)} weeks ({n} days){incl}"
                if m.group(1) == "months":
                    mo = (b.year - a.year) * 12 + b.month - a.month - (1 if b.day < a.day else 0)
                    return f"{mo} whole months ({n} days){incl}"
                return f"{n} days{_weeks(n)}{incl}"
        m = re.fullmatch(r"(?:the\s+)?(?:number of\s+)?(days|weeks|sleeps)\s+(?:are there\s+|is it\s+|left\s+)?"
                         r"(until|till|to|before|since|after)\s+(" + _D + r")", t)
        if m:
            fut = m.group(2) in ("until", "till", "to", "before")
            d = parse_date(m.group(3), today, future=fut)
            if d:
                n = (d - today).days if fut else (today - d).days
                if m.group(1) == "weeks":
                    return f"{fmt(round(n / 7, 1))} weeks ({n} days) {'until' if fut else 'since'} {_say(d)}"
                return f"{n} days{_weeks(n)} {'until' if fut else 'since'} {_say(d)}"
        # a date moved by an amount
        m = re.fullmatch(r"(?:the\s+)?(?:date\s+)?(" + _D + r")\s*(\+|-|plus|minus)\s*" + _N + r"\s*" + _UNIT, t)
        if m:
            d, sign, n, unit = parse_date(m.group(1), today), m.group(2), _num(m.group(3)), m.group(4)
            if d and n is not None:
                r = _shift(d, -n if sign in ("-", "minus") else n, unit)
                if r:
                    return _say(r)
        m = re.fullmatch(r"(?:the\s+)?(?:date\s+|day\s+)?(?:it will be\s+|was it\s+)?" + _N + r"\s*" + _UNIT +
                         r"\s+(from|after|before|ago|later)(?:\s+(" + _D + r"))?(?:\s+be)?", t)
        if m:
            n, unit, rel, base = _num(m.group(1)), m.group(2), m.group(3), m.group(4)
            d = parse_date(base, today) if base else today
            if d and n is not None:
                r = _shift(d, -n if rel in ("before", "ago") else n, unit)
                if r:
                    return _say(r)
        # weekday of a date
        m = re.fullmatch(r"(?:the\s+)?(?:day of (?:the )?week|weekday|day)\s*(?:of|for|is|was|will)?\s*(?:it\s+)?"
                         r"(?:on\s+)?(" + _D + r")(?:\s+(?:be|fall on|on))?", t) or \
            re.fullmatch(r"(?:day of (?:the )?week|weekday)?\s*(?:is|was|will be|does)?\s*(" + _D + r")\s*(?:fall on|be|on)?",
                         t)
        if m:
            d = parse_date(m.group(1), today)
            if d:
                return _say(d)
        m = re.fullmatch(r"(?:the\s+)?date\s+(?:of\s+|is\s+|on\s+)?(" + _D + r")", t)
        if m:
            d = parse_date(m.group(1), today)
            if d:
                return _say(d)
    except (ValueError, OverflowError) as e:
        return f"can't work out that date: {e}"
    return ("can't read that. Try: \"days between 2026-11-26 and 2027-03-14\", \"2026-11-26 + 45 days\", "
            "\"weekday of 2027-03-14\", \"days until Christmas\", \"9:40 + 135 minutes\"")


# ---- units ------------------------------------------------------------------------------------------------------

# unit -> (kind, how many base units one of it is); base units: m, g, ml, s, m/s, m2, byte
UNITS = {}


def _u(kind, factor, *names):
    for n in names:
        UNITS[n] = (kind, factor)


_u("length", 0.001, "mm", "millimeter", "millimeters", "millimetre", "millimetres")
_u("length", 0.01, "cm", "centimeter", "centimeters", "centimetre", "centimetres")
_u("length", 1.0, "m", "meter", "meters", "metre", "metres")
_u("length", 1000.0, "km", "kilometer", "kilometers", "kilometre", "kilometres")
_u("length", 0.0254, "in", "inch", "inches", '"')
_u("length", 0.3048, "ft", "foot", "feet", "'")
_u("length", 0.9144, "yd", "yard", "yards")
_u("length", 1609.344, "mi", "mile", "miles")
_u("length", 1852.0, "nmi", "nautical mile", "nautical miles")
_u("mass", 0.001, "mg", "milligram", "milligrams")
_u("mass", 1.0, "g", "gram", "grams", "gm")
_u("mass", 1000.0, "kg", "kilogram", "kilograms", "kilo", "kilos")
_u("mass", 28.349523125, "oz", "ounce", "ounces")
_u("mass", 453.59237, "lb", "lbs", "pound", "pounds")
_u("mass", 6350.29318, "st", "stone", "stones")
_u("mass", 1e6, "t", "tonne", "tonnes", "metric ton", "metric tons")
_u("volume", 1.0, "ml", "milliliter", "milliliters", "millilitre", "millilitres", "cc")
_u("volume", 1000.0, "l", "liter", "liters", "litre", "litres")
_u("volume", 4.92892159375, "tsp", "teaspoon", "teaspoons")
_u("volume", 14.78676478125, "tbsp", "tablespoon", "tablespoons")
_u("volume", 29.5735295625, "fl oz", "floz", "fluid ounce", "fluid ounces")
_u("volume", 236.5882365, "cup", "cups")
_u("volume", 473.176473, "pt", "pint", "pints")
_u("volume", 946.352946, "qt", "quart", "quarts")
_u("volume", 3785.411784, "gal", "gallon", "gallons")
_u("time", 1.0, "s", "sec", "secs", "second", "seconds")
_u("time", 60.0, "min", "mins", "minute", "minutes")
_u("time", 3600.0, "h", "hr", "hrs", "hour", "hours")
_u("time", 86400.0, "day", "days")
_u("time", 604800.0, "week", "weeks")
_u("speed", 1.0, "m/s", "meters per second", "metres per second")
_u("speed", 1000 / 3600, "km/h", "kmh", "kph", "kilometers per hour", "kilometres per hour")
_u("speed", 1609.344 / 3600, "mph", "miles per hour")
_u("speed", 1852 / 3600, "kn", "knot", "knots")
_u("area", 1.0, "m2", "sq m", "square meter", "square meters", "square metre", "square metres")
_u("area", 0.09290304, "ft2", "sq ft", "square foot", "square feet")
_u("area", 4046.8564224, "acre", "acres")
_u("area", 10000.0, "ha", "hectare", "hectares")
_u("area", 1e6, "km2", "sq km", "square kilometer", "square kilometers")
_u("area", 2589988.110336, "mi2", "sq mi", "square mile", "square miles")
_u("data", 1.0, "b", "byte", "bytes")
_u("data", 1e3, "kb", "kilobyte", "kilobytes")
_u("data", 1e6, "mb", "megabyte", "megabytes")
_u("data", 1e9, "gb", "gigabyte", "gigabytes")
_u("data", 1e12, "tb", "terabyte", "terabytes")
TEMPS = {"c": "C", "°c": "C", "celsius": "C", "degrees c": "C", "degrees celsius": "C", "centigrade": "C",
         "f": "F", "°f": "F", "fahrenheit": "F", "degrees f": "F", "degrees fahrenheit": "F",
         "k": "K", "kelvin": "K", "kelvins": "K"}
_UNAMES = sorted(list(UNITS) + list(TEMPS), key=len, reverse=True)
_UPAT = "|".join(re.escape(u) for u in _UNAMES)


def _unit(s):
    s = re.sub(r"\s+", " ", (s or "").strip().lower().rstrip(".?!"))
    s = re.sub(r"^(?:degrees?|deg)\s+(?=[cfk]\b|celsius|fahrenheit|kelvin)", "degrees ", s)
    if s in TEMPS:
        return ("temp", TEMPS[s])
    return UNITS.get(s)


def _to_c(v, u):
    return v if u == "C" else (v - 32) * 5 / 9 if u == "F" else v - 273.15


def _from_c(v, u):
    return v if u == "C" else v * 9 / 5 + 32 if u == "F" else v + 273.15


def convert_value(n, a, b):
    """n of unit a in unit b, or raises ValueError."""
    ua, ub = _unit(a), _unit(b)
    if not ua or not ub:
        raise ValueError(f"unknown unit {a if not ua else b!r}")
    if ua[0] == "temp" or ub[0] == "temp":
        if ua[0] != ub[0]:
            raise ValueError("can't turn a temperature into another kind of unit")
        return _from_c(_to_c(n, ua[1]), ub[1])
    if ua[0] != ub[0]:
        raise ValueError(f"{a} is {ua[0]} and {b} is {ub[0]}: they can't be converted (a cup of flour has no one weight)")
    return n * ua[1] / ub[1]


def _round_sig(v):
    if v == 0:
        return "0"
    digits = max(0, 5 - int(math.floor(math.log10(abs(v)))) - 1) if abs(v) < 1e5 else 0
    return fmt(round(v, min(digits, 6)))


def convert(text):
    t = (text or "").strip().lower().replace("°", "° ").replace("  ", " ")
    t = t.replace("° c", "°c").replace("° f", "°f").replace("° ", "°")
    m = re.fullmatch(r"(?:convert\s+)?(-?\d[\d,]*(?:\.\d+)?)\s*(" + _UPAT + r")\s+(?:to|in|into|as|=)\s+(" + _UPAT + r")"
                     r"[?.!]?", t)
    if not m:
        return 'can\'t read that. Write it as "<number> <unit> to <unit>", e.g. "3.4 lb to g" or "350 F to C"'
    try:
        n = float(m.group(1).replace(",", ""))
        v = convert_value(n, m.group(2), m.group(3))
    except ValueError as e:
        return f"can't convert that: {e}"
    def name(u):
        return f"°{TEMPS[u]}" if u in TEMPS and TEMPS[u] != "K" else u
    return f"{fmt(n)} {name(m.group(2))} = {_round_sig(v)} {name(m.group(3))}"


# ---- whole questions answered by code ----------------------------------------------------------------------------

_LEAD = re.compile(r"^(?:hey |ok |okay |so )?(?:please )?(?:can you |could you )?(?:tell me |work out |figure out )?"
                   r"(?:what(?:'s| is| are| was| will be| would be)?|how much is|how much are|calculate|compute|"
                   r"evaluate|work out|solve)?\s*", re.I)
_HAS_OP = re.compile(r"\d\s*(?:[-+*/x×÷^%]|\*\*)\s*[\d(.]|\b(?:sqrt|cbrt|square root|cube root|factorial|mean|median|"
                     r"average|times|plus|minus|divided by|multiplied by|squared|cubed|log|ln|sin|cos|tan)\b|\d\s*%\s*of",
                     re.I)


def direct(question, today=None):
    """(answer, tool) when code alone can answer the whole question, else None."""
    q = (question or "").strip().replace("’", "'")
    if not q or len(q) > 200:
        return None
    ql = q.lower().rstrip("?.! ")
    # conversions: "convert 3.4 lb to g", "3.4 lb in grams", "how many grams in 3.4 pounds", "what is 350 F in C"
    m = re.fullmatch(r"(?:convert |what(?:'s| is) |how much is )?(-?\d[\d,]*(?:\.\d+)?)\s*(" + _UPAT + r")\s+"
                     r"(?:to|in|into|as)\s+(" + _UPAT + r")", ql)
    if m:
        r = convert(f"{m.group(1)} {m.group(2)} to {m.group(3)}")
        return (r, "convert") if not r.startswith("can't") else None
    m = re.fullmatch(r"how many (" + _UPAT + r") (?:are |is )?(?:there )?(?:in|is|are|make|to) (?:a |an |one )?"
                     r"(-?\d[\d,]*(?:\.\d+)?)?\s*(" + _UPAT + r")", ql)
    if m:
        n = m.group(2) or "1"
        r = convert(f"{n} {m.group(3)} to {m.group(1)}")
        return (r, "convert") if not r.startswith("can't") else None
    # dates and clock times
    if re.search(r"\b(?:days?|weeks?|months?|years?|weekday|day of the week|date|minutes?|hours?|sleeps)\b", ql) and \
            re.search(r"\b(?:until|till|since|between|from|after|before|ago|later|weekday|day of the week|what day|"
                      r"date|plus|minus)\b|[+-]", ql) and not re.search(r"\b(?:i|my|me|we|our|us)\b", ql):
        r = date_tool(ql, today)
        if not r.startswith("can't"):
            return r, "date"
    m = re.fullmatch(r"what day (?:of the week )?(?:is|was|will be|does|did) (.+?)(?: (?:be|fall on|fall|land on))?", ql)
    if m:
        r = date_tool(f"weekday of {m.group(1)}", today)
        if not r.startswith("can't"):
            return r, "date"
    # plain arithmetic: what's left after the lead-in must be an expression
    if _HAS_OP.search(ql):
        expr = _LEAD.sub("", ql, count=1).strip()
        expr = re.sub(r"^(?:the\s+)?(?:value of\s+)?", "", expr)
        expr = re.sub(r"\b(square|cube) root of\s+", lambda mm: "sqrt " if mm.group(1) == "square" else "cbrt ", expr)
        expr = re.sub(r"^(average|mean|median) of\s+(.+)$", lambda mm: f"{mm.group(1)}({mm.group(2)})", expr)
        expr = re.sub(r"\b(sqrt|cbrt|factorial|log|ln|sin|cos|tan)\s+of\s+(\d+(?:\.\d+)?)", r"\1(\2)", expr)
        expr = re.sub(r"\s+and\s+", ", ", expr) if re.match(r"(?:average|mean|median)\(", expr) else expr
        if re.search(r"[a-z]{2,}", re.sub(r"\b(?:sqrt|cbrt|factorial|mean|median|average|avg|log|ln|sin|cos|tan|"
                                           r"round|pi|times|plus|minus|divided by|multiplied by|squared|cubed|of|mod)\b",
                                           "", expr)):
            return None                 # words left over: a word problem, for a model
        try:
            v = calc_value(expr)
        except Exception:  # noqa: BLE001
            return None
        return fmt(v), "calc"
    return None
