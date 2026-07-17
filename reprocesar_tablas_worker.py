#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Worker de reproceso de tablas en paralelo. Uso: python reprocesar_tablas_worker.py <k> <n>
Procesa los papers cuyo índice (en la lista ordenada de papers con tablas) cumple i % n == k.
Pensado para lanzar N copias en paralelo (concurrencia = N). Respeta el cache (reanudable)."""
import sys
from pathlib import Path

# La consola de Windows (cp1252) revienta al imprimir símbolos como ⚠/±/α. Forzar UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import describir_tablas

OUT = Path("./descripciones")


def main():
    k, n = int(sys.argv[1]), int(sys.argv[2])
    stems = sorted(p.name[: -len("_tablas.jsonl")] for p in OUT.glob("*_tablas.jsonl"))
    mine = [s for i, s in enumerate(stems) if i % n == k]
    print(f"[worker {k}/{n}] {len(mine)} papers: " + " | ".join(s[:30] for s in mine), flush=True)
    for s in mine:
        print(f"\n=== [worker {k}] {s} ===", flush=True)
        try:
            describir_tablas.main(s)
        except Exception as e:
            print(f"[worker {k}] ERROR en {s}: {e}", flush=True)
    print(f"[worker {k}] FIN", flush=True)


if __name__ == "__main__":
    main()
