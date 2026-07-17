"""Medidor de recuperación RAG contra eval/rag_eval.jsonl.

Pega a /search del backend VIVO (127.0.0.1:8901) y mide, por tipo de pregunta,
si aparece lo que "debe_recuperar". Sirve para comparar ANTES/DESPUÉS del re-ranking.

Uso:
  python eval/run_eval.py            # línea base (sin re-ranking)
  python eval/run_eval.py --rerank   # con re-ranking (si el backend lo soporta)
  python eval/run_eval.py --corpus   # buscar en TODO el corpus (no acotar al doc)

Requiere el backend corriendo. Lee RAG_TOKEN del .env. No gasta tokens de LLM
salvo el embedding de cada consulta (y el re-ranking, si está activo).
"""
import json
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent.parent
# Set opcional como argumento posicional (por defecto rag_eval.jsonl).
_files = [a for a in sys.argv[1:] if not a.startswith("--")]
EVAL = Path(_files[0]) if _files else (BASE / "eval" / "rag_eval.jsonl")
URL = "http://127.0.0.1:8901/search"
TOKEN = dotenv_values(BASE / ".env").get("RAG_TOKEN", "") or ""
HDRS = {"Content-Type": "application/json"}
if TOKEN:
    HDRS["Authorization"] = f"Bearer {TOKEN}"

RERANK = "--rerank" in sys.argv
ACOTAR = "--corpus" not in sys.argv   # por defecto acota al doc de la pregunta


def buscar(query, doc):
    body = {"query": query, "top_k": 6, "rerank": RERANK}
    if ACOTAR:
        body["doc"] = doc
    r = requests.post(URL, json=body, headers=HDRS, timeout=90)
    r.raise_for_status()
    return r.json()["results"]


def blob(res):
    return " ".join([res.get("titulo", ""), res.get("texto", ""),
                     res.get("img", ""), res.get("source", "")]).lower()


def tipo_ok(res, tipo):
    if tipo == "figura":
        return res.get("type") == "figure"
    if tipo == "tabla":
        return res.get("type") == "table"
    return True


def acierto(results, terms, tipo):
    """True si algún término aparece en un resultado del tipo correcto."""
    for res in results:
        if not tipo_ok(res, tipo):
            continue
        b = blob(res)
        if any(t.lower() in b for t in terms):
            return True
    return False


def img_esperada(terms):
    for t in terms:
        if t.startswith("_page"):
            return t
    return None


def rank_figura(results, img_id):
    """Posición (1-based) de la figura esperada entre las figuras recuperadas; 0 si no está."""
    figs = [r for r in results if r.get("type") == "figure"]
    for i, r in enumerate(figs, 1):
        if img_id and img_id.lower() in (r.get("img", "").lower()):
            return i
    return 0


def main():
    preguntas = [json.loads(l) for l in EVAL.read_text(encoding="utf-8").splitlines() if l.strip()]
    modo = ("RE-RANKING" if RERANK else "LÍNEA BASE") + (" · corpus" if not ACOTAR else " · por-doc")
    print(f"=== {modo} — {len(preguntas)} preguntas ===\n")

    por_tipo = {}
    detalle_fig = []
    for q in preguntas:
        tipo = q["tipo"]
        res = buscar(q["pregunta"], q["doc"])
        ok = acierto(res, q["debe_recuperar"], tipo)
        por_tipo.setdefault(tipo, [0, 0])
        por_tipo[tipo][0] += 1 if ok else 0
        por_tipo[tipo][1] += 1
        if tipo == "figura":
            img = img_esperada(q["debe_recuperar"])
            rk = rank_figura(res, img)
            detalle_fig.append((q["pregunta"][:55], img, rk, ok))
        print(f"  [{'OK ' if ok else 'MISS'}] ({tipo}) {q['pregunta'][:70]}")

    print("\n--- Recall por tipo ---")
    total_ok = total = 0
    for tipo, (ok, n) in sorted(por_tipo.items()):
        print(f"  {tipo:8} {ok}/{n}  ({100*ok//n}%)")
        total_ok += ok
        total += n
    print(f"  {'TOTAL':8} {total_ok}/{total}  ({100*total_ok//total}%)")

    if detalle_fig:
        print("\n--- Detalle figuras (rank de la figura esperada entre las recuperadas) ---")
        for preg, img, rk, ok in detalle_fig:
            pos = f"rank {rk}" if rk else "NO recuperada"
            print(f"  [{'OK ' if ok else 'MISS'}] {pos:14} {img:24} {preg}")


if __name__ == "__main__":
    main()
