"""Smoke test corto para el backend de Papers_Asistente (GaIA).

Verifica que lo básico vive tras tocar código: /health, /documentos, /outline,
/search y /figura. NO llama /ask ni /quiz (gastan tokens de LLM).

Uso:
    python scripts/smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

BASE_URL = "http://127.0.0.1:8901"
TIMEOUT = 30  # segundos — /search puede tardar por el embedding

# Carga el .env de la raíz del proyecto (junto a backend.py)
RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")
RAG_TOKEN = os.environ.get("RAG_TOKEN", "")

HEADERS = {"Authorization": f"Bearer {RAG_TOKEN}"} if RAG_TOKEN else {}

checks = []  # (nombre, ok: bool, detalle: str)


def check(nombre, ok, detalle):
    estado = "[OK]" if ok else "[FALLA]"
    print(f"{estado} {nombre} — {detalle}")
    checks.append((nombre, ok, detalle))


def main():
    # 1. GET /health
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=TIMEOUT)
        ok = r.status_code == 200 and r.json().get("status") == "ok"
        check("GET /health", ok, f"HTTP {r.status_code}, body={r.json()}")
    except Exception as e:
        check("GET /health", False, f"excepción: {e}")

    # 2. GET /documentos
    docs = []
    try:
        r = requests.get(f"{BASE_URL}/documentos", timeout=TIMEOUT)
        data = r.json()
        docs = data.get("docs", [])
        ok = r.status_code == 200 and len(docs) > 0
        check("GET /documentos", ok, f"HTTP {r.status_code}, {len(docs)} documentos")
    except Exception as e:
        check("GET /documentos", False, f"excepción: {e}")

    primer_doc = docs[0]["source"] if docs else None

    # 3. GET /outline?doc=<primer doc real>
    if primer_doc:
        try:
            r = requests.get(f"{BASE_URL}/outline", params={"doc": primer_doc}, timeout=TIMEOUT)
            ok = r.status_code == 200 and "secciones" in r.json()
            n_sec = len(r.json().get("secciones", [])) if ok else 0
            check("GET /outline", ok, f"doc='{primer_doc}', HTTP {r.status_code}, {n_sec} secciones")
        except Exception as e:
            check("GET /outline", False, f"excepción: {e}")
    else:
        check("GET /outline", False, "sin documentos para probar (se saltó)")

    # 4. POST /search
    try:
        r = requests.post(
            f"{BASE_URL}/search",
            json={"query": "¿Qué es un pórfido cuprífero?", "top_k": 6},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        data = r.json()
        resultados = data.get("results", [])
        ok = r.status_code == 200 and len(resultados) > 0
        check("POST /search", ok, f"HTTP {r.status_code}, {len(resultados)} resultados")
    except Exception as e:
        check("POST /search", False, f"excepción: {e}")

    # 5. GET /figura — busca una figura real vía /figuras, probando doc por doc si hace falta
    fig_encontrada = False
    if docs:
        for d in docs:
            source = d["source"]
            try:
                r = requests.get(f"{BASE_URL}/figuras", params={"doc": source}, timeout=TIMEOUT)
                figs = r.json().get("figuras", []) if r.status_code == 200 else []
            except Exception:
                figs = []
            if figs and figs[0].get("img"):
                img = figs[0]["img"]
                try:
                    r2 = requests.get(f"{BASE_URL}/figura", params={"doc": source, "img": img}, timeout=TIMEOUT)
                    ok = r2.status_code == 200 and len(r2.content) > 0
                    check("GET /figura", ok, f"doc='{source}', img='{img}', HTTP {r2.status_code}, {len(r2.content)} bytes")
                except Exception as e:
                    check("GET /figura", False, f"excepción: {e}")
                fig_encontrada = True
                break
        if not fig_encontrada:
            check("GET /figura", False, "ningún documento tiene figuras indexadas (se saltó)")
    else:
        check("GET /figura", False, "sin documentos para probar (se saltó)")

    # Resumen final
    total = len(checks)
    ok_count = sum(1 for _, ok, _ in checks if ok)
    print(f"\n{ok_count}/{total} checks OK")
    return 0 if ok_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
