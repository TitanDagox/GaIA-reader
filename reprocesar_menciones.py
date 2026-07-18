#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Re-describe TODAS las figuras del corpus con la mejora de "menciones distantes"
(describir_figuras.py, 2026-07-17) y re-indexa cada paper.

Por cada paper:  borra descripciones/_cache_<stem>/  →  describir_figuras.py <stem>
                 (re-describe con Sonnet, ahora con las menciones)  →  indexar.py <stem>

- REANUDABLE: registro en descripciones/_reprocesado_menciones.txt; re-correr salta
  los papers ya hechos (no re-paga Sonnet).
- COMPLETITUD: tras describir, compara figuras del plan vs descritas; si un fallo
  transitorio dejó el paper a medias, reintenta (el cache hace barato el reintento).
  Si queda corto tras 3 intentos (p.ej. imagen faltante legítima), AVISA y sigue.
- SEGURO: aborta si el backend (8901) está arriba o si qdrant_data está bloqueado.
- Costo: Sonnet visión (créditos AWS/Bedrock). Embeddings de Gemini = calderilla.

Uso:
    python -u reprocesar_menciones.py                     # todos
    python -u reprocesar_menciones.py "stem1" "stem2"     # solo esos
    python -u reprocesar_menciones.py --excluir "Perello" # todos menos los que matcheen
"""

import os
import sys
import json
import socket
import shutil
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DESC = BASE / "descripciones"
REGISTRO = DESC / "_reprocesado_menciones.txt"
AVISOS = DESC / "_reproc_avisos.txt"
PY = sys.executable


def backend_arriba() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", 8901)) == 0


def qdrant_bloqueado() -> bool:
    lock = BASE / "qdrant_data" / ".lock"
    if not lock.exists():
        return False
    try:
        lock.rename(lock)
        return False
    except OSError:
        return True


def descubrir_stems() -> list[str]:
    stems = []
    for f in sorted(DESC.glob("*.jsonl")):
        if f.name.endswith("_tablas.jsonl"):
            continue
        stems.append(f.stem)
    return stems


def figuras_esperadas(stem: str):
    """Nº de figuras que el plan debería producir (grupos no omitidos). None si no hay plan."""
    p = DESC / f"{stem}_figuras_plan.json"
    if not p.exists():
        return None
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
        return sum(1 for g in plan.get("grupos", []) if not g.get("omitir"))
    except Exception:
        return None


def figuras_descritas(stem: str) -> int:
    p = DESC / f"{stem}.jsonl"
    if not p.exists():
        return 0
    return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())


def hechos() -> set[str]:
    return set(REGISTRO.read_text(encoding="utf-8").splitlines()) if REGISTRO.exists() else set()


def marcar(archivo: Path, stem: str):
    with archivo.open("a", encoding="utf-8") as f:
        f.write(stem + "\n")


def correr(script: str, stem: str) -> int:
    return subprocess.run([PY, str(BASE / script), stem], cwd=str(BASE)).returncode


def asegurar_aprobado(stem: str):
    """Fuerza aprobado:true en el plan de figuras (si existe). Estos papers ya fueron
    aprobados/indexados en su día; re-describirlos con la MISMA agrupación no debe re-pausar
    en el checkpoint. Los que no tienen plan lo regeneran auto-aprobado (REVISION_FIGURAS=off)."""
    p = DESC / f"{stem}_figuras_plan.json"
    if not p.exists():
        return
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    if not plan.get("aprobado"):
        plan["aprobado"] = True
        p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  plan auto-aprobado para el reproceso: {stem[:55]}", flush=True)


def main(argv):
    rehacer = "--rehacer" in argv          # rehacer los stems dados aunque estén en el registro
    argv = [a for a in argv if a != "--rehacer"]
    excluir = []
    if "--excluir" in argv:
        i = argv.index("--excluir")
        excluir = argv[i + 1:]
        argv = argv[:i]
    pedidos = [a for a in argv if not a.startswith("--")]

    if backend_arriba():
        sys.exit("ABORTA: el backend está en el puerto 8901. Párralo antes (bloquea qdrant_data).")
    if qdrant_bloqueado():
        sys.exit("ABORTA: qdrant_data está bloqueado por otro proceso (¿ingesta/indexar corriendo?).")

    # Reproceso masivo de papers YA aprobados → no re-pausar en el checkpoint humano.
    # (Planes regenerados —papers sin plan— nacen aprobados; los existentes se fuerzan abajo.)
    os.environ["REVISION_FIGURAS"] = "off"

    stems = pedidos or descubrir_stems()
    if excluir:
        stems = [s for s in stems if not any(x.lower() in s.lower() for x in excluir)]
    ya = set() if rehacer else hechos()   # --rehacer ignora el registro (redescribe los pedidos)
    pendientes = [s for s in stems if s not in ya]

    print(f"Papers en alcance: {len(stems)}  |  ya reprocesados: {len(stems)-len(pendientes)}  "
          f"|  pendientes: {len(pendientes)}", flush=True)
    if not pendientes:
        print("Nada que hacer. (Borra descripciones/_reprocesado_menciones.txt para forzar.)")
        return

    t0, con_aviso = time.time(), 0
    for n, stem in enumerate(pendientes, 1):
        print("\n" + "=" * 72, flush=True)
        print(f"[{n}/{len(pendientes)}]  {stem}", flush=True)
        print("=" * 72, flush=True)
        cache = DESC / f"_cache_{stem}"
        if cache.exists():
            shutil.rmtree(cache)
            print(f"  cache borrado: {cache.name}", flush=True)

        asegurar_aprobado(stem)
        esperadas = figuras_esperadas(stem)
        descritas = 0
        for intento in range(1, 4):
            rc = correr("describir_figuras.py", stem)
            if rc == 3:
                sys.exit(f"ABORTA en {stem}: describir_figuras devolvió 3 (¿plan sin aprobar?).")
            if rc != 0:
                sys.exit(f"ABORTA en {stem}: describir_figuras falló (rc={rc}).")
            descritas = figuras_descritas(stem)
            if esperadas is None or descritas >= esperadas:
                break
            print(f"  incompleto: {descritas}/{esperadas}; reintento {intento} (cache resume)...", flush=True)
        else:
            con_aviso += 1
            marcar(AVISOS, f"{stem}\t{descritas}/{esperadas} figuras")
            print(f"  AVISO: {stem} quedó {descritas}/{esperadas} tras 3 intentos "
                  "(¿imagen faltante?). Sigo, pero revísalo.", flush=True)

        rc = correr("indexar.py", stem)
        if rc != 0:
            sys.exit(f"ABORTA en {stem}: indexar falló (rc={rc}). El re-describir SÍ quedó (cache); "
                     "re-corre para retomar.")
        marcar(REGISTRO, stem)
        print(f"  OK ({descritas} figuras) — {n}/{len(pendientes)}  "
              f"[{time.time()-t0:.0f}s acumulados]", flush=True)

    print(f"\nLISTO: {len(pendientes)} papers reprocesados en {time.time()-t0:.0f}s "
          f"({con_aviso} con aviso). Reinicia el backend para usar la app.", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
