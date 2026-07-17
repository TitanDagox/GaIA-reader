#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paso 3d — Indexar en Qdrant local.

Junta las tres fuentes de un documento y las sube como puntos a Qdrant:
  - Chunks de TEXTO       (chunk.py sobre md/<doc>.md)           type="text"
  - Descripciones de FIGURAS (descripciones/<doc>.jsonl)         type="figure"
  - Explicaciones de TABLAS  (descripciones/<doc>_tablas.jsonl)  type="table"

Embeddings: gemini-embedding-2, taskType=RETRIEVAL_DOCUMENT, dim 1536 (batch).
IDs deterministas (uuid5) → re-indexar ACTUALIZA en vez de duplicar.
Qdrant LOCAL embebido (QDRANT_PATH). Un solo proceso a la vez (bloquea la carpeta).

Uso:
    python indexar.py "Porphyry Copper Systems (Sillitoe, 2010)_noocr"
"""

import os
import sys
import json
import uuid
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue

from chunk import chunkear

load_dotenv()

MD_DIR = Path(os.environ.get("MD_DIR", "./md"))
DESC_DIR = Path("./descripciones")
QDRANT_PATH = os.environ.get("QDRANT_PATH", "./qdrant_data")
COLLECTION = os.environ.get("COLLECTION_NAME", "papers")
EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "gemini").lower()  # gemini | ollama (local)
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-2")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1536"))
OLLAMA_LOCAL_URL = os.environ.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434")
MANIFEST = Path("./embed_manifest.json")
NS = uuid.UUID("d3adb33f-0000-4000-8000-000000000001")  # namespace fijo para uuid5

_EP = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:batchEmbedContents"


def verificar_manifest():
    """Modelo y corpus están CASADOS: indexar con un modelo distinto al del corpus
    existente produce resultados basura silenciosos. Aborta si hay mismatch."""
    if MANIFEST.exists():
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if m.get("embed_model") != EMBED_MODEL or int(m.get("embed_dim", 0)) != EMBED_DIM:
            raise SystemExit(
                f"ERROR: el corpus fue indexado con {m.get('embed_model')}/{m.get('embed_dim')} "
                f"pero el .env pide {EMBED_MODEL}/{EMBED_DIM}.\n"
                f"O restaura el .env, o borra qdrant_data/ y embed_manifest.json y re-indexa TODO.")


def escribir_manifest():
    MANIFEST.write_text(json.dumps(
        {"embed_model": EMBED_MODEL, "embed_dim": EMBED_DIM, "collection": COLLECTION,
         "creado": time.strftime("%Y-%m-%d")}, ensure_ascii=False, indent=2), encoding="utf-8")


def embed_batch(textos: list[str]) -> list[list[float]]:
    """Embebe una lista de textos (documentos) en un solo request batch."""
    if EMBED_PROVIDER == "ollama":
        # Modelo local vía Ollama (bge-m3, qwen3-embedding…): sin key ni cuota.
        r = requests.post(f"{OLLAMA_LOCAL_URL}/api/embed",
                          json={"model": EMBED_MODEL, "input": textos}, timeout=300)
        r.raise_for_status()
        return r.json()["embeddings"]
    reqs = [{"model": f"models/{EMBED_MODEL}",
             "content": {"parts": [{"text": t}]},
             "taskType": "RETRIEVAL_DOCUMENT",
             "outputDimensionality": EMBED_DIM} for t in textos]
    for intento in range(4):
        r = requests.post(_EP, json={"requests": reqs},
                          headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, timeout=120)
        if r.status_code in (503, 429) and intento < 3:
            time.sleep(2 ** intento * 5)
            continue
        r.raise_for_status()
        break
    return [e["values"] for e in r.json()["embeddings"]]


def _pid(source: str, tipo: str, key: str) -> str:
    return str(uuid.uuid5(NS, f"{source}|{tipo}|{key}"))


def recopilar(stem: str) -> list[dict]:
    """Devuelve registros {text_embed, payload} de texto + figuras + tablas."""
    source = stem.replace("_noocr", "")
    regs = []

    # 1) TEXTO
    md = (MD_DIR / f"{stem}.md").read_text(encoding="utf-8")
    for c in chunkear(md, source=source):
        if c.type != "text":   # las tablas del chunker se reemplazan por sus explicaciones
            continue
        regs.append({
            "text_embed": c.content,  # ya incluye [breadcrumb]
            "payload": {"texto": c.texto, "breadcrumb": c.breadcrumb,
                        "source": source, "type": "text"},
            "id": _pid(source, "text", str(c.indice)),
        })

    # 2) FIGURAS
    figs = DESC_DIR / f"{stem}.jsonl"
    if figs.exists():
        for ln in figs.read_text(encoding="utf-8").splitlines():
            d = json.loads(ln)
            texto = f"{d.get('caption','')}\n{d['descripcion']}".strip()
            regs.append({
                "text_embed": texto,
                "payload": {"texto": d["descripcion"], "caption": d.get("caption", ""),
                            "source": source, "type": "figure", "img": d.get("img", "")},
                "id": _pid(source, "figure", d.get("img", "")),
            })

    # 3) TABLAS
    tabs = DESC_DIR / f"{stem}_tablas.jsonl"
    if tabs.exists():
        for i, ln in enumerate(tabs.read_text(encoding="utf-8").splitlines()):
            d = json.loads(ln)
            texto = f"{d.get('caption','')}\n{d['descripcion']}".strip()
            regs.append({
                "text_embed": texto,
                "payload": {"texto": d["descripcion"], "caption": d.get("caption", ""),
                            "source": source, "type": "table"},
                "id": _pid(source, "table", str(i)),
            })
    return regs


def main(stem: str):
    verificar_manifest()
    regs = recopilar(stem)
    tipos = {}
    for r in regs:
        tipos[r["payload"]["type"]] = tipos.get(r["payload"]["type"], 0) + 1
    print(f"{len(regs)} puntos a indexar: {tipos}")

    # Embeddings en batches
    vectores = []
    B = 50
    for i in range(0, len(regs), B):
        lote = regs[i:i + B]
        print(f"  embeddings {i+1}-{i+len(lote)} / {len(regs)} ...", flush=True)
        vectores.extend(embed_batch([r["text_embed"] for r in lote]))

    # Qdrant local
    client = QdrantClient(path=QDRANT_PATH)
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE))
        print(f"Colección '{COLLECTION}' creada ({EMBED_DIM} dims, Cosine).")
    else:
        # Re-indexado idempotente: borrar los puntos previos de ESTE documento
        # (así los chunks que ya no se generan —ej. References— no quedan huérfanos).
        source = regs[0]["payload"]["source"] if regs else stem.replace("_noocr", "")
        client.delete(COLLECTION, points_selector=Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=source))]))

    puntos = [PointStruct(id=r["id"], vector=v, payload=r["payload"])
              for r, v in zip(regs, vectores)]
    client.upsert(COLLECTION, points=puntos)
    total = client.count(COLLECTION, exact=True).count
    client.close()   # cerrar explícito → evita el warning de shutdown en Windows
    escribir_manifest()
    print(f"Indexados {len(puntos)} puntos. Total en '{COLLECTION}': {total}.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('Uso: python indexar.py "<doc_stem sin .md>"')
    main(sys.argv[1])
