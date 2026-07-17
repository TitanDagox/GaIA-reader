#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paso 3b — Esquema por secciones (el "resumen detallado por títulos").

Dos productos, calculados UNA vez por documento:
  1. Jerarquía real de títulos: los niveles #/##/### de marker NO son fiables,
     así que se le pasa a Gemini SOLO la lista de títulos y devuelve el nivel
     correcto (1=sección mayor, 2=subsección, 3=...). Barato. Fallback: niveles
     de marker si el LLM falla.
  2. Resumen detallado de cada sección (map sobre secciones).

Salida:
  outlines/<doc>.md    → tabla de contenidos anidada + resumen por sección (legible)
  outlines/<doc>.jsonl → {titulo, nivel, resumen} por sección (para el endpoint /outline)

Reanudable: cachea cada resumen en outlines/_cache_<doc>/<n>.md.
"""

import os
import re
import sys
import json
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from chunk import _secciones, es_excluida, MIN_CHARS

load_dotenv()

MD_DIR = Path(os.environ.get("MD_DIR", "./md"))
OUT_DIR = Path(os.environ.get("OUTLINES_DIR", "./outlines"))
GEMINI_MODEL = os.environ.get("GEMINI_CHAT_MODEL", "gemini-3.5-flash")


def _disciplina() -> str:
    """Disciplina para los prompts, leída de perfil.json (mismo perfil que usa el chat).
    Recién clonado (sin perfil.json) devuelve un default neutral, sin sesgo de dominio."""
    try:
        d = json.loads((Path(__file__).resolve().parent / "perfil.json")
                       .read_text(encoding="utf-8")).get("disciplina", "").strip()
        return d or "temas técnicos"
    except Exception:
        return "temas técnicos"


DISCIPLINA = _disciplina()


def _gemini(prompt: str, sistema: str = "") -> str:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if sistema:
        body["systemInstruction"] = {"parts": [{"text": sistema}]}
    for intento in range(4):
        r = requests.post(url, json=body,
                          headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, timeout=120)
        if r.status_code in (503, 429) and intento < 3:
            time.sleep(2 ** intento * 5)
            continue
        r.raise_for_status()
        break
    data = r.json()
    return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()


def _ollama_cloud(prompt: str, sistema: str = "") -> str:
    """Realiza una petición POST no-streaming a Ollama Cloud (Gemma 4 31B por defecto)."""
    url = os.environ.get("OLLAMA_CLOUD_URL", "https://ollama.com").rstrip("/") + "/api/chat"
    model = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")
    api_key = os.environ.get("OLLAMA_API_KEY")
    if not api_key:
        raise ValueError("OLLAMA_API_KEY no está configurada")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    messages = []
    if sistema:
        messages.append({"role": "system", "content": sistema})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": model,
        "messages": messages,
        "stream": False
    }

    for intento in range(4):
        try:
            r = requests.post(url, json=body, headers=headers, timeout=120)
            if r.status_code in (503, 429) and intento < 3:
                time.sleep(2 ** intento * 5)
                continue
            r.raise_for_status()
            break
        except Exception as e:
            if intento < 3:
                time.sleep(2 ** intento * 5)
                continue
            raise e

    data = r.json()
    return data["message"]["content"].strip()



def reconstruir_jerarquia(titulos: list[str], niveles_marker: list[int]) -> list[int]:
    """Devuelve un nivel (1/2/3) por título. Fallback: niveles de marker normalizados."""
    lista = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titulos))
    prompt = (
        "Estos son los TÍTULOS de las secciones de un documento técnico, en orden. "
        "Los niveles de encabezado originales no son fiables. Asigna a cada título su "
        "nivel jerárquico REAL: 1 = sección principal, 2 = subsección, 3 = sub-subsección. "
        "Usa el sentido del contenido y las convenciones (un título en MAYÚSCULAS o de tema "
        "amplio suele ser nivel 1; uno específico bajo él, nivel 2).\n"
        "Responde SOLO con JSON: una lista de enteros (el nivel de cada título, en el mismo "
        f"orden), sin texto adicional.\n\nTÍTULOS:\n{lista}"
    )
    try:
        txt = _gemini(prompt).strip()
        txt = re.sub(r"^```(json)?|```$", "", txt, flags=re.MULTILINE).strip()
        niveles = json.loads(txt)
        if isinstance(niveles, list) and len(niveles) == len(titulos):
            return [int(n) for n in niveles]
    except Exception as e:
        print(f"(jerarquía por LLM falló: {e} — uso niveles de marker)")
    # Fallback: normalizar niveles de marker a 1..3 por rango
    unicos = sorted(set(niveles_marker))
    rango = {v: min(i + 1, 3) for i, v in enumerate(unicos)}
    return [rango[v] for v in niveles_marker]


def resumir(titulo: str, cuerpo: str) -> str:
    prompt = (
        f"Resume la siguiente sección de un documento técnico de {DISCIPLINA}, titulada "
        f"'{titulo}'. El resumen debe ser DETALLADO (no una línea): captura todos los "
        f"conceptos, definiciones, datos y relaciones clave, en 3-6 viñetas o un párrafo "
        f"denso. No inventes nada fuera del texto. IMPORTANTE: empieza DIRECTO con el "
        f"contenido, sin ninguna frase introductoria ni meta-comentario (nada de 'Aquí "
        f"tiene un resumen...'). Responde en español.\n\n=== SECCIÓN ===\n{cuerpo}"
    )
    sistema = f"Eres un experto en {DISCIPLINA} que sintetiza documentos técnicos con precisión."
    
    if os.environ.get("OLLAMA_API_KEY"):
        try:
            print("(usando Gemma)...", end=" ", flush=True)
            return _limpiar_preambulo(_ollama_cloud(prompt, sistema))
        except Exception as e:
            print(f"(falló Gemma: {e} — usando fallback a Gemini)...", end=" ", flush=True)

    return _limpiar_preambulo(_gemini(prompt, sistema=sistema))



def _slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60] or "x"


_PREAMBULO = re.compile(
    r"^\s*(aquí|aqui|a continuación|claro|este resumen|el siguiente).{0,80}:\s*\n+",
    re.IGNORECASE)


def _limpiar_preambulo(txt: str) -> str:
    return _PREAMBULO.sub("", txt).strip()


def main(stem: str):
    md = (MD_DIR / f"{stem}.md").read_text(encoding="utf-8")
    # Incluir TODAS las secciones para la ESTRUCTURA (los títulos-padre sin cuerpo
    # propio son contenedores válidos → dan la jerarquía). Solo se RESUME las que
    # tienen cuerpo; las vacías quedan como entrada de TOC sin resumen.
    secs = [(t, n, c) for t, n, c in _secciones(md)
            if not es_excluida(t) and t != "(inicio)"]
    if not secs:
        sys.exit("No hay secciones.")

    titulos = [t for t, _, _ in secs]
    niveles_marker = [n for _, n, _ in secs]
    print(f"{len(secs)} secciones ({sum(1 for _,_,c in secs if len(c.strip())>=MIN_CHARS)} con cuerpo). Reconstruyendo jerarquía...")
    niveles = reconstruir_jerarquia(titulos, niveles_marker)

    OUT_DIR.mkdir(exist_ok=True)
    cache_dir = OUT_DIR / f"_cache_{stem}"
    cache_dir.mkdir(exist_ok=True)

    registros = []
    for i, ((titulo, _n, cuerpo), nivel) in enumerate(zip(secs, niveles), 1):
        if len(cuerpo.strip()) < MIN_CHARS:
            registros.append({"titulo": titulo, "nivel": nivel, "resumen": ""})
            print(f"[{i:>2}/{len(secs)}] {titulo[:40]}: (contenedor, sin cuerpo)")
            continue
        cache_file = cache_dir / f"{_slug(titulo)}.md"
        if cache_file.exists():
            resumen = _limpiar_preambulo(cache_file.read_text(encoding="utf-8"))
            print(f"[{i:>2}/{len(secs)}] {titulo[:40]}: cache.")
        else:
            print(f"[{i:>2}/{len(secs)}] {titulo[:40]}: resumiendo ...", end=" ", flush=True)
            try:
                resumen = resumir(titulo, cuerpo)
            except Exception as e:
                print(f"FALLO: {e}")
                break
            cache_file.write_text(resumen, encoding="utf-8")
            print("ok")
        registros.append({"titulo": titulo, "nivel": nivel, "resumen": resumen})

    # Salida legible (TOC anidado + resúmenes)
    partes = [f"# Esquema — {stem.replace('_noocr','')}\n"]
    for r in registros:
        h = "#" * min(r["nivel"] + 1, 6)
        partes.append(f"\n{h} {r['titulo']}\n\n{r['resumen']}\n")
    (OUT_DIR / f"{stem}.md").write_text("\n".join(partes), encoding="utf-8")
    with (OUT_DIR / f"{stem}.jsonl").open("w", encoding="utf-8") as f:
        for r in registros:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nListo: {len(registros)} secciones -> outlines/{stem}.md")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('Uso: python secciones.py "<doc_stem sin .md>"')
    main(sys.argv[1])
