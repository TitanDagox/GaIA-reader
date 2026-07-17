#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paso 3a — Chunking por sección, alineado a párrafos (NUNCA corta a mitad).

Reglas (ver diseño en CONTEXTO):
  1. Frontera dura en cada encabezado (#/##/###) → una sección.
  2. Si la sección cabe en MAX_CHARS → un solo chunk (sección completa). Ideal.
  3. Si no cabe → se empacan PÁRRAFOS ENTEROS hasta MAX_CHARS; el siguiente
     párrafo abre chunk nuevo. Nunca se parte un párrafo.
  4. Solape de OVERLAP_PARRAFOS párrafo(s) entre chunks de la misma sección.
  5. Se antepone el breadcrumb (título de sección) a cada chunk → mejora la
     recuperación y sirve para citar.

Las líneas de imagen `![](...)` y sus pies `FIG. N...` se EXCLUYEN del texto
(las figuras se indexan aparte, como puntos type="figure" desde descripciones/).
"""

import re
from dataclasses import dataclass, field

MAX_CHARS = 2000
MIN_CHARS = 30                 # descarta migajas (ej. direcciones, líneas sueltas)
OVERLAP_MAX_CHARS = 400        # solo se solapa un "trozo" si es corto (evita duplicar párrafos enormes)

# Secciones a excluir del índice (bajo valor para Q&A). Multilingüe. Configurable.
SECCIONES_EXCLUIDAS = (
    "references", "reference", "bibliography", "acknowledgments", "acknowledgements",
    "referencias", "bibliografía", "bibliografia", "agradecimientos", "copyright",
    # Front-matter de revistas (Frontiers, Elsevier, etc.): metadatos, no contenido.
    "edited by", "reviewed by", "correspondence", "specialty section", "citation",
    "received", "accepted", "published", "author contributions",
)


def es_excluida(titulo: str) -> bool:
    """True si el título es una sección de bajo valor (referencias, front-matter, etc.),
    tolerando numeración, links y puntuación al inicio ('8. REFERENCES', '\\Correspondence:')."""
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", titulo)          # markdown links → texto
    t = re.sub(r"^[\s\d.)\-–—\\*_#'\"]+", "", t.lower()).strip()
    return any(t.startswith(w) for w in SECCIONES_EXCLUIDAS)

_H = re.compile(r"^(#{1,6})\s+(.*)$")
_IMG = re.compile(r"^\s*!\[\]\([^)]*\)\s*$")
# Pies de figura/tabla en ES e EN: FIG./Figura/Fig., TABLE/TABLA/Tabla/Cuadro/Ilustración.
_CAP = re.compile(
    r"^\s*(fig(\.|ura|s)?|table|tabla|cuadro|ilustraci[oó]n|foto(graf[ií]a)?)\s*\.?\s*\d",
    re.IGNORECASE,
)
_SENT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Chunk:
    content: str          # texto con breadcrumb antepuesto (lo que se embebe)
    texto: str            # solo el texto, sin breadcrumb
    breadcrumb: str
    source: str
    indice: int
    type: str = "text"
    metadata: dict = field(default_factory=dict)


def _limpiar_titulo(t: str) -> str:
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)   # markdown links [texto](url) → texto
    return re.sub(r"[*_`]", "", t).lstrip("\\").strip()


def _secciones(md: str):
    """Divide el md en (titulo, nivel, cuerpo) por encabezados. El texto antes
    del primer encabezado va como sección '(inicio)'."""
    # Preprocesar Abstract (solo en los primeros 10000 caracteres)
    if not re.search(r"^#+\s+Abstract\b", md[:10000], re.MULTILINE | re.IGNORECASE):
        # Reemplazar la primera ocurrencia de "Abstract " al inicio de una línea
        part_head = md[:10000]
        part_tail = md[10000:]
        new_head, count = re.subn(r"^(\s*Abstract\b)", r"\n# Abstract\n\n\1", part_head, count=1, flags=re.MULTILINE | re.IGNORECASE)
        if count > 0:
            md = new_head + part_tail

    lineas = md.split("\n")
    secs, titulo, nivel, buf = [], "(inicio)", 0, []
    for ln in lineas:
        m = _H.match(ln)
        if m:
            if buf:
                secs.append((titulo, nivel, "\n".join(buf)))
            titulo, nivel, buf = _limpiar_titulo(m.group(2)), len(m.group(1)), []
        else:
            buf.append(ln)
    if buf:
        secs.append((titulo, nivel, "\n".join(buf)))
    return secs


def es_tabla_valida(tabla: str) -> bool:
    lineas = [l.strip() for l in tabla.split("\n") if l.strip()]
    if len(lineas) < 3:
        return False
    # La segunda línea suele ser el separador: |---|
    # Verifiquemos si las líneas a partir de la tercera tienen contenido real (letras/números)
    tiene_datos = False
    for l in lineas[2:]:
        limpio = re.sub(r"[|:\s\-–—]+", "", l)
        if limpio:
            tiene_datos = True
            break
    return tiene_datos


def _bloques(cuerpo: str):
    """Divide el cuerpo en bloques tipados: ('table', md) o ('text', párrafo).
    Una tabla = bloque con >=2 líneas que contienen '|' (se preserva su estructura,
    NO se une con espacios). El resto son párrafos de prosa (líneas unidas)."""
    out = []
    for bloque in re.split(r"\n\s*\n", cuerpo):
        lineas = bloque.split("\n")
        pipe = [l for l in lineas if l.count("|") >= 2]
        if len(pipe) >= 2:
            tabla = "\n".join(l for l in lineas if l.strip())
            if es_tabla_valida(tabla):
                out.append(("table", tabla))
            else:
                # Si no es válida como tabla, la tratamos como texto plano (prosa)
                limpio = [l for l in lineas if not _IMG.match(l) and not _CAP.match(l)]
                p = " ".join(re.sub(r"[|\s]+", " ", l).strip() for l in limpio if l.strip())
                p = re.sub(r"\s+", " ", p).strip()
                if p:
                    out.append(("text", p))
        else:
            limpio = [l for l in lineas if not _IMG.match(l) and not _CAP.match(l)]
            p = " ".join(l.strip() for l in limpio if l.strip()).strip()
            if p:
                out.append(("text", p))
    return out


def _trozos(parrafos: list[str]) -> list[str]:
    """Convierte párrafos en 'trozos', cada uno <= MAX_CHARS. Un párrafo que no
    excede el tope queda intacto; uno que lo excede se parte POR ORACIÓN (último
    recurso, nunca a mitad de oración)."""
    trozos = []
    for p in parrafos:
        if len(p) <= MAX_CHARS:
            trozos.append(p)
            continue
        cur, largo = [], 0
        for s in _SENT.split(p):
            if cur and largo + len(s) + 1 > MAX_CHARS:
                trozos.append(" ".join(cur))
                cur, largo = [], 0
            cur.append(s)
            largo += len(s) + 1
        if cur:
            trozos.append(" ".join(cur))
    return trozos


def _embed_head(source: str, ruta: str) -> str:
    """Encabezado que se ANTEPONE al texto ANTES de vectorizar (no se muestra al
    usuario). Ancla cada chunk a SU documento y a su ruta de secciones → desambigua
    chunks casi-idénticos entre papers parecidos (p.ej. Hoek-Brown 2002 vs 2018) y
    evita que el fragmento pierda su contexto global al ser embebido."""
    if ruta:
        return f"[Documento: {source} · Sección: {ruta}]"
    return f"[Documento: {source}]"


def _add(chunks, idx, ruta, titulo, texto, source, tipo):
    content = f"{_embed_head(source, ruta)}\n{texto}"
    chunks.append(Chunk(content=content, texto=texto, breadcrumb=titulo,
                        source=source, indice=idx, type=tipo,
                        metadata={"source": source, "breadcrumb": titulo, "type": tipo}))
    return idx + 1


def chunkear(md: str, source: str) -> list[Chunk]:
    chunks, idx = [], 0
    for titulo, _nivel, cuerpo in _secciones(md):
        # La 'ruta' que se antepone al embedding es el título de la sección hoja.
        # NO se reconstruye jerarquía profunda a propósito: los niveles de título de
        # marker no son fiables (ver NOTAS_TECNICAS.md §5 #3), así que un path profundo sería
        # frágil. El ancla (Documento) ya es lo que más desambigua entre papers.
        ruta = "" if titulo == "(inicio)" else titulo
        if es_excluida(titulo):
            continue
        bloques = _bloques(cuerpo)
        # las tablas van como chunk propio (type="table"); la prosa se empaqueta
        prosa = [t for tipo, t in bloques if tipo == "text"]
        tablas = [t for tipo, t in bloques if tipo == "table"]

        trozos = _trozos(prosa)
        grupos, actual, largo = [], [], 0
        for t in trozos:
            if actual and largo + len(t) + 2 > MAX_CHARS:
                grupos.append(actual)
                ultimo = actual[-1]
                actual = [ultimo] if len(ultimo) <= OVERLAP_MAX_CHARS else []
                largo = sum(len(x) + 2 for x in actual)
            actual.append(t)
            largo += len(t) + 2
        if actual:
            grupos.append(actual)

        for g in grupos:
            texto = "\n\n".join(g)
            if len(texto) >= MIN_CHARS:
                idx = _add(chunks, idx, ruta, titulo, texto, source, "text")

        for tabla in tablas:
            if len(tabla) >= MIN_CHARS:
                idx = _add(chunks, idx, ruta, titulo, tabla, source, "table")
    return chunks


if __name__ == "__main__":
    import sys, os
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv()
    MD_DIR = Path(os.environ.get("MD_DIR", "./md"))
    if len(sys.argv) != 2:
        sys.exit('Uso: python chunk.py "<doc_stem sin .md>"')
    stem = sys.argv[1]
    md = (MD_DIR / f"{stem}.md").read_text(encoding="utf-8")
    chunks = chunkear(md, source=stem.replace("_noocr", ""))
    # Reporte de validación
    texto_ch = [c for c in chunks if c.type == "text"]
    tabla_ch = [c for c in chunks if c.type == "table"]
    largos = [len(c.texto) for c in texto_ch]
    print(f"Total chunks: {len(chunks)}  (texto: {len(texto_ch)}, tabla: {len(tabla_ch)})")
    print(f"Chunks de TEXTO (chars): min={min(largos)}  máx={max(largos)}  prom={sum(largos)//len(largos)}")
    print(f"Chunks de texto > MAX_CHARS ({MAX_CHARS}): {sum(1 for l in largos if l > MAX_CHARS)}")
    print("\n=== primeros 3 chunks (inicio y fin, para ver que no cortan párrafos) ===")
    for c in chunks[:3]:
        print(f"\n--- #{c.indice} [{c.breadcrumb}] ({len(c.texto)} chars) ---")
        print("INICIO:", c.texto[:150].replace("\n", " "))
        print("FIN   :", c.texto[-150:].replace("\n", " "))
