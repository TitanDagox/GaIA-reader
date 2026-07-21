#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_corpus.py — Servidor MCP (stdio) que expone las 3 herramientas de Investigación del corpus
(buscar_en_corpus, ver_esquema, leer_seccion) para que el CLI de Claude Code —motor por
SUSCRIPCIÓN (provider `claude_cli`)— las use en su bucle agéntico (Fase A).

NO toca Qdrant directo (el backend en marcha lo bloquea): llama al endpoint `/agent_tool` del
backend, que ejecuta `_ejecutar_tool`. Así devuelve texto IDÉNTICO al que ven DeepSeek/Gemini en
su Fase A. Lo lanza Claude Code vía `--mcp-config`; recibe alcance/token/URL por variables de
entorno (CORPUS_SCOPE, CORPUS_TOKEN, CORPUS_BACKEND_URL) que inyecta backend._mcp_config_corpus.
"""
import os
import json

import requests
from mcp.server.fastmcp import FastMCP

BACKEND = os.environ.get("CORPUS_BACKEND_URL", "http://127.0.0.1:8901")
TOKEN = os.environ.get("CORPUS_TOKEN", "")
try:
    SCOPE = json.loads(os.environ.get("CORPUS_SCOPE", "[]"))
except Exception:
    SCOPE = []

mcp = FastMCP("corpus")


def _call(tool: str, args: dict) -> str:
    """Ejecuta una herramienta del corpus vía el backend. Nunca lanza: cualquier error vuelve como
    texto para que el modelo lo lea y reintente (no rompe la investigación)."""
    try:
        r = requests.post(f"{BACKEND}/agent_tool",
                          json={"tool": tool, "args": args, "scope": SCOPE},
                          headers={"Authorization": f"Bearer {TOKEN}"}, timeout=90)
        r.raise_for_status()
        return r.json().get("result", "")
    except Exception as e:
        return f"(error consultando el corpus: {e})"


@mcp.tool()
def buscar_en_corpus(consulta: str, documento: str = "") -> str:
    """Búsqueda semántica en la biblioteca del usuario. Devuelve los fragmentos (texto, figuras,
    tablas) más afines. Úsala para localizar dónde se trata un tema. Acota a un paper con
    'documento', o déjalo vacío para buscar en todos."""
    return _call("buscar_en_corpus", {"consulta": consulta, "documento": documento})


@mcp.tool()
def ver_esquema(documento: str) -> str:
    """Tabla de contenidos + resumen por sección de UN paper. Úsala para saber qué secciones tiene
    y elegir cuál leer entera, sin gastar de más."""
    return _call("ver_esquema", {"documento": documento})


@mcp.tool()
def leer_seccion(documento: str, titulo: str) -> str:
    """Devuelve el texto COMPLETO de una sección de un paper (para leer un método o resultado de
    corrido, no en fragmentos). Usa antes ver_esquema para el título exacto."""
    return _call("leer_seccion", {"documento": documento, "titulo": titulo})


if __name__ == "__main__":
    mcp.run()
