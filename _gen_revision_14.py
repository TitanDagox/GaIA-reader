# -*- coding: utf-8 -*-
"""Genera una página única con los grupos de figuras de los 14 papers auto-aprobados,
para revisarlos de un vistazo. También regenera los HTML individuales. Solo LEE los
plan.json existentes; no llama a ninguna API."""
import json
import html as _html
from urllib.parse import quote
from pathlib import Path
import describir_figuras as d

DESC = Path("descripciones")
STEMS = [
    "Band Selection Hyperspectral Std-Dev (Kurz 2025)",
    "Clay Minerals Mapping Imaging Spectroscopy (Grandjean 2019)",
    "GSI Characterization Tool for Rock Masses (Marinos-Hoek 2007)",
    "Hazard Classification Stability Steep Slopes Open-Pit (Li 2022)",
    "Hoek-Brown Failure Criterion - 2002 Edition (Hoek 2002)",
    "HySpex Alteration Porphyry Cu RandomForest (Wang 2022)",
    "Lithological Mapping Landsat-8 ASTER Anti-Atlas (Baid 2023)",
    "Porphyry Copper Systems (Sillitoe 2010)",
    "Practical Estimates of Rock Mass Strength (Hoek-Brown 1997)",
    "Quantification of the GSI Chart (Hoek 2013)",
    "The Andean Porphyry Systems (Camus 2005)",
    "The GSI - Applications and Limitations (Marinos-Hoek 2005)",
    "The Hoek-Brown Failure Criterion and GSI - 2018 Edition (Hoek-Brown 2018)",
    "The Q-Slope Method for Rock Slope Engineering (Bar-Barton 2017)",
]

secciones = []
for stem in STEMS:
    p = DESC / f"{stem}_figuras_plan.json"
    if not p.exists():
        secciones.append(f"<section><h2>{_html.escape(stem)}</h2><p>(sin plan)</p></section>")
        continue
    plan = json.loads(p.read_text(encoding="utf-8"))
    # regenerar también el HTML individual (por si lo prefiere)
    d._escribir_html_revision(plan, DESC / f"{stem}_figuras_revision.html", stem)
    grupos = []
    for g in plan.get("grupos", []):
        accion = (f"FUSIONA {len(g['imgs'])}→1 mosaico" if len(g["imgs"]) > 1 else "figura individual")
        omit = ' <span class="omit">[OMITIDA]</span>' if g.get("omitir") else ""
        imgs = "".join(
            f'<figure><img src="../md/{quote(stem)}_figs/{quote(i)}" loading="lazy">'
            f"<figcaption>{_html.escape(i)}</figcaption></figure>" for i in g["imgs"])
        grupos.append(f'<div class="grp"><div class="lab">Grupo {g.get("n","?")} — {accion}{omit}</div>'
                      f'<p class="cap">{_html.escape(g.get("caption") or "(sin pie detectado)")}</p>'
                      f'<div class="row">{imgs}</div></div>')
    desc = ", ".join(_html.escape(x.get("img", "")) for x in plan.get("descartadas", [])) or "(ninguna)"
    secciones.append(f'<section><h2>{_html.escape(stem)}</h2>{"".join(grupos)}'
                     f'<p class="desc">Descartadas como logo/decoración: {desc}</p></section>')

pagina = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Revisión de figuras — 14 papers auto-aprobados</title><style>
body{{font-family:system-ui;margin:2rem;background:#f4f4f0;color:#222;max-width:1200px}}
h1{{position:sticky;top:0;background:#f4f4f0;padding:.5rem 0;border-bottom:2px solid #999}}
section{{background:#fff;border:1px solid #ccc;padding:1rem;margin:1.5rem 0;border-radius:6px}}
h2{{margin:0 0 .8rem;font-size:1.05rem;color:#7a3b1d}}
.grp{{border-top:1px dashed #ddd;padding:.6rem 0}}
.lab{{font-family:monospace;font-size:.8rem;color:#333;font-weight:bold}}
.cap{{color:#555;font-size:.9rem;margin:.2rem 0 .6rem}}
.row{{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start}}
figure{{margin:0;text-align:center}} img{{max-height:230px;max-width:330px;border:1px solid #999;background:#fff}}
figcaption{{font-family:monospace;font-size:.68rem;color:#888}}
.omit{{color:#c00}} .desc{{color:#888;font-size:.8rem;margin-top:.6rem}}
</style></head><body>
<h1>Revisión de figuras — {len(STEMS)} papers auto-aprobados</h1>
<p>Escanea cada paper: ¿mosaicos bien agrupados? ¿algún logo colado? Si algo se ve mal,
avísame el paper y lo re-describimos. (Descripciones y búsqueda ya están actualizadas; esto
es solo para revisar la <b>agrupación</b> de figuras.)</p>
{"".join(secciones)}
</body></html>"""

out = DESC / "_REVISION_14_papers.html"
out.write_text(pagina, encoding="utf-8")
print(f"OK -> {out.resolve()}")
