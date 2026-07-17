#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paso 2 de la ingesta — Describir figuras/gráficos/tablas con Gemini (visión).

Para cada imagen `![](...)` del Markdown extraído:
  - toma su pie de figura (línea 'FIG. N...') + el texto de contexto alrededor,
  - le pide a Gemini una descripción CONSCIENTE DEL TIPO:
      * diagrama esquemático -> qué proceso/relación ilustra y sus elementos,
      * GRÁFICO (ejes/curvas) -> qué hay en cada eje, la tendencia, y QUÉ TRANSMITE,
      * tabla -> estructura y conclusiones clave.
Reanudable: cachea cada descripción en descripciones/_cache_<doc>/<img>.md.
Salida: descripciones/<doc>.md (legible) + <doc>.jsonl (para indexar luego).

Uso:
    python describir_figuras.py "Porphyry Copper Systems (Sillitoe, 2010)_noocr"
"""

import os
import re
import sys
import json
import time
import base64
import hashlib
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

MD_DIR = Path(os.environ.get("MD_DIR", "./md"))
OUT_DIR = Path("./descripciones")
GEMINI_MODEL = os.environ.get("GEMINI_CHAT_MODEL", "gemini-3.5-flash")
# Proveedor de visión para describir figuras: "bedrock" (Claude Sonnet, calidad tope
# con créditos AWS) o "gemini". Bedrock evita gastar la cuota paga de Gemini.
DESCRIBE_PROVIDER = os.environ.get("VISION_PROVIDER", "bedrock").lower()
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-6")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
CTX_CHARS = 900  # cuántos caracteres de contexto tomar antes/después de la figura
# Punto de control humano: con "on" (default), la primera pasada genera un PLAN de
# figuras (agrupaciones de mosaicos, pies, descartes) y SE DETIENE para que el usuario lo
# revise/corrija ANTES de gastar tokens en Sonnet y embeddings. "off" = flujo directo.
REVISION_FIGURAS = os.environ.get("REVISION_FIGURAS", "on").lower() in ("on", "true", "1", "yes")


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

# Pie de figura/tabla al inicio de línea, tolerando negrita/itálica (**Figure 5.**).
# OJO: 'fig\b' NO matchea "Figure" (el \b corta dentro de la palabra) → hay que cubrir
# las formas completas: fig / figs / fig. / figure(s) / figura(s), table(s), tabla(s)...
_KW_CAP = re.compile(
    r"^[\s*_]*(fig(ure|ura)?s?|tables?|tablas?|cuadros?|ilustraci\w*|foto\w*|gr[áa]ficos?|charts?)\b",
    re.IGNORECASE,
)

# marker deja anclas de referencias cruzadas (<span id="page-5-0"></span>) pegadas a
# algunos pies de figura (cuando el destino del enlace interno cae en el caption).
_HTML_TAG = re.compile(r"</?\w+[^>]*>")


def _limpiar_caption(texto: str) -> str:
    """Quita anclas/etiquetas HTML residuales de marker y normaliza espacios."""
    return re.sub(r"\s+", " ", _HTML_TAG.sub("", texto or "")).strip()

SISTEMA = f"Eres un experto en {DISCIPLINA} que explica figuras técnicas a un lector en formación."

PROMPT_BASE = (
    f"Vas a explicar UNA figura de un paper/libro de {DISCIPLINA}. Primero CLASIFÍCALA "
    "mentalmente en uno de estos tipos y adapta tu explicación:\n"
    "- DIAGRAMA ESQUEMÁTICO (corte, modelo conceptual): explica qué proceso o relación "
    "ilustra, los elementos clave y su significado espacial/temporal.\n"
    "- GRÁFICO (ejes, curvas, puntos, campos): di EXPLÍCITAMENTE qué variable va en cada "
    "eje, qué representan las curvas/campos/símbolos, cuál es la TENDENCIA o relación "
    "principal, y —lo más importante— QUÉ TRANSMITE o qué hay que concluir de él. Si sirve, "
    "da un ejemplo de cómo leerlo ('a mayor X, ...').\n"
    "- TABLA: resume su estructura (qué compara) y las conclusiones clave.\n\n"
    "Apóyate en el PIE DE FIGURA y el TEXTO DE CONTEXTO para anclar la explicación en lo que "
    "el autor quiso mostrar, pero describe también lo que se ve en la imagen. Sé preciso, "
    "profesional y claro (2-3 párrafos). IMPORTANTE: empieza DIRECTO con el contenido, sin "
    "saludos, sin dirigirte al lector, sin frases introductorias (nada de 'Hola' ni 'Esta "
    "figura que tienes delante'). Al final, agrega una línea 'Palabras clave:' con "
    "5-8 términos para búsqueda. Responde en español."
)


_bedrock_client = None


def _get_bedrock():
    """Cliente Bedrock (Messages API, con visión) creado una sola vez.
    Autentica con la API key de Bedrock (AWS_BEARER_TOKEN_BEDROCK en el .env)."""
    global _bedrock_client
    if _bedrock_client is None:
        from anthropic import AnthropicBedrock
        _bedrock_client = AnthropicBedrock(aws_region=AWS_REGION)
    return _bedrock_client


def _bedrock_configurado() -> bool:
    """Heurística simple para avisar con un mensaje claro (no un traceback) si faltan
    credenciales de Bedrock, antes de gastar tiempo describiendo figuras."""
    return bool(
        os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        or (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))
        or os.environ.get("AWS_PROFILE")
    )


def _describir_bedrock(b64: str, mime: str, prompt: str) -> str:
    """Describe una figura con Claude Sonnet en Bedrock. El SDK reintenta 429/5xx solo."""
    msg = _get_bedrock().messages.create(
        model=BEDROCK_MODEL,
        max_tokens=1500,
        system=SISTEMA,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
            {"type": "text", "text": prompt},
        ]}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def describir_imagen(img_path: Path, caption: str, contexto: str) -> str:
    b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
    mime = "image/jpeg" if img_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    prompt = (f"{PROMPT_BASE}\n\n=== PIE DE FIGURA ===\n{caption or '(sin pie de figura)'}"
              f"\n\n=== TEXTO DE CONTEXTO ===\n{contexto or '(sin contexto)'}")
    if DESCRIBE_PROVIDER != "gemini":  # bedrock por defecto; solo "gemini" fuerza Gemini
        return _describir_bedrock(b64, mime, prompt)
    # --- Gemini (respaldo) ---
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    body = {
        "systemInstruction": {"parts": [{"text": SISTEMA}]},
        "contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime, "data": b64}},
        ]}],
    }
    # Reintento ante errores transitorios (503 Service Unavailable, 429 rate limit).
    for intento in range(4):
        r = requests.post(url, json=body,
                          headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, timeout=120)
        if r.status_code in (503, 429) and intento < 3:
            espera = 2 ** intento * 5  # 5, 10, 20 s
            print(f"(transitorio {r.status_code}, reintento en {espera}s)", end=" ", flush=True)
            time.sleep(espera)
            continue
        r.raise_for_status()
        break
    data = r.json()
    return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()


def combinar_imagenes(image_paths, output_path):
    """Combina múltiples imágenes en un solo archivo JPEG (apilado vertical o grilla)."""
    from PIL import Image
    images = [Image.open(p) for p in image_paths]
    n = len(images)
    if n == 1:
        images[0].convert("RGB").save(output_path, "JPEG", quality=90)
        return
        
    if n <= 3:
        # Apilado vertical: redimensiona al ancho de la primera imagen
        target_width = images[0].width
        resized_images = []
        total_height = 0
        for img in images:
            aspect = img.height / img.width
            h = int(target_width * aspect)
            resized_images.append(img.resize((target_width, h), Image.Resampling.LANCZOS))
            total_height += h
            
        new_img = Image.new("RGB", (target_width, total_height), (255, 255, 255))
        y_offset = 0
        for img in resized_images:
            new_img.paste(img, (0, y_offset))
            y_offset += img.height
        new_img.save(output_path, "JPEG", quality=90)
    else:
        # Grilla de 2 columnas
        cols = 2
        rows = (n + 1) // 2
        target_width = max(img.width for img in images)
        resized_images = []
        for img in images:
            aspect = img.height / img.width
            h = int(target_width * aspect)
            resized_images.append(img.resize((target_width, h), Image.Resampling.LANCZOS))
            
        row_heights = []
        for r in range(rows):
            row_imgs = resized_images[r*cols : (r+1)*cols]
            row_heights.append(max(img.height for img in row_imgs))
            
        total_width = target_width * cols
        total_height = sum(row_heights)
        
        new_img = Image.new("RGB", (total_width, total_height), (255, 255, 255))
        
        for idx, img in enumerate(resized_images):
            r = idx // cols
            c = idx % cols
            x_offset = c * target_width
            y_offset = sum(row_heights[:r])
            new_img.paste(img, (x_offset, y_offset))
            
        new_img.save(output_path, "JPEG", quality=90)


def parsear_grupos(md_text: str, figs_dir: Path):
    """Analiza el md y PROPONE grupos de imágenes (mosaicos) sin describir ni fusionar
    nada aún. Devuelve (grupos, descartadas): grupo = {imgs, caption, contexto, omitir}."""
    from PIL import Image
    lineas = md_text.split("\n")
    patron_img = re.compile(r"!\[\]\(([^)]+)\)")

    # 1. Todas las imágenes referenciadas en el md que existen en disco
    todas = []
    for idx, ln in enumerate(lineas):
        m = patron_img.search(ln)
        if m:
            nombre = Path(m.group(1)).name
            p = figs_dir / nombre
            if p.exists():
                todas.append({"line_idx": idx, "img_name": nombre, "path": p})

    # 2. Filtrar logos y decoraciones (muy angostas O muy bajas)
    filtradas, descartadas = [], []
    for img in todas:
        try:
            with Image.open(img["path"]) as im:
                w, h = im.size
            if w < 250 or h < 100:
                descartadas.append({"img": img["img_name"], "dim": f"{w}x{h}"})
                continue
        except Exception as e:
            print(f"  (Error leyendo dimensiones de {img['img_name']}: {e})")
        filtradas.append(img)

    if not filtradas:
        return [], descartadas

    # 3. Agrupar imágenes consecutivas de la misma página; un título (#) o un pie de
    #    figura/tabla en medio actúa como barrera (separa grupos).
    grupos_raw = []
    actual = [filtradas[0]]
    for item in filtradas[1:]:
        last = actual[-1]
        lp = re.search(r"_page_(\d+)_", last["img_name"])
        cp = re.search(r"_page_(\d+)_", item["img_name"])
        juntas = bool(lp and cp and lp.group(1) == cp.group(1))
        if juntas:
            for k in range(last["line_idx"] + 1, item["line_idx"]):
                lv = _limpiar_caption(lineas[k])   # sin anclas <span> de marker
                if lv and (lv.startswith("#") or _KW_CAP.match(lv)):
                    juntas = False
                    break
        if juntas:
            actual.append(item)
        else:
            grupos_raw.append(actual)
            actual = [item]
    grupos_raw.append(actual)

    # 4. Pie y contexto por grupo (búsqueda bidireccional con barrera en otras imágenes)
    grupos = []
    for grp in grupos_raw:
        first_idx, last_idx = grp[0]["line_idx"], grp[-1]["line_idx"]
        caption = ""
        for j in range(last_idx + 1, min(last_idx + 4, len(lineas))):      # adelante
            if patron_img.search(lineas[j]):
                break
            limpia = _limpiar_caption(lineas[j])
            if limpia and _KW_CAP.match(limpia):
                caption = limpia
                break
        if not caption:                                                     # atrás
            for j in range(first_idx - 1, max(-1, first_idx - 4), -1):
                if patron_img.search(lineas[j]):
                    break
                limpia = _limpiar_caption(lineas[j])
                if limpia and _KW_CAP.match(limpia):
                    caption = limpia
                    break
        if not caption:                                                     # respaldo
            for j in range(last_idx + 1, min(last_idx + 4, len(lineas))):
                if patron_img.search(lineas[j]):
                    break
                limpia = _limpiar_caption(lineas[j])
                if limpia:
                    caption = limpia
                    break

        antes = " ".join(l for l in lineas[max(0, first_idx - 6):first_idx] if not patron_img.search(l))
        despues = " ".join(l for l in lineas[last_idx + 1:last_idx + 8] if not patron_img.search(l))
        contexto = (antes[-CTX_CHARS:] + " ... " + despues[:CTX_CHARS]).strip()
        grupos.append({"imgs": [g["img_name"] for g in grp], "caption": caption,
                       "contexto": contexto, "omitir": False})
    return grupos, descartadas


def _materializar(grupos, figs_dir: Path):
    """Convierte los grupos (ya revisados/aprobados) en figuras concretas. Los mosaicos
    se fusionan en disco con nombre derivado de SUS MIEMBROS (hash): si el usuario edita el
    grupo, cambia el nombre → nunca se reutiliza una descripción cacheada obsoleta."""
    figuras = []
    for g in grupos:
        if g.get("omitir"):
            continue
        imgs = [i for i in g.get("imgs", []) if (figs_dir / i).exists()]
        if not imgs:
            continue
        if len(imgs) == 1:
            nombre = imgs[0]
        else:
            # La firma incluye el TAMAÑO de cada miembro: si el usuario recorta o reemplaza
            # un panel, cambia la firma → mosaico y descripción se regeneran (no se
            # reutiliza el cache de la versión vieja).
            firma = hashlib.md5("|".join(
                f"{i}:{(figs_dir / i).stat().st_size}" for i in imgs
            ).encode("utf-8")).hexdigest()[:6]
            nombre = f"{Path(imgs[0]).stem}_merged_{firma}.jpeg"
            destino = figs_dir / nombre
            if not destino.exists():
                print(f"  -> Creando mosaico: {nombre} ({len(imgs)} imágenes)")
                combinar_imagenes([figs_dir / i for i in imgs], destino)
        figuras.append({"img": nombre, "caption": g.get("caption", ""),
                        "contexto": g.get("contexto", "")})
    return figuras


def _escribir_html_revision(plan, html_path: Path, doc_stem: str):
    """Página estática para que el usuario revise a ojo las agrupaciones propuestas."""
    import html as _html
    from urllib.parse import quote
    bloques = []
    for g in plan["grupos"]:
        accion = (f"FUSIONAR {len(g['imgs'])} imágenes en 1 mosaico"
                  if len(g["imgs"]) > 1 else "figura individual")
        omit = ' <span class="omit">[OMITIR]</span>' if g.get("omitir") else ""
        imgs = "".join(
            f'<figure><img src="../md/{quote(doc_stem)}_figs/{quote(i)}" loading="lazy">'
            f"<figcaption>{_html.escape(i)}</figcaption></figure>" for i in g["imgs"])
        bloques.append(
            f'<section><h2>Grupo {g["n"]} — {accion}{omit}</h2>'
            f'<p class="cap">{_html.escape(g.get("caption") or "(sin pie detectado)")}</p>'
            f'<div class="row">{imgs}</div></section>')
    desc = "".join(f"<li>{_html.escape(d['img'])} ({d.get('dim', '?')})</li>"
                   for d in plan.get("descartadas", [])) or "<li>(ninguna)</li>"
    html_path.write_text(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Revisión de figuras — {_html.escape(doc_stem)}</title><style>
body{{font-family:system-ui;margin:2rem;background:#f4f4f0;color:#222;max-width:1100px}}
section{{background:#fff;border:1px solid #ccc;padding:1rem;margin-bottom:1.5rem}}
h2{{margin:0 0 .3rem;font-size:1rem}} .cap{{color:#555;font-size:.9rem;margin:.2rem 0 .8rem}}
.row{{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start}}
figure{{margin:0;text-align:center}} img{{max-height:240px;max-width:340px;border:1px solid #999}}
figcaption{{font-family:monospace;font-size:.7rem}} .omit{{color:#c00}}
.aviso{{background:#fff3cd;border:1px solid #d4b106;padding:1rem;margin-bottom:1.5rem;font-size:.95rem}}
</style></head><body>
<h1>Revisión de figuras — {_html.escape(doc_stem)}</h1>
<div class="aviso"><b>Cómo aprobar:</b> cada grupo de &gt;1 imagen se fusionará en un mosaico y se
describirá con UNA llamada a Sonnet. Para corregir, edita
<code>descripciones/{_html.escape(doc_stem)}_figuras_plan.json</code>: mueve nombres entre las
listas <code>"imgs"</code> para separar/unir grupos (puedes crear grupos nuevos copiando el
formato), pon <code>"omitir": true</code> para descartar uno, y corrige <code>"caption"</code>
si está mal. Al terminar pon <code>"aprobado": true</code> y re-ejecuta el mismo comando.
<b>Nada se envía a Sonnet ni a embeddings hasta que apruebes.</b></div>
{''.join(bloques)}
<h2>Descartadas como logo/decoración</h2><ul>{desc}</ul>
</body></html>""", encoding="utf-8")


def _generar_pendientes_manual(figuras, figs_dir: Path, cache_dir: Path, doc_stem: str) -> list:
    """VISION_PROVIDER=manual: no llama a ninguna API. Escribe en
    descripciones/<doc_stem>_PENDIENTES/ todo lo que un agente de IA con visión necesita
    para describir las figuras SIN cache aún, con el prompt exacto y el formato de
    salida/caché que espera el resto del pipeline. Devuelve las figuras pendientes
    (vacío si ya estaban todas cacheadas -> el pipeline sigue normal)."""
    pendientes = [f for f in figuras if not (cache_dir / f"{f['img']}.md").exists()]
    if not pendientes:
        return pendientes

    pend_dir = OUT_DIR / f"{doc_stem}_PENDIENTES"
    pend_dir.mkdir(exist_ok=True)

    listado = "\n".join(
        f"- {(figs_dir / f['img']).resolve()}  (pie: {f['caption'] or '(sin pie)'})"
        for f in pendientes
    )
    (pend_dir / "imagenes_pendientes.txt").write_text(listado, encoding="utf-8")

    bloques_img = "\n".join(
        f"### `{f['img']}`\n"
        f"- Ruta de la imagen: `{(figs_dir / f['img']).resolve()}`\n"
        f"- Pie de figura: {f['caption'] or '(sin pie de figura)'}\n"
        f"- Texto de contexto: {f['contexto'] or '(sin contexto)'}\n"
        for f in pendientes
    )
    ejemplo = pendientes[0]["img"]
    instrucciones = f"""# Describir figuras pendientes — {doc_stem}

`VISION_PROVIDER=manual`: este script NO llamó a ninguna API de visión. Se necesita que
un agente de IA con visión (tú) describa cada imagen listada abajo y escriba el
resultado en el caché, con el formato EXACTO que espera el resto del pipeline
(`describir_figuras.py` / `ingesta.py`).

## Rol (sistema)
{SISTEMA}

## Prompt (aplícalo a CADA imagen, usando SU pie y SU contexto de abajo)
{PROMPT_BASE}

## Imágenes a describir ({len(pendientes)})
{bloques_img}
## Formato de salida (OBLIGATORIO)
Por cada imagen, crea un archivo de texto plano en:

    descripciones/_cache_{doc_stem}/<nombre_de_imagen>.md

El contenido es SOLO el texto de la descripción (sin JSON, sin metadatos), tal como lo
devolvería el modelo: 2-3 párrafos de explicación + una línea final
"Palabras clave: término1, término2, ...". Por ejemplo, para `{ejemplo}` el archivo
sería:

    descripciones/_cache_{doc_stem}/{ejemplo}.md

## Al terminar
Re-corre `python ingesta.py "{doc_stem}"` (o `python describir_figuras.py "{doc_stem}"`):
el script detecta los cachés nuevos y sigue el pipeline como si nada.
"""
    (pend_dir / "DESCRIBIR_FIGURAS.md").write_text(instrucciones, encoding="utf-8")
    return pendientes


def main(doc_stem: str):
    md_path = MD_DIR / f"{doc_stem}.md"
    figs_dir = MD_DIR / f"{doc_stem}_figs"
    if not md_path.exists():
        sys.exit(f"ERROR: no existe {md_path}")
    OUT_DIR.mkdir(exist_ok=True)

    # ── Punto de control humano: plan revisable ANTES de gastar Sonnet/embeddings ──
    plan_path = OUT_DIR / f"{doc_stem}_figuras_plan.json"
    html_path = OUT_DIR / f"{doc_stem}_figuras_revision.html"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        grupos, descartadas = parsear_grupos(md_path.read_text(encoding="utf-8"), figs_dir)
        for n, g in enumerate(grupos, 1):
            g["n"] = n
        plan = {"doc": doc_stem, "aprobado": not REVISION_FIGURAS,
                "grupos": grupos, "descartadas": descartadas}
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if not plan.get("aprobado"):
        _escribir_html_revision(plan, html_path, doc_stem)
        print("\n[PAUSA] REVISION PENDIENTE - no se ha gastado ninguna API todavia.")
        print("   1. Abre el EDITOR VISUAL (arrastrar/recortar/editar pies, sin tocar JSON):")
        print(f"        python revisar_figuras.py \"{doc_stem}\"")
        print("   2. Revisa los grupos, corrige lo que haga falta y aprieta [APROBAR PLAN].")
        print("   3. Re-ejecuta el mismo comando de ingesta: retoma desde aquí y recien")
        print("      entonces describe con Sonnet e indexa con embeddings.")
        print(f"   (Alternativa sin editor: revisar {html_path.name} y editar el plan JSON a mano.")
        print("    Para saltar esta pausa en un doc de confianza: REVISION_FIGURAS=off)")
        sys.exit(3)

    figuras = _materializar(plan["grupos"], figs_dir)
    cache_dir = OUT_DIR / f"_cache_{doc_stem}"
    cache_dir.mkdir(exist_ok=True)

    # ── Fallback controlado por VISION_PROVIDER (bedrock | gemini | manual) ──
    if DESCRIBE_PROVIDER == "manual":
        pendientes = _generar_pendientes_manual(figuras, figs_dir, cache_dir, doc_stem)
        if pendientes:
            print(f"\n[PAUSA] VISION_PROVIDER=manual: {len(pendientes)} figura(s) sin describir.")
            print(f"   Instrucciones autocontenidas para el agente:")
            print(f"     descripciones/{doc_stem}_PENDIENTES/DESCRIBIR_FIGURAS.md")
            print(f"   Cuando el agente escriba los .md en descripciones/_cache_{doc_stem}/, "
                  "re-ejecuta el mismo comando.")
            sys.exit(3)
    elif DESCRIBE_PROVIDER == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            sys.exit("ERROR: VISION_PROVIDER=gemini pero falta GEMINI_API_KEY en el .env.\n"
                      "  Alternativas: VISION_PROVIDER=bedrock (si tienes AWS_BEARER_TOKEN_BEDROCK) "
                      "o VISION_PROVIDER=manual (un agente de IA describe las figuras sin gastar API).")
    elif not _bedrock_configurado():  # bedrock (default) o un valor no reconocido
        sys.exit("ERROR: VISION_PROVIDER=bedrock pero falta AWS_BEARER_TOKEN_BEDROCK "
                  "(o credenciales AWS) en el .env.\n"
                  "  Alternativas: VISION_PROVIDER=gemini (si tienes GEMINI_API_KEY) "
                  "o VISION_PROVIDER=manual (un agente de IA describe las figuras sin gastar API).")

    modelo = {"gemini": GEMINI_MODEL, "manual": "manual (sin API)"}.get(DESCRIBE_PROVIDER, BEDROCK_MODEL)
    print(f"{len(figuras)} figuras (plan aprobado) en {md_path.name}  "
          f"(visión: {DESCRIBE_PROVIDER} / {modelo})")

    registros = []
    for n, fig in enumerate(figuras, 1):
        img_path = figs_dir / fig["img"]
        if not img_path.exists():
            print(f"[{n:>2}] {fig['img']}: NO existe la imagen, salto.")
            continue
        cache_file = cache_dir / f"{fig['img']}.md"
        if cache_file.exists():
            desc = cache_file.read_text(encoding="utf-8")
            print(f"[{n:>2}] {fig['img']}: cache.")
        else:
            print(f"[{n:>2}] {fig['img']}: describiendo ...", end=" ", flush=True)
            try:
                desc = describir_imagen(img_path, fig["caption"], fig["contexto"])
            except Exception as e:
                print(f"FALLO: {e}")
                break
            cache_file.write_text(desc, encoding="utf-8")
            print("ok")
        registros.append({**fig, "descripcion": desc})

    # Salida legible
    partes = [f"# Descripciones de figuras — {doc_stem}\n"]
    for r in registros:
        partes.append(f"\n## {r['img']}\n\n**Pie:** {r['caption']}\n\n{r['descripcion']}\n")
    (OUT_DIR / f"{doc_stem}.md").write_text("\n".join(partes), encoding="utf-8")
    # Salida para indexar
    with (OUT_DIR / f"{doc_stem}.jsonl").open("w", encoding="utf-8") as f:
        for r in registros:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nListo: {len(registros)} descripciones -> descripciones/{doc_stem}.md")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('Uso: python describir_figuras.py "<doc_stem sin .md>"')
    main(sys.argv[1])
