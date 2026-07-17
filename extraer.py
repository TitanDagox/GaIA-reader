#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paso 1 de la ingesta — Extracción de un PDF digital con marker-pdf.

Objetivo de esta etapa (verificable a ojo antes de seguir):
  - Respetar el layout de 2 columnas (papers académicos).
  - Extraer las FIGURAS como archivos de imagen aparte (esenciales en Sillitoe).
  - Dejar un .md legible en md/<nombre>.md y las imágenes en md/<nombre>_figs/.

Uso:
    python extraer.py "Porphyry Copper Systems (Sillitoe, 2010).pdf"
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RAW_DIR = Path(os.environ.get("RAW_DIR", "./raw"))
MD_DIR = Path(os.environ.get("MD_DIR", "./md"))


def extraer(nombre_pdf: str, disable_ocr: bool = False, sufijo: str = ""):
    pdf_path = RAW_DIR / nombre_pdf
    if not pdf_path.exists():
        sys.exit(f"ERROR: no existe {pdf_path}")

    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered

    # disable_ocr=True → usa la capa de texto existente del PDF (rápido, sin re-OCR).
    # Solo válido para PDFs DIGITALES (con texto seleccionable).
    config = {"disable_ocr": True} if disable_ocr else {}

    print(f"Cargando modelos de marker... (disable_ocr={disable_ocr})")
    converter = PdfConverter(artifact_dict=create_model_dict(), config=config)

    print(f"Extrayendo '{pdf_path.name}' ...")
    rendered = converter(str(pdf_path))
    texto, _, imagenes = text_from_rendered(rendered)

    stem = pdf_path.stem + sufijo
    MD_DIR.mkdir(parents=True, exist_ok=True)
    md_path = MD_DIR / f"{stem}.md"
    md_path.write_text(texto, encoding="utf-8")

    # Guardar las figuras extraídas
    figs_dir = MD_DIR / f"{stem}_figs"
    n_figs = 0
    if imagenes:
        figs_dir.mkdir(parents=True, exist_ok=True)
        for nombre, img in imagenes.items():
            destino = figs_dir / nombre
            img.save(destino)
            n_figs += 1

    print(f"\n--- RESULTADO ---")
    print(f"Markdown: {md_path}  ({len(texto):,} chars)")
    print(f"Figuras extraídas: {n_figs}  -> {figs_dir if n_figs else '(ninguna)'}")
    return md_path, figs_dir, n_figs


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('Uso: python extraer.py "<archivo en raw/>" [--no-ocr]')
    no_ocr = "--no-ocr" in sys.argv
    extraer(sys.argv[1], disable_ocr=no_ocr, sufijo="_noocr" if no_ocr else "")
