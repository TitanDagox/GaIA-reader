#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paso 3c — Reconstruir tablas como Markdown FIEL con visión (enfoque híbrido).

Antes (fallback): marker convierte la tabla a Markdown pero, en tablas complejas/
apaisadas, DESORDENA las celdas; un LLM la EXPLICABA desde ese texto (sin verla).
Problema: en tablas rotadas se perdían filas/valores (validado en Sillitoe Table 2).

Ahora (por defecto, VISION_TABLES=on): para cada tabla ubicamos su página en el PDF,
la renderizamos DERECHA (des-rotada) y le pedimos a un LLM con VISIÓN que reconstruya
la tabla en Markdown limpio, usando la imagen como VERDAD de estructura/orden y el
texto de marker solo como cotejo de ortografía.

Selección de modelo por dificultad (ahorra créditos Bedrock):
  - Gemma 4 (Ollama cloud) por defecto — gratis, bien en tablas simples/medianas.
  - Claude Sonnet (Bedrock) SOLO en tablas difíciles (rotadas / muchas columnas /
    celdas muy largas), donde Gemma revuelve el contenido.

Fallback: si no se puede ubicar/renderizar la tabla o falla la visión, se cae al
método de TEXTO anterior (nunca se pierde una tabla). Las tablas de baja confianza
se listan en descripciones/<doc>_tablas_revisar.txt (revisión OPCIONAL, no bloqueante).

Salida: descripciones/<doc>_tablas.md + <doc>_tablas.jsonl. Reanudable (cache).
"""

import base64
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from chunk import _secciones, _bloques, es_excluida, MIN_CHARS
from secciones import _gemini, _slug, _ollama_cloud

load_dotenv()

MD_DIR = Path(os.environ.get("MD_DIR", "./md"))
RAW_DIR = Path(os.environ.get("RAW_DIR", "./raw"))
OUT_DIR = Path("./descripciones")

# Visión para tablas (on por defecto). "off" vuelve al método de solo texto.
VISION_TABLES = os.environ.get("VISION_TABLES", "on").lower() in ("on", "true", "1", "yes")
# Forzar Sonnet para TODAS las tablas (máxima fidelidad; ignora la heurística Gemma/Sonnet).
TABLES_FORCE_SONNET = os.environ.get("TABLES_FORCE_SONNET", "").lower() in ("1", "on", "true", "yes")

DESCRIBE_PROVIDER = os.environ.get("VISION_PROVIDER", "bedrock").lower()
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-6")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
OLLAMA_URL = os.environ.get("OLLAMA_CLOUD_URL", "https://ollama.com").rstrip("/") + "/api/chat"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")

_bedrock_client = None
_TCAP = re.compile(r"(TABLE|TABLA|Tabla|Cuadro)\s*\.?\s*\d+[.:]?\s*[^\n|]*", re.IGNORECASE)


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

SISTEMA = f"Eres un experto en {DISCIPLINA} que explica tablas técnicas a un lector en formación."
SISTEMA_VIS = (f"Eres un asistente experto en extraer tablas técnicas de {DISCIPLINA} desde imágenes de "
               "PDF. Tu prioridad absoluta es la FIDELIDAD: no inventar, no omitir, no resumir datos.")

PROMPT_VIS = (
    "Reconstruye la TABLA de la IMAGEN como una tabla Markdown (GitHub-flavored) LIMPIA y FIEL.\n\n"
    "Reglas:\n"
    "- La IMAGEN es la verdad de terreno para la estructura (columnas, filas) y el ORDEN del texto.\n"
    "- El texto OCR de marker (abajo) tiene TODO el contenido pero el ORDEN de líneas dentro de "
    "cada celda puede estar INVERTIDO (tabla rotada). Úsalo SOLO para confirmar ortografía de "
    "términos técnicos, NO para el orden.\n"
    "- Incluye TODAS las filas y columnas visibles. No omitas ninguna. No inventes celdas.\n"
    "- Une el texto multilínea de cada celda en una frase legible (orden de la imagen).\n"
    "- Respeta símbolos ± y superíndices. Celdas vacías: déjalas vacías.\n"
    "- Si hay NOTAS AL PIE, reprodúcelas después de la tabla.\n"
    "- Si en la imagen hay VARIAS tablas, reconstruye SOLO la que corresponde a este título: {caption}\n"
    "Formato de salida EXACTO:\n"
    "1) La tabla Markdown (y sus notas al pie).\n"
    "2) En una línea nueva, '---RESUMEN---'.\n"
    "3) 1-2 frases en español: qué organiza y qué transmite la tabla. Nada más.\n\n"
    "=== TEXTO OCR DE MARKER (orden posiblemente invertido dentro de celdas) ===\n{marker}"
)

# Método de TEXTO (fallback) — el explicador anterior, intacto.
PROMPT_TEXTO = (
    f"Vas a explicar UNA TABLA de un paper/libro de {DISCIPLINA}. La tabla fue extraída "
    "automáticamente de un PDF y el ORDEN de las celdas puede estar DESORDENADO, pero el "
    "CONTENIDO está completo. Usa el título de la tabla, el contexto y tu conocimiento del "
    "dominio para:\n"
    "1. Decir QUÉ organiza la tabla: qué compara, y cuáles son sus columnas y filas.\n"
    "2. Explicar QUÉ TRANSMITE o para qué sirve (la conclusión que el lector debe sacar).\n"
    "3. Si puedes, reconstruir las relaciones clave fila por fila.\n"
    "No inventes datos que no aparezcan. Sé claro y profesional (2-4 párrafos o lista). "
    "IMPORTANTE: empieza DIRECTO con el punto 1, sin saludos ni frases introductorias. Al final "
    "agrega 'Palabras clave:' con 5-8 términos. Responde en español."
)


# ─────────────────────────── clientes de modelos ───────────────────────────
def _bedrock_text(prompt: str, system: str) -> str:
    global _bedrock_client
    if _bedrock_client is None:
        from anthropic import AnthropicBedrock
        _bedrock_client = AnthropicBedrock(aws_region=AWS_REGION)
    msg = _bedrock_client.messages.create(
        model=BEDROCK_MODEL, max_tokens=1500, system=system,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _bedrock_vision(png_b64: str, prompt: str, system: str) -> str:
    global _bedrock_client
    if _bedrock_client is None:
        from anthropic import AnthropicBedrock
        _bedrock_client = AnthropicBedrock(aws_region=AWS_REGION)
    msg = _bedrock_client.messages.create(
        model=BEDROCK_MODEL, max_tokens=4000, system=system,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_b64}},
            {"type": "text", "text": prompt},
        ]}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _bedrock_configurado() -> bool:
    """Heurística simple para avisar con un mensaje claro (no un traceback) si faltan
    credenciales de Bedrock, antes de gastar tiempo reconstruyendo tablas."""
    return bool(
        os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        or (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))
        or os.environ.get("AWS_PROFILE")
    )


def _gemini_vision(png_b64: str, prompt: str, system: str) -> str:
    """Respaldo de _bedrock_vision cuando VISION_PROVIDER=gemini: misma tarea (reconstruir
    la tabla desde la imagen) pero con Gemini. Mismo patrón de reintentos que _gemini()."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{os.environ.get('GEMINI_CHAT_MODEL', 'gemini-3.5-flash')}:generateContent")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/png", "data": png_b64}},
        ]}],
    }
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


def _ollama_vision(png_b64: str, prompt: str, system: str) -> str:
    api_key = os.environ.get("OLLAMA_API_KEY")
    if not api_key:
        raise ValueError("OLLAMA_API_KEY no está configurada")
    body = {"model": OLLAMA_MODEL, "stream": False, "options": {"num_predict": 4000},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt, "images": [png_b64]}]}
    for intento in range(4):
        r = requests.post(OLLAMA_URL, json=body,
                          headers={"Authorization": f"Bearer {api_key}"}, timeout=180)
        if r.status_code in (503, 429) and intento < 3:
            time.sleep(2 ** intento * 5); continue
        r.raise_for_status(); break
    return r.json()["message"]["content"].strip()


# ─────────────────────────── localización + render ───────────────────────────
_COMUNES = {"table", "tabla", "figure", "figura", "cuadro", "results", "values", "value",
            "number", "total", "average", "system", "systems", "sample", "samples"}


def _celda_tokens(tabla_md: str, extra: str = "") -> list[str]:
    """Cadenas distintivas para localizar la tabla en el PDF: frases multi-palabra de las
    celdas Y palabras sueltas largas (headers, p.ej. 'Eigenvector', 'Reflectance',
    'acceleration') — clave para tablas numéricas donde casi todo son números."""
    toks = set()
    for celda in re.split(r"[|\n]|<br>", tabla_md + " " + extra):
        c = re.sub(r"[*_`#]", "", celda).strip()
        if len(c) >= 10 and " " in c and re.search(r"[A-Za-zÁÉÍÓÚáéíóúñ]{4,}", c):
            toks.add(c[:40])
        for w in re.findall(r"[A-Za-zÁÉÍÓÚáéíóúñ]{6,}", c):  # palabras-header distintivas
            if w.lower() not in _COMUNES:
                toks.add(w)
    return list(toks)[:40]


def _localizar_pagina(doc, caption: str, tabla_md: str):
    """Devuelve (pageno, ambigua: bool) o (None, True). Busca el título; si no, rankea
    páginas por nº de tokens de celda encontrados."""
    import fitz  # noqa
    # 1) por título de tabla (lo más distintivo)
    cap = re.sub(r"^\s*(TABLE|TABLA|Tabla|Cuadro)\s*\.?\s*\d+[.:]?\s*", "", caption or "").strip()
    cap = re.sub(r"\s+", " ", cap)[:45]
    if len(cap) >= 12:
        hits = [i for i in range(len(doc)) if doc[i].search_for(cap)]
        if len(hits) == 1:
            return hits[0], False
    # 2) por densidad de tokens de celda (+ palabras del título; clave en tablas numéricas)
    toks = _celda_tokens(tabla_md, extra=caption or "")
    if not toks:
        return (None, True)
    score = {}
    for t in toks:
        for i in range(len(doc)):
            try:
                if doc[i].search_for(t):
                    score[i] = score.get(i, 0) + 1
            except Exception:
                pass
    if not score:
        return (None, True)
    mejor = max(score, key=score.get)
    top = score[mejor]
    # ambigua si otra página empata casi igual
    empatan = [p for p, s in score.items() if s >= top - 1 and p != mejor]
    return mejor, (top < 2 or len(empatan) > 0)


def _dir_dominante(page):
    rots = {}
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            d = l.get("dir", (1, 0))
            rots[d] = rots.get(d, 0) + 1
    return max(rots, key=rots.get) if rots else (1.0, 0.0)


def _render_upright(doc, pageno: int, zoom: float = 3.0):
    """Renderiza la página a PNG, rotándola para que el texto quede horizontal.
    Devuelve (png_bytes, rotated: bool)."""
    import fitz
    from PIL import Image
    page = doc[pageno]
    d = _dir_dominante(page)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    rotated = d != (1.0, 0.0)
    if d == (0.0, -1.0):
        img = img.rotate(-90, expand=True)
    elif d == (0.0, 1.0):
        img = img.rotate(90, expand=True)
    elif d == (-1.0, 0.0):
        img = img.rotate(180, expand=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), rotated


# ─────────────────────────── heurística de dificultad ───────────────────────────
def _ncols(tabla_md: str) -> int:
    for l in tabla_md.split("\n"):
        if l.count("|") >= 2:
            return l.count("|") - 1
    return 0


def _es_dificil(tabla_md: str, rotated: bool) -> bool:
    if rotated:
        return True
    if _ncols(tabla_md) >= 7:
        return True
    celdas = re.split(r"[|\n]", tabla_md)
    if any(len(c.strip()) > 250 for c in celdas):
        return True
    return False


def _n_filas_datos(md: str) -> int:
    """Cuenta filas de datos de una tabla markdown (excluye header y separador)."""
    filas = [l for l in md.split("\n") if l.count("|") >= 2]
    datos = [l for l in filas if not re.fullmatch(r"[\s|:\-–—]+", l)]
    return max(0, len(datos) - 1)  # -1 por el header


# ─────────────────────────── reconstrucción ───────────────────────────
def _explicar_texto(tabla_md: str, caption: str, contexto: str) -> str:
    prompt = (f"{PROMPT_TEXTO}\n\n=== TÍTULO ===\n{caption}\n\n=== CONTEXTO ===\n{contexto[:900]}\n\n"
              f"=== TABLA (Markdown, orden posible desordenado) ===\n{tabla_md[:6000]}")
    if os.environ.get("OLLAMA_API_KEY"):
        try:
            return _ollama_cloud(prompt, SISTEMA)
        except Exception:
            pass
    if DESCRIBE_PROVIDER != "gemini":
        return _bedrock_text(prompt, SISTEMA)
    return _gemini(prompt, SISTEMA)


def _partir_salida(salida: str):
    """Separa la tabla markdown del resumen ('---RESUMEN---')."""
    if "---RESUMEN---" in salida:
        tab, res = salida.split("---RESUMEN---", 1)
        return tab.strip(), res.strip()
    return salida.strip(), ""


def _titulo_de_markdown(md: str, fallback: str) -> str:
    """Extrae el título real de la tabla (TABLE/TABLA/Cuadro N ...) del markdown
    reconstruido, para reemplazar el caption basura que a veces deja marker."""
    for l in md.split("\n")[:6]:
        if "|" in l:
            continue
        m = re.search(r"(TABLE|TABLA|Tabla|Cuadro)\s*\.?\s*\d+[.:]?[^\n|]*", l, re.IGNORECASE)
        if m:
            return re.sub(r"[*_#`]", "", m.group(0)).strip()[:160]
    return fallback


def reconstruir_tabla(doc, tabla_md: str, caption: str, contexto: str) -> dict:
    """Devuelve dict con: markdown, resumen, texto (para indexar), modelo, pagina, dificil,
    revisar (bool), motivo. Nunca lanza: cae a texto si algo falla."""
    if not VISION_TABLES or doc is None:
        desc = _explicar_texto(tabla_md, caption, contexto)
        return {"markdown": "", "resumen": desc, "texto": desc, "modelo": "texto",
                "pagina": None, "dificil": False, "revisar": doc is None, "motivo": "sin_pdf"}

    pageno, ambigua = _localizar_pagina(doc, caption, tabla_md)
    if pageno is None:
        desc = _explicar_texto(tabla_md, caption, contexto)
        return {"markdown": "", "resumen": desc, "texto": desc, "modelo": "texto",
                "pagina": None, "dificil": False, "revisar": True, "motivo": "no_ubicada"}

    try:
        png, rotated = _render_upright(doc, pageno)
    except Exception as e:
        desc = _explicar_texto(tabla_md, caption, contexto)
        return {"markdown": "", "resumen": desc, "texto": desc, "modelo": "texto",
                "pagina": pageno, "dificil": False, "revisar": True, "motivo": f"render:{e}"}

    dificil = _es_dificil(tabla_md, rotated)
    b64 = base64.b64encode(png).decode()
    prompt = PROMPT_VIS.format(caption=caption[:120], marker=tabla_md[:6000])
    usar_sonnet = dificil or TABLES_FORCE_SONNET  # Gemma por defecto; Sonnet en difíciles o forzado

    # Vision "de alta fidelidad" (tablas difíciles o forzadas): Bedrock por defecto,
    # Gemini si VISION_PROVIDER=gemini (mismo prompt/salida, evita depender de AWS).
    if DESCRIBE_PROVIDER == "gemini":
        _vision_alta, etiqueta = _gemini_vision, "gemini-vision"
    else:
        _vision_alta, etiqueta = _bedrock_vision, "sonnet-vision"

    try:
        if usar_sonnet:
            salida = _vision_alta(b64, prompt, SISTEMA_VIS); modelo = etiqueta
        else:
            salida = _ollama_vision(b64, prompt, SISTEMA_VIS); modelo = "gemma-vision"
    except Exception as e:
        # si Gemma falla, intenta el proveedor de alta fidelidad; si ese también falla, cae a texto
        try:
            salida = _vision_alta(b64, prompt, SISTEMA_VIS); modelo = f"{etiqueta}(fb)"
        except Exception:
            desc = _explicar_texto(tabla_md, caption, contexto)
            return {"markdown": "", "resumen": desc, "texto": desc, "modelo": "texto",
                    "pagina": pageno, "dificil": dificil, "revisar": True, "motivo": f"vision:{e}"}

    markdown, resumen = _partir_salida(salida)
    # confianza: comparar nº de filas reconstruidas vs las que trae marker
    fm, fv = _n_filas_datos(tabla_md), _n_filas_datos(markdown)
    revisar = ambigua or (fm and abs(fm - fv) > 1) or fv == 0
    motivo = ""
    if ambigua:
        motivo = "pagina_ambigua"
    elif fm and abs(fm - fv) > 1:
        motivo = f"filas marker={fm} vs recon={fv}"
    elif fv == 0:
        motivo = "sin_filas"
    texto = markdown + (("\n\n" + resumen) if resumen else "")
    return {"markdown": markdown, "resumen": resumen, "texto": texto, "modelo": modelo,
            "pagina": pageno, "dificil": dificil, "revisar": revisar, "motivo": motivo,
            "caption": _titulo_de_markdown(markdown, caption)}


def _generar_pendientes_manual_tablas(doc, tablas, cache_dir: Path, stem: str) -> list:
    """VISION_PROVIDER=manual: no llama a ninguna API. Escribe en
    descripciones/<stem>_PENDIENTES_tablas/ todo lo que un agente de IA con visión
    necesita para reconstruir las tablas SIN cache aún (imagen de la página si se pudo
    ubicar/renderizar, texto OCR de marker, prompt exacto y el formato de caché que
    espera el resto del pipeline). Devuelve las tablas pendientes (vacío si ya estaban
    todas cacheadas -> el pipeline sigue normal)."""
    pendientes = [(n, tb, cache_dir / f"{n}_{_slug(tb['caption'])}.json")
                  for n, tb in enumerate(tablas, 1)
                  if not (cache_dir / f"{n}_{_slug(tb['caption'])}.json").exists()]
    if not pendientes:
        return pendientes

    pend_dir = OUT_DIR / f"{stem}_PENDIENTES_tablas"
    imgs_dir = pend_dir / "imgs"
    imgs_dir.mkdir(parents=True, exist_ok=True)

    bloques = []
    for n, tb, cache_file in pendientes:
        img_ref = None
        if doc is not None:
            try:
                pageno, _ambigua = _localizar_pagina(doc, tb["caption"], tb["tabla_md"])
                if pageno is not None:
                    png, _rot = _render_upright(doc, pageno)
                    img_path = imgs_dir / f"{n}.png"
                    img_path.write_bytes(png)
                    img_ref = img_path.resolve()
            except Exception:
                img_ref = None
        bloques.append(
            f"### Tabla {n} — {tb['caption']}\n"
            f"- Archivo de caché a crear: `descripciones/_cache_tablas_{stem}/{cache_file.name}`\n"
            f"- Imagen de la página: "
            f"{f'`{img_ref}`' if img_ref else '(no se pudo ubicar/renderizar; usa el texto OCR de abajo)'}\n"
            f"- Sección: {tb['seccion']}\n"
            f"- Contexto: {tb['contexto'][:900] or '(sin contexto)'}\n"
            f"- Texto OCR de marker (orden posiblemente invertido dentro de celdas):\n\n"
            f"```\n{tb['tabla_md'][:4000]}\n```\n"
        )

    _n0, tb0, _cf0 = pendientes[0]
    instrucciones = f"""# Reconstruir tablas pendientes — {stem}

`VISION_PROVIDER=manual`: este script NO llamó a ninguna API de visión. Se necesita que
un agente de IA con visión (tú) reconstruya cada tabla listada abajo y escriba el
resultado en el caché, con el formato EXACTO que espera el resto del pipeline
(`describir_tablas.py` / `ingesta.py`).

## Rol (sistema, para reconstruir la tabla desde la imagen)
{SISTEMA_VIS}

## Prompt (rellena {{caption}} y {{marker}} con los de CADA tabla; usa la imagen de la
página como verdad de estructura/orden, el texto OCR solo para cotejar ortografía)
{PROMPT_VIS}

## Tablas a reconstruir ({len(pendientes)})
{''.join(bloques)}
## Formato de salida (OBLIGATORIO)
Por cada tabla, crea un archivo JSON en la ruta indicada arriba
(`descripciones/_cache_tablas_{stem}/<n>_<slug-del-caption>.json`) con esta forma EXACTA
("seccion"/"tabla_md"/"contexto" son los dados arriba; "caption" puede ajustarse al
título real que veas en la imagen):

```json
{{
  "seccion": "{tb0['seccion']}",
  "tabla_md": "<tal cual el texto OCR de marker de esa tabla>",
  "caption": "<título final de la tabla>",
  "contexto": "<tal cual el contexto dado>",
  "markdown": "<la tabla reconstruida, Markdown GitHub-flavored>",
  "resumen": "<1-2 frases: qué organiza y qué transmite la tabla>",
  "texto": "<markdown>\\n\\n<resumen>",
  "modelo": "manual",
  "pagina": null,
  "dificil": false,
  "revisar": false,
  "motivo": "",
  "descripcion": "<igual a \\"texto\\">"
}}
```

Sigue las reglas del prompt: NO omitas filas/columnas, no inventes celdas, respeta ± y
superíndices, notas al pie después de la tabla.

## Al terminar
Re-corre `python ingesta.py "{stem}"` (o `python describir_tablas.py "{stem}"`): el
script detecta los cachés nuevos y sigue el pipeline como si nada.
"""
    (pend_dir / "DESCRIBIR_TABLAS.md").write_text(instrucciones, encoding="utf-8")
    return pendientes


def _caption(tabla_md: str, contexto: str, titulo_sec: str) -> str:
    for fuente in (tabla_md, contexto):
        m = _TCAP.search(fuente)
        if m:
            return m.group(0).strip()[:160]
    return f"(sin título; sección: {titulo_sec})"


def main(stem: str):
    # La consola Windows (cp1252) revienta al imprimir ⚠/±. Forzar UTF-8 también cuando
    # nos llaman desde ingesta.py (no solo standalone/worker).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    md = (MD_DIR / f"{stem}.md").read_text(encoding="utf-8")
    source = stem.replace("_noocr", "")
    pdf_path = RAW_DIR / f"{source}.pdf"
    doc = None
    if VISION_TABLES:
        try:
            import fitz
            if pdf_path.exists():
                doc = fitz.open(pdf_path)
            else:
                print(f"AVISO: no está {pdf_path.name}; tablas por TEXTO (sin visión).")
        except Exception as e:
            print(f"AVISO: no se pudo abrir el PDF ({e}); tablas por TEXTO.")

    tablas = []
    for titulo, _n, cuerpo in _secciones(md):
        if es_excluida(titulo):
            continue
        bloques = _bloques(cuerpo)
        prosa = " ".join(t for tp, t in bloques if tp == "text")
        for tp, t in bloques:
            if tp == "table" and len(t) >= MIN_CHARS:
                tablas.append({"seccion": titulo, "tabla_md": t,
                               "caption": _caption(t, prosa, titulo), "contexto": prosa})
    print(f"{len(tablas)} tablas encontradas en {stem}.md  "
          f"(visión: {'ON' if doc is not None else 'OFF/texto'})")
    if not tablas:
        if doc is not None:
            doc.close()
        return

    OUT_DIR.mkdir(exist_ok=True)
    cache_dir = OUT_DIR / f"_cache_tablas_{stem}"
    cache_dir.mkdir(exist_ok=True)

    # ── Fallback controlado por VISION_PROVIDER (bedrock | gemini | manual) ──
    if DESCRIBE_PROVIDER == "manual":
        pendientes = _generar_pendientes_manual_tablas(doc, tablas, cache_dir, stem)
        if pendientes:
            if doc is not None:
                doc.close()
            print(f"\n[PAUSA] VISION_PROVIDER=manual: {len(pendientes)} tabla(s) sin reconstruir.")
            print("   Instrucciones autocontenidas para el agente:")
            print(f"     descripciones/{stem}_PENDIENTES_tablas/DESCRIBIR_TABLAS.md")
            print(f"   Cuando el agente escriba los .json en descripciones/_cache_tablas_{stem}/, "
                  "re-ejecuta el mismo comando.")
            sys.exit(3)
    elif DESCRIBE_PROVIDER == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            if doc is not None:
                doc.close()
            sys.exit("ERROR: VISION_PROVIDER=gemini pero falta GEMINI_API_KEY en el .env.\n"
                      "  Alternativas: VISION_PROVIDER=bedrock (si tienes AWS_BEARER_TOKEN_BEDROCK) "
                      "o VISION_PROVIDER=manual (un agente de IA reconstruye las tablas sin gastar API).")
    elif not _bedrock_configurado():  # bedrock (default) o un valor no reconocido
        if doc is not None:
            doc.close()
        sys.exit("ERROR: VISION_PROVIDER=bedrock pero falta AWS_BEARER_TOKEN_BEDROCK "
                  "(o credenciales AWS) en el .env.\n"
                  "  Alternativas: VISION_PROVIDER=gemini (si tienes GEMINI_API_KEY) "
                  "o VISION_PROVIDER=manual (un agente de IA reconstruye las tablas sin gastar API).")

    registros, para_revisar = [], []
    for n, tb in enumerate(tablas, 1):
        cache_file = cache_dir / f"{n}_{_slug(tb['caption'])}.json"
        if cache_file.exists():
            rec = json.loads(cache_file.read_text(encoding="utf-8"))
            print(f"[{n}] {tb['caption'][:45]}: cache ({rec.get('modelo','?')}).")
        else:
            print(f"[{n}] {tb['caption'][:45]}: reconstruyendo ...", end=" ", flush=True)
            try:
                info = reconstruir_tabla(doc, tb["tabla_md"], tb["caption"], tb["contexto"])
            except Exception as e:
                print(f"FALLO: {e}"); break
            rec = {**tb, **info, "descripcion": info["texto"]}
            cache_file.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"ok [{info['modelo']}{' [revisar]' if info['revisar'] else ''}]")
        registros.append(rec)
        if rec.get("revisar"):
            para_revisar.append((n, rec["caption"], rec.get("motivo", "")))

    if doc is not None:
        doc.close()

    partes = [f"# Explicación de tablas — {source}\n"]
    for r in registros:
        marca = "  ⚠ REVISAR" if r.get("revisar") else ""
        partes.append(f"\n## {r['caption']}{marca}\n\n*(sección: {r['seccion']} · modelo: "
                      f"{r.get('modelo','?')} · pág: {r.get('pagina')})*\n\n{r['descripcion']}\n")
    (OUT_DIR / f"{stem}_tablas.md").write_text("\n".join(partes), encoding="utf-8")
    with (OUT_DIR / f"{stem}_tablas.jsonl").open("w", encoding="utf-8") as f:
        for r in registros:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if para_revisar:
        lst = "\n".join(f"[{n}] {cap[:70]}  →  {mot}" for n, cap, mot in para_revisar)
        (OUT_DIR / f"{stem}_tablas_revisar.txt").write_text(lst, encoding="utf-8")
        print(f"\n[!] {len(para_revisar)} tabla(s) marcadas para revisión opcional -> "
              f"{stem}_tablas_revisar.txt")
    print(f"\nListo: {len(registros)} tablas -> descripciones/{stem}_tablas.md")


if __name__ == "__main__":
    try:  # evita crashes por símbolos (⚠/±) en consola Windows cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) != 2:
        sys.exit('Uso: python describir_tablas.py "<doc_stem sin .md>"')
    main(sys.argv[1])
