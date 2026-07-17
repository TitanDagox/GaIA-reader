#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Editor VISUAL de planes de figuras — punto de control humano de la ingesta.

Levanta una mini-app local (puerto 8902, independiente del backend y de Qdrant, así
que puede correr en cualquier momento) donde el usuario revisa y corrige el plan de figuras
SIN tocar JSON: arrastrar imágenes entre grupos, editar pies, omitir/descartar,
recortar con el mouse (Cropper.js) y aprobar con un botón.

Uso:
    python revisar_figuras.py                  # abre el editor (elegir doc en la UI)
    python revisar_figuras.py "<doc_stem>"     # abre directo ese documento

Al aprobar, re-ejecutar la ingesta del doc para continuar (describir + indexar):
    python ingesta.py "<archivo.pdf>"
"""

import json
import os
import sys
import webbrowser
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

load_dotenv()

MD_DIR = Path(os.environ.get("MD_DIR", "./md"))
OUT_DIR = Path("./descripciones")
BASE = Path(__file__).resolve().parent
PORT = int(os.environ.get("REVISION_PORT", "8902"))

app = FastAPI(title="Revisión visual de figuras")


def _plan_path(doc: str) -> Path:
    return OUT_DIR / f"{doc}_figuras_plan.json"


@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE / "revision_figuras.html").read_text(encoding="utf-8")


@app.get("/planes")
def planes():
    """Todos los planes existentes, con su estado, para el selector de la UI."""
    out = []
    for p in sorted(OUT_DIR.glob("*_figuras_plan.json")):
        doc = p.name[: -len("_figuras_plan.json")]
        try:
            aprobado = json.loads(p.read_text(encoding="utf-8")).get("aprobado", False)
        except Exception:
            aprobado = None
        out.append({"doc": doc, "aprobado": aprobado})
    return {"planes": out}


@app.get("/plan")
def plan(doc: str):
    p = _plan_path(doc)
    if not p.exists():
        raise HTTPException(404, f"No existe plan para '{doc}'")
    return json.loads(p.read_text(encoding="utf-8"))


class PlanReq(BaseModel):
    doc: str
    plan: dict


@app.post("/plan")
def guardar_plan(req: PlanReq):
    p = _plan_path(req.doc)
    if not p.exists():
        raise HTTPException(404, f"No existe plan para '{req.doc}'")
    p.write_text(json.dumps(req.plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "aprobado": bool(req.plan.get("aprobado"))}


@app.get("/img")
def img(doc: str, img: str):
    p = MD_DIR / f"{doc}_figs" / Path(img).name   # Path(...).name evita rutas raras
    if not p.exists():
        raise HTTPException(404, "imagen no existe")
    return FileResponse(p)


class CropReq(BaseModel):
    doc: str
    img: str
    x: float
    y: float
    w: float
    h: float


def _invalidar_cache(doc: str, img: str):
    """Si la imagen cambió (recorte/reemplazo), su descripción cacheada queda obsoleta."""
    c = OUT_DIR / f"_cache_{doc}" / f"{img}.md"
    if c.exists():
        c.unlink()


@app.post("/recortar")
def recortar(req: CropReq):
    """Recorta la imagen ORIGINAL en disco (respaldo .bak la primera vez)."""
    from PIL import Image
    p = MD_DIR / f"{req.doc}_figs" / Path(req.img).name
    if not p.exists():
        raise HTTPException(404, "imagen no existe")
    bak = p.with_suffix(p.suffix + ".bak")
    if not bak.exists():
        bak.write_bytes(p.read_bytes())
    with Image.open(p) as im:
        caja = (max(0, int(req.x)), max(0, int(req.y)),
                min(im.width, int(req.x + req.w)), min(im.height, int(req.y + req.h)))
        if caja[2] - caja[0] < 10 or caja[3] - caja[1] < 10:
            raise HTTPException(400, "recorte demasiado pequeño")
        rec = im.crop(caja)
        if p.suffix.lower() == ".png":
            rec.save(p, "PNG")
        else:
            rec.convert("RGB").save(p, "JPEG", quality=92)
    _invalidar_cache(req.doc, p.name)
    return {"ok": True}


class RestReq(BaseModel):
    doc: str
    img: str


@app.post("/restaurar")
def restaurar(req: RestReq):
    p = MD_DIR / f"{req.doc}_figs" / Path(req.img).name
    bak = p.with_suffix(p.suffix + ".bak")
    if not bak.exists():
        raise HTTPException(404, "no hay respaldo para restaurar")
    p.write_bytes(bak.read_bytes())
    _invalidar_cache(req.doc, p.name)
    return {"ok": True}


class SubirReq(BaseModel):
    doc: str
    data: str              # dataURL (data:image/...;base64,xxx) o base64 pelado
    nombre: str | None = None   # si viene, REEMPLAZA esa imagen (con respaldo .bak)


@app.post("/subir")
def subir(req: SubirReq):
    """Sube una captura del usuario: nueva (se agrega al grupo desde la UI) o
    reemplazando una imagen mal extraída (mismo nombre, respaldo .bak)."""
    import base64
    import io
    import re as _re
    import time
    from PIL import Image
    m = _re.match(r"data:image/\w+;base64,(.+)", req.data, _re.S)
    try:
        raw = base64.b64decode(m.group(1) if m else req.data)
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        raise HTTPException(400, "el archivo no es una imagen válida")
    figs = MD_DIR / f"{req.doc}_figs"
    if not figs.exists():
        raise HTTPException(404, "el documento no tiene carpeta de figuras")
    if req.nombre:
        p = figs / Path(req.nombre).name
        if not p.exists():
            raise HTTPException(404, "la imagen a reemplazar no existe")
        bak = p.with_suffix(p.suffix + ".bak")
        if not bak.exists():
            bak.write_bytes(p.read_bytes())
    else:
        p = figs / f"subida_{int(time.time() * 1000)}.jpeg"
    if p.suffix.lower() == ".png":
        im.save(p, "PNG")
    else:
        im.convert("RGB").save(p, "JPEG", quality=92)
    _invalidar_cache(req.doc, p.name)
    return {"ok": True, "img": p.name}


if __name__ == "__main__":
    doc = sys.argv[1] if len(sys.argv) > 1 else ""
    url = f"http://127.0.0.1:{PORT}/" + (f"?doc={doc}" if doc else "")
    print(f"Editor de revisión de figuras:  {url}   (Ctrl+C para cerrar)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
