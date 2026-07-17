#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orquestador de ingesta — un comando para procesar un PDF de principio a fin.

Encadena: router → extraer → describir figuras → explicar tablas → esquema por
secciones → indexar en Qdrant local. Cada sub-paso es reanudable (usa sus propios
caches), así que si algo falla, re-ejecutar retoma donde quedó.

Uso:
    python ingesta.py "nombre del archivo.pdf"      # debe estar en raw/

Router:
  - PDF DIGITAL (texto seleccionable) → marker con disable_ocr=True (rápido, ~14 min/40pág).
  - PDF ESCANEADO (imagen)            → marker con OCR (más lento, pero necesario).
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RAW_DIR = Path(os.environ.get("RAW_DIR", "./raw"))
MD_DIR = Path(os.environ.get("MD_DIR", "./md"))

TEXTO_MINIMO = 40  # chars en la 1ª página; debajo de esto se asume escaneado


def es_pdf_digital(pdf_path: Path) -> bool:
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            return False
        return len((pdf.pages[0].extract_text() or "").strip()) >= TEXTO_MINIMO


def main(nombre_pdf: str):
    pdf_path = RAW_DIR / nombre_pdf
    if not pdf_path.exists():
        sys.exit(f"ERROR: no existe {pdf_path}. Copia el PDF a raw/ primero.")
    stem = pdf_path.stem

    print(f"\n{'='*60}\nINGESTA: {nombre_pdf}\n{'='*60}")

    # 1. Router + 2. Extracción (saltar si ya existe el md — es el paso caro)
    md_path = MD_DIR / f"{stem}.md"
    if md_path.exists():
        print(f"[1-2] Extracción: ya existe {md_path.name}, salto.")
    else:
        digital = es_pdf_digital(pdf_path)
        print(f"[1] Router: {'DIGITAL' if digital else 'ESCANEADO'}")
        print(f"[2] Extrayendo con marker (disable_ocr={digital})... puede tardar.")
        from extraer import extraer
        extraer(nombre_pdf, disable_ocr=digital, sufijo="")

    # 3. Figuras  4. Tablas  5. Secciones  6. Indexar (cada uno reanudable)
    import describir_figuras, describir_tablas, secciones, indexar
    print("\n[3] Describiendo figuras...");  describir_figuras.main(stem)
    print("\n[4] Explicando tablas...");     describir_tablas.main(stem)
    print("\n[5] Esquema por secciones..."); secciones.main(stem)
    print("\n[6] Indexando en Qdrant...");   indexar.main(stem)

    print(f"\n{'='*60}\nLISTO: '{stem}' ingerido e indexado.\n{'='*60}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('Uso: python ingesta.py "<archivo.pdf en raw/>"')
    main(sys.argv[1])
