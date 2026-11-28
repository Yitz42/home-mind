"""Science data: parsing each source's raw file (CODATA text, Wikidata query results, NaturalProofs JSON, a WebBook page,
Materials Project records), compiling science.db, and matching questions to exact lines - all offline, from fixtures
shaped like the real downloads."""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import science  # noqa: E402
from homemind.science import (Science, compile_raw, formula_key, norm, parse_codata, parse_webbook,  # noqa: E402
                              naturalproofs_entries, clean_wikitext, block, sources)
from homemind.knowledge import Knowledge  # noqa: E402

CODATA = """  Fundamental Physical Constants --- Complete Listing
             2022 CODATA adjustment


  From:  http://physics.nist.gov/constants



  Quantity                                                       Value                 Uncertainty           Unit
-----------------------------------------------------------------------------------------------------------------------------
alpha particle-electron mass ratio                          7294.299 541 71          0.000 000 17
Avogadro constant                                           6.022 140 76 e23         (exact)                  mol^-1
electron mass                                               9.109 383 7139 e-31      0.000 000 0028 e-31      kg
Newtonian constant of gravitation                           6.674 30 e-11            0.000 15 e-11            m^3 kg^-1 s^-2
speed of light in vacuum                                    299 792 458              (exact)                  m s^-1
standard acceleration of gravity                            9.806 65                 (exact)                  m s^-2
"""


def uri(q):
    return {"type": "uri", "value": "http://www.wikidata.org/entity/" + q}


def lit(v):
    return {"type": "literal", "value": str(v)}


def results(rows):
    return {"head": {"vars": []}, "results": {"bindings": rows}}


def q_row(q, label, amount, unit, sl, tamt=None, tunit=None, phase=None):
    r = {"item": uri(q), "itemLabel": lit(label), "amount": lit(amount), "unitLabel": lit(unit), "sl": lit(sl)}
    if tamt is not None:
        r["tamt"], r["tunitLabel"] = lit(tamt), lit(tunit)
    if phase:
        r["phaseLabel"] = lit(phase)
    return r


DETAILS = [
    {"item": uri("Q663"), "label": lit("aluminium"), "desc": lit("chemical element with symbol Al and atomic number 13"),
     "sl": lit(250), "aliases": lit("Al|aluminum|element 13"), "kinds": lit("chemical element"), "cas": lit("7429-90-5")},
    {"item": uri("Q670"), "label": lit("silicon"), "desc": lit("chemical element with symbol Si"), "sl": lit(200),
     "aliases": lit("Si|element 14"), "kinds": lit("chemical element")},
    {"item": uri("Q658"), "label": lit("sodium"), "sl": lit(200), "aliases": lit("Na"), "kinds": lit("chemical element")},
    {"item": uri("Q688"), "label": lit("chlorine"), "sl": lit(200), "aliases": lit("Cl"), "kinds": lit("chemical element")},
    {"item": uri("Q283"), "label": lit("water"), "desc": lit("chemical compound"), "sl": lit(300),
     "aliases": lit("H2O|dihydrogen monoxide"), "formula": lit("H₂O"), "kinds": lit("chemical compound"),
     "cas": lit("7732-18-5")},
    {"item": uri("Q2314"), "label": lit("sodium chloride"), "desc": lit("chemical compound"), "sl": lit(120),
     "aliases": lit("table salt|NaCl|halite"), "formula": lit("NaCl"), "kinds": lit("chemical compound"),
     "cas": lit("7647-14-5")},
    {"item": uri("Q29539"), "label": lit("density"), "desc": lit("mass per volume"), "sl": lit(150),
     "aliases": lit("volumetric mass density"), "kinds": lit("physical quantity")},
    {"item": uri("Q11518"), "label": lit("Pythagorean theorem"), "desc": lit("relation between the sides of a right "
     "triangle"), "sl": lit(140), "aliases": lit("Pythagoras' theorem|Pythagoras theorem"), "kinds": lit("theorem")},
    {"item": uri("Q11573"), "label": lit("metre"), "desc": lit("SI unit of length"), "sl": lit(200),
     "aliases": lit("meter|m"), "kinds": lit("SI base unit|unit of length")},
    {"item": uri("Q218593"), "label": lit("inch"), "desc": lit("unit of length"), "sl": lit(100),
     "aliases": lit("in|inches"), "kinds": lit("unit of length")},
]

NATURALPROOFS = {"dataset": {
    "theorems": [
        {"id": 1, "type": "theorem", "title": "Pythagoras's Theorem",
         "contents": ["Let $\\triangle ABC$ be a [[Definition:Right Triangle|right triangle]] with $c$ as the "
                      "[[Definition:Hypotenuse|hypotenuse]].", "Then:", ":$a^2 + b^2 = c^2$"],
         "proofs": [{"contents": ["a long proof that is not kept"]}]},
        {"id": 2, "type": "theorem", "title": "Fermat's Little Theorem",
         "contents": ["Let $p$ be a [[Definition:Prime Number|prime number]].", "Then $a^p \\equiv a \\pmod p$."],
         "proofs": []},
        {"id": 3, "type": "theorem", "title": "Sum of Sequence of Odd Squares",
         "contents": ["{{begin-eqn}}", "{{eqn | l = \\sum_{i = 1}^n (2i-1)^2 | r = \\frac {n(2n-1)(2n+1)} 3}}",
                      "{{end-eqn}}"], "proofs": []},
    ],
    "definitions": [
        {"id": 10, "type": "definition", "title": "Definition:Prime Number",
         "contents": ["A '''prime number''' $p$ is a [[Definition:Positive Integer|positive integer]] that has "
                      "exactly two [[Definition:Divisor of Integer|divisors]]: $1$ and $p$."]},
    ],
    "others": [{"id": 20, "type": "other", "title": "Mathematician:Pythagoras of Samos", "contents": ["A Greek."]}],
    "retrieval_examples": [1, 2]}, "splits": {"train": {"examples": [[1, [10]]]}}}

WEBBOOK = """<html><head><title>Water</title></head><body>
<h1 id="Top">Water</h1><ul><li><strong>Formula</strong>: H<sub>2</sub>O</li>
<li><strong>Molecular weight</strong>: 18.0153</li></ul>
<h2>Phase change data</h2>
<table class="data" aria-label="One dimensional data">
<tr><th>Quantity</th><th>Value</th><th>Units</th><th>Method</th><th>Reference</th><th>Comment</th></tr>
<tr class="exp"><td>T<sub>boil</sub></td><td>373.17</td><td>K</td><td>N/A</td><td>Ref 1</td><td></td></tr>
<tr class="exp"><td>T<sub>boil</sub></td><td>373.2 &plusmn; 0.1</td><td>K</td><td>AVG</td><td>N/A</td><td></td></tr>
<tr class="exp"><td>T<sub>boil</sub></td><td>373.15</td><td>K</td><td>N/A</td><td>Ref 3</td><td></td></tr>
<tr class="exp"><td>T<sub>c</sub></td><td>647.14</td><td>K</td><td>N/A</td><td>Ref 4</td><td></td></tr>
<tr class="exp"><td>P<sub>c</sub></td><td>220.64</td><td>bar</td><td>N/A</td><td>Ref 5</td><td></td></tr>
</table>
<table class="data" aria-label="Enthalpy of vaporization">
<tr><th>&Delta;<sub>vap</sub>H (kJ/mol)</th><th>Temperature (K)</th><th>Method</th><th>Reference</th></tr>
<tr><td>40.65</td><td>373.</td><td>N/A</td><td>Ref 6</td></tr>
<tr><td>43.98</td><td>298.</td><td>N/A</td><td>Ref 7</td></tr>
<tr><td>44.0</td><td>300.</td><td>N/A</td><td>Ref 8</td></tr>
</table>
<table class="data" aria-label="Antoine Equation Parameters">
<tr><th>Temperature (K)</th><th>A</th><th>B</th><th>C</th></tr><tr><td>344. to 373.</td><td>5.08354</td><td>1663.125</td>
<td>-45.622</td></tr></table>
</body></html>"""

MP = [{"material_id": "mp-149", "formula_pretty": "Si", "density": 2.2828, "band_gap": 0.6105,
       "energy_above_hull": 0.0, "formation_energy_per_atom": 0.0, "is_metal": False,
       "symmetry": {"crystal_system": "Cubic", "symbol": "Fd-3m"}, "bulk_modulus": {"vrh": 88.0}},
      {"material_id": "mp-22862", "formula_pretty": "NaCl", "density": 2.16, "band_gap": 5.0,
       "energy_above_hull": 0.0, "formation_energy_per_atom": -2.1, "is_metal": False,
       "symmetry": {"crystal_system": "Cubic", "symbol": "Fm-3m"}},
      {"material_id": "mp-999", "formula_pretty": "NaCl", "density": 2.0, "band_gap": 4.0,
       "energy_above_hull": 0.2, "is_metal": False, "symmetry": {}}]


def write_raw(raw, with_mp=True):
    wd = os.path.join(raw, "wikidata")
    os.makedirs(os.path.join(raw, "webbook"), exist_ok=True)
    os.makedirs(wd, exist_ok=True)
    with open(os.path.join(raw, "codata.txt"), "w") as f:
        f.write(CODATA)
    dump = lambda name, obj: json.dump(obj, open(os.path.join(wd, name), "w"))  # noqa: E731
    dump("details_0000.json", results(DETAILS))
    dump("q_density.json", results([
        q_row("Q663", "aluminium", "+2.7", "gram per cubic centimetre", 250, 20, "degree Celsius"),
        q_row("Q663", "aluminium", "+2.375", "gram per cubic centimetre", 250, None, None, "liquid"),
        q_row("Q283", "water", "+0.9998", "gram per cubic centimetre", 300, 0, "degree Celsius"),
        q_row("Q2314", "sodium chloride", "+2.165", "gram per cubic centimetre", 120),
        q_row("Q670", "silicon", "+2.3290", "gram per cubic centimetre", 200)]))
    dump("q_specific_heat_capacity.json", results([
        q_row("Q283", "water", "+4.184", "joule per gram kelvin", 300, 25, "degree Celsius"),
        q_row("Q663", "aluminium", "+0.897", "joule per gram kelvin", 250)]))
    dump("q_melting_point.json", results([
        q_row("Q663", "aluminium", "+660.32", "degree Celsius", 250),
        q_row("Q2314", "sodium chloride", "+801", "degree Celsius", 120)]))
    dump("q_molar_mass.json", results([q_row("Q2314", "sodium chloride", "+57.959", "dalton", 120)]))
    dump("q_molar_mass_0000.json", results([q_row("Q658", "sodium", "+22.98976928", "dalton", 200),
                                             q_row("Q688", "chlorine", "+35.45", "dalton", 200)]))
    dump("t_defining_formula.json", results([
        {"item": uri("Q29539"), "itemLabel": lit("density"), "val": lit('<math xmlns="http://www.w3.org/1998/Math/'
         'MathML"><mrow><mi>&#x03C1;</mi><mo>=</mo><mfrac><mi>m</mi><mi>V</mi></mfrac></mrow></math>'), "sl": lit(150)},
        {"item": uri("Q11518"), "itemLabel": lit("Pythagorean theorem"), "val": lit("a^2 + b^2 = c^2"),
         "sl": lit(140)}]))
    dump("t_proofwiki_id.json", results([
        {"item": uri("Q11518"), "itemLabel": lit("Pythagorean theorem"), "val": lit("Pythagoras's_Theorem")}]))
    dump("units.json", results([
        {"item": uri("Q218593"), "itemLabel": lit("inch"), "amount": lit("+0.0254"), "siLabel": lit("metre"),
         "sl": lit(100)}]))
    json.dump(NATURALPROOFS, open(os.path.join(raw, "naturalproofs_proofwiki.json"), "w"))
    with open(os.path.join(raw, "webbook", "7732-18-5.html"), "w") as f:
        f.write(WEBBOOK)
    with open(os.path.join(raw, "webbook", "64-17-5.html"), "w") as f:
        f.write("<html><head><title>Registry Number Not Found</title></head><body></body></html>")
    if with_mp:
        json.dump(MP, open(os.path.join(raw, "materials_project.json"), "w"))


def main():
    # --- small parts -------------------------------------------------------------------------------------------------
    assert norm("Aluminium's density") == "aluminum density"
    assert norm("the Speed of Light") == "speed of light"
    assert formula_key("Al₂O₃") == "Al2O3" and formula_key("O3Al2") == "Al2O3"
    assert formula_key("Ca(OH)2") == "CaH2O2" and formula_key("H2O") == "H2O"
    assert formula_key("CO", strict=True) == "" and formula_key("Co", strict=True) == ""
    assert formula_key("NaCl", strict=True) == "ClNa" and formula_key("CO2", strict=True) == "CO2"
    assert formula_key("DNA") == "" and formula_key("What") == ""
    rows = parse_codata(CODATA)
    by = {r["name"]: r for r in rows}
    assert by["speed of light in vacuum"]["value"] == "299792458" and by["speed of light in vacuum"]["exact"]
    assert by["Newtonian constant of gravitation"]["value"] == "6.67430e-11"
    assert by["Newtonian constant of gravitation"]["unit"] == "m^3 kg^-1 s^-2"
    assert by["alpha particle-electron mass ratio"]["unit"] == "" and by["alpha particle-electron mass ratio"]["uncertainty"]
    wb = parse_webbook(WEBBOOK)
    assert wb["name"] == "Water" and wb["formula"] == "H2O" and wb["mw"] == "18.0153", wb
    props = [f[0] for f in wb["facts"]]
    assert props.count("boiling point") == 2, props                   # two values of one quantity at most
    assert ("critical temperature", "647.14 K", "") in wb["facts"]
    assert ("enthalpy of vaporization", "40.65 kJ/mol", "at 373. K") in wb["facts"]
    assert not any("Antoine" in f[0] or f[0] == "A" for f in wb["facts"])   # coefficient tables are skipped
    assert parse_webbook("<html><head><title>Registry Number Not Found</title></head></html>") is None
    ents = naturalproofs_entries(NATURALPROOFS)
    titles = [e["title"] for e in ents]
    assert "Pythagoras's Theorem" in titles and "Definition:Prime Number" in titles, titles
    py = next(e for e in ents if e["title"] == "Pythagoras's Theorem")
    assert "right triangle" in py["text"] and "$a^2 + b^2 = c^2$" in py["text"] and "proof" not in py["text"]
    assert "[[" not in clean_wikitext(["see [[Definition:Prime Number|primes]] here"])
    odd = next(e for e in ents if e["title"].startswith("Sum of Sequence"))
    assert "\\frac" in odd["text"] and "{{" not in odd["text"], odd["text"]

    # --- compile and ask --------------------------------------------------------------------------------------------
    kdir = tempfile.mkdtemp()
    raw = os.path.join(kdir, "science_raw")
    write_raw(raw)
    report = compile_raw(raw, os.path.join(kdir, "science.db"))
    assert report["codata"] == 6 and report["wikidata"] >= 8 and report["proofwiki"] >= 4, report
    assert report["webbook"] >= 4 and report["materials_project"] == 2, report
    sc = Science(kdir, {})
    assert sc.ready() and not sc.stale()

    def ask(q):
        return "\n".join(sc.lines(q))

    a = ask("what's the density of aluminum?")
    assert "aluminium [Wikidata Q663]: density 2.7 g/cm³ (at 20 °C)" in a, a
    assert "rho" not in a                                            # not the quantity 'density' itself
    a = ask("What is the density of aluminium")                      # British spelling
    assert "2.7 g/cm³" in a, a
    a = ask("specific heat of water")
    assert "water [Wikidata Q283]: specific heat capacity 4.184 J/(g·K) (at 25 °C)" in a, a
    assert "density" not in a                                        # only what was asked
    a = ask("what's the speed of light")
    assert "speed of light in vacuum [NIST CODATA 2022]: value 299792458 m s^-1 (exact)" in a, a
    a = ask("gravitational constant value")
    assert "6.67430e-11 m^3 kg^-1 s^-2" in a, a
    a = ask("mass of electron")
    assert "9.1093837139e-31 kg" in a, a
    a = ask("acceleration due to gravity")
    assert "9.80665" in a, a
    a = ask("melting point of NaCl")                                 # a chemical formula
    assert "sodium chloride" in a and "801 °C" in a, a
    a = ask("molar mass of table salt")
    assert "58.440 g/mol (from the formula)" in a and "57.959" not in a, a   # not Wikidata's monoisotopic mass
    a = ask("formula for density")
    assert "density [Wikidata Q29539]: defining formula ρ = m/V" in a, a
    assert science.mathml_text('<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>T</mi><mo>=</mo><mfrac>'
                               '<mn>1</mn><mn>2</mn></mfrac><mi>m</mi><msup><mi>v</mi><mn>2</mn></msup></math>') \
        == "T = 1/2 m v^2"
    a = ask("boiling point of water")                                # from the WebBook, by CAS number
    assert "water [NIST WebBook]: boiling point 373.17 K" in a, a
    a = ask("critical temperature of water")
    assert "647.14 K" in a, a
    a = ask("band gap of Si")                                        # Materials Project by formula
    assert "Si (mp-149) [Materials Project" in a and "band gap 0.611 eV" in a, a
    a = ask("what's the band gap of silicon")                         # the element linked by its symbol
    assert "band gap 0.611 eV" in a, a
    assert ask("band gap of In") == ""                                # 'In' is a word, not indium
    a = ask("band gap of NaCl")                                      # the stable polymorph, beside the Wikidata subject
    assert "mp-22862" in a and "mp-999" not in a, a
    a = ask("state the pythagorean theorem")
    assert "Pythagoras's Theorem [ProofWiki]: Let $\\triangle ABC$ be a right triangle" in a, a
    assert "a^2 + b^2 = c^2" in a, a
    a = ask("what does fermat's little theorem say")
    assert "Fermat's Little Theorem [ProofWiki]" in a, a
    a = ask("what is the definition of a prime number")
    assert a.count("[ProofWiki]") == 1, a
    a = ask("definition of a prime number")
    assert "Prime Number [ProofWiki]: A prime number $p$ is a positive integer" in a, a
    assert science.unit_short("joule per kilogram kelvin difference") == "J/(kg·K)"
    a = ask("what is the definition of a prime number in math")
    assert "Prime Number [ProofWiki]" in a, a
    a = ask("how many meters in an inch")
    assert "1 inch = 0.0254 metre" in a, a
    assert ask("inch") == ""                                          # a unit alone, no conversion asked
    assert ask("water the plants at noon") == ""                      # a common word with no property asked
    assert ask("what's the weather tomorrow") == ""
    assert ask("is CO poisonous") == ""                               # 'CO' read as a word, not a formula
    assert ask("theorem about cats and dogs") == ""

    # --- the block, the source line, Knowledge ---------------------------------------------------------------------
    lines = sc.lines("specific heat of water")
    ctx = "NOTES\n\n" + block(lines) + "\n\nREFERENCE (offline Wikipedia, 2026-01 snapshot; ...):\n- Water: text"
    src = sources(ctx, "Water's specific heat is 4.184 J/(g·K) at 25 °C.")
    assert src == [{"title": "Wikidata: water", "kind": "data"}], src
    assert sources(ctx, "I'm not sure.") == []
    src = sources(block(sc.lines("speed of light")), "It is 299,792,458 m/s.")
    assert src and src[0]["title"] == "NIST CODATA 2022: speed of light in vacuum", src

    kn = Knowledge(os.path.dirname(kdir) if False else tempfile.mkdtemp(), {"knowledge_science": True})
    write_raw(os.path.join(kn.dir, "science_raw"), with_mp=False)
    asyncio.run(kn.science.compile())
    assert kn.summary()["science_ready"] and kn.status["science"]["counts"]["materials_project"] == 0
    ref = kn.reference_lines("density of aluminum")
    assert ref.startswith("REFERENCE DATA") and "2.7 g/cm³" in ref, ref
    assert kn.reference_lines("what's the density of my desk") == ""  # about the user's own things
    look = kn.lookup("density of aluminum")                           # no Wikipedia file: data alone
    assert look.get("data") and "2.7" in look["data"][0], look
    assert "error" in kn.lookup("zzzz qqqq")

    # --- when it runs ------------------------------------------------------------------------------------------------
    k = Knowledge(tempfile.mkdtemp(), {"knowledge_window": "00:00-23:59", "knowledge_food": False,
                                       "knowledge_wikipedia": False})
    started = []

    async def fake_build(force_window=False):
        started.append(force_window)
    k.science.build = fake_build

    async def go():
        await k.tick()
        await asyncio.sleep(0)
        assert started == [False], started
        k.status.setdefault("science", {})["tried"] = __import__("time").time()
        k._task = None
        await k.tick()
        await asyncio.sleep(0)
        assert started == [False]                                     # tried a moment ago: not again yet
        assert k.start_now("science") == "started"
        await asyncio.sleep(0)
        assert started == [False, True]
    asyncio.run(go())
    off = Knowledge(tempfile.mkdtemp(), {"knowledge_science": False})
    assert not off.science.due()
    s2 = Science(tempfile.mkdtemp(), {"materials_project_key": "  abc "})
    assert s2.mp_key == "abc" and s2.due()
    assert science.UA.startswith("HomeMind/") and "@" not in science.UA
    print("SCIENCE OK")


main()
