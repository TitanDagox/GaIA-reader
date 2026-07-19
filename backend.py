#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backend.py — Backend FastAPI del Papers Assistant. Corre LOCAL en el PC.

Qdrant LOCAL embebido + embeddings Gemini + LLM de chat multi-proveedor
(Ollama Cloud / Gemini / DeepSeek).

Endpoints:
  GET  /health   → estado + nº de puntos por tipo
  GET  /providers→ proveedores de chat disponibles (según keys del .env)
  GET  /documentos → documentos indexados (sources)
  GET  /outline?doc=... → resumen por secciones pre-computado (de outlines/)
  POST /search   → recuperación semántica (texto + figuras + tablas)
  POST /ask      → RAG con citas + FALLBACK WEB si el documento no cubre la pregunta

Auth: Bearer RAG_TOKEN (protege si algún día se expone el puerto).
OJO: Qdrant embebido BLOQUEA qdrant_data/ → parar este backend antes de reindexar.

Arranque:  python -m uvicorn backend:app --host 127.0.0.1 --port 8901
"""

import os
import sys
import re
import json

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
import time
import uuid
import base64
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny

# ── Config ───────────────────────────────────────────────────────────────────
# Rutas relativas al ARCHIVO (no al cwd) → el backend arranca desde cualquier carpeta.
BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")


def _ruta(env_key: str, default: str) -> Path:
    p = Path(os.environ.get(env_key, default))
    return p if p.is_absolute() else BASE / p


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Proveedor de embeddings: gemini (default, cloud) | ollama (local: bge-m3, qwen3-embedding…).
# OJO: modelo y corpus están CASADOS (ver embed_manifest.json); cambiar ⇒ re-indexar todo.
EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "gemini").lower()
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-2")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1536"))
OLLAMA_LOCAL_URL = os.environ.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434")
QDRANT_PATH = str(_ruta("QDRANT_PATH", "./qdrant_data"))
COLLECTION = os.environ.get("COLLECTION_NAME", "papers")
RAG_TOKEN = os.environ.get("RAG_TOKEN", "")
MD_DIR = _ruta("MD_DIR", "./md")
RAW_DIR = _ruta("RAW_DIR", "./raw")               # PDFs originales (para el visor)
OUTLINES_DIR = _ruta("OUTLINES_DIR", "./outlines")
CONV_DIR = _ruta("CONV_DIR", "./conversaciones")   # libretas de terreno (JSON)
CONV_DIR.mkdir(parents=True, exist_ok=True)

CHAT_PROVIDER = os.environ.get("CHAT_PROVIDER", "gemini").lower()
OLLAMA_URL = os.environ.get("OLLAMA_CLOUD_URL", "https://ollama.com")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")
GEMINI_CHAT_MODEL = os.environ.get("GEMINI_CHAT_MODEL", "gemini-3.5-flash")
DEEPSEEK_URL = os.environ.get("DEEPSEEK_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_REASONING_EFFORT = os.environ.get("DEEPSEEK_REASONING_EFFORT", "high")  # high|max
# Techo de salida del chat/quiz. 4096 truncaba quizzes grandes (10 MC ricas) → JSON cortado.
CHAT_MAX_TOKENS = int(os.environ.get("CHAT_MAX_TOKENS", "8192"))

from anthropic import AnthropicBedrock
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-6")
_bedrock = None
def _bedrock_client():
    global _bedrock
    if _bedrock is None:
        _bedrock = AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-east-1"))
    return _bedrock

WEB_FALLBACK = False

# Sin GEMINI_API_KEY la app ARRANCA igual (modo configuración): el frontend abre el
# wizard y las rutas que necesitan la key devuelven 503 con instrucción clara.
SETUP_REQUIRED = not GEMINI_API_KEY
if SETUP_REQUIRED:
    print("AVISO: falta GEMINI_API_KEY en .env — arrancando en modo configuración "
          "(completa las keys en la pestaña Configuración de la app).")

qdrant = QdrantClient(path=QDRANT_PATH)
session = requests.Session()
app = FastAPI(title="Papers Assistant Backend (local)")
# App local: solo orígenes de loopback (la UI se sirve same-origin en 127.0.0.1).
app.add_middleware(CORSMiddleware,
                   allow_origin_regex=r"http://(127\.0\.0\.1|localhost)(:\d+)?",
                   allow_methods=["*"], allow_headers=["*"])

VISION_OK = {"ollama": True, "gemini": True, "deepseek": False}


def check_token(authorization):
    if RAG_TOKEN and authorization != f"Bearer {RAG_TOKEN}":
        raise HTTPException(status_code=401, detail="Token inválido o ausente")


# ── Perfil del usuario (inyectado en los prompts del tutor/quiz/Anki) ────────
# perfil.json es local (gitignored). Sin archivo → perfil por defecto NEUTRAL
# (agnóstico de disciplina): así, recién clonado, GaIA es un tutor genérico y no
# arrastra el perfil de nadie. Editable desde la pestaña Configuración.
PERFIL_PATH = BASE / "perfil.json"
PERFIL_CAMPOS = ("nombre", "rol", "disciplina", "nivel", "objetivo", "instrucciones")
PERFIL_DEFAULT = {
    "nombre": "",
    "rol": "lector",
    "disciplina": "temas técnicos",
    "nivel": "intermedio",               # principiante | intermedio | avanzado
    "objetivo": "entender papers y libros técnicos densos",
    "instrucciones": "",                 # instrucciones libres del lector, solo para el chat (ver sys_chat)
}


def perfil() -> dict:
    try:
        data = json.loads(PERFIL_PATH.read_text(encoding="utf-8"))
        return {**PERFIL_DEFAULT, **{k: str(v).strip() for k, v in data.items() if str(v).strip()}}
    except Exception:
        return dict(PERFIL_DEFAULT)


# ── Manifest de embeddings (modelo ↔ corpus deben coincidir) ─────────────────
# Si el corpus fue vectorizado con un modelo y se consulta con otro, los resultados
# son basura SILENCIOSA. El manifest registra con qué modelo se indexó; al arrancar
# se compara con el .env y se avisa (aquí y en /health).
EMBED_MANIFEST = BASE / "embed_manifest.json"
EMBED_MISMATCH = ""


def _check_embed_manifest():
    global EMBED_MISMATCH
    try:
        if EMBED_MANIFEST.exists():
            m = json.loads(EMBED_MANIFEST.read_text(encoding="utf-8"))
            if m.get("embed_model") != EMBED_MODEL or int(m.get("embed_dim", 0)) != EMBED_DIM:
                EMBED_MISMATCH = (f"el corpus fue indexado con {m.get('embed_model')}/{m.get('embed_dim')} "
                                  f"pero el .env pide {EMBED_MODEL}/{EMBED_DIM}. Restaura el .env o re-indexa todo.")
                print(f"ADVERTENCIA EMBEDDINGS: {EMBED_MISMATCH}")
        else:
            # Bootstrap: corpus existente sin manifest (instalación previa) → registrar el actual.
            if qdrant.collection_exists(COLLECTION) and qdrant.get_collection(COLLECTION).points_count:
                EMBED_MANIFEST.write_text(json.dumps(
                    {"embed_model": EMBED_MODEL, "embed_dim": EMBED_DIM, "collection": COLLECTION,
                     "creado": time.strftime("%Y-%m-%d")}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"AVISO: no se pudo verificar embed_manifest.json: {e}")


_check_embed_manifest()


# ── Embedding de consulta ────────────────────────────────────────────────────
def embed_query(text: str) -> list:
    if EMBED_MISMATCH:
        raise HTTPException(status_code=503, detail=f"Embeddings inconsistentes: {EMBED_MISMATCH}")
    if EMBED_PROVIDER == "ollama":
        # Modelo local vía Ollama (bge-m3, qwen3-embedding…): sin key ni cuota.
        r = session.post(f"{OLLAMA_LOCAL_URL}/api/embed",
                         json={"model": EMBED_MODEL, "input": text}, timeout=60)
        r.raise_for_status()
        return r.json()["embeddings"][0]
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Falta GEMINI_API_KEY: configúrala en la pestaña Configuración.")
    ep = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:embedContent"
    body = {"model": f"models/{EMBED_MODEL}", "content": {"parts": [{"text": text}]},
            "taskType": "RETRIEVAL_QUERY", "outputDimensionality": EMBED_DIM}
    r = session.post(ep, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=60)
    r.raise_for_status()
    return r.json()["embedding"]["values"]


def _query(vec, limit, tipo=None, doc=None, docs=None):
    must = []
    if tipo:
        must.append(FieldCondition(key="type", match=MatchValue(value=tipo)))
    if doc:                                  # un solo documento (precede)
        must.append(FieldCondition(key="source", match=MatchValue(value=doc)))
    elif docs:                               # conjunto de fuentes (un cuaderno)
        must.append(FieldCondition(key="source", match=MatchAny(any=list(docs))))
    flt = Filter(must=must) if must else None
    return qdrant.query_points(COLLECTION, query=vec, limit=limit,
                               query_filter=flt, with_payload=True).points


def _fila(h):
    p = h.payload or {}
    titulo = p.get("caption") or p.get("breadcrumb") or ""
    return {"type": p.get("type", "text"), "titulo": titulo, "texto": p.get("texto", ""),
            "source": p.get("source", ""), "img": p.get("img", ""),
            "score": round(h.score, 4)}


def buscar(query: str, top_k: int = 6, fig_k: int = 3, tab_k: int = 2, doc=None, docs=None):
    """Recuperación híbrida: texto + figuras + tablas por separado (para que
    figuras/tablas no queden tapadas por el texto en el ranking conjunto).
    `doc`=un documento; `docs`=conjunto de fuentes (cuaderno); ambos vacíos = todo el corpus."""
    vec = embed_query(query)
    out = [_fila(h) for h in _query(vec, top_k, "text", doc, docs)]
    if fig_k:
        out += [_fila(h) for h in _query(vec, fig_k, "figure", doc, docs)]
    if tab_k:
        out += [_fila(h) for h in _query(vec, tab_k, "table", doc, docs)]
    return out


def _fila_payload(p, score=0.99):
    titulo = p.get("caption") or p.get("breadcrumb") or ""
    return {"type": p.get("type", "text"), "titulo": titulo, "texto": p.get("texto", ""),
            "source": p.get("source", ""), "img": p.get("img", ""), "score": score}


def _num_caption(cap):
    """Número de figura del pie ('Figure 6. …' → 6). None si no hay.
    Tolera markdown/puntuación inicial ('**Figure 2:**' → 2)."""
    m = re.match(r"[^A-Za-z0-9]*(?:figuras?|figures?|fig)\.?\s*(\d+)", cap or "", re.I)
    return int(m.group(1)) if m else None


def figuras_por_numero(doc, nums):
    """Lookup DETERMINISTA: figuras cuyo pie de figura es 'Figure N' (no semántico).
    Resuelve 'explícame la figura 6' → trae exactamente esa figura con su imagen."""
    if not doc or not nums:
        return []
    must = [FieldCondition(key="type", match=MatchValue(value="figure")),
            FieldCondition(key="source", match=MatchValue(value=doc))]
    pts = qdrant.scroll(COLLECTION, scroll_filter=Filter(must=must),
                        limit=500, with_payload=True)[0]
    return [_fila_payload(p.payload) for p in pts
            if _num_caption((p.payload or {}).get("caption", "")) in nums]


def nums_figura_en(query):
    """Números de figura referidos explícitamente en la consulta ('figura 6', 'figs 2 y 3')."""
    return {int(n) for n in re.findall(r"(?:figuras?|figures?|fig)\.?\s*(\d+)", query or "", re.I)}


# ── Mapa del cuaderno (leyenda para consultas panorámicas) ───────────────────
_ficha_cache: dict = {}   # stem → (mtime, ficha)


def _ficha_doc(stem: str, max_chars: int = 700):
    """Ficha compacta de un paper: el resumen global de su outline (nivel 1; si el
    título raíz vino vacío —'OPEN', 'Chapter'—, la primera entrada con resumen)."""
    p = OUTLINES_DIR / f"{stem}.jsonl"
    if not p.exists():
        return None
    mt = p.stat().st_mtime
    hit = _ficha_cache.get(stem)
    if hit and hit[0] == mt:
        return hit[1]
    ficha = None
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                txt = re.sub(r"\s+", " ", d.get("resumen") or "").strip()
                if not txt:
                    continue                      # raíces vacías ('OPEN', 'Chapter')
                if len(txt) > max_chars:
                    txt = txt[:max_chars].rsplit(" ", 1)[0] + "…"
                ficha = txt
                break
    except Exception:
        ficha = None
    _ficha_cache[stem] = (mt, ficha)
    return ficha


def mapa_cuaderno(docs) -> str:
    """Bloque 'MAPA DEL CUADERNO': lista TODOS los papers del alcance con su ficha.
    Complementa al RAG en preguntas panorámicas ('¿por cuál parto?', '¿qué paper cubre X?'):
    los top_k chunks recuperados suelen tocar 2-3 docs y el modelo no ve el resto."""
    filas = []
    for stem in docs:
        ficha = _ficha_doc(stem)
        filas.append(f"• {stem}\n  {ficha}" if ficha else f"• {stem}")
    return "\n".join(filas)


# ── Proveedores de chat (yield de tokens) ────────────────────────────────────
def _chat_ollama(system, user_text, images, model):
    msg = {"role": "user", "content": user_text}
    if images:
        msg["images"] = images
    body = {"model": model or OLLAMA_MODEL,
            "messages": [{"role": "system", "content": system}, msg], "stream": True}
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}
    with session.post(f"{OLLAMA_URL}/api/chat", json=body, headers=headers,
                      stream=True, timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                tok = json.loads(line).get("message", {}).get("content", "")
                if tok:
                    yield tok
            except Exception:
                continue


def _chat_gemini(system, user_text, images, model):
    parts = [{"text": user_text}]
    for b64 in images or []:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    body = {"contents": [{"role": "user", "parts": parts}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {"maxOutputTokens": 8192}}
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model or GEMINI_CHAT_MODEL}:streamGenerateContent?alt=sse")
    with session.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY},
                      stream=True, timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or not line.startswith(b"data: "):
                continue
            try:
                data = json.loads(line[6:])
                for p in data["candidates"][0]["content"]["parts"]:
                    if p.get("thought"):
                        continue
                    if "text" in p:
                        yield p["text"]
            except Exception:
                continue


def _chat_deepseek(system, user_text, images, model, thinking=False):
    body = {"model": model or DEEPSEEK_MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user_text}], "stream": True,
            "max_tokens": CHAT_MAX_TOKENS}
    if thinking:                                   # razonamiento explícito (más lento)
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT
    with session.post(f"{DEEPSEEK_URL}/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                      stream=True, timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or not line.startswith(b"data: "):
                continue
            chunk = line[6:]
            if chunk.strip() == b"[DONE]":
                break
            try:
                delta = json.loads(chunk)["choices"][0]["delta"]
                thought = delta.get("reasoning_content", "")
                content = delta.get("content", "")
                if thought:
                    yield "thought", thought
                if content:
                    yield "content", content
            except Exception:
                continue


def _chat_bedrock(system, user_text, images, model):
    # images: para el quiz no se usan
    content = [{"type": "text", "text": user_text}]
    with _bedrock_client().messages.stream(
        model=model or BEDROCK_MODEL, max_tokens=CHAT_MAX_TOKENS,
        system=system, messages=[{"role": "user", "content": content}]
    ) as stream:
        for text in stream.text_stream:
            yield text


PROVIDERS = {"ollama": _chat_ollama, "gemini": _chat_gemini, "deepseek": _chat_deepseek, "bedrock": _chat_bedrock}

def _lector_txt(p: dict) -> str:
  """'un geólogo' / 'un lector' — descripción del lector para los prompts."""
  return "un " + p["rol"]


def sys_chat() -> str:
  p = perfil()
  nombre = p["nombre"] or "el lector"
  nivel = p.get("nivel") or "intermedio"
  return (
    "Eres Papers Assistant, un tutor experto en " + p["disciplina"] + " que ayuda a " + _lector_txt(p) +
    " a ENTENDER papers y libros técnicos. Su objetivo declarado: " + p["objetivo"] + ". "
    "NIVEL del lector: " + nivel + " — calibra la profundidad y cuánto das por sabido según ese nivel "
    "(principiante = más contexto y definiciones; avanzado = ve al grano, menos básico).\n"
    "Respondes SIEMPRE en español, claro y didáctico, con fórmulas "
    "en LaTeX cuando ayude.\n"
    "FUENTES Y CONOCIMIENTO: para afirmaciones SOBRE EL DOCUMENTO (qué dice el paper, sus datos, "
    "figuras, resultados, valores concretos), usa ÚNICAMENTE el CONTEXTO y cítalo; NO inventes datos "
    "específicos del documento. PERO si el lector pregunta por un CONCEPTO GENERAL de " + p["disciplina"] +
    " que el CONTEXTO no define (p.ej. qué significa 'resistencia a la compresión', 'módulo "
    "de deformación' o 'cohesión'), SÍ debes explicarlo con tu conocimiento general como buen tutor "
    "—NO te niegues—, avisando de forma breve que esa parte es conocimiento general y no proviene del "
    "documento (p.ej. 'Fuera del paper, en términos generales: …'). El objetivo es que " + nombre + " APRENDA.\n"
    "El CONTEXTO puede incluir descripciones de FIGURAS/GRÁFICOS y explicaciones de TABLAS: úsalas "
    "para explicar qué muestran.\n"
    "CONVERSACIÓN CONTINUA: esto es un diálogo en curso. NO saludes ni abras con muletillas "
    "('¡Hola!', 'Qué excelente pregunta', 'Como tu tutor'): entra DIRECTO al contenido. Si hay "
    "'CONVERSACIÓN PREVIA', tenla en cuenta para dar continuidad, pero NO la repitas.\n"
    "FORMATO: la respuesta se muestra en una columna ANGOSTA. Para comparar pocos atributos "
    "(2-3 columnas cortas) usa una TABLA Markdown. Pero si comparas MUCHOS atributos por elemento "
    "(p.ej. cada zona de alteración con posición, paragénesis, sulfuros y vetillas), NO uses una "
    "tabla ancha (obliga a scroll lateral y se pierde información): usa UNA ENTRADA POR ELEMENTO — "
    "un subtítulo con el nombre del elemento y debajo una lista compacta 'atributo: valor'. "
    "Evita tablas de más de 3-4 columnas.\n"
    "MOSTRAR FIGURAS: cuando expliques una fuente marcada como [FIGURA], INSERTA la imagen en tu "
    "respuesta escribiendo, en una linea SOLA, el marcador [[FIG:N]] (con el N de esa [Fuente N]) "
    "JUSTO ANTES de explicarla, para que el lector vea la figura y debajo tu explicacion de como "
    "funciona. Usa el marcador SOLO para fuentes [FIGURA] (que tienen imagen), nunca para texto ni tablas.\n"
    "LaTeX: escribe TODA expresión matemática, símbolo o unidad SIEMPRE entre signos de dólar "
    "$...$ (ej. $\\sim 4\\,\\text{wt\\%}$, $>3\\text{ km}$, $T<350\\,°C$, $\\pm$). NUNCA dejes "
    "comandos LaTeX (\\sim, \\text, \\pm, subíndices) fuera de $...$. Dentro de \\text{...} escapa "
    "el porcentaje como \\%.\n"
    "CITAS: al usar un fragmento del CONTEXTO, cítalo como (Fuente N) copiando el N del encabezado "
    "[Fuente N]. Incluye al menos una cita CUANDO te apoyes en el CONTEXTO (si respondes un concepto "
    "general que no está en el documento, no fuerces citas).\n"
    "CITAS TEXTUALES EN ESPAÑOL: cuando transcribas literalmente un fragmento del documento y el "
    "CONTEXTO esté en inglés (u otro idioma), TRADÚCELO al español dentro de las comillas —NO lo "
    "dejes en el idioma original—. Traduce con fidelidad, conservando los términos técnicos y las "
    "siglas/fórmulas (p.ej. Cu-Au-Mo, VNIR-SWIR) tal cual. Mantén igualmente el marcador (Fuente N)."
    # Instrucciones libres del lector (solo chat: quiz/Anki generan JSON y texto libre los rompe).
    + ("\nINSTRUCCIONES PERSONALES DEL LECTOR (síguelas en estilo y forma; si alguna choca con las "
       "reglas de FUENTES/citas de arriba, las reglas de arriba GANAN):\n" + p["instrucciones"][:2000]
       if p["instrucciones"] else "")
)


# ── Requests ─────────────────────────────────────────────────────────────────
class SearchReq(BaseModel):
    query: str
    top_k: int = 6
    doc: str | None = None


class AskReq(BaseModel):
    query: str
    top_k: int = 6
    provider: str | None = None
    model: str | None = None
    thinking: bool = False            # solo DeepSeek: razonamiento explícito
    attach_images: bool = True
    doc: str | None = None            # limitar a un documento (opcional)
    docs: list | None = None          # limitar a un conjunto de fuentes (cuaderno)
    doc_foco: str | None = None       # Investigación: paper que el lector tiene abierto. NO restringe
                                      # el alcance; es una pista para anclar "este paper"/"el paper".
    web_fallback: bool | None = None  # override del .env
    historial: list | None = None     # turnos previos [{q, a}] para dar memoria conversacional
    imagenes_usuario: list | None = None  # capturas adjuntadas en el chat (dataURLs); p.ej.
                                          # una tabla del PDF cuando marker la extrajo mal
    agentic: bool = False             # "Investigación profunda": el modelo indaga el corpus con
                                      # herramientas antes de responder (Fase A). Off por defecto.


# ── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    extra = {"setup_required": SETUP_REQUIRED, "embed_mismatch": EMBED_MISMATCH,
             "embed_provider": EMBED_PROVIDER}
    try:
        if not qdrant.collection_exists(COLLECTION):
            # Instalación nueva: corpus vacío NO es un error → el frontend muestra onboarding.
            return {"status": "ok", "collection": COLLECTION, "points": 0,
                    "text": 0, "figure": 0, "table": 0, "corpus_vacio": True,
                    "embed_model": EMBED_MODEL, "default_provider": CHAT_PROVIDER,
                    "web_fallback": WEB_FALLBACK, **extra}
        info = qdrant.get_collection(COLLECTION)

        def _c(t):
            return qdrant.count(COLLECTION, exact=True, count_filter=Filter(
                must=[FieldCondition(key="type", match=MatchValue(value=t))])).count
        return {"status": "ok", "collection": COLLECTION, "points": info.points_count,
                "text": _c("text"), "figure": _c("figure"), "table": _c("table"),
                "corpus_vacio": not info.points_count,
                "embed_model": EMBED_MODEL, "default_provider": CHAT_PROVIDER,
                "web_fallback": WEB_FALLBACK, **extra}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e), **extra})


def _motores():
    """Lista de motores {provider, model, label, grupo, vision, thinking_capaz} según las keys
    presentes en el .env. La usan /providers (para el selector) y _resuelve_motor (guard
    modelo↔proveedor): es la misma fuente de verdad, no dos listas que se puedan desincronizar."""
    motores = []
    if DEEPSEEK_API_KEY:
        motores.append({"provider": "deepseek", "model": "deepseek-v4-flash",
                        "label": "DeepSeek V4 Flash", "grupo": "DeepSeek",
                        "vision": False, "thinking_capaz": True})
        motores.append({"provider": "deepseek", "model": "deepseek-v4-pro",
                        "label": "DeepSeek V4 Pro", "grupo": "DeepSeek",
                        "vision": False, "thinking_capaz": True})
    if GEMINI_API_KEY:
        motores.append({"provider": "gemini", "model": GEMINI_CHAT_MODEL,
                        "label": "Gemini 3.5 Flash", "grupo": "Google",
                        "vision": True, "thinking_capaz": False})
    if OLLAMA_API_KEY:
        motores.append({"provider": "ollama", "model": OLLAMA_MODEL,
                        "label": "Gemma 4", "grupo": "Ollama Cloud",
                        "vision": True, "thinking_capaz": False})
    if os.environ.get("AWS_REGION") or os.environ.get("AWS_ACCESS_KEY_ID"):
        motores.append({"provider": "bedrock", "model": BEDROCK_MODEL,
                        "label": "Claude Sonnet 4.6", "grupo": "Amazon Bedrock",
                        "vision": True, "thinking_capaz": False})
    # "Investigación profunda" (Fase A): capaz si su proveedor tiene adaptador de tool-use.
    # Derivado de AGENT_ADAPTERS → se auto-activa al sumar Sonnet/Gemini/Gemma (una sola fuente).
    for m in motores:
        m["agentic_capaz"] = m["provider"] in AGENT_ADAPTERS
    return motores


def _resuelve_motor(provider_req, model_req):
    """Guard modelo↔proveedor: cuando el front cambia de motor pero arrastra un `model` que
    corresponde a OTRO proveedor (p.ej. quedó seleccionado "deepseek-v4-flash" y el usuario pasa
    a Gemini), enviarlo tal cual produce un 404 contra el endpoint del proveedor resuelto. Si el
    model pedido existe SOLO en otro proveedor, se descarta (None) para que cada _chat_* use su
    default; si no aparece en ningún motor conocido se deja pasar (puede ser un model custom
    puesto a mano en el .env)."""
    provider = (provider_req or CHAT_PROVIDER).lower()
    model = model_req
    if model:
        motores = _motores()
        de_otro_provider = any(m["model"] == model and m["provider"] != provider for m in motores)
        del_provider_resuelto = any(m["model"] == model and m["provider"] == provider for m in motores)
        if de_otro_provider and not del_provider_resuelto:
            model = None
    return provider, model


@app.get("/providers")
def providers():
    """MOTORES para el selector, agrupados por proveedor y con nombre comercial limpio.
    El thinking ya NO duplica entradas: los motores capaces llevan thinking_capaz=True y
    el front lo activa con un toggle aparte (AskReq.thinking)."""
    return {"default": CHAT_PROVIDER, "default_model": "deepseek-v4-flash", "motores": _motores()}


# ── Configuración (wizard / panel de la app) ─────────────────────────────────
# Permite gestionar keys y perfil SIN editar el .env a mano. Las keys se guardan
# en el .env local (gitignored) y se aplican en caliente donde es posible.
CONFIG_KEYS = ("GEMINI_API_KEY", "DEEPSEEK_API_KEY", "OLLAMA_API_KEY",
               "AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION")


def _mask(v: str) -> str:
    v = v or ""
    return (v[:4] + "…" + v[-4:]) if len(v) > 10 else ("•" * len(v))


def _env_set(updates: dict):
    """Actualiza claves en el .env (crea el archivo si falta) y en os.environ."""
    env_path = BASE / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    hechas = set()
    for i, l in enumerate(lines):
        s = l.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        if k in updates:
            lines[i] = f"{k}={updates[k]}"
            hechas.add(k)
    for k, v in updates.items():
        if k not in hechas:
            lines.append(f"{k}={v}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for k, v in updates.items():
        os.environ[k] = v


VISION_PROVIDERS = ("bedrock", "gemini", "manual")


class ConfigReq(BaseModel):
    keys: dict | None = None       # {NOMBRE_ENV: valor} solo las que se quieran cambiar
    chat_provider: str | None = None
    vision_provider: str | None = None   # bedrock | gemini | manual (sin API, la describe un agente)
    perfil: dict | None = None     # {nombre, rol, disciplina, objetivo}
    generar_token: bool = False    # genera RAG_TOKEN si no existe


@app.get("/config")
def config_get(authorization: str | None = Header(default=None)):
    check_token(authorization)
    return {
        "keys": {k: {"definida": bool(os.environ.get(k)), "mascara": _mask(os.environ.get(k, ""))}
                 for k in CONFIG_KEYS},
        "chat_provider": CHAT_PROVIDER,
        "embed_provider": EMBED_PROVIDER, "embed_model": EMBED_MODEL,
        "vision_provider": os.environ.get("VISION_PROVIDER", "bedrock"),
        "perfil": perfil(),
        "setup_required": SETUP_REQUIRED,
        "rag_token_definido": bool(RAG_TOKEN),
        "embed_mismatch": EMBED_MISMATCH,
    }


@app.post("/config")
def config_set(req: ConfigReq, authorization: str | None = Header(default=None)):
    check_token(authorization)
    global GEMINI_API_KEY, DEEPSEEK_API_KEY, OLLAMA_API_KEY, CHAT_PROVIDER, RAG_TOKEN, SETUP_REQUIRED
    reload_needed = False
    updates = {}
    for k, v in (req.keys or {}).items():
        if k in CONFIG_KEYS and isinstance(v, str) and v.strip():
            updates[k] = v.strip()
    if req.chat_provider and req.chat_provider.lower() in PROVIDERS:
        updates["CHAT_PROVIDER"] = req.chat_provider.lower()
        CHAT_PROVIDER = req.chat_provider.lower()
    if req.vision_provider and req.vision_provider.lower() in VISION_PROVIDERS:
        # Lo lee la ingesta (proceso aparte) desde el .env en su próxima corrida.
        updates["VISION_PROVIDER"] = req.vision_provider.lower()
    if req.generar_token and not RAG_TOKEN:
        import secrets
        updates["RAG_TOKEN"] = secrets.token_urlsafe(32)
        RAG_TOKEN = updates["RAG_TOKEN"]
        reload_needed = True   # la página abierta lleva el token viejo inyectado
    if updates:
        _env_set(updates)
    # Aplicar en caliente las keys más usadas.
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", GEMINI_API_KEY)
    DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)
    OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", OLLAMA_API_KEY)
    SETUP_REQUIRED = not GEMINI_API_KEY
    if req.perfil is not None:
        limpio = {k: str(req.perfil.get(k, "")).strip() for k in PERFIL_CAMPOS}
        limpio = {k: v for k, v in limpio.items() if v}
        PERFIL_PATH.write_text(json.dumps({**PERFIL_DEFAULT, **limpio}, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return {"ok": True, "reload": reload_needed, "setup_required": SETUP_REQUIRED}


class ProbarReq(BaseModel):
    proveedor: str


@app.post("/config/probar")
def config_probar(req: ProbarReq, authorization: str | None = Header(default=None)):
    """Prueba de conexión por proveedor. Todas las pruebas son gratis salvo Bedrock
    (una llamada mínima de 1 token, costo despreciable)."""
    check_token(authorization)
    p = req.proveedor.lower()
    try:
        if p == "gemini":
            r = session.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                            headers={"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")}, timeout=20)
            r.raise_for_status()
        elif p == "deepseek":
            r = session.get(f"{DEEPSEEK_URL}/models",
                            headers={"Authorization": f"Bearer {os.environ.get('DEEPSEEK_API_KEY', '')}"},
                            timeout=20)
            r.raise_for_status()
        elif p == "ollama":
            r = session.get(f"{OLLAMA_URL}/api/tags",
                            headers={"Authorization": f"Bearer {os.environ.get('OLLAMA_API_KEY', '')}"},
                            timeout=20)
            r.raise_for_status()
        elif p == "bedrock":
            global _bedrock
            _bedrock = None   # recrear cliente por si la key cambió
            _bedrock_client().messages.create(model=BEDROCK_MODEL, max_tokens=1,
                                              messages=[{"role": "user", "content": "ping"}])
        else:
            return {"ok": False, "detail": f"Proveedor desconocido: {p}"}
        return {"ok": True, "detail": "Conexión correcta."}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:300]}


@app.get("/documentos")
def documentos():
    """Documentos indexados (sources distintos) + conteos por tipo (para la portada del
    cuaderno) + si tienen esquema."""
    pts = qdrant.scroll(COLLECTION, limit=10000, with_payload=True)[0]
    srcs = {}
    for p in pts:
        s = p.payload.get("source", "?")
        d = srcs.setdefault(s, {"puntos": 0, "figuras": 0, "tablas": 0})
        d["puntos"] += 1
        t = p.payload.get("type", "text")
        if t == "figure":
            d["figuras"] += 1
        elif t == "table":
            d["tablas"] += 1
    out = []
    for s, d in sorted(srcs.items()):
        of = OUTLINES_DIR / f"{s}.jsonl"
        secciones = sum(1 for _ in of.open(encoding="utf-8")) if of.exists() else 0
        out.append({"source": s, "puntos": d["puntos"], "figuras": d["figuras"],
                    "tablas": d["tablas"], "secciones": secciones,
                    "tiene_outline": of.exists()})
    return {"docs": out}


@app.get("/outline")
def outline(doc: str):
    """Resumen por secciones pre-computado (jerarquía + resumen detallado)."""
    f = OUTLINES_DIR / f"{doc}.jsonl"
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"Sin esquema para '{doc}'")
    secciones = [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines()]
    return {"doc": doc, "secciones": secciones}


# ── Cuadernos (agrupan documentos por área; acotan el RAG) ────────────────────
CUADERNOS_FILE = BASE / "cuadernos.json"


def _load_cuadernos() -> list:
    if CUADERNOS_FILE.exists():
        try:
            return json.loads(CUADERNOS_FILE.read_text(encoding="utf-8")).get("cuadernos", [])
        except Exception:
            return []
    return []


def _save_cuadernos(lst: list):
    CUADERNOS_FILE.write_text(json.dumps({"cuadernos": lst}, ensure_ascii=False, indent=2),
                              encoding="utf-8")


class CuadernoReq(BaseModel):
    id: str | None = None
    nombre: str
    docs: list = []            # lista de `source` (un doc puede estar en varios cuadernos)


@app.get("/cuadernos")
def cuadernos():
    return {"cuadernos": _load_cuadernos()}


@app.post("/cuaderno")
def guardar_cuaderno(c: CuadernoReq, authorization: str | None = Header(default=None)):
    check_token(authorization)
    lst = _load_cuadernos()
    if c.id:                                   # actualizar existente
        for cu in lst:
            if cu.get("id") == c.id:
                cu["nombre"], cu["docs"], cu["ts"] = c.nombre, c.docs, time.time() * 1000
                _save_cuadernos(lst)
                return {"ok": True, "id": c.id}
    nid = str(uuid.uuid4())                     # crear nuevo
    lst.append({"id": nid, "nombre": c.nombre, "docs": c.docs, "ts": time.time() * 1000})
    _save_cuadernos(lst)
    return {"ok": True, "id": nid}


@app.delete("/cuaderno")
def borrar_cuaderno(id: str, authorization: str | None = Header(default=None)):
    check_token(authorization)
    _save_cuadernos([c for c in _load_cuadernos() if c.get("id") != id])
    return {"ok": True}


# ── Libretas (conversaciones persistentes) ───────────────────────────────────
class ConvReq(BaseModel):
    id: str
    doc: str | None = None
    cuaderno: str | None = None    # id del cuaderno al que pertenece la libreta
    titulo: str = ""
    ts: float = 0.0
    turnos: list = []          # [{q, a, fuentes, web}]


def _sid(cid: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_-]", "", cid)[:64]   # evita path traversal


@app.post("/conversacion")
def guardar_conv(c: ConvReq, authorization: str | None = Header(default=None)):
    check_token(authorization)
    sid = _sid(c.id)
    if not sid:
        raise HTTPException(status_code=400, detail="id inválido")
    d = c.model_dump()
    d["id"] = sid
    if not d.get("ts"):
        d["ts"] = time.time() * 1000
    (CONV_DIR / f"{sid}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "id": sid}


@app.get("/conversaciones")
def listar_conv(authorization: str | None = Header(default=None)):
    check_token(authorization)
    out = []
    for f in CONV_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": d["id"], "titulo": d.get("titulo", ""),
                        "doc": d.get("doc"), "cuaderno": d.get("cuaderno"),
                        "ts": d.get("ts", 0), "n": len(d.get("turnos", []))})
        except Exception:
            continue
    out.sort(key=lambda x: x["ts"], reverse=True)
    return {"conversaciones": out}


@app.get("/conversacion")
def cargar_conv(id: str, authorization: str | None = Header(default=None)):
    check_token(authorization)
    f = CONV_DIR / f"{_sid(id)}.json"
    if not f.exists():
        raise HTTPException(status_code=404, detail="Libreta no encontrada")
    return json.loads(f.read_text(encoding="utf-8"))


@app.delete("/conversacion")
def borrar_conv(id: str, authorization: str | None = Header(default=None)):
    check_token(authorization)
    f = CONV_DIR / f"{_sid(id)}.json"
    if f.exists():
        f.unlink()
    return {"ok": True}


@app.post("/search")
def search(req: SearchReq, authorization: str | None = Header(default=None)):
    check_token(authorization)
    try:
        return {"results": buscar(req.query, req.top_k, doc=req.doc)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en /search: {e}")


def _img_b64(source: str, img: str):
    p = MD_DIR / f"{source}_figs" / img
    if p.exists():
        return base64.b64encode(p.read_bytes()).decode("ascii")
    return None


def _dataurl_a_jpeg_b64(dataurl: str):
    """Captura adjuntada por el usuario (dataURL o base64) → JPEG base64.
    Se re-encodea siempre a JPEG porque los proveedores asumen image/jpeg."""
    import io
    from PIL import Image
    m = re.match(r"data:image/\w+;base64,(.+)", dataurl or "", re.S)
    try:
        raw = base64.b64decode(m.group(1) if m else (dataurl or ""))
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return None
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ── Investigación agéntica (tool-use): el modelo indaga el corpus antes de responder ──
# Diseño en 2 FASES (ver NOTAS_TECNICAS): FASE A = bucle de herramientas SIN streaming
# (el modelo pide búsquedas/lecturas, las ejecutamos y le devolvemos los resultados; así
# esquivamos los bugs de "streaming + tool_calls" de Gemini/Gemma). FASE B = la respuesta
# final se transmite con los _chat_* de siempre, usando como CONTEXTO la evidencia
# recolectada. Si la Fase A falla, ask() cae al RAG de un tiro. Off por defecto (toggle UI).
AGENT_MAX_ROUNDS = 5          # tope de vueltas de herramientas (acota costo/latencia)
AGENT_BUSCAR_K = 5            # resultados de texto por búsqueda dentro del bucle
AGENT_SNIPPET = 900           # chars por resultado en el tool-result (leer_seccion da el resto)
AGENT_SECCION_MAX = 6000      # chars máx que devuelve leer_seccion (acota tokens)


def _scope_sources(doc=None, docs=None) -> list:
    """Papers válidos para el alcance actual: un doc, un cuaderno, o TODO el corpus."""
    if doc:
        return [doc]
    if docs:
        return list(docs)
    pts = qdrant.scroll(COLLECTION, limit=10000, with_payload=True)[0]
    return sorted({(p.payload or {}).get("source", "") for p in pts} - {""})


def _tool_buscar(consulta: str, documento: str, scope: list) -> str:
    """buscar_en_corpus: recuperación semántica. `documento` acota a un paper (si está en el
    alcance); vacío = todo el alcance."""
    doc = documento if (documento and documento in scope) else None
    docs = None if doc else scope
    filas = buscar(consulta, top_k=AGENT_BUSCAR_K, fig_k=2, tab_k=1, doc=doc, docs=docs)
    if not filas:
        return "(sin resultados para esa consulta)"
    out = []
    for r in filas:
        et = {"figure": "[FIGURA] ", "table": "[TABLA] "}.get(r["type"], "")
        out.append(f"• {et}{r['source']} — {r['titulo']}\n{(r['texto'] or '')[:AGENT_SNIPPET]}")
    return "\n\n".join(out)


def _tool_ver_esquema(documento: str, scope: list) -> str:
    """ver_esquema: TOC + resumen por sección (de outlines/), para navegar barato."""
    if documento not in scope:
        return f"'{documento}' no está en el alcance. Papers disponibles: {', '.join(scope)}"
    p = OUTLINES_DIR / f"{documento}.jsonl"
    if not p.exists():
        return f"(sin esquema pre-computado para '{documento}')"
    lineas = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        d = json.loads(ln)
        sangria = "  " * max(0, int(d.get("nivel", 1)) - 1)
        res = re.sub(r"\s+", " ", d.get("resumen") or "").strip()
        res = (": " + res[:180] + "…") if res else ""
        lineas.append(f"{sangria}- {d.get('titulo', '')}{res}")
    return "\n".join(lineas) or "(esquema vacío)"


def _tool_leer_seccion(documento: str, titulo: str, scope: list) -> str:
    """leer_seccion: cuerpo COMPLETO de una sección (no fragmentos)."""
    if documento not in scope:
        return f"'{documento}' no está en el alcance. Papers disponibles: {', '.join(scope)}"
    try:
        return _texto_seccion(documento, titulo)[:AGENT_SECCION_MAX]
    except HTTPException as e:
        return str(e.detail)


# Herramientas expuestas al modelo (spec agnóstico; se traduce por proveedor en la Fase A).
AGENT_TOOLS = [
    {"name": "buscar_en_corpus",
     "descripcion": "Búsqueda semántica en la biblioteca del usuario. Devuelve los fragmentos "
                    "(texto, figuras, tablas) más afines. Úsala para localizar dónde se trata un "
                    "tema. Acota a un paper con 'documento', o déjalo vacío para buscar en todos.",
     "params": {"consulta": ("string", "Qué buscar, en lenguaje natural."),
                "documento": ("string", "Opcional: nombre exacto del paper para acotar la búsqueda.")},
     "requeridos": ["consulta"]},
    {"name": "ver_esquema",
     "descripcion": "Tabla de contenidos + resumen por sección de UN paper. Úsala para saber qué "
                    "secciones tiene y elegir cuál leer entera, sin gastar de más.",
     "params": {"documento": ("string", "Nombre exacto del paper.")},
     "requeridos": ["documento"]},
    {"name": "leer_seccion",
     "descripcion": "Devuelve el texto COMPLETO de una sección de un paper (para leer un método o "
                    "resultado de corrido, no en fragmentos). Usa antes ver_esquema para el título exacto.",
     "params": {"documento": ("string", "Nombre exacto del paper."),
                "titulo": ("string", "Título exacto de la sección (como aparece en ver_esquema).")},
     "requeridos": ["documento", "titulo"]},
]


def _ejecutar_tool(nombre: str, args: dict, scope: list) -> str:
    """Despacha una llamada de herramienta a su implementación. Nunca lanza: cualquier error
    vuelve como texto para que el modelo lo lea y reintente (no rompe la Fase A)."""
    try:
        if nombre == "buscar_en_corpus":
            return _tool_buscar(args.get("consulta", ""), args.get("documento", ""), scope)
        if nombre == "ver_esquema":
            return _tool_ver_esquema(args.get("documento", ""), scope)
        if nombre == "leer_seccion":
            return _tool_leer_seccion(args.get("documento", ""), args.get("titulo", ""), scope)
        return f"(herramienta desconocida: {nombre})"
    except Exception as e:
        return f"(error ejecutando {nombre}: {e})"


def _tools_openai() -> list:
    """Traduce AGENT_TOOLS al formato de function-calling de OpenAI (DeepSeek, Gemma/Ollama)."""
    out = []
    for t in AGENT_TOOLS:
        props = {k: {"type": ty, "description": desc} for k, (ty, desc) in t["params"].items()}
        out.append({"type": "function", "function": {
            "name": t["name"], "description": t["descripcion"],
            "parameters": {"type": "object", "properties": props, "required": t["requeridos"]}}})
    return out


SYS_AGENTE = (
    "Eres un investigador que, ANTES de responder, indaga la biblioteca del usuario con las "
    "herramientas dadas. Descompón la pregunta, busca en los papers pertinentes (uno por uno si "
    "hace falta comparar), abre esquemas y lee secciones completas cuando necesites detalle. "
    "Para preguntas que abarcan varios papers, revisa CADA paper pertinente por separado. No "
    "inventes: usa solo lo que las herramientas devuelven. Cuando tengas evidencia suficiente, "
    "deja de llamar herramientas (no hace falta que redactes la respuesta final aquí)."
)


def _corto(doc: str) -> str:
    """Nombre corto y legible de un paper para el progreso: el '(Autor año)' del final si existe."""
    m = re.search(r"\(([^)]+)\)\s*$", doc or "")
    return m.group(1) if m else (doc or "")[:40]


def _paso_legible(fn: str, args: dict) -> str:
    """Traza de herramienta → línea amigable para mostrar en vivo en la UI (Fase A)."""
    if fn == "buscar_en_corpus":
        cons = (args.get("consulta") or "")[:60]
        doc = args.get("documento")
        return f"🔎 Buscando «{cons}»" + (f" en {_corto(doc)}" if doc else " en todo el cuaderno")
    if fn == "ver_esquema":
        return f"🗂 Revisando el índice de {_corto(args.get('documento', ''))}"
    if fn == "leer_seccion":
        return (f"📖 Leyendo «{(args.get('titulo') or '')[:50]}» "
                f"de {_corto(args.get('documento', ''))}")
    return f"⚙️ {fn}"


def _investigar_deepseek(system: str, pregunta: str, scope: list, model: str,
                         max_rounds: int = AGENT_MAX_ROUNDS):
    """FASE A con DeepSeek V4 (formato OpenAI, no-streaming). GENERADOR: hace `yield` de cada
    paso legible (para el progreso en vivo) y RETORNA (evidencia_texto, trazas) al terminar."""
    tools = _tools_openai()
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": pregunta}]
    evidencia, trazas = [], []
    for _ in range(max_rounds):
        body = {"model": model or DEEPSEEK_MODEL, "messages": messages, "tools": tools,
                "tool_choice": "auto", "stream": False, "max_tokens": CHAT_MAX_TOKENS}
        r = session.post(f"{DEEPSEEK_URL}/chat/completions", json=body,
                         headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}, timeout=300)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        tcs = msg.get("tool_calls") or []
        if not tcs:
            break                       # el modelo ya no pide herramientas → evidencia completa
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
        for tc in tcs:
            fn = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            yield _paso_legible(fn, args)
            resultado = _ejecutar_tool(fn, args, scope)
            firma = ", ".join(f"{k}={v}" for k, v in args.items())
            trazas.append({"tool": fn, "args": args})
            evidencia.append(f"▸ {fn}({firma})\n{resultado}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": resultado})
    return "\n\n---\n\n".join(evidencia), trazas


def _tools_gemini() -> list:
    """Traduce AGENT_TOOLS al formato function_declarations de Gemini (tipos en MAYÚSCULA)."""
    decls = []
    for t in AGENT_TOOLS:
        props = {k: {"type": "STRING", "description": desc} for k, (ty, desc) in t["params"].items()}
        decls.append({"name": t["name"], "description": t["descripcion"],
                      "parameters": {"type": "OBJECT", "properties": props, "required": t["requeridos"]}})
    return [{"function_declarations": decls}]


def _investigar_gemini(system: str, pregunta: str, scope: list, model: str,
                       max_rounds: int = AGENT_MAX_ROUNDS):
    """FASE A con Gemini (generateContent, NO-streaming → esquiva el bug de streaming+functionCall).
    Round-trip: functionCall (role model) → functionResponse (role function)."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model or GEMINI_CHAT_MODEL}:generateContent")
    base = {"systemInstruction": {"parts": [{"text": system}]}, "tools": _tools_gemini(),
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}}}
    contents = [{"role": "user", "parts": [{"text": pregunta}]}]
    evidencia, trazas = [], []
    for _ in range(max_rounds):
        r = session.post(url, json={**base, "contents": contents},
                         headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=300)
        r.raise_for_status()
        cand = (r.json().get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        fcalls = [p["functionCall"] for p in parts if "functionCall" in p]
        if not fcalls:
            break
        contents.append(cand["content"])                 # turno del modelo, verbatim
        respuestas = []
        for fc in fcalls:
            fn, args = fc.get("name", ""), (fc.get("args") or {})
            yield _paso_legible(fn, args)
            resultado = _ejecutar_tool(fn, args, scope)
            firma = ", ".join(f"{k}={v}" for k, v in args.items())
            trazas.append({"tool": fn, "args": args})
            evidencia.append(f"▸ {fn}({firma})\n{resultado}")
            respuestas.append({"functionResponse": {"name": fn, "response": {"content": resultado}}})
        contents.append({"role": "function", "parts": respuestas})
    return "\n\n---\n\n".join(evidencia), trazas


def _investigar_ollama(system: str, pregunta: str, scope: list, model: str,
                       max_rounds: int = AGENT_MAX_ROUNDS):
    """FASE A con Gemma vía Ollama `/api/chat` nativo, NO-streaming (evita el bug de tool_calls
    en streaming del endpoint OpenAI-compatible). tool-result = {role:tool, content, tool_name}."""
    tools = _tools_openai()
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": pregunta}]
    evidencia, trazas = [], []
    for _ in range(max_rounds):
        body = {"model": model or OLLAMA_MODEL, "messages": messages, "tools": tools, "stream": False}
        r = session.post(f"{OLLAMA_URL}/api/chat", json=body, headers=headers, timeout=300)
        r.raise_for_status()
        msg = r.json().get("message", {})
        tcs = msg.get("tool_calls") or []
        if not tcs:
            break
        messages.append(msg)                             # turno del asistente, verbatim
        for tc in tcs:
            fn = tc["function"]["name"]
            args = tc["function"].get("arguments") or {}
            if isinstance(args, str):                    # por si el modelo la manda como string
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            yield _paso_legible(fn, args)
            resultado = _ejecutar_tool(fn, args, scope)
            firma = ", ".join(f"{k}={v}" for k, v in args.items())
            trazas.append({"tool": fn, "args": args})
            evidencia.append(f"▸ {fn}({firma})\n{resultado}")
            messages.append({"role": "tool", "content": resultado, "tool_name": fn})
    return "\n\n---\n\n".join(evidencia), trazas


# Adaptadores de FASE A por proveedor = motores donde el modo agéntico rinde. En la evaluación
# 2026-07-17 (cuaderno GSI) DeepSeek V4 (22 tool-calls, investigación ejemplar) y Gemini 3.5
# (5 calls, respuesta completa y correcta) lo hicieron bien; Gemma 4 hizo UNA sola llamada
# (degeneró a RAG de un tiro), fue el más lento y mezcló métodos → se deja SIN registrar (su
# adaptador `_investigar_ollama` queda listo por si mejora, pero el botón se le apaga solo vía
# `agentic_capaz`). Sonnet/Bedrock pendiente (es el confiable; no hizo falta evaluarlo).
AGENT_ADAPTERS = {"deepseek": _investigar_deepseek, "gemini": _investigar_gemini}


@app.post("/ask")
def ask(req: AskReq, authorization: str | None = Header(default=None)):
    check_token(authorization)
    provider, model = _resuelve_motor(req.provider, req.model)
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Proveedor desconocido: {provider}")

    fig_pedida = False  # la figura nombrada se encontró determinísticamente → no ir a la web
    try:
        resultados = buscar(req.query, req.top_k, doc=req.doc, docs=req.docs)
        # Si el usuario nombra una figura explícitamente ("figura 6"), traerla por número
        # (lookup determinista) y anteponerla para que se adjunte SU imagen, no otra.
        # Solo aplica con un documento concreto (o un cuaderno de un solo doc): con varios
        # docs "figura 6" es ambiguo.
        doc_fig = req.doc or (req.docs[0] if req.docs and len(req.docs) == 1 else None)
        nums = nums_figura_en(req.query)
        if nums and doc_fig:
            pedidas = figuras_por_numero(doc_fig, nums)
            if pedidas:
                fig_pedida = True
                imgs_ya = {r["img"] for r in resultados if r["img"]}
                nuevas = [f for f in pedidas if f["img"] not in imgs_ya]
                resultados = nuevas + [r for r in resultados
                                       if not (r["type"] == "figure" and r["img"] in {p["img"] for p in nuevas})]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error recuperando contexto: {e}")

    mejor = max((r["score"] for r in resultados), default=0.0)

    # Capturas adjuntadas por el usuario en el chat (máx 3). Van PRIMERO en la lista:
    # son su duda concreta (p.ej. la tabla real del PDF cuando marker la desordenó).
    imgs_usuario = []
    for du in (req.imagenes_usuario or [])[:3]:
        b = _dataurl_a_jpeg_b64(du)
        if b:
            imgs_usuario.append(b)
    if imgs_usuario and not VISION_OK.get(provider, False):
        raise HTTPException(status_code=400,
                            detail="Adjuntaste una imagen, pero el modelo seleccionado no tiene "
                                   "visión. Elige un motor con '·visión' (Gemini o Gemma).")

    # Construir contexto con citas [Fuente N]; adjuntar imágenes de figuras si el que va a
    # responder tiene visión (con fallback web responde Gemini, que ve, aunque el proveedor
    # seleccionado sea solo-texto).
    permite_img = req.attach_images and VISION_OK.get(provider, False)
    bloques, fuentes, imagenes = [], [], []
    for i, r in enumerate(resultados, 1):
        etiqueta = {"text": "", "figure": "[FIGURA] ", "table": "[TABLA] "}.get(r["type"], "")
        bloques.append(f"[Fuente {i}] {etiqueta}{r['titulo']} — {r['source']}\n{r['texto']}")
        fuentes.append({"n": i, "type": r["type"], "titulo": r["titulo"],
                        "source": r["source"], "img": r["img"], "score": r["score"],
                        "preview": (r["texto"] or "")[:380]})
        if permite_img and r["type"] == "figure" and r["img"] and len(imagenes) < 4:
            b = _img_b64(r["source"], r["img"])
            if b:
                imagenes.append(b)

    contexto = "\n\n---\n\n".join(bloques) if bloques else "(sin resultados relevantes)"

    # MAPA DEL CUADERNO: si el alcance es "todo el cuaderno" (docs sin doc), el modelo ve
    # SIEMPRE la lista completa de papers con su ficha. Sin esto, en preguntas panorámicas
    # ("¿por cuál paper parto?") solo conoce los 2-3 docs que tocaron los chunks recuperados.
    mapa = ""
    if not req.doc and req.docs:
        mapa = (f"=== MAPA DEL CUADERNO ({len(req.docs)} documentos consultados) ===\n"
                "Listado COMPLETO de los papers de este cuaderno, cada uno con su resumen. TODOS "
                "están completos e indexados en la base (aunque de alguno no aparezcan fragmentos "
                "abajo): NO digas que de un paper 'solo hay una referencia'. El CONTEXTO de abajo "
                "trae solo los fragmentos más afines a la pregunta (puede tocar pocos papers). Para "
                "preguntas PANORÁMICAS (rutas de lectura, comparar papers, qué documento cubre qué "
                "tema) considera TODOS los papers de este mapa, citándolos por nombre; para el "
                "detalle fino usa las [Fuente N] del CONTEXTO.\n\n"
                + mapa_cuaderno(req.docs) + "\n\n")

    nota_img = ""
    if imgs_usuario:
        imagenes = imgs_usuario + imagenes
        nota_img = (f"\n\n=== IMAGEN(ES) ADJUNTADA(S) POR EL USUARIO ({len(imgs_usuario)}) ===\n"
                    "Son las PRIMERAS imágenes del mensaje y contienen su duda concreta (p.ej. "
                    "una tabla o figura del paper capturada directo del PDF). LÉELAS con visión "
                    "como fuente PRIMARIA de su contenido —la versión textual del CONTEXTO puede "
                    "venir con celdas desordenadas por la extracción— y usa el CONTEXTO del paper "
                    "para interpretarlas, conectarlas con lo que el autor dice y complementar.")

    # Memoria conversacional: preámbulo con los últimos turnos (respuestas recortadas).
    previa = ""
    partes = []
    for t in (req.historial or [])[-4:]:
        q = (t.get("q") or "").strip() if isinstance(t, dict) else ""
        a = (t.get("a") or "").strip() if isinstance(t, dict) else ""
        if q:
            partes.append(f"Usuario: {q}\nTutor: {a[:900]}")
    if partes:
        previa = ("=== CONVERSACIÓN PREVIA (contexto del diálogo; NO la repitas) ===\n"
                  + "\n\n".join(partes) + "\n\n")

    def stream():
        # ── FASE A (opcional): investigación agéntica con herramientas ANTES de responder ──
        # Corre DENTRO del stream para emitir el progreso en vivo ({"status": …}); así el ~1-2 min
        # de investigación no es a ciegas. Si algo falla, se ignora y se responde con el RAG de un
        # tiro (el CONTEXTO normal sigue presente → nunca queda peor). Solo si el toggle está on y
        # el proveedor tiene adaptador (DeepSeek, Gemini).
        evidencia_block, agentic_ok, agentic_trazas = "", False, []
        try:
            if req.agentic and provider in AGENT_ADAPTERS:
                lento = provider != "deepseek"   # Gemini/otros: sin thinking, avisar que tarda más
                aviso = ("🔬 Investigación profunda: voy a indagar tu biblioteca paso a paso. "
                         + ("Con este motor puede tardar un par de minutos — puedes volver luego; "
                            "el progreso queda registrado aquí." if lento
                            else "Suele tomar 1-2 minutos."))
                yield json.dumps({"status": aviso}) + "\n"
                try:
                    scope = _scope_sources(req.doc, req.docs)
                    sys_ag = SYS_AGENTE + "\n\n=== BIBLIOTECA EN ALCANCE ===\n" + mapa_cuaderno(scope)
                    if req.doc_foco and req.doc_foco in scope:
                        sys_ag += (
                            "\n\n=== EN FOCO (lectura actual) ===\nEl lector tiene abierto ahora mismo el "
                            f"paper «{req.doc_foco}». Es el punto de partida y el sujeto por defecto cuando "
                            "la pregunta dice «este paper», «el paper», «el documento» o «acá». Deja que la "
                            "pregunta marque la amplitud: si pide comparar con papers concretos, cíñete a "
                            "esos y no arrastres otros de la biblioteca; solo si la pregunta es abierta "
                            "(p.ej. «qué otros métodos existen») explora libremente el resto del alcance.")
                    gen_ag = AGENT_ADAPTERS[provider](sys_ag, req.query, scope, model)
                    evidencia = ""
                    while True:
                        try:
                            paso = next(gen_ag)
                        except StopIteration as fin:
                            evidencia, agentic_trazas = fin.value or ("", [])
                            break
                        yield json.dumps({"status": paso}) + "\n"
                    if evidencia:
                        evidencia_block = ("=== INVESTIGACIÓN (hallazgos indagando la biblioteca; "
                                           "úsalos como fuente PRINCIPAL, citando el paper) ===\n"
                                           + evidencia + "\n\n")
                        agentic_ok = True
                        yield json.dumps({"status": "✍️ Redactando la respuesta…"}) + "\n"
                except Exception:
                    evidencia_block, agentic_ok, agentic_trazas = "", False, []  # fallback al RAG

            user = (f"{mapa}{evidencia_block}=== CONTEXTO ===\n{contexto}{nota_img}\n\n"
                    f"{previa}=== PREGUNTA ===\n{req.query}")
            if provider == "deepseek":
                gen = _chat_deepseek(sys_chat(), user, imagenes, model, req.thinking)
            else:
                gen = PROVIDERS[provider](sys_chat(), user, imagenes, model)
            for item in gen:
                if isinstance(item, tuple):
                    tipo, val = item
                    if tipo == "thought":
                        yield json.dumps({"thought": val}) + "\n"
                    else:
                        yield json.dumps({"token": val}) + "\n"
                else:
                    yield json.dumps({"token": item}) + "\n"
        except Exception as e:
            yield json.dumps({"error": f"{provider}: {e}"}) + "\n"
        yield json.dumps({"done": True, "provider": provider, "mejor_score": mejor,
                          "sources": fuentes, "agentic": agentic_ok,
                          "trazas": agentic_trazas}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")



# ── Interfaz web (app local) ─────────────────────────────────────────────────
from fastapi.responses import HTMLResponse, FileResponse


@app.get("/app", response_class=HTMLResponse)
def app_web():
    """Sirve la interfaz. Inyecta el RAG_TOKEN (solo viaja en localhost)."""
    html_path = BASE / "app.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Falta app.html")
    html = html_path.read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__RAG_TOKEN__", RAG_TOKEN))


@app.get("/figura")
def figura(doc: str, img: str):
    """Sirve la imagen original de una figura (md/<doc>_figs/<img>)."""
    nombre = Path(img).name          # sin rutas — solo el nombre de archivo
    p = MD_DIR / f"{doc}_figs" / nombre
    if not p.exists():
        raise HTTPException(status_code=404, detail="Figura no encontrada")
    return FileResponse(p)


@app.get("/pdf")
def pdf(doc: str):
    """Sirve el PDF original (raw/<doc>.pdf) para el visor central. inline → el navegador lo abre.
    OJO: usa .name, NO .stem: un título con punto (p.ej. 'et al. 2004') hace que .stem recorte
    ' 2004)' creyéndolo una extensión → 404 falso. El source nunca trae '.pdf'."""
    nombre = Path(doc).name          # nombre completo (con sus puntos); solo se le quita la ruta
    p = RAW_DIR / f"{nombre}.pdf"
    if not p.exists():
        raise HTTPException(status_code=404, detail="PDF no encontrado")
    return FileResponse(p, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{nombre}.pdf"'})


@app.get("/md")
def md(doc: str):
    """Sirve el markdown de marker (md/<doc>.md) como texto, para el lector central.
    Las refs de imagen (![](_page_x.jpeg)) las reescribe el frontend a /figura."""
    stem = Path(doc).name            # sin rutas — solo el nombre base
    p = MD_DIR / f"{stem}.md"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Markdown no encontrado")
    return FileResponse(p, media_type="text/markdown; charset=utf-8")


# ── Revisión / recorte de figuras (editor en la app) ─────────────────────────
def _fig_path(doc: str, img: str) -> Path:
    """Ruta segura a una figura (md/<doc>_figs/<img>), sin escapes de ruta."""
    return MD_DIR / f"{Path(doc).name}_figs" / Path(img).name


@app.get("/figuras")
def figuras(doc: str):
    """Lista las figuras indexadas de un doc (img + caption + descripción) para la galería de QA."""
    must = [FieldCondition(key="type", match=MatchValue(value="figure")),
            FieldCondition(key="source", match=MatchValue(value=doc))]
    pts = qdrant.scroll(COLLECTION, scroll_filter=Filter(must=must),
                        limit=500, with_payload=True)[0]
    figs = []
    for p in pts:
        pl = p.payload or {}
        img = pl.get("img", "")
        editada = (_fig_path(doc, img).parent / (Path(img).name + ".orig")).exists() if img else False
        figs.append({"img": img, "caption": pl.get("caption", ""),
                     "descripcion": pl.get("texto", ""), "editada": editada})
    figs.sort(key=lambda r: r["img"])
    return {"doc": doc, "figuras": figs}


class RecorteReq(BaseModel):
    doc: str
    img: str
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0


@app.post("/figura/recortar")
def recortar_figura(req: RecorteReq, authorization: str | None = Header(default=None)):
    """Recorta la figura al recuadro dado (px) y SOBRESCRIBE el archivo. Respalda el original 1 vez
    (<img>.orig) y siempre recorta DESDE ese original, para poder re-recortar o restaurar."""
    check_token(authorization)
    from PIL import Image
    p = _fig_path(req.doc, req.img)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Figura no encontrada")
    orig = p.parent / (p.name + ".orig")
    if not orig.exists():
        shutil.copy2(p, orig)                     # respaldo prístino (una sola vez)
    im = Image.open(orig).convert("RGB")          # recortar siempre desde el original
    x, y = max(0, req.x), max(0, req.y)
    box = (x, y, min(im.width, x + max(1, req.w)), min(im.height, y + max(1, req.h)))
    im.crop(box).save(p, quality=92)
    return {"ok": True}


@app.post("/figura/restaurar")
def restaurar_figura(req: RecorteReq, authorization: str | None = Header(default=None)):
    """Restaura la figura a su original respaldado (<img>.orig)."""
    check_token(authorization)
    p = _fig_path(req.doc, req.img)
    orig = p.parent / (p.name + ".orig")
    if orig.exists():
        shutil.copy2(orig, p)
        return {"ok": True, "restaurado": True}
    return {"ok": True, "restaurado": False}


# ── Generador de tarjetas Anki (active recall) ───────────────────────────────
def sys_anki() -> str:
  return (
    "Eres un generador de tarjetas de estudio (flashcards) para ACTIVE RECALL de un " + perfil()["rol"] + ". "
    "A partir del CONTENIDO dado, crea tarjetas ATÓMICAS (una idea por tarjeta), en español, claras. "
    "Cada tarjeta: 'q' = pregunta breve y precisa; 'a' = respuesta concisa (1-3 frases). "
    "Evita tarjetas triviales, ambiguas o redundantes; prioriza conceptos, relaciones causa-efecto, "
    "valores/criterios clave, definiciones y clasificaciones. Usa LaTeX SOLO si aporta, con \\(...\\) "
    "en línea y \\[...\\] en bloque (compatibles con Anki/MathJax). Básate ESTRICTAMENTE en el "
    "CONTENIDO; no inventes datos externos. "
    "Responde ÚNICAMENTE con un array JSON válido con este formato exacto: "
    "[{\"q\":\"...\",\"a\":\"...\"}]. Sin texto adicional, sin markdown, sin ```."
)


def _parse_cards(txt: str):
    """Extrae el array JSON de tarjetas de la salida del LLM, tolerante a ``` o texto extra."""
    t = (txt or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    data = None
    try:
        data = json.loads(t)
    except Exception:
        m = re.search(r"\[.*\]", t, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    out = []
    for c in (data or []):
        if isinstance(c, dict) and c.get("q") and c.get("a"):
            out.append({"q": str(c["q"]).strip(), "a": str(c["a"]).strip()})
    return out


class AnkiReq(BaseModel):
    texto: str
    doc: str | None = None
    tema: str | None = None
    n: int = 8
    provider: str | None = None
    model: str | None = None


@app.post("/anki")
def anki(req: AnkiReq, authorization: str | None = Header(default=None)):
    """Genera tarjetas Anki (pregunta/respuesta) a partir de un texto (respuesta, sección o selección)."""
    check_token(authorization)
    provider, model = _resuelve_motor(req.provider, req.model)
    if provider not in PROVIDERS:
        provider = CHAT_PROVIDER
    n = max(1, min(req.n, 20))
    tema = f"Tema/título: {req.tema}\n\n" if req.tema else ""
    user = f"{tema}Genera hasta {n} tarjetas de active recall.\n\n=== CONTENIDO ===\n{req.texto}"
    try:
        if provider == "deepseek":
            gen = _chat_deepseek(sys_anki(), user, [], model, False)
            txt = "".join(val for tipo, val in gen if tipo == "content")
        else:
            gen = PROVIDERS[provider](sys_anki(), user, [], model)
            txt = "".join(gen)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generando tarjetas: {e}")
    return {"cards": _parse_cards(txt)}


# ── Quiz-me (Tarea 5) ────────────────────────────────────────────────────────
from chunk import _secciones

def _texto_completo(provider, gen) -> str:
    if provider == "deepseek":
        return "".join(v for t, v in gen if t == "content")
    return "".join(gen)

class QuizGenReq(BaseModel):
    doc: str                       # source (obligatorio)
    seccion: str | None = None     # titulo de sección; si None → paper completo
    n_mc: int = 4
    n_vf: int = 4
    n_desarrollo: int = 2
    dificultad: str = "media"      # "facil" | "media" | "dificil"
    provider: str | None = None
    model: str | None = None

class QuizCalReq(BaseModel):
    enunciado: str
    puntos_clave: list             # la rúbrica que vino en la pregunta
    respuesta_modelo: str
    respuesta_usuario: str
    provider: str | None = None
    model: str | None = None

def _texto_paper(doc: str) -> str:
    stem = Path(doc).stem
    p = MD_DIR / f"{stem}.md"
    if not p.exists():
        p = MD_DIR / f"{doc}.md"
    if not p.exists():
        for f in MD_DIR.glob("*.md"):
            if f.stem.lower() == stem.lower():
                p = f
                break
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"No se encontró el archivo markdown para '{doc}'")
    return p.read_text(encoding="utf-8")

def _limpiar_titulo_match(t: str) -> str:
    t = re.sub(r"^[\s\d.)\-–—]+", "", t.lower()).strip()
    return re.sub(r"[*_`#\s]+", "", t)

def _texto_seccion(doc: str, titulo: str) -> str:
    md = _texto_paper(doc)
    secs = _secciones(md)
    idx = -1
    t_match = _limpiar_titulo_match(titulo)
    for i, (t, n, c) in enumerate(secs):
        if _limpiar_titulo_match(t) == t_match:
            idx = i
            break
    if idx == -1:
        raise HTTPException(status_code=404, detail=f"Sección '{titulo}' no encontrada en el documento '{doc}'")
    
    cuerpo_seccion = [secs[idx][2]]
    nivel_raiz = secs[idx][1]
    
    for i in range(idx + 1, len(secs)):
        t_sub, n_sub, c_sub = secs[i]
        if n_sub > nivel_raiz:
            cuerpo_seccion.append(f"\n\n# {t_sub}\n\n{c_sub}")
        else:
            break
            
    return "\n\n".join(cuerpo_seccion)

def _figuras_tablas_txt(doc: str, texto_scope: str | None = None) -> tuple[str, str]:
    figuras_list = []
    desc_path = BASE / "descripciones" / f"{doc}.jsonl"
    if not desc_path.exists():
        desc_path = BASE / "descripciones" / f"{Path(doc).stem}.jsonl"
    if desc_path.exists():
        with open(desc_path, "r", encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    try:
                        figuras_list.append(json.loads(ln))
                    except Exception:
                        continue
                        
    tablas_list = []
    tab_path = BASE / "descripciones" / f"{doc}_tablas.jsonl"
    if not tab_path.exists():
        tab_path = BASE / "descripciones" / f"{Path(doc).stem}_tablas.jsonl"
    if tab_path.exists():
        with open(tab_path, "r", encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    try:
                        tablas_list.append(json.loads(ln))
                    except Exception:
                        continue
                        
    if texto_scope:
        nums_mencionados = set(re.findall(r'\b\d+\b', texto_scope))
        
        figuras_filtradas = []
        for f in figuras_list:
            img_name = f.get("img", "")
            caption = f.get("caption", "") or f.get("texto", "")
            num = _num_caption(caption)
            mencionado = False
            if img_name and img_name in texto_scope:
                mencionado = True
            elif num is not None and str(num) in nums_mencionados:
                mencionado = True
            if mencionado:
                figuras_filtradas.append(f)
                
        tablas_filtradas = []
        for t in tablas_list:
            caption = t.get("caption", "") or t.get("texto", "")
            num = _num_caption(caption)
            mencionado = False
            if num is not None and str(num) in nums_mencionados:
                mencionado = True
            if mencionado:
                tablas_filtradas.append(t)
                
        if not figuras_filtradas:
            figuras_filtradas = figuras_list
        if not tablas_filtradas:
            tablas_filtradas = tablas_list
            
        figuras_list = figuras_filtradas
        tablas_list = tablas_filtradas

    bloque_figs = []
    for f in figuras_list:
        img_id = f.get("img", "")
        cap = f.get("caption", "")
        desc = f.get("descripcion", "") or f.get("texto", "")
        bloque_figs.append(f"[FIG img={img_id}] caption: {cap}\n{desc}")
    bloque_figs_txt = "\n\n".join(bloque_figs) if bloque_figs else "ninguna"

    bloque_tabs = []
    for t in tablas_list:
        cap = t.get("caption", "")
        desc = t.get("descripcion", "") or t.get("texto", "")
        bloque_tabs.append(f"[TABLA] caption: {cap}\n{desc}")
    bloque_tabs_txt = "\n\n".join(bloque_tabs) if bloque_tabs else "ninguna"
    
    return bloque_figs_txt, bloque_tabs_txt

def _extraer_json_bloque(txt: str) -> str:
    # Buscar el inicio de un objeto o array
    idx_obj = txt.find("{")
    idx_arr = txt.find("[")
    
    if idx_obj == -1 and idx_arr == -1:
        return txt
        
    start_char = "{" if (idx_obj != -1 and (idx_arr == -1 or idx_obj < idx_arr)) else "["
    end_char = "}" if start_char == "{" else "]"
    start_idx = idx_obj if start_char == "{" else idx_arr
    
    # Encontrar el cierre balanceado
    count = 0
    end_idx = -1
    for i in range(start_idx, len(txt)):
        char = txt[i]
        if char == start_char:
            count += 1
        elif char == end_char:
            count -= 1
            if count == 0:
                end_idx = i
                break
                
    if end_idx != -1:
        return txt[start_idx:end_idx+1]
    return txt[start_idx:]

def _rescatar_items_quiz(txt: str) -> list:
    """Rescata los objetos {...} COMPLETOS del array 'quiz' aunque el JSON global venga
    TRUNCADO (respuesta cortada por max_tokens): el último item, cortado a medias, se
    descarta y los previos se conservan. Escanea respetando strings y escapes."""
    m = re.search(r'"quiz"\s*:\s*\[', txt)
    if m:
        s = txt[m.end():]
    else:
        i = txt.find("[")
        if i == -1:
            return []
        s = txt[i + 1:]
    out, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(s):
        if in_str:
            if esc:            esc = False
            elif ch == "\\":   esc = True
            elif ch == '"':    in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        obj = json.loads(s[start:i + 1])
                        if isinstance(obj, dict):
                            out.append(obj)
                    except Exception:
                        pass
                    start = -1
        elif ch == "]" and depth == 0:
            break                              # fin del array (JSON bien formado)
    return out


def _parse_quiz(txt: str) -> list:
    t = (txt or "").strip()
    
    # 1. Limpieza básica de comillas tipográficas
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    
    # 2. Quitar marcas de bloque si existen
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    
    # 3. Intentar parsear directo
    data = None
    try:
        data = json.loads(t)
    except Exception:
        # Extraer el bloque balanceado
        bloque = _extraer_json_bloque(t)
        try:
            data = json.loads(bloque)
        except Exception:
            # Reemplazar posibles saltos de línea ilegales dentro de strings en JSON
            try:
                bloque_limpio = re.sub(r'(?<=[:,\s\[{])"([^"]*)"(?=[\s,\]}]|$)', lambda m: '"' + m.group(1).replace('\n', '\\n').replace('\r', '\\r') + '"', bloque)
                data = json.loads(bloque_limpio)
            except Exception:
                data = None
                
    if not data or not isinstance(data, dict) or "quiz" not in data:
        # Si vino como array directo en lugar de objeto {"quiz": [...]}
        if isinstance(data, list):
            data = {"quiz": data}
        else:
            # Buscar si hay un array balanceado en el texto
            idx_arr = t.find("[")
            if idx_arr != -1:
                bloque_arr = _extraer_json_bloque(t[idx_arr:])
                try:
                    arr = json.loads(bloque_arr)
                    if isinstance(arr, list):
                        data = {"quiz": arr}
                except Exception:
                    pass
                    
    # Última red: si nada parseó (típicamente JSON truncado por max_tokens), rescatar los
    # items completos del array y descartar el que quedó a medias.
    if not data or not isinstance(data, dict) or not data.get("quiz"):
        rescatados = _rescatar_items_quiz(t)
        if rescatados:
            data = {"quiz": rescatados}

    if not data or not isinstance(data, dict) or "quiz" not in data:
        return []

    out = []
    auto_id = 1
    for item in data.get("quiz", []):
        if not isinstance(item, dict):
            continue
        
        # Enunciado es obligatorio
        enunciado = item.get("enunciado", "").strip()
        if not enunciado:
            continue
            
        tipo = item.get("tipo", "").strip().lower()
        if tipo not in ("mc", "vf", "desarrollo"):
            if "opciones" in item:
                tipo = "mc"
            elif "puntos_clave" in item or "respuesta_modelo" in item:
                tipo = "desarrollo"
            else:
                tipo = "vf"
                
        qid = item.get("id", auto_id)
        try:
            qid = int(qid)
        except Exception:
            qid = auto_id
        auto_id = max(auto_id, qid + 1)
        
        clean = {
            "id": qid,
            "tipo": tipo,
            "enunciado": enunciado,
            "cita": str(item.get("cita", "")),
            "explicacion": str(item.get("explicacion", "")),
            "nivel_bloom": str(item.get("nivel_bloom", "comprension")),
            "seccion": str(item.get("seccion", "")),
            "figura": item.get("figura") if item.get("figura") else None
        }
        
        if tipo == "mc":
            ops = item.get("opciones", [])
            if not isinstance(ops, list) or len(ops) < 2:
                ops = ["Opción A", "Opción B", "Opción C", "Opción D"]
            clean["opciones"] = [str(x) for x in ops]
            
            corr = item.get("correcta", 0)
            try:
                corr = int(corr)
                if corr < 0 or corr >= len(clean["opciones"]):
                    corr = 0
            except Exception:
                corr = 0
            clean["correcta"] = corr
            
        elif tipo == "vf":
            corr = item.get("correcta", True)
            if isinstance(corr, str):
                corr = corr.lower().strip() in ("true", "t", "v", "verdadero", "1")
            else:
                corr = bool(corr)
            clean["correcta"] = corr
            
        elif tipo == "desarrollo":
            pts = item.get("puntos_clave", [])
            if isinstance(pts, str):
                pts = [pts]
            elif not isinstance(pts, list):
                pts = []
            clean["puntos_clave"] = [str(x) for x in pts]
            clean["respuesta_modelo"] = str(item.get("respuesta_modelo", "Respuesta de referencia no provista."))
            
        out.append(clean)
    return out

def _parse_calificacion(txt: str) -> dict:
    t = (txt or "").strip()
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    data = None
    try:
        data = json.loads(t)
    except Exception:
        bloque = _extraer_json_bloque(t)
        try:
            data = json.loads(bloque)
        except Exception:
            data = None
            
    if not data or not isinstance(data, dict):
        return {
            "puntaje": 0,
            "veredicto": "incorrecto",
            "aciertos": [],
            "faltantes": ["Error al interpretar la calificación."],
            "feedback": "El tutor no pudo estructurar la respuesta en un JSON legible.",
            "respuesta_modelo": ""
        }
        
    puntaje = 0
    try:
        puntaje = int(data.get("puntaje", 0))
    except Exception:
        pass
        
    veredicto = str(data.get("veredicto", "incorrecto")).strip().lower()
    if veredicto not in ("correcto", "parcial", "incorrecto"):
        veredicto = "parcial" if puntaje >= 40 else "incorrecto"
        
    aciertos = data.get("aciertos", [])
    if isinstance(aciertos, str):
        aciertos = [aciertos]
    elif not isinstance(aciertos, list):
        aciertos = []
        
    faltantes = data.get("faltantes", [])
    if isinstance(faltantes, str):
        faltantes = [faltantes]
    elif not isinstance(faltantes, list):
        faltantes = []
        
    return {
        "puntaje": puntaje,
        "veredicto": veredicto,
        "aciertos": [str(x) for x in aciertos],
        "faltantes": [str(x) for x in faltantes],
        "feedback": str(data.get("feedback", "")),
        "respuesta_modelo": str(data.get("respuesta_modelo", ""))
    }

def sys_quiz() -> str:
  p = perfil()
  return (
    "Eres un DISEÑADOR DE EVALUACIONES experto en " + p["disciplina"] + ". Tu trabajo es crear preguntas de\n"
    "examen RIGUROSAS para que un " + p["rol"] + " se autoevalúe sobre un documento técnico. Escribes en español.\n\n"
    "PRINCIPIOS DE CALIDAD (obligatorios):\n"
    "- FUNDAMENTA cada pregunta y su respuesta EXCLUSIVAMENTE en el CONTEXTO entregado. Si algo no está en\n"
    "  el contexto, NO lo preguntes. Cada pregunta lleva una \"cita\" (sección o figura de origen).\n"
    "- NADA de trivia: prohibido preguntar por autores, año, nombres de revista, o siglas por la sigla\n"
    "  misma. Prohibido lo respondible sin ENTENDER (copiar-pegar una frase).\n"
    "- MEZCLA niveles cognitivos (Bloom): incluye recuerdo, comprensión y —sobre todo— APLICACIÓN y\n"
    "  ANÁLISIS (interpretar una figura/tabla, comparar procesos, inferir consecuencias, aplicar a un\n"
    "  escenario). Prioriza aplicación/análisis para las de desarrollo.\n"
    "- OPCIÓN MÚLTIPLE: 4 opciones, UNA correcta. Los 3 distractores deben ser PLAUSIBLES y representar\n"
    "  errores conceptuales típicos (\"tentador pero incorrecto por una razón concreta\"), nunca rellenos\n"
    "  absurdos ni \"todas las anteriores\". Varía la posición de la correcta.\n"
    "- VERDADERO/FALSO: la afirmación debe apuntar a una DISTINCIÓN SUTIL o a un MITO/ERROR común, no a algo\n"
    "  obvio. En la explicación aclara POR QUÉ.\n"
    "- DESARROLLO: pregunta abierta que exija razonar/explicar/interpretar. Provee \"puntos_clave\" (la\n"
    "  rúbrica: qué DEBE contener una buena respuesta) y una \"respuesta_modelo\" ideal, ambas basadas en el\n"
    "  contexto.\n"
    "- FIGURAS: si el contexto trae \"FIGURAS DISPONIBLES\", genera al menos 1 pregunta que exija INTERPRETAR\n"
    "  una figura; en esa pregunta copia el identificador de la figura en el campo \"figura\" (el valor de\n"
    "  img=...). Para preguntas que no son de figura, \"figura\": null.\n"
    "- Ajusta la DIFICULTAD pedida (facil/media/dificil): más dificil = más análisis, distractores más finos,\n"
    "  escenarios de aplicación.\n\n"
    "SALIDA: devuelve ÚNICAMENTE un objeto JSON válido con la forma:\n"
    "{\"quiz\":[ ... ]}  (sin texto antes ni después, sin ``` ```).\n"
    "Cada item sigue EXACTAMENTE el esquema indicado en el mensaje del usuario. Usa LaTeX entre $...$ para\n"
    "símbolos/unidades. Responde en español."
)

def sys_quiz_grade() -> str:
  return (
    "Eres un TUTOR de " + perfil()["disciplina"] + " que corrige respuestas de desarrollo con criterio JUSTO y formativo, en\n"
    "español. Calificas comparando la respuesta del estudiante contra una rúbrica (puntos_clave) y una\n"
    "respuesta modelo. Das crédito PARCIAL. No castigas la redacción; evalúas la COMPRENSIÓN. Eres honesto:\n"
    "si está mal, lo dices, pero explicas cómo mejorar. Cita la fuente cuando corresponda.\n\n"
    "SALIDA: ÚNICAMENTE un objeto JSON:\n"
    "{\"puntaje\": 0-100, \"veredicto\": \"correcto\"|\"parcial\"|\"incorrecto\",\n"
    " \"aciertos\": [..], \"faltantes\": [..], \"feedback\": \"2-4 frases\", \"respuesta_modelo\": \"..\"}\n"
    "Sin texto fuera del JSON, sin ``` ```. LaTeX entre $...$."
)

def _generar_lote_preguntas(provider: str, model: str | None, system_prompt: str, user_prompt: str) -> list:
    try:
        if provider == "deepseek":
            gen = _chat_deepseek(system_prompt, user_prompt, [], model, False)
            txt = _texto_completo(provider, gen)
        else:
            gen = PROVIDERS[provider](system_prompt, user_prompt, [], model)
            txt = _texto_completo(provider, gen)
            
        items = _parse_quiz(txt)
        if not items:
            # Reintentar una vez con un prompt ligeramente más estricto si falló
            print("DEBUG: Reintentando generación de lote por fallo de parseo...")
            intentos_prompt = user_prompt + "\n\nIMPORTANTE: Responde ÚNICAMENTE con el formato JSON solicitado. Sin explicaciones adicionales."
            if provider == "deepseek":
                gen = _chat_deepseek(system_prompt, intentos_prompt, [], model, False)
                txt = _texto_completo(provider, gen)
            else:
                gen = PROVIDERS[provider](system_prompt, intentos_prompt, [], model)
                txt = _texto_completo(provider, gen)
            items = _parse_quiz(txt)
            if not items:
                # Si volvió a fallar, guardar para diagnóstico
                try:
                    with open(BASE / "debug_quiz_error.txt", "w", encoding="utf-8") as f_err:
                        f_err.write(txt)
                    print("DEBUG: Lote fallido guardado en debug_quiz_error.txt para diagnóstico.")
                except Exception:
                    pass
        return items
    except Exception as e:
        print(f"ERROR: Falló generación de lote: {e}")
        return []

@app.post("/quiz/generar")
def generar_quiz(req: QuizGenReq, authorization: str | None = Header(default=None)):
    check_token(authorization)
    provider, model = _resuelve_motor(req.provider, req.model)
    if provider not in PROVIDERS:
        provider = CHAT_PROVIDER

    if req.seccion:
        texto_scope = _texto_seccion(req.doc, req.seccion)
        alcance_str = f"la sección '{req.seccion}'"
    else:
        texto_scope = _texto_paper(req.doc)
        alcance_str = "todo el paper"

    bloque_figs, bloque_tabs = _figuras_tablas_txt(req.doc, texto_scope)

    # Construir las tareas de generación
    tareas = []
    
    # 1. Opción Múltiple
    if req.n_mc > 0:
        p_mc = (
            f"Genera exactamente {req.n_mc} preguntas de opción múltiple (tipo \"mc\").\n"
            f"Dificultad: {req.dificultad}.\n"
            f"Alcance: {alcance_str}.\n\n"
            f"Esquema de cada item en el array \"quiz\" (JSON):\n"
            f"- id (int), tipo (\"mc\"), enunciado, opciones (lista de 4 strings), correcta (índice 0-based de la correcta), cita, explicacion, nivel_bloom (\"recuerdo\"|\"comprension\"|\"aplicacion\"|\"analisis\"), seccion, figura (img de figura disponible o null).\n\n"
            f"=== CONTEXTO (documento: {req.doc}) ===\n"
            f"{texto_scope}\n\n"
            f"=== FIGURAS DISPONIBLES ===\n"
            f"{bloque_figs}\n\n"
            f"=== TABLAS DISPONIBLES ===\n"
            f"{bloque_tabs}\n\n"
            f"Devuelve SOLO el JSON {{\"quiz\":[...]}}."
        )
        tareas.append(("mc", p_mc))
        
    # 2. Verdadero / Falso
    if req.n_vf > 0:
        p_vf = (
            f"Genera exactamente {req.n_vf} preguntas de verdadero/falso (tipo \"vf\").\n"
            f"Dificultad: {req.dificultad}.\n"
            f"Alcance: {alcance_str}.\n\n"
            f"Esquema de cada item en el array \"quiz\" (JSON):\n"
            f"- id (int), tipo (\"vf\"), enunciado, correcta (true|false), cita, explicacion, nivel_bloom (\"recuerdo\"|\"comprension\"|\"aplicacion\"|\"analisis\"), seccion, figura (img de figura disponible o null).\n\n"
            f"=== CONTEXTO (documento: {req.doc}) ===\n"
            f"{texto_scope}\n\n"
            f"=== FIGURAS DISPONIBLES ===\n"
            f"{bloque_figs}\n\n"
            f"=== TABLAS DISPONIBLES ===\n"
            f"{bloque_tabs}\n\n"
            f"Devuelve SOLO el JSON {{\"quiz\":[...]}}."
        )
        tareas.append(("vf", p_vf))
        
    # 3. Desarrollo
    if req.n_desarrollo > 0:
        p_des = (
            f"Genera exactamente {req.n_desarrollo} preguntas de desarrollo (tipo \"desarrollo\").\n"
            f"Dificultad: {req.dificultad}.\n"
            f"Alcance: {alcance_str}.\n\n"
            f"Esquema de cada item en el array \"quiz\" (JSON):\n"
            f"- id (int), tipo (\"desarrollo\"), enunciado, puntos_clave (lista de strings, la rúbrica), respuesta_modelo (string), cita, explicacion, nivel_bloom (\"recuerdo\"|\"comprension\"|\"aplicacion\"|\"analisis\"), seccion, figura (img de figura disponible o null).\n\n"
            f"=== CONTEXTO (documento: {req.doc}) ===\n"
            f"{texto_scope}\n\n"
            f"=== FIGURAS DISPONIBLES ===\n"
            f"{bloque_figs}\n\n"
            f"=== TABLAS DISPONIBLES ===\n"
            f"{bloque_tabs}\n\n"
            f"Devuelve SOLO el JSON {{\"quiz\":[...]}}."
        )
        tareas.append(("desarrollo", p_des))

    if not tareas:
        raise HTTPException(status_code=400, detail="Debes solicitar al menos 1 pregunta en la mezcla.")

    # Ejecutar en paralelo usando ThreadPoolExecutor
    quiz_items = []
    with ThreadPoolExecutor(max_workers=len(tareas)) as executor:
        futures = {executor.submit(_generar_lote_preguntas, provider, model, sys_quiz(), prompt): tipo for tipo, prompt in tareas}
        for fut in futures:
            tipo = futures[fut]
            try:
                items = fut.result()
                if items:
                    quiz_items.extend(items)
            except Exception as e:
                print(f"ERROR obteniendo futuro para {tipo}: {e}")

    # Reasignar IDs secuenciales únicos para evitar duplicados
    for idx, item in enumerate(quiz_items, 1):
        item["id"] = idx

    if not quiz_items:
        raise HTTPException(status_code=500, detail="El LLM no generó un JSON de quiz válido o legible en ninguno de los lotes.")

    return {"quiz": quiz_items}

@app.post("/quiz/calificar")
def calificar_desarrollo(req: QuizCalReq, authorization: str | None = Header(default=None)):
    check_token(authorization)
    provider, model = _resuelve_motor(req.provider, req.model)
    if provider not in PROVIDERS:
        provider = CHAT_PROVIDER

    user_prompt = (
        f"PREGUNTA: {req.enunciado}\n\n"
        f"RÚBRICA (puntos clave que debe cubrir): {req.puntos_clave}\n\n"
        f"RESPUESTA MODELO (referencia): {req.respuesta_modelo}\n\n"
        f"RESPUESTA DEL ESTUDIANTE: {req.respuesta_usuario}\n\n"
        f"Califica siguiendo la rúbrica. Devuelve SOLO el JSON indicado."
    )

    try:
        if provider == "deepseek":
            gen = _chat_deepseek(sys_quiz_grade(), user_prompt, [], model, False)
            txt = _texto_completo(provider, gen)
        else:
            gen = PROVIDERS[provider](sys_quiz_grade(), user_prompt, [], model)
            txt = _texto_completo(provider, gen)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al calificar: {e}")

    return _parse_calificacion(txt)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("RAG_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost"):
        print(f"ADVERTENCIA: host={host} expone la app FUERA de este PC. "
              "El corpus y las conversaciones quedan accesibles en la red; "
              "asegúrate de tener RAG_TOKEN definido en el .env.")
    uvicorn.run(app, host=host, port=int(os.environ.get("RAG_PORT", "8901")))
