"""Science data: exact numbers and statements from structured sources, kept on the Home Assistant machine.

    "what's the density of aluminum?"        -> aluminium [Wikidata]: density 2.7 g/cm³ (at 20 °C)
    "specific heat of water"                  -> water [Wikidata]: specific heat capacity 4.184 J/(g·K) ...
    "speed of light"                          -> speed of light in vacuum [NIST CODATA]: 299792458 m s^-1 (exact)
    "state the pythagorean theorem"           -> Pythagoras's Theorem [ProofWiki]: Let △ABC be a right triangle ...
    "band gap of Si"                          -> Si [Materials Project mp-149]: band gap 0.61 eV ...

Sources (each fetched by this machine, then compiled into knowledge/science.db):
  NIST CODATA      the fundamental physical constants (one text file, public domain).
  Wikidata         a science subset, CC0: chemical elements, compounds and materials with a Wikipedia article and their
                   measured properties (density, melting and boiling point, heat capacity, conductivity, moduli...),
                   physics quantities with their defining formulas, units with their SI value, and the names that
                   point at ProofWiki theorems. Property ids are looked up by their English label when building.
  ProofWiki        theorems, definitions and axioms (CC BY-SA 3.0) from the published NaturalProofs copy on Zenodo,
                   not by crawling ProofWiki (its robots.txt asks automated readers not to use its API).
  NIST WebBook     phase-change and thermochemistry tables for the most common compounds (CAS numbers from
                   Wikidata), one page per compound, 5 seconds apart as the site's robots.txt asks, inside the
                   download window. Saved for use here only.
  Materials Project computed properties of stable inorganic materials (CC BY 4.0), only with the user's own API key
                   (option materials_project_key).

Raw downloads stay in knowledge/science_raw so the database can be rebuilt (a new version of this file) without
downloading again. Questions are matched by names (labels and aliases, chemical formulas like NaCl, CAS numbers)
and property words; nothing is matched by a model and nothing leaves the house at question time.
"""
import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from html.parser import HTMLParser

log = logging.getLogger("homemind.science")

SCIENCE_VERSION = 1          # bump when compile() changes: the database is rebuilt from the raw files
REFRESH_DAYS = 180           # Wikidata and CODATA are fetched again after this
CODATA_URL = "https://physics.nist.gov/cuu/Constants/Table/allascii.txt"
WDQS = "https://query.wikidata.org/sparql"
NATURALPROOFS_URL = "https://zenodo.org/records/4902289/files/naturalproofs_proofwiki.json?download=1"
WEBBOOK_URL = "https://webbook.nist.gov/cgi/cbook.cgi?ID=C{id}&Units=SI&Mask=7"
WEBBOOK_MAX = 1500           # compounds, most-linked first
WEBBOOK_DELAY = 5.0          # seconds between pages (the site's Crawl-delay)
MP_URL = "https://api.materialsproject.org/materials/summary/"
MP_FIELDS = ("material_id,formula_pretty,density,band_gap,energy_above_hull,formation_energy_per_atom,is_metal,"
             "symmetry,bulk_modulus,shear_modulus,theoretical")
UA = "HomeMind/1.0 (self-hosted Home Assistant add-on; downloads reference data once for offline answers)"
DETAIL_BATCH = 300

# ---- what is taken from Wikidata ---------------------------------------------------------------------------------
# (name used here, English labels to look the property up by, id used when no label matches, restriction)
WD_QUANTITY = [
    ("density", ["density"], "P2054", ""),
    ("melting point", ["melting point"], "P2101", ""),
    ("boiling point", ["boiling point"], "P2102", ""),
    ("specific heat capacity", ["specific heat capacity"], "P2056", ""),
    ("thermal conductivity", ["thermal conductivity"], "P2068", ""),
    ("flash point", ["flash point"], "P2128", ""),
    ("autoignition temperature", ["autoignition temperature"], "P2199", ""),
    ("refractive index", ["refractive index"], "P1109", ""),
    ("molar mass", ["mass"], "P2067", "chem"),
    ("atomic number", ["atomic number"], "P1086", ""),
    ("electronegativity", ["electronegativity"], "P1108", ""),
    ("ionization energy", ["ionization energy"], "P2260", ""),
    ("enthalpy of vaporization", ["enthalpy of vaporization"], "P2116", ""),
    ("enthalpy of fusion", ["enthalpy of fusion"], "", ""),
    ("standard enthalpy of formation", ["standard enthalpy of formation"], "", ""),
    ("heat of combustion", ["combustion enthalpy", "heat of combustion", "enthalpy of combustion"], "P2117", ""),
    ("sublimation temperature", ["sublimation temperature"], "P2113", ""),
    ("critical temperature", ["critical temperature"], "", ""),
    ("critical pressure", ["critical pressure"], "", ""),
    ("vapor pressure", ["vapor pressure", "vapour pressure"], "", ""),
    ("solubility", ["solubility"], "P2177", ""),
    ("pKa", ["acid dissociation constant"], "P1117", ""),
    ("Young's modulus", ["Young's modulus", "Young modulus"], "", ""),
    ("shear modulus", ["shear modulus"], "", ""),
    ("bulk modulus", ["bulk modulus"], "", ""),
    ("tensile strength", ["tensile strength", "ultimate tensile strength"], "", ""),
    ("yield strength", ["yield strength"], "", ""),
    ("Poisson's ratio", ["Poisson's ratio", "Poisson ratio"], "", ""),
    ("electrical resistivity", ["electrical resistivity"], "", ""),
    ("electrical conductivity", ["electrical conductivity"], "", ""),
    ("thermal expansion coefficient", ["thermal expansion coefficient", "coefficient of thermal expansion",
                                       "coefficient of linear thermal expansion", "linear thermal expansion coefficient"],
     "", ""),
    ("Mohs hardness", ["Mohs' hardness", "Mohs hardness"], "", ""),
    ("speed of sound", ["speed of sound"], "", ""),
    ("viscosity", ["dynamic viscosity", "viscosity"], "", ""),
]
WD_TEXT = [               # string or math properties, fetched whole
    ("defining formula", ["defining formula"], "P2534"),
    ("ProofWiki ID", ["ProofWiki ID"], "P6781"),
]
WD_EXTRA = [("chemical formula", ["chemical formula"], "P274"), ("CAS number", ["CAS Registry Number"], "P231"),
            ("conversion to SI unit", ["conversion to SI unit"], "P2370"), ("temperature", ["temperature"], "P2076"),
            ("phase of matter", ["phase of matter"], "P515"), ("instance of", ["instance of"], "P31")]

UNIT_SHORT = {
    "kelvin": "K", "degree celsius": "°C", "degree fahrenheit": "°F", "gram per cubic centimetre": "g/cm³",
    "kilogram per cubic metre": "kg/m³", "gram per litre": "g/L", "gram per millilitre": "g/mL",
    "joule per kilogram kelvin": "J/(kg·K)", "joule per gram kelvin": "J/(g·K)", "joule per mole kelvin": "J/(mol·K)",
    "joule per kilogram per kelvin": "J/(kg·K)", "joule per gram per kelvin": "J/(g·K)",
    "watt per metre kelvin": "W/(m·K)", "watt per metre per kelvin": "W/(m·K)", "pascal": "Pa", "kilopascal": "kPa",
    "megapascal": "MPa", "gigapascal": "GPa", "hectopascal": "hPa", "bar": "bar", "atmosphere": "atm",
    "millimetre of mercury": "mmHg", "joule per mole": "J/mol", "kilojoule per mole": "kJ/mol",
    "kilojoule per kilogram": "kJ/kg", "megajoule per kilogram": "MJ/kg", "joule per gram": "J/g", "electronvolt": "eV",
    "gram per mole": "g/mol", "kilogram per mole": "kg/mol", "dalton": "Da", "atomic mass unit": "u",
    "metre per second": "m/s", "ohm metre": "Ω·m", "ohm-metre": "Ω·m", "siemens per metre": "S/m",
    "millipascal second": "mPa·s", "millipascal-second": "mPa·s", "pascal second": "Pa·s", "pascal-second": "Pa·s",
    "centipoise": "cP", "gram per 100 gram of solvent": "g/100 g", "per kelvin": "1/K", "reciprocal kelvin": "1/K",
    "metre": "m", "kilogram": "kg", "gram": "g", "second": "s", "joule": "J", "watt": "W", "newton": "N",
}
NON_UNITS = {"1", "", "unit of measurement"}

# ---- the WebBook's table symbols ----------------------------------------------------------------------------------
WB_NAMES = {
    "Tboil": "boiling point", "Tfus": "melting point", "Ttriple": "triple point temperature",
    "Ptriple": "triple point pressure", "Tc": "critical temperature", "Pc": "critical pressure",
    "Vc": "critical volume", "ρc": "critical density", "ΔvapH°": "enthalpy of vaporization",
    "ΔvapH": "enthalpy of vaporization", "ΔfusH": "enthalpy of fusion", "ΔfusH°": "enthalpy of fusion",
    "ΔsubH": "enthalpy of sublimation", "ΔsubH°": "enthalpy of sublimation", "ΔfusS": "entropy of fusion",
    "ΔvapS": "entropy of vaporization", "ΔfH°gas": "standard enthalpy of formation (gas)",
    "ΔfH°liquid": "standard enthalpy of formation (liquid)", "ΔfH°solid": "standard enthalpy of formation (solid)",
    "ΔcH°gas": "heat of combustion (gas)", "ΔcH°liquid": "heat of combustion (liquid)",
    "ΔcH°solid": "heat of combustion (solid)", "S°gas": "standard entropy (gas)",
    "S°liquid": "standard entropy (liquid)", "S°solid,1bar": "standard entropy (solid)",
    "S°solid": "standard entropy (solid)", "Cp,gas": "molar heat capacity (gas)",
    "Cp,liquid": "molar heat capacity (liquid)", "Cp,solid": "molar heat capacity (solid)",
}

# ---- question side ------------------------------------------------------------------------------------------------
# a property word in the question -> the fact names it asks for (a fact matches when its name starts with one)
PROP_CUES = [
    (r"\bdens(?:e|ity|ities)\b|\bhow heavy\b|\bweighs? per\b", ["density", "critical density"]),
    (r"\bmelt(?:s|ing)?\b|\bfreez(?:e|es|ing) point\b|\bfusion\b", ["melting point", "enthalpy of fusion"]),
    (r"\bboil(?:s|ing)?\b", ["boiling point"]),
    (r"\bspecific heat\b|\bheat capacit(?:y|ies)\b|\bcp\b", ["specific heat capacity", "molar heat capacity"]),
    (r"\bthermal(?:ly)? conduct\w*\b|\bconducts? heat\b|\bheat conduct\w*\b", ["thermal conductivity"]),
    (r"\bflash ?point\b", ["flash point"]),
    (r"\bauto-?ignition\b|\bignit(?:e|es|ion) temperature\b", ["autoignition temperature"]),
    (r"\brefractive\b|\brefraction\b|\bindex of refraction\b", ["refractive index"]),
    (r"\bmolar mass\b|\bmolecular (?:weight|mass)\b|\batomic (?:weight|mass)\b|\bformula weight\b", ["molar mass"]),
    (r"\batomic number\b|\bproton number\b", ["atomic number"]),
    (r"\belectronegativ\w*\b", ["electronegativity"]),
    (r"\bionization energ\w*\b|\bionisation energ\w*\b", ["ionization energy"]),
    (r"\b(?:enthalpy|heat) of vapo(?:u)?ri[sz]ation\b|\blatent heat\b", ["enthalpy of vaporization",
                                                                         "enthalpy of fusion"]),
    (r"\b(?:enthalpy|heat) of fusion\b", ["enthalpy of fusion"]),
    (r"\b(?:enthalpy|heat) of formation\b", ["standard enthalpy of formation"]),
    (r"\b(?:enthalpy|heat) of combustion\b|\bheating value\b|\benergy content\b", ["heat of combustion"]),
    (r"\bsublim\w*\b", ["sublimation temperature", "enthalpy of sublimation"]),
    (r"\bcritical (?:temperature|point)\b", ["critical temperature"]),
    (r"\bcritical (?:pressure|point)\b", ["critical pressure"]),
    (r"\btriple point\b", ["triple point"]),
    (r"\bvapou?r pressure\b", ["vapor pressure"]),
    (r"\bsolub\w*\b|\bdissolve\b", ["solubility"]),
    (r"\bpka\b|\bacid(?:ity| dissociation)\b", ["pKa"]),
    (r"\byoung'?s modulus\b|\belastic modulus\b|\bmodulus of elasticity\b|\bstiff(?:ness)?\b",
     ["Young's modulus"]),
    (r"\bshear modulus\b|\bmodulus of rigidity\b", ["shear modulus"]),
    (r"\bbulk modulus\b|\bcompressib\w*\b", ["bulk modulus"]),
    (r"\btensile\b|\bultimate strength\b", ["tensile strength"]),
    (r"\byield (?:strength|stress|point)\b", ["yield strength"]),
    (r"\bpoisson\b", ["Poisson's ratio"]),
    (r"\bresistiv\w*\b|\bresistance\b", ["electrical resistivity"]),
    (r"\belectrical(?:ly)? conduct\w*\b|\bconducts? electricity\b", ["electrical conductivity"]),
    (r"\bthermal expansion\b|\bexpan(?:d|ds|sion) when heated\b|\bexpansion coefficient\b",
     ["thermal expansion coefficient"]),
    (r"\bhardness\b|\bhow hard\b|\bmohs\b", ["Mohs hardness"]),
    (r"\bspeed of sound\b|\bsound speed\b", ["speed of sound"]),
    (r"\bviscos\w*\b", ["viscosity"]),
    (r"\bentropy\b", ["standard entropy", "entropy of"]),
    (r"\bband ?gap\b", ["band gap"]),
    (r"\bformation energy\b|\bstabl(?:e|ility)\b|\benergy above hull\b", ["formation energy", "energy above hull"]),
    (r"\bcrystal\b|\bspace group\b|\blattice\b", ["crystal structure"]),
    (r"\bformula\b|\bequation\b|\bdefin\w*\b", ["defining formula", "chemical formula"]),
    (r"\bconvert\w*\b|\bconversion\b|\bhow many\b|\bequals?\b|\bin (?:si|metric)\b|\bsi units?\b", ["in SI units"]),
    (r"\bvalue of\b|\bconstant\b", ["value"]),
]
PROP_RX = [(re.compile(rx, re.I), props) for rx, props in PROP_CUES]
MATH_CUE = re.compile(r"\b(?:theorem|lemma|corollary|prove|proof|proofs|axiom|axioms|definition|define|defined|"
                      r"identity|inequality|conjecture|criterion|postulate|what does .{3,60} (?:state|say))\b", re.I)
THEOREM_WORDS = re.compile(r"\b(?:theorem|lemma|law|formula|identity|inequality|principle|rule|conjecture|"
                           r"criterion|test|postulate|equation|corollary|axiom)\b", re.I)
STOP = set("the a an of in on at to for is are was were be what who when where which how why does do did can could "
           "and or with from by about as it its that this than then there their into much many tell me give show "
           "please explain find get value i you we my your our state says say whats what's".split())
CUE_WORDS = set("theorem lemma corollary prove proof proofs axiom axioms definition define defined identity "
                "inequality conjecture criterion postulate statement formula equation".split())
COMMON = STOP | set("one two three per not no yes all some any more most less very just also only like make use "
                    "used using thing things way part type kind".split())
BRIT = {"aluminium": "aluminum", "sulphur": "sulfur", "caesium": "cesium", "sulphate": "sulfate",
        "sulphide": "sulfide", "sulphuric": "sulfuric", "sulphurous": "sulfurous", "vapour": "vapor",
        "litre": "liter", "metre": "meter", "centimetre": "centimeter", "millimetre": "millimeter",
        "kilometre": "kilometer", "colour": "color", "fibre": "fiber", "ageing": "aging"}
ARTICLES = {"a", "an", "the"}
ELEMENTS = set("H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb "
               "Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf "
               "Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh "
               "Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og".split())
AMBIGUOUS_SYMBOLS = set("In As At Be He No Am Es Pa Po Re Ho Hf Mt Md Ds Ts Og Bi Ga Ca Co Cu Ne Ni Sc".split())
SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
FORMULA_TOKEN = re.compile(r"(?<![\w])(?:[A-Z][a-z]?\d*|\(|\)\d*){2,}(?![\w])|(?<![\w])[A-Z][a-z]?\d+(?![\w])")
CARD_ORDER = ["value", "defining formula", "density", "melting point", "boiling point", "specific heat capacity",
              "molar heat capacity", "thermal conductivity", "Young's modulus", "tensile strength", "molar mass",
              "atomic number", "chemical formula", "band gap", "in SI units"]
CONSTANT_ALIASES = {
    "speed of light in vacuum": ["speed of light", "light speed", "c"],
    "newtonian constant of gravitation": ["gravitational constant", "universal gravitational constant",
                                          "newton gravitational constant", "big g", "constant of gravitation"],
    "planck constant": ["planck constant"],
    "reduced planck constant": ["h bar", "hbar", "dirac constant"],
    "elementary charge": ["charge of electron", "electron charge", "charge of proton", "proton charge"],
    "avogadro constant": ["avogadro number", "avogadros number"],
    "boltzmann constant": ["boltzmann constant"],
    "molar gas constant": ["gas constant", "ideal gas constant", "universal gas constant"],
    "stefan-boltzmann constant": ["stefan boltzmann constant"],
    "standard acceleration of gravity": ["acceleration due to gravity", "gravitational acceleration",
                                         "standard gravity", "acceleration of gravity", "g force constant"],
    "vacuum electric permittivity": ["permittivity of free space", "vacuum permittivity", "electric constant",
                                     "epsilon naught", "epsilon zero"],
    "vacuum mag. permeability": ["permeability of free space", "vacuum permeability", "magnetic constant", "mu naught"],
    "fine-structure constant": ["fine structure constant", "alpha constant"],
    "standard atmosphere": ["atmospheric pressure at sea level", "one atmosphere", "standard pressure"],
    "faraday constant": ["faraday constant"],
    "rydberg constant": ["rydberg constant"],
    "bohr radius": ["bohr radius"],
    "atomic mass constant": ["atomic mass unit", "dalton", "unified atomic mass unit"],
}


def norm(s):
    """Lower case, no accents, no possessive 's, no articles, British spellings as American, words joined by one space."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("’", "'")
    s = re.sub(r"'s\b", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(BRIT.get(w, w) for w in s.split() if w not in ARTICLES)


def formula_key(f, strict=False):
    """A chemical formula as sorted element counts ('Al₂O₃' and 'O3Al2' -> 'Al2O3'); '' if it isn't one.
    strict (a word in a question): needs a digit, a lower-case letter or 3+ letters, and two element groups or a digit,
    so 'CO', 'Co', 'US' or 'He' as a word aren't read as formulas."""
    t = (f or "").translate(SUBSCRIPTS).replace(" ", "")
    if not t or len(t) > 40 or not re.fullmatch(r"(?:[A-Z][a-z]?\d*|\(|\)\d*)+", t):
        return ""
    if strict:
        groups = re.findall(r"[A-Z][a-z]?", t)
        if not (re.search(r"\d", t) or (len(groups) >= 2 and (re.search(r"[a-z]", t) or len(t) >= 3))):
            return ""
    counts, stack, i = {}, [{}], 0
    toks = re.findall(r"[A-Z][a-z]?\d*|\(|\)\d*", t)
    for tok in toks:
        if tok == "(":
            stack.append({})
        elif tok.startswith(")"):
            if len(stack) < 2:
                return ""
            n = int(tok[1:] or 1)
            inner = stack.pop()
            for e, c in inner.items():
                stack[-1][e] = stack[-1].get(e, 0) + c * n
        else:
            m = re.match(r"([A-Z][a-z]?)(\d*)", tok)
            e, n = m.group(1), int(m.group(2) or 1)
            if e not in ELEMENTS:
                return ""
            stack[-1][e] = stack[-1].get(e, 0) + n
        i += 1
    if len(stack) != 1:
        return ""
    counts = stack[0]
    return "".join(f"{e}{n if n > 1 else ''}" for e, n in sorted(counts.items()))


def _num(text):
    m = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", str(text or ""))
    try:
        return float(m.group(0)) if m else None
    except ValueError:
        return None


def _amount(a):
    """Wikidata amounts come as '+2.7' or '+1.0E-5'."""
    s = str(a or "").strip()
    if s.startswith("+"):
        s = s[1:]
    if re.fullmatch(r"-?\d+\.0+", s):
        s = s.split(".")[0]
    return s


def unit_short(label):
    lab = (label or "").strip()
    if lab.lower() in NON_UNITS or re.fullmatch(r"Q\d+", lab):
        return ""
    return UNIT_SHORT.get(lab.lower(), lab)


def _qid(uri):
    m = re.search(r"/(Q\d+|P\d+)$", str(uri or ""))
    return m.group(1) if m else ""


def _b(row, k):
    v = row.get(k)
    return v.get("value", "") if isinstance(v, dict) else ""


# ---- parsing the raw files ----------------------------------------------------------------------------------------
def parse_codata(text):
    """allascii.txt -> [{name, value, uncertainty, unit, exact}]"""
    out, started = [], False
    for line in (text or "").splitlines():
        if not started:
            if line.startswith("----"):
                started = True
            continue
        if not line.strip():
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 2:
            continue
        name, value = parts[0], parts[1]
        unc = parts[2] if len(parts) > 2 else ""
        unit = parts[3] if len(parts) > 3 else ""
        if unc and not re.search(r"\d|exact", unc):     # no uncertainty column: the third part is the unit
            unit, unc = unc, ""
        val = re.sub(r"\s+", "", value).replace("...", "")
        val = re.sub(r"e", "e", val)
        out.append({"name": name, "value": val, "uncertainty": re.sub(r"\s+", "", unc), "unit": unit,
                    "exact": "exact" in unc})
    return out


class _Tables(HTMLParser):
    """Every <table> as rows of (cell text, is_header); <sub>/<sup> text joins the word ('T<sub>boil</sub>' -> Tboil)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._stack, self.title, self._in_title = [], [], "", False
        self.text = []
        self._cell = None
        self._row = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = ([], tag == "th")
        elif tag == "title":
            self._in_title = True
        elif tag in ("br", "li", "p", "div", "h1", "h2", "h3"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append((re.sub(r"\s+", " ", "".join(self._cell[0])).strip(), self._cell[1]))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            if self._row:
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._cell is not None:
            self._cell[0].append(data)
        self.text.append(data)


def parse_webbook(page):
    """A WebBook compound page -> {name, formula, mw, facts: [(prop, value, cond)]} or None when not found."""
    p = _Tables()
    try:
        p.feed(page if isinstance(page, str) else page.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    title = re.sub(r"\s+", " ", p.title).strip()
    if not title or re.search(r"not found|error", title, re.I):
        return None
    text = re.sub(r"[ \t]+", " ", "".join(p.text))
    formula = re.search(r"Formula\s*:\s*([A-Za-z0-9()\[\]·.+\-]+)", text)
    mw = re.search(r"Molecular weight\s*:\s*([\d.]+)", text)
    facts, per = [], {}
    for tbl in p.tables:
        if not tbl:
            continue
        head = [c for c, h in tbl[0]]
        if len(head) < 2 or not all(h for c, h in tbl[0]):
            continue
        rows = [[c for c, h in r] for r in tbl[1:]]
        h0 = head[0].lower()
        if h0 == "quantity" and len(head) >= 3 and head[1].lower() == "value":
            for r in rows:
                if len(r) < 3 or not r[0] or not r[1]:
                    continue
                sym = r[0].replace(" ", "")
                prop = WB_NAMES.get(sym, sym)
                if per.get(prop, 0) >= 2:
                    continue
                per[prop] = per.get(prop, 0) + 1
                facts.append((prop, f"{r[1]} {r[2]}".strip(), ""))
            continue
        m = re.match(r"^(.+?)\s*\(([^)]+)\)$", head[0])
        tcol = next((i for i, c in enumerate(head) if c.lower().startswith("temperature")), None)
        if not m or h0.startswith("temperature") or tcol is None:
            continue                                      # Shomate and Antoine tables: coefficients, not values
        sym, unit = m.group(1).replace(" ", ""), m.group(2)
        prop = WB_NAMES.get(sym, sym)
        for r in rows[:2]:
            if len(r) <= tcol or not r[0]:
                continue
            facts.append((prop, f"{r[0]} {unit}".strip(), f"at {r[tcol]} K" if r[tcol] else ""))
    return {"name": title, "formula": formula.group(1) if formula else "", "mw": mw.group(1) if mw else "",
            "facts": facts}


def _templates(t):
    """Replace each {{...}} template (braces counted, so LaTeX like {i = 1} inside is fine): an eqn line becomes
    '$l o r$', anything else goes."""
    out, i = [], 0
    while True:
        j = t.find("{{", i)
        if j < 0:
            out.append(t[i:])
            break
        out.append(t[i:j])
        depth, k = 0, j
        while k < len(t):
            if t.startswith("{{", k) or t[k] == "{":
                step = 2 if t.startswith("{{", k) else 1
                depth += step
                k += step
            elif t[k] == "}":
                depth -= 1
                k += 1
                if depth <= 0:
                    break
            else:
                k += 1
        inner = t[j + 2:k - 2] if k - j >= 4 else ""
        parts, d, cur = [], 0, ""
        for ch in inner:
            if ch == "{":
                d += 1
            elif ch == "}":
                d -= 1
            if ch == "|" and d == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        if parts[0].strip().lower() == "eqn":
            kv = {}
            for p_ in parts[1:]:
                if "=" in p_:
                    key, val = p_.split("=", 1)
                    kv[key.strip().lower()] = val.strip()
            eq = " ".join(x for x in (kv.get("l", ""), kv.get("o", "=" if kv.get("r") else ""), kv.get("r", "")) if x)
            out.append(f" ${eq}$ " if eq else " ")
        else:
            out.append(" ")
        i = k
    return "".join(out)


def clean_wikitext(lines):
    """NaturalProofs contents (wikitext lines) -> readable text that keeps the $LaTeX$."""
    if isinstance(lines, str):
        lines = lines.splitlines()
    out = []
    for ln in lines or []:
        t = str(ln)
        t = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", t)
        t = _templates(t)
        t = re.sub(r"'{2,}", "", t)
        t = re.sub(r"<[^>]+>", "", html.unescape(t))
        t = re.sub(r"^[:*#]+\s*", "", t.strip())
        if t:
            out.append(t)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def naturalproofs_entries(data):
    """Every theorem, definition and other page in the NaturalProofs file, however it is nested:
    [{title, kind, text}]. Proofs are skipped (statements only)."""
    out = []

    def walk(x, kind_hint=""):
        if isinstance(x, dict):
            if "title" in x and "contents" in x and not isinstance(x.get("title"), (dict, list)):
                kind = str(x.get("type") or kind_hint or "").lower()
                out.append({"title": str(x["title"]), "kind": kind, "text": clean_wikitext(x["contents"])})
                return
            for k, v in x.items():
                if k in ("proofs", "retrieval_examples", "splits", "refs", "ref_ids"):
                    continue
                walk(v, k if k in ("theorems", "definitions", "others") else kind_hint)
        elif isinstance(x, list):
            for v in x:
                walk(v, kind_hint)
    walk(data)
    return out


def _pw_kind(title, kind):
    k = (kind or "").lower()
    if title.startswith("Definition:") or "definition" in k:
        return "definition"
    if title.startswith("Axiom:") or "axiom" in k:
        return "axiom"
    if "theorem" in k or k == "theorems" or not k:
        return "theorem"
    return "theorem" if not title.split(":")[0] in ("Mathematician", "Book", "Symbol") else "other"


def _pw_label(title):
    return re.sub(r"^(?:Definition|Axiom):", "", title).replace("_", " ").strip()


# ---- the database -------------------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE subjects(id INTEGER PRIMARY KEY, key TEXT UNIQUE, label TEXT, kind TEXT, descr TEXT, rank REAL,
                      source TEXT, fkey TEXT);
CREATE TABLE names(norm TEXT, sid INTEGER);
CREATE TABLE facts(sid INTEGER, prop TEXT, value TEXT, num REAL, cond TEXT, source TEXT);
CREATE TABLE docs(sid INTEGER PRIMARY KEY, title TEXT, body TEXT);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
"""
INDEXES = """
CREATE INDEX names_norm ON names(norm);
CREATE INDEX facts_sid ON facts(sid);
CREATE INDEX subjects_fkey ON subjects(fkey);
"""


class Compiler:
    def __init__(self, path):
        self.path = path
        if os.path.exists(path):
            os.remove(path)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.keys = {}
        self.counts = {}
        try:
            self.db.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, tokenize='porter unicode61')")
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    def subject(self, key, label, kind, descr="", rank=0.0, source="", fkey=""):
        sid = self.keys.get(key)
        if sid:
            if fkey or descr:
                self.db.execute("UPDATE subjects SET fkey=COALESCE(NULLIF(fkey,''), ?), "
                                "descr=COALESCE(NULLIF(descr,''), ?) WHERE id=?", (fkey, descr, sid))
            return sid
        cur = self.db.execute("INSERT INTO subjects(key,label,kind,descr,rank,source,fkey) VALUES(?,?,?,?,?,?,?)",
                              (key, label, kind, descr, rank, source, fkey))
        self.keys[key] = cur.lastrowid
        return cur.lastrowid

    def name(self, sid, text, raw=False):
        n = text if raw else norm(text)
        if not n or len(n) > 90:
            return
        if not raw:
            words = n.split()
            if (len(words) == 1 and (len(n) < 3 or n in COMMON)) or n.isdigit() or all(w in COMMON for w in words):
                return
        self.db.execute("INSERT INTO names(norm,sid) VALUES(?,?)", (n, sid))

    def fact(self, sid, prop, value, cond="", source=""):
        if value in (None, ""):
            return
        self.db.execute("INSERT INTO facts(sid,prop,value,num,cond,source) VALUES(?,?,?,?,?,?)",
                        (sid, prop, str(value), _num(value), cond or "", source))
        self.counts[source] = self.counts.get(source, 0) + 1

    def doc(self, sid, title, body):
        self.db.execute("INSERT OR REPLACE INTO docs(sid,title,body) VALUES(?,?,?)", (sid, title, body))
        if self.fts:
            self.db.execute("INSERT INTO docs_fts(rowid,title,body) VALUES(?,?,?)", (sid, title, body))

    def finish(self, meta):
        self.db.executescript(INDEXES)
        # one subject per name is enough for most; keep the names table small and the best ranks first
        self.db.execute("DELETE FROM names WHERE rowid NOT IN (SELECT MIN(rowid) FROM names GROUP BY norm, sid)")
        for k, v in meta.items():
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v)))
        self.db.commit()
        self.db.execute("VACUUM")
        self.db.close()


def _kind_from(kinds, label):
    k = (kinds or "").lower()
    if "chemical element" in k:
        return "element"
    if "unit of" in k or "unit" == k:
        return "unit"
    if "physical constant" in k:
        return "constant"
    if "alloy" in k or "material" in k or "mineral" in k or "rock" in k:
        return "material"
    if "chemical compound" in k or "chemical substance" in k or "chemical entity" in k or "molecule" in k:
        return "compound"
    if "quantity" in k or "physical law" in k or "equation" in k:
        return "quantity"
    return "thing"


def compile_raw(raw, path, have_mp=False):
    """Build science.db at path from the raw downloads in raw/. Returns counts by source."""
    c = Compiler(path)
    report = {}
    # -- Wikidata subjects (labels, aliases, formula, CAS) ----------------------------------------------------------
    wd = os.path.join(raw, "wikidata")
    details = {}
    if os.path.isdir(wd):
        for fn in sorted(os.listdir(wd)):
            if fn.startswith("details_") and fn.endswith(".json"):
                for row in _load_rows(os.path.join(wd, fn)):
                    q = _qid(_b(row, "item"))
                    if q:
                        details[q] = row
    qs_rank = {}

    def wd_subject(q, label="", descr="", rank=0.0):
        d = details.get(q, {})
        lab = _b(d, "label") or label or q
        if lab == q:
            return None
        kind = _kind_from(_b(d, "kinds"), lab)
        fk = formula_key(_b(d, "formula"))
        if not fk and kind == "element":              # an element's symbol is its formula (links Materials Project)
            fk = next((a for a in (_b(d, "aliases") or "").split("|") if re.fullmatch(r"[A-Z][a-z]?", a)
                       and a in ELEMENTS), "")
        sid = c.subject("wd:" + q, lab, kind, _b(d, "desc") or descr, float(_b(d, "sl") or rank or 0),
                        "Wikidata " + q, fk)
        if sid not in qs_rank:
            qs_rank[sid] = True
            c.name(sid, lab)
            for a in (_b(d, "aliases") or "").split("|"):
                if a and not re.fullmatch(r"[A-Z][a-z]?", a):          # element symbols aren't names
                    c.name(sid, a)
            if fk:
                c.name(sid, "f:" + fk, raw=True)
                c.fact(sid, "chemical formula", _b(d, "formula").translate(SUBSCRIPTS), source="Wikidata")
            cas = _b(d, "cas")
            if cas:
                c.name(sid, "cas:" + cas, raw=True)
        return sid

    n_wd = 0
    if os.path.isdir(wd):
        for canon, labels, pid, restrict in WD_QUANTITY:
            files = sorted(os.path.join(wd, fn) for fn in os.listdir(wd)
                           if re.fullmatch("q_" + re.escape(_slug(canon)) + r"(?:_\d{4})?\.json", fn))
            if not files:
                continue
            seen = {}
            for row in (r for f in files for r in _load_rows(f)):
                q = _qid(_b(row, "item"))
                sid = wd_subject(q, _b(row, "itemLabel"), _b(row, "itemDescription"), _num(_b(row, "sl")) or 0)
                if not sid or seen.get(sid, 0) >= 3:
                    continue
                amt = _amount(_b(row, "amount"))
                unit = unit_short(_b(row, "unitLabel"))
                if canon == "molar mass":
                    d_ = details.get(q, {})
                    if unit not in ("Da", "u", "g/mol", "") or not (formula_key(_b(d_, "formula")) or
                                                                   "chemical element" in (_b(d_, "kinds") or "")):
                        continue                 # the mass of a planet or a person isn't a molar mass
                    unit = "g/mol"
                cond = []
                if _b(row, "tamt"):
                    cond.append(f"at {_amount(_b(row, 'tamt'))} {unit_short(_b(row, 'tunitLabel'))}".strip())
                if _b(row, "phaseLabel"):
                    cond.append(_b(row, "phaseLabel"))
                val = f"{amt} {unit}".strip()
                dup = c.db.execute("SELECT 1 FROM facts WHERE sid=? AND prop=? AND value=? AND cond=?",
                                   (sid, canon, val, ", ".join(cond))).fetchone()
                if dup:
                    continue
                seen[sid] = seen.get(sid, 0) + 1
                c.fact(sid, canon, val, ", ".join(cond), "Wikidata")
                n_wd += 1
        f = os.path.join(wd, "t_defining_formula.json")
        for row in _load_rows(f) if os.path.exists(f) else []:
            q = _qid(_b(row, "item"))
            sid = wd_subject(q, _b(row, "itemLabel"), _b(row, "itemDescription"), _num(_b(row, "sl")) or 0)
            if sid:
                val = re.sub(r"\s+", " ", _b(row, "val")).strip()
                if val and not c.db.execute("SELECT 1 FROM facts WHERE sid=? AND prop='defining formula' AND value=?",
                                            (sid, val)).fetchone():
                    c.fact(sid, "defining formula", val, source="Wikidata")
                    n_wd += 1
        f = os.path.join(wd, "units.json")
        for row in _load_rows(f) if os.path.exists(f) else []:
            q = _qid(_b(row, "item"))
            sid = wd_subject(q, _b(row, "itemLabel"), _b(row, "itemDescription"), _num(_b(row, "sl")) or 0)
            si = _b(row, "siLabel")
            if sid and si:
                c.db.execute("UPDATE subjects SET kind='unit' WHERE id=?", (sid,))
                c.fact(sid, "in SI units", f"1 {c.db.execute('SELECT label FROM subjects WHERE id=?', (sid,)).fetchone()[0]}"
                       f" = {_amount(_b(row, 'amount'))} {si}", source="Wikidata")
                n_wd += 1
    report["wikidata"] = n_wd

    # -- ProofWiki (NaturalProofs) ----------------------------------------------------------------------------------
    pw_alias = {}
    f = os.path.join(wd, "t_proofwiki_id.json")
    for row in _load_rows(f) if os.path.exists(f) else []:
        q = _qid(_b(row, "item"))
        names = [_b(row, "itemLabel")] + (_b(details.get(q, {}), "aliases") or "").split("|")
        pw_alias.setdefault(norm(_b(row, "val").replace("_", " ")), []).extend(x for x in names if x)
    n_pw = 0
    npf = os.path.join(raw, "naturalproofs_proofwiki.json")
    if os.path.exists(npf):
        try:
            with open(npf, encoding="utf-8") as fh:
                data = json.load(fh)
            entries = naturalproofs_entries(data)
            del data
        except (OSError, ValueError) as e:
            log.warning("naturalproofs not read: %s", e)
            entries = []
        for e in entries:
            title = e["title"]
            if not e["text"] or title.split(":")[0] in ("Mathematician", "Book", "Symbol", "Category", "Help"):
                continue
            kind = _pw_kind(title, e["kind"])
            label = _pw_label(title)
            sid = c.subject("pw:" + title, label, kind, "", 0.0, "ProofWiki")
            body = e["text"][:2500]
            c.doc(sid, label, body)
            n = norm(label)
            if kind == "theorem" and len(n.split()) >= 2 and THEOREM_WORDS.search(label):
                c.name(sid, label)
            for a in pw_alias.get(norm(title.replace("_", " ")), []):
                if len(norm(a).split()) >= 2:
                    c.name(sid, a)
            n_pw += 1
    report["proofwiki"] = n_pw

    # -- NIST CODATA ------------------------------------------------------------------------------------------------
    n_cd = 0
    f = os.path.join(raw, "codata.txt")
    if os.path.exists(f):
        with open(f, encoding="utf-8", errors="replace") as fh:
            rows = parse_codata(fh.read())
        for r in rows:
            sid = c.subject("codata:" + r["name"], r["name"], "constant", "fundamental physical constant", 10.0,
                            "NIST CODATA")
            val = r["value"] + (" " + r["unit"] if r["unit"] else "")
            val += " (exact)" if r["exact"] else (f" (standard uncertainty {r['uncertainty']})" if r["uncertainty"]
                                                  else "")
            c.fact(sid, "value", val, source="NIST CODATA")
            low = r["name"].lower()
            c.name(sid, low)
            if low.endswith(" in vacuum"):
                c.name(sid, low[:-10])
            m = re.match(r"^(.+) mass$", low)
            if m and len(m.group(1).split()) <= 2:
                c.name(sid, "mass of " + m.group(1))
            for a in CONSTANT_ALIASES.get(low, []):
                if len(norm(a)) >= 2:
                    c.name(sid, a, raw=len(norm(a)) < 3)
            n_cd += 1
    report["codata"] = n_cd

    # -- NIST WebBook -----------------------------------------------------------------------------------------------
    n_wb = 0
    wbdir = os.path.join(raw, "webbook")
    if os.path.isdir(wbdir):
        for fn in sorted(os.listdir(wbdir)):
            if not fn.endswith(".html"):
                continue
            cas = fn[:-5]
            try:
                with open(os.path.join(wbdir, fn), encoding="utf-8", errors="replace") as fh:
                    page = parse_webbook(fh.read())
            except OSError:
                page = None
            if not page or not page["facts"]:
                continue
            row = c.db.execute("SELECT sid FROM names WHERE norm=?", ("cas:" + cas,)).fetchone()
            if row:
                sid = row[0]
            else:
                sid = c.subject("cas:" + cas, page["name"], "compound", "", 0.0, "NIST WebBook",
                                formula_key(page["formula"]))
                c.name(sid, page["name"])
                c.name(sid, "cas:" + cas, raw=True)
            for prop, val, cond in page["facts"]:
                c.fact(sid, prop, val, cond, "NIST WebBook")
                n_wb += 1
            if page["mw"]:
                c.fact(sid, "molar mass", page["mw"] + " g/mol", "", "NIST WebBook")
    report["webbook"] = n_wb

    # -- Materials Project ------------------------------------------------------------------------------------------
    n_mp = 0
    f = os.path.join(raw, "materials_project.json")
    if os.path.exists(f):
        try:
            with open(f, encoding="utf-8") as fh:
                docs = json.load(fh)
        except (OSError, ValueError):
            docs = []
        best = {}
        for d in docs:
            fk = formula_key(d.get("formula_pretty") or "")
            if not fk:
                continue
            e = d.get("energy_above_hull")
            e = e if isinstance(e, (int, float)) else 9e9
            if fk not in best or e < best[fk][0]:
                best[fk] = (e, d)
        for fk, (e, d) in best.items():
            mid = d.get("material_id") or ""
            sym = d.get("symmetry") or {}
            label = f"{d.get('formula_pretty')} ({mid})"
            sid = c.subject("mp:" + mid, label, "mp material", "", 0.0, "Materials Project " + mid, fk)
            c.name(sid, "mpf:" + fk, raw=True)
            if isinstance(d.get("density"), (int, float)):
                c.fact(sid, "density", f"{d['density']:.3g} g/cm³", "computed", "Materials Project")
            if isinstance(d.get("band_gap"), (int, float)):
                c.fact(sid, "band gap", f"{d['band_gap']:.3g} eV", "DFT estimate, often low", "Materials Project")
            if isinstance(d.get("formation_energy_per_atom"), (int, float)):
                c.fact(sid, "formation energy", f"{d['formation_energy_per_atom']:.3g} eV/atom", "computed",
                       "Materials Project")
            if isinstance(d.get("energy_above_hull"), (int, float)):
                c.fact(sid, "energy above hull", f"{d['energy_above_hull']:.3g} eV/atom", "", "Materials Project")
            for key, prop in (("bulk_modulus", "bulk modulus"), ("shear_modulus", "shear modulus")):
                v = d.get(key)
                v = v.get("vrh") if isinstance(v, dict) else v
                if isinstance(v, (int, float)):
                    c.fact(sid, prop, f"{v:.3g} GPa", "computed", "Materials Project")
            if isinstance(sym, dict) and (sym.get("crystal_system") or sym.get("symbol")):
                c.fact(sid, "crystal structure", f"{sym.get('crystal_system') or ''} {sym.get('symbol') or ''}".strip(),
                       "", "Materials Project")
            if d.get("is_metal") is not None:
                c.fact(sid, "metal", "yes" if d.get("is_metal") else "no", "", "Materials Project")
            n_mp += 1
    report["materials_project"] = n_mp
    c.finish({"version": SCIENCE_VERSION, "built": time.time(), "counts": report, "fts": c.fts})
    return report


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _load_rows(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return []
    if isinstance(d, dict):
        return d.get("results", {}).get("bindings", []) if "results" in d else d.get("rows", [])
    return d if isinstance(d, list) else []


# ---- reading ------------------------------------------------------------------------------------------------------
class ScienceDB:
    """Read side: match a question to subjects and facts, and to ProofWiki statements."""

    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        meta = dict(self.db.execute("SELECT k, v FROM meta").fetchall())
        self.meta = {k: json.loads(v) for k, v in meta.items()}
        self.fts = bool(self.meta.get("fts"))
        self.counts = self.meta.get("counts", {})

    def close(self):
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass

    def wanted(self, question):
        out = []
        for rx, props in PROP_RX:
            if rx.search(question or ""):
                out += [p for p in props if p not in out]
        return out

    def _by_name(self, n):
        return self.db.execute("SELECT s.id, s.label, s.kind, s.rank, s.source, s.fkey FROM names n JOIN subjects s "
                               "ON s.id = n.sid WHERE n.norm = ? ORDER BY s.rank DESC LIMIT 3", (n,)).fetchall()

    def subjects(self, question):
        """[(sid, label, kind, source, fkey, words matched, how)] best first."""
        toks = norm(question).split()
        used = [False] * len(toks)
        found, seen = [], set()
        for size in range(min(7, len(toks)), 0, -1):
            for i in range(0, len(toks) - size + 1):
                if any(used[i:i + size]):
                    continue
                win = toks[i:i + size]
                if all(w in COMMON for w in win) or (size == 1 and (len(win[0]) < 3 or win[0] in COMMON)):
                    continue
                rows = self._by_name(" ".join(win))
                if not rows:
                    continue
                for j in range(i, i + size):
                    used[j] = True
                for sid, label, kind, rank, source, fkey in rows[:2]:
                    if sid not in seen:
                        seen.add(sid)
                        found.append((sid, label, kind, source, fkey, size, "name"))
        symbols = [m.group(0) for m in re.finditer(r"(?<![\w])[A-Z][a-z](?![\w])", question or "")
                   if m.group(0) in ELEMENTS and m.group(0) not in AMBIGUOUS_SYMBOLS]
        for m in list(FORMULA_TOKEN.finditer(question or "")) + symbols:
            fk = formula_key(m, strict=False) if isinstance(m, str) else formula_key(m.group(0), strict=True)
            if not fk:
                continue
            for sid, label, kind, rank, source, fkey in self._by_name("f:" + fk)[:1]:
                if sid not in seen:
                    seen.add(sid)
                    found.append((sid, label, kind, source, fkey, 2, "formula"))
            if not any(f[4] == fk for f in found):
                for sid, label, kind, rank, source, fkey in self._by_name("mpf:" + fk)[:1]:
                    if sid not in seen:
                        seen.add(sid)
                        found.append((sid, label, kind, source, fkey, 2, "formula"))
        for m in re.finditer(r"\b\d{2,7}-\d{2}-\d\b", question or ""):
            for sid, label, kind, rank, source, fkey in self._by_name("cas:" + m.group(0))[:1]:
                if sid not in seen:
                    seen.add(sid)
                    found.append((sid, label, kind, source, fkey, 2, "cas"))
        found.sort(key=lambda f: -f[5])
        return found

    def facts(self, sid):
        return self.db.execute("SELECT prop, value, cond, source FROM facts WHERE sid=? ORDER BY rowid",
                               (sid,)).fetchall()

    def _mp_for(self, fkey):
        if not fkey:
            return None
        r = self._by_name("mpf:" + fkey)
        return r[0] if r else None

    def docs(self, question, n=2):
        """ProofWiki statements for a math question: by a theorem's name first, then a full-text title search."""
        if not MATH_CUE.search(question or ""):
            return []
        out = []
        for sid, label, kind, source, fkey, size, how in self.subjects(question):
            if source == "ProofWiki":
                row = self.db.execute("SELECT title, body FROM docs WHERE sid=?", (sid,)).fetchone()
                if row:
                    out.append((sid, row[0], row[1]))
        terms = [w for w in norm(question).split() if w not in STOP and w not in CUE_WORDS and len(w) >= 3
                 and not w.isdigit()]
        if len(out) < n and terms and self.fts:
            pref = [w[:5] for w in terms]
            want_def = re.search(r"\bdefin", question or "", re.I)
            for joiner in (" AND ", " OR "):
                q = joiner.join(f'"{p}"*' for p in pref)
                try:
                    rows = self.db.execute(
                        "SELECT d.sid, d.title, d.body, s.kind FROM docs_fts f JOIN docs d ON d.sid = f.rowid "
                        "JOIN subjects s ON s.id = d.sid WHERE docs_fts MATCH ? "
                        "ORDER BY bm25(docs_fts, 12.0, 1.0) LIMIT 12", ("title : (" + q + ")",)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                good = []
                for sid, title, body, kind in rows:
                    tw = norm(title).split()
                    hit = sum(1 for p in pref if any(t.startswith(p) for t in tw))
                    if hit < max(1, (len(pref) + 1) // 2):
                        continue
                    extra = len(tw) - hit                          # the closest title, not a long variant of it
                    good.append((-(hit * 2) + extra * 0.3 + (0 if (kind == "definition") == bool(want_def) else 1),
                                 sid, title, body))
                good.sort(key=lambda g: g[0])
                for _, sid, title, body in good:
                    if len(out) >= n:
                        break
                    if all(o[0] != sid for o in out):
                        out.append((sid, title, body))
                if out:
                    break
        return out[:n]

    def lines(self, question, max_subjects=3, max_facts=8, chars=700):
        """The data for a question as reference lines (each '- label [source]: facts'), or []."""
        want = self.wanted(question)
        subs = self.subjects(question)
        out, done = [], set()
        prop_words = {norm(p) for p in want}

        def pick(facts, kind):
            if want:
                sel = [f for f in facts if any(f[0].lower().startswith(w.lower()) for w in want)]
                if kind == "constant" and not sel:
                    sel = [f for f in facts if f[0] == "value"]
                return sel
            order = {p: i for i, p in enumerate(CARD_ORDER)}
            return sorted(facts, key=lambda f: next((order[p] for p in order if f[0].startswith(p)), 99))[:6]

        others = [s for s in subs if norm(s[1]) not in prop_words]
        for sid, label, kind, source, fkey, size, how in subs:
            if len(done) >= max_subjects:
                break
            if source == "ProofWiki" or sid in done:
                continue
            if others and norm(label) in prop_words and len(subs) > 1:
                continue                     # 'density' the quantity when the question is the density of something
            if kind == "unit" and "in SI units" not in want:
                continue
            single = size == 1 and how == "name"
            if single and not want and kind not in ("constant",):
                continue                     # a lone common word: only when the question asks for a property
            facts = pick(self.facts(sid), kind)
            mp = self._mp_for(fkey) if kind != "mp material" else None
            mp_facts = pick(self.facts(mp[0]), "mp material") if mp else []
            if not facts and not mp_facts:
                continue
            done.add(sid)
            by_src = {}
            for prop, value, cond, src in facts[:max_facts]:
                by_src.setdefault(src, []).append(_fmt(prop, value, cond))
            for src, items in by_src.items():
                tag = source if src == "Wikidata" and source.startswith("Wikidata") else src
                if src == "NIST CODATA":
                    tag = "NIST CODATA 2022"
                out.append(f"- {label} [{tag}]: " + "; ".join(items))
            if mp_facts:
                done.add(mp[0])
                out.append(f"- {mp[1]} [{mp[4]}]: " + "; ".join(_fmt(*f[:3]) for f in mp_facts[:max_facts]))
        for sid, title, body in self.docs(question):
            text = body if len(body) <= chars else body[:chars].rsplit(" ", 1)[0] + " ..."
            out.append(f"- {title} [ProofWiki]: {text}")
        return out


def _fmt(prop, value, cond):
    return f"{prop} {value}" + (f" ({cond})" if cond else "")


DATA_HEADER = "REFERENCE DATA"


def block(lines):
    if not lines:
        return ""
    return (f"{DATA_HEADER} (Wikidata, NIST, ProofWiki, Materials Project; exact values and statements to answer "
            f"from, general facts, not about the user):\n" + "\n".join(lines))


def sources(ctx, answer):
    """Data lines an answer used, for its source line: a number from the line appears in the answer, or the answer
    names a ProofWiki theorem it was given. Up to three."""
    if not ctx or DATA_HEADER not in ctx or not answer:
        return []
    blk = ctx[ctx.index(DATA_HEADER):].split("\n\n", 1)[0]
    ans = re.sub(r"(?<=\d)[ ,](?=\d{3}\b)", "", answer)
    low = ans.lower()
    out = []
    for m in re.finditer(r"(?m)^- (.{1,200}?) \[([^\]]{1,80})\]: (.*)$", blk):
        label, src, body = m.group(1), m.group(2), m.group(3)
        nums = [x for x in re.findall(r"\d+(?:\.\d+)?", body.replace(" ", "")) if len(x.replace(".", "")) >= 2]
        used = any(x in ans.replace(" ", "") for x in nums) if src != "ProofWiki" else \
            any(w in low for w in norm(label).split() if len(w) > 4)
        if used:
            t = {"title": f"{src.split(' Q')[0]}: {label}", "kind": "data"}
            if t not in out:
                out.append(t)
    return out[:3]


# ---- fetching -----------------------------------------------------------------------------------------------------
class Science:
    """Fetches the raw files (by parts, each resumable), compiles science.db and answers from it."""

    def __init__(self, kdir, opts=None, status=None, save=None, in_window=None, download=None):
        o = opts or {}
        self.dir = kdir
        self.raw = os.path.join(kdir, "science_raw")
        self.path = os.path.join(kdir, "science.db")
        self.want = o.get("knowledge_science", True) is not False
        self.mp_key = str(o.get("materials_project_key") or "").strip()
        self.status = status if status is not None else {}
        self._save = save or (lambda: None)
        self.in_window = in_window or (lambda: True)
        self.download = download
        self.db = None
        self.open()

    @property
    def st(self):
        return self.status.setdefault("science", {"state": "not started"})

    def open(self):
        if self.db:
            self.db.close()
            self.db = None
        if os.path.exists(self.path):
            try:
                self.db = ScienceDB(self.path)
            except Exception as e:  # noqa: BLE001
                log.warning("science db not opened: %s", e)
                self.db = None

    def ready(self):
        return self.db is not None

    def stale(self):
        return self.db is None or (self.db.meta.get("version") or 0) < SCIENCE_VERSION

    def due(self, now=None):
        """Something to do: never built, built by an older version, a part left unfinished, or old data."""
        if not self.want:
            return False
        if self.stale() or self.st.get("pending"):
            return True
        built = self.st.get("fetched") or 0
        return (now or time.time()) - built > REFRESH_DAYS * 86400

    def lines(self, question):
        if not self.db:
            return []
        try:
            return self.db.lines(question)
        except Exception as e:  # noqa: BLE001
            log.warning("science lookup failed: %s", e)
            return []

    # -- network ----------------------------------------------------------------------------------------------------
    async def _get(self, s, url, **kw):
        for attempt in range(4):
            async with s.get(url, **kw) as r:
                if r.status in (429, 503) and attempt < 3:
                    await asyncio.sleep(min(int(r.headers.get("Retry-After", "30") or 30), 120))
                    continue
                if r.status != 200:
                    raise RuntimeError(f"{url.split('?')[0]}: HTTP {r.status}")
                return await r.read()
        raise RuntimeError(f"{url}: gave up")

    async def _sparql(self, s, query):
        for attempt in range(4):
            async with s.post(WDQS, data={"query": query},
                              headers={"Accept": "application/sparql-results+json", "User-Agent": UA}) as r:
                if r.status in (429, 503, 502, 504) and attempt < 3:
                    await asyncio.sleep(min(int(r.headers.get("Retry-After", "20") or 20), 120))
                    continue
                if r.status != 200:
                    body = (await r.text())[:200]
                    raise RuntimeError(f"Wikidata query: HTTP {r.status} {body}")
                return json.loads(await r.read())
        raise RuntimeError("Wikidata query: gave up")

    def _part(self, name, **kw):
        p = self.st.setdefault("parts", {}).setdefault(name, {})
        p.update(kw)
        self._save()

    async def fetch_codata(self, s):
        dest = os.path.join(self.raw, "codata.txt")
        body = await self._get(s, CODATA_URL, headers={"User-Agent": UA})
        if b"----" not in body:
            raise RuntimeError("CODATA file looks wrong")
        with open(dest, "wb") as f:
            f.write(body)
        self._part("codata", state="done", bytes=len(body))

    async def resolve_props(self, s):
        labels = sorted({lab for _, labs, *_ in WD_QUANTITY + WD_TEXT + WD_EXTRA for lab in labs})
        vals = " ".join(json.dumps(lab) + "@en" for lab in labels)
        q = ("SELECT ?p ?label ?type WHERE { VALUES ?label { " + vals + " } ?p wikibase:propertyType ?type ; "
             "rdfs:label ?label . }")
        res = await self._sparql(s, q)
        found = {}
        for row in res.get("results", {}).get("bindings", []):
            found.setdefault(_b(row, "label").lower(), []).append((_qid(_b(row, "p")), _b(row, "type").rsplit("#", 1)[-1]))
        out = {}
        for canon, labs, pid, *rest in WD_QUANTITY + WD_TEXT + WD_EXTRA:
            hit = ""
            for lab in labs:
                for p, typ in found.get(lab.lower(), []):
                    if p:
                        hit = p
                        break
                if hit:
                    break
            out[canon] = hit or pid
        with open(os.path.join(self.raw, "wikidata", "props.json"), "w") as f:
            json.dump(out, f)
        return out

    async def fetch_wikidata(self, s, force=False):
        wd = os.path.join(self.raw, "wikidata")
        os.makedirs(wd, exist_ok=True)
        props = await self.resolve_props(s)
        temp, phase = props.get("temperature") or "P2076", props.get("phase of matter") or "P515"
        items, failed, done = set(), [], 0
        for canon, labs, pid, restrict in WD_QUANTITY:
            p = props.get(canon)
            dest = os.path.join(wd, "q_" + _slug(canon) + ".json")
            if not p:
                failed.append(canon + " (no property)")
                continue
            if restrict == "chem":
                continue                         # asked for the items already found, after their names (below)
            if not os.path.exists(dest) or force:
                extra = f"?item wdt:{props.get('chemical formula') or 'P274'} ?f . FILTER(?sl >= 3)" \
                    if restrict == "chem" else "FILTER(?sl >= 1)"
                q = ("SELECT ?item ?itemLabel ?itemDescription ?amount ?unitLabel ?tamt ?tunitLabel ?phaseLabel ?sl "
                     "WHERE { ?item p:" + p + " ?st . ?st psv:" + p + " ?v . ?v wikibase:quantityAmount ?amount . "
                     "OPTIONAL { ?v wikibase:quantityUnit ?unit . } "
                     "OPTIONAL { ?st pqv:" + temp + " ?tv . ?tv wikibase:quantityAmount ?tamt . "
                     "OPTIONAL { ?tv wikibase:quantityUnit ?tunit . } } "
                     "OPTIONAL { ?st pq:" + phase + " ?phase . } "
                     "?st wikibase:rank ?rank . FILTER(?rank != wikibase:DeprecatedRank) "
                     "?item wikibase:sitelinks ?sl . " + extra + " "
                     "SERVICE wikibase:label { bd:serviceParam wikibase:language \"en\". } }")
                try:
                    res = await self._sparql(s, q)
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{canon}: {str(e)[:80]}")
                    continue
                with open(dest, "w") as f:
                    json.dump(res, f)
                await asyncio.sleep(1)
            for row in _load_rows(dest):
                q_ = _qid(_b(row, "item"))
                if q_:
                    items.add(q_)
            done += 1
            self._part("wikidata", state="querying", done=done, items=len(items), failed=failed)
        for canon, labs, pid in WD_TEXT:
            p = props.get(canon)
            dest = os.path.join(wd, "t_" + _slug(canon) + ".json")
            if p and (not os.path.exists(dest) or force):
                q = ("SELECT ?item ?itemLabel ?itemDescription ?val ?sl WHERE { ?item wdt:" + p + " ?val . "
                     "?item wikibase:sitelinks ?sl . SERVICE wikibase:label { bd:serviceParam wikibase:language "
                     "\"en\". } }")
                try:
                    res = await self._sparql(s, q)
                    with open(dest, "w") as f:
                        json.dump(res, f)
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{canon}: {str(e)[:80]}")
                await asyncio.sleep(1)
            for row in _load_rows(dest):
                q_ = _qid(_b(row, "item"))
                if q_:
                    items.add(q_)
        dest = os.path.join(wd, "units.json")
        si = props.get("conversion to SI unit") or "P2370"
        if not os.path.exists(dest) or force:
            q = ("SELECT ?item ?itemLabel ?itemDescription ?amount ?siLabel ?sl WHERE { ?item p:" + si + " ?st . "
                 "?st psv:" + si + " ?v . ?v wikibase:quantityAmount ?amount ; wikibase:quantityUnit ?si . "
                 "?item wikibase:sitelinks ?sl . FILTER(?sl >= 3) "
                 "SERVICE wikibase:label { bd:serviceParam wikibase:language \"en\". } }")
            try:
                res = await self._sparql(s, q)
                with open(dest, "w") as f:
                    json.dump(res, f)
            except Exception as e:  # noqa: BLE001
                failed.append(f"units: {str(e)[:80]}")
        for row in _load_rows(dest):
            q_ = _qid(_b(row, "item"))
            if q_:
                items.add(q_)
        # names, aliases, formula, CAS and kind for every item, in batches
        items = sorted(items, key=lambda x: int(x[1:]))
        f_p, cas_p = props.get("chemical formula") or "P274", props.get("CAS number") or "P231"
        inst = props.get("instance of") or "P31"
        nb = (len(items) + DETAIL_BATCH - 1) // DETAIL_BATCH
        for bi in range(nb):
            dest = os.path.join(wd, f"details_{bi:04d}.json")
            batch = items[bi * DETAIL_BATCH:(bi + 1) * DETAIL_BATCH]
            if os.path.exists(dest) and not force:
                continue
            q = ("SELECT ?item ?label ?desc ?sl (GROUP_CONCAT(DISTINCT ?alias; separator=\"|\") AS ?aliases) "
                 "(SAMPLE(?f) AS ?formula) (SAMPLE(?c) AS ?cas) (GROUP_CONCAT(DISTINCT ?il; separator=\"|\") AS ?kinds) "
                 "WHERE { VALUES ?item { " + " ".join("wd:" + x for x in batch) + " } "
                 "OPTIONAL { ?item rdfs:label ?label FILTER(LANG(?label) = \"en\") } "
                 "OPTIONAL { ?item schema:description ?desc FILTER(LANG(?desc) = \"en\") } "
                 "OPTIONAL { ?item skos:altLabel ?alias FILTER(LANG(?alias) = \"en\") } "
                 "OPTIONAL { ?item wdt:" + f_p + " ?f } OPTIONAL { ?item wdt:" + cas_p + " ?c } "
                 "OPTIONAL { ?item wdt:" + inst + " ?in . ?in rdfs:label ?il FILTER(LANG(?il) = \"en\") } "
                 "OPTIONAL { ?item wikibase:sitelinks ?sl } } GROUP BY ?item ?label ?desc ?sl")
            try:
                res = await self._sparql(s, q)
                with open(dest, "w") as f:
                    json.dump(res, f)
            except Exception as e:  # noqa: BLE001
                failed.append(f"details {bi}: {str(e)[:80]}")
            self._part("wikidata", state="names", batch=bi + 1, batches=nb, items=len(items), failed=failed[-8:])
            await asyncio.sleep(0.5)
        # a property everything has (mass): only for the items found above, in batches, so the query stays small
        for canon, labs, pid, restrict in WD_QUANTITY:
            p = props.get(canon)
            if restrict != "chem" or not p:
                continue
            for bi in range(nb):
                dest = os.path.join(wd, f"q_{_slug(canon)}_{bi:04d}.json")
                if os.path.exists(dest) and not force:
                    continue
                batch = items[bi * DETAIL_BATCH:(bi + 1) * DETAIL_BATCH]
                q = ("SELECT ?item ?amount ?unitLabel ?sl WHERE { VALUES ?item { " +
                     " ".join("wd:" + x for x in batch) + " } ?item p:" + p + " ?st . ?st psv:" + p + " ?v . "
                     "?v wikibase:quantityAmount ?amount . OPTIONAL { ?v wikibase:quantityUnit ?unit . } "
                     "?st wikibase:rank ?rank . FILTER(?rank != wikibase:DeprecatedRank) "
                     "OPTIONAL { ?item wikibase:sitelinks ?sl } "
                     "SERVICE wikibase:label { bd:serviceParam wikibase:language \"en\". } }")
                try:
                    res = await self._sparql(s, q)
                    with open(dest, "w") as f:
                        json.dump(res, f)
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{canon} {bi}: {str(e)[:80]}")
                await asyncio.sleep(0.5)
        self._part("wikidata", state="done" if not failed else "done with gaps", items=len(items),
                   failed=failed[-8:], props={k: v for k, v in props.items()})

    def webbook_list(self):
        """CAS numbers of the most-linked compounds in the Wikidata details, most-linked first."""
        wd = os.path.join(self.raw, "wikidata")
        rows = []
        if os.path.isdir(wd):
            for fn in os.listdir(wd):
                if fn.startswith("details_"):
                    for row in _load_rows(os.path.join(wd, fn)):
                        cas = _b(row, "cas")
                        if re.fullmatch(r"\d{2,7}-\d{2}-\d", cas or ""):
                            rows.append((-(_num(_b(row, "sl")) or 0), cas))
        rows.sort()
        out, seen = [], set()
        for _, cas in rows:
            if cas not in seen:
                seen.add(cas)
                out.append(cas)
        return out[:WEBBOOK_MAX]

    async def fetch_webbook(self, s, respect_window=True):
        d = os.path.join(self.raw, "webbook")
        os.makedirs(d, exist_ok=True)
        todo = [c for c in self.webbook_list() if not os.path.exists(os.path.join(d, c + ".html"))]
        total = len(self.webbook_list())
        n = total - len(todo)
        for cas in todo:
            if respect_window and not self.in_window():
                self._part("webbook", state="paused until the download window", done=n, total=total)
                return False
            try:
                body = await self._get(s, WEBBOOK_URL.format(id=cas.replace("-", "")), headers={"User-Agent": UA})
            except Exception as e:  # noqa: BLE001
                body = f"<html><head><title>error</title></head><body>{html.escape(str(e)[:100])}</body></html>"\
                    .encode()
            with open(os.path.join(d, cas + ".html"), "wb") as f:
                f.write(body)
            n += 1
            if n % 20 == 0:
                self._part("webbook", state="downloading", done=n, total=total)
            await asyncio.sleep(WEBBOOK_DELAY)
        self._part("webbook", state="done", done=n, total=total)
        return True

    async def fetch_mp(self, s):
        if not self.mp_key:
            self._part("materials_project", state="needs the option materials_project_key")
            return
        docs, skip, limit = [], 0, 1000
        while True:
            params = {"is_stable": "true", "_fields": MP_FIELDS, "_limit": str(limit), "_skip": str(skip)}
            async with s.get(MP_URL, params=params, headers={"X-API-KEY": self.mp_key, "User-Agent": UA,
                                                              "accept": "application/json"}) as r:
                if r.status in (401, 403):
                    self._part("materials_project", state="the API key was refused")
                    return
                if r.status != 200:
                    raise RuntimeError(f"Materials Project: HTTP {r.status}")
                page = json.loads(await r.read())
            data = page.get("data") or []
            docs += data
            self._part("materials_project", state="downloading", done=len(docs))
            if len(data) < limit or skip > 400000:
                break
            skip += limit
            await asyncio.sleep(0.5)
        with open(os.path.join(self.raw, "materials_project.json"), "w") as f:
            json.dump(docs, f)
        self._part("materials_project", state="done", materials=len(docs))

    async def compile(self):
        tmp = self.path + ".tmp"
        report = await asyncio.to_thread(compile_raw, self.raw, tmp, bool(self.mp_key))
        if self.db:
            self.db.close()
            self.db = None
        os.replace(tmp, self.path)
        self.open()
        self.st.update(compiled=time.time(), counts=report, version=SCIENCE_VERSION,
                       size_mb=round(os.path.getsize(self.path) / 1e6, 1))
        self._save()
        return report

    async def build(self, force_window=False):
        """Fetch what's missing, compile, then the slow WebBook pages, then compile again."""
        import aiohttp
        os.makedirs(self.raw, exist_ok=True)
        st = self.st
        refresh = (time.time() - (st.get("fetched") or 0)) > REFRESH_DAYS * 86400 and st.get("fetched")
        st.update(state="downloading", pending=True, error="", tried=time.time())
        self._save()
        timeout = aiohttp.ClientTimeout(total=None, sock_read=180)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            for name, coro in (("codata", lambda: self.fetch_codata(s)),
                               ("wikidata", lambda: self.fetch_wikidata(s, force=bool(refresh))),
                               ("materials_project", lambda: self.fetch_mp(s))):
                if name == "codata" and os.path.exists(os.path.join(self.raw, "codata.txt")) and not refresh:
                    continue
                if name == "materials_project" and os.path.exists(os.path.join(self.raw, "materials_project.json")) \
                        and not refresh:
                    continue
                try:
                    await coro()
                except Exception as e:  # noqa: BLE001
                    log.exception("science %s", name)
                    self._part(name, state="error", error=str(e)[:200])
            npf = os.path.join(self.raw, "naturalproofs_proofwiki.json")
            if not os.path.exists(npf) and self.download:
                try:
                    ok = await self.download(NATURALPROOFS_URL, npf, "science_dl", respect_window=not force_window)
                    self._part("proofwiki", state="done" if ok else "download paused")
                except Exception as e:  # noqa: BLE001
                    log.exception("science naturalproofs")
                    self._part("proofwiki", state="error", error=str(e)[:200])
            st.update(state="building")
            self._save()
            await self.compile()
            st.update(fetched=time.time() if refresh or not st.get("fetched") else st["fetched"])
            try:
                finished = await self.fetch_webbook(s, respect_window=not force_window)
            except Exception as e:  # noqa: BLE001
                log.exception("science webbook")
                self._part("webbook", state="error", error=str(e)[:200])
                finished = False
            if (st.get("parts", {}).get("webbook", {}).get("done") or 0):
                await self.compile()
        st.update(state="ready" if self.db else "failed", pending=not finished, finished=time.time())
        self._save()
        return self.db is not None
