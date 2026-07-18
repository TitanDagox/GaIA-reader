# Changelog — GaIA

Cambios notables del proyecto, por fecha, en lenguaje conciso. **Sin números de versión** por ahora;
se etiquetará `v1.0` cuando el repositorio se haga público. Lo más reciente va arriba.

> Convención: cada cambio hecho se anota aquí en una línea. El *porqué* y el diseño vivo están en
> `CONTEXTO.md`; el detalle técnico y los bugs resueltos, en `NOTAS_TECNICAS.md`.

## 2026-07-17

### Chat — investigación agéntica (tool-use)
- **Modo "Investigación profunda" (toggle 🔬 en la UI, off por defecto)**: el modelo indaga la
  biblioteca con herramientas ANTES de responder, para comparaciones precisas entre papers y
  preguntas multi-salto. Diseño en 2 fases: FASE A = bucle de tool-use SIN streaming (esquiva los
  bugs de streaming+tool_calls de Gemini/Gemma); FASE B = respuesta final por los `_chat_*` de
  siempre, con la evidencia recolectada como contexto. 3 herramientas: `buscar_en_corpus`,
  `ver_esquema`, `leer_seccion` (reutilizan `buscar`/`outlines`/`_texto_seccion`). Fallback al RAG
  de un tiro si la Fase A falla → nunca peor que antes. `agentic_capaz` por motor derivado de
  `AGENT_ADAPTERS`. El evento `done` trae `trazas` (herramientas que llamó el modelo).
- **Evaluación de calidad y gating (cuaderno GSI, misma pregunta a los inciertos)**: DeepSeek V4 Pro
  (22 tool-calls) y **V4 Flash (19 calls, 64 s, la respuesta más completa/precisa —leyó bien hasta los
  coeficientes AHP de Li 2026)**, y Gemini 3.5 Flash (5 calls, completa y correcta en lo grande, pero
  menos fiable en cifras finas) rinden bien → **botón habilitado**. Gemma 4 hizo UNA sola llamada (degeneró a RAG de un tiro), fue el
  más lento (116 s) y mezcló métodos → **botón desactivado** para Gemma (su adaptador queda listo por si
  mejora). Sonnet/Bedrock pendiente (el confiable; no se evaluó). Adaptadores: DeepSeek (OpenAI),
  Gemini (`generateContent`, `functionResponse` role=function), Gemma (Ollama `/api/chat` nativo).
- **Progreso en vivo de la Fase A**: la investigación ya no es a ciegas. La Fase A corre DENTRO del
  stream y emite eventos `{"status":…}` legibles ("🔎 Buscando…", "🗂 Revisando el índice de…",
  "📖 Leyendo «sección» de (Autor año)", "✍️ Redactando…") + un aviso inicial (más enfático en Gemini,
  que tarda más). La UI los pinta en un panel colapsable "Investigando…" (reutiliza el estilo del panel
  de pensamiento) que al terminar se reetiqueta a "Investigación" y se colapsa. Los adaptadores pasaron
  a ser generadores (`yield` del paso, `return` de la evidencia).

### Ingesta — figuras
- **Re-revisión humana de 14 papers y reproceso con `--rehacer`**: los 14 que se habían auto-aprobado
  (sin checkpoint) se revisaron a mano en el editor de figuras (correcciones de agrupación, imágenes
  añadidas, pies corregidos) y se re-describieron con `reprocesar_menciones.py --rehacer` (nuevo flag:
  redescribe los stems dados aunque estén en el registro). 14/14 sin avisos; Qdrant re-indexada.
- **`_limpiar_caption` también quita enlaces markdown del pie**: marker a veces deja `[texto](#ancla)`
  y brackets escapados (`\[6\]`) en el pie; ahora se colapsan a `texto`/`[6]` antes de mandarlo a Sonnet.
- **Pies con símbolos matemáticos OCR-eados por marker**: se corrigen a mano en el editor (p.ej.
  Hoek-Brown 2018 Fig. 5, `σci/|σt|` que marker leyó `sci/jstj`). Detalle en `NOTAS_TECNICAS.md`.
- **Reproceso completo del corpus con las menciones**: re-descritas las ~528 figuras de los 42 papers
  (Perello ya venía con menciones desde su ingesta) vía `reprocesar_menciones.py` (reanudable, con
  chequeo de completitud y auto-aprobación del checkpoint para papers ya aprobados). 41/41 sin avisos;
  ~$8 de crédito Bedrock. 14 papers sin checkpoint humano previo quedaron auto-aprobados (para ojear su
  `_figuras_revision.html`). Colección Qdrant re-indexada (2476 puntos).
- **Menciones distantes en la descripción de figuras**: `describir_figuras.py` ahora recolecta las
  oraciones de TODO el paper que citan cada figura por número ("…como muestra la Fig. 6…", a menudo
  lejos del pie, en Discusión/Resultados donde el autor dice qué concluir) y las pasa a Sonnet como
  bloque `=== MENCIONES EN EL TEXTO ===`, junto al pie y el contexto local. Regla simple (regex +
  segmentación por oración con el punto de "Fig." protegido), sin nueva capa de IA. No confunde
  "Fig. 6" con "Fig. 60". Para que una figura ya cacheada se beneficie hay que borrar su `.md` en
  `descripciones/_cache_<doc>/` y re-describir.

## 2026-07-11

### Documentación
- **Runbook para el asistente de IA en el README**: guion por fases (reconocer si hay corpus, instalar,
  pedir claves, **entrevistar al usuario** para el perfil —con 2-3 instrucciones personalizadas
  sugeridas—, cargar/ingerir corpus y verificar) para que cualquiera clone el repo y lo levante con su
  agente (Claude Code / Antigravity / Codex) sin depender del dueño del repo. Agnóstico de profesión.

### Añadido
- **Selector de modo de visión en Configuración**: la pestaña de Configuración ahora deja elegir
  cómo se describen figuras/tablas al ingerir (Bedrock / Gemini / **Sin API · Agente IA**) y guarda
  `VISION_PROVIDER` en el `.env` por ti, sin editarlo a mano. El modo "Sin API" (`manual`) deja las
  imágenes pendientes para que Claude Code / Antigravity las describan gratis. Valores desconocidos
  en el `.env` se muestran como Bedrock (que es como los trata el pipeline).

### Cambiado
- **Campo "Situación" del perfil eliminado**: solo añadía un paréntesis tras el rol en el prompt del
  chat (`un geólogo (estudiante)`), no se usaba en Quiz ni Anki y se pisaba con Rol y Nivel. Fuera de
  `PERFIL_CAMPOS`/`PERFIL_DEFAULT`, del modal de Configuración, del runbook del README y de la entrevista.
  Los `perfil.json` antiguos con `situacion` siguen cargando (se ignora).
- **Prompts de ingesta agnósticos de disciplina**: `describir_figuras.py`, `describir_tablas.py` y
  `secciones.py` ya no llevan "geología" hardcodeada; leen la `disciplina` de `perfil.json` (mismo
  perfil que el chat) con default neutral `temas técnicos`. Recién clonado, el repo no arrastra el
  sesgo de dominio del mantenedor.
- **Datos personales fuera del repo**: se quitó el nombre del mantenedor de comentarios de código
  (`backend.py`, `describir_figuras.py`, `revisar_figuras.py`) y del nombre del empleador en los
  comentarios de `.gitignore`. La atribución de copyright en `LICENSE` se mantiene.

## 2026-07-10

### Añadido
- **Instrucciones al tutor**: campo de texto libre en el perfil de estudio (modal Configuración)
  que el lector rellena a su gusto (estilo *custom instructions*); se anexa solo al system prompt
  del chat, tope 2000 caracteres. No aplica al Quiz ni a las tarjetas Anki (generan JSON y texto
  libre los rompería).

### Arreglado
- **Nombre del capítulo 32 en Clark 1990**: corrección del error ortográfico de "Montmo rillo nite s" a "Montmorillonites" en los archivos del outline, texto principal e indexación en Qdrant.
- **Guard modelo↔proveedor** (`_resuelve_motor()` en `backend.py`, usado en `/ask`, `/anki`,
  `/quiz/generar` y `/quiz/calificar`): un `model` que solo existe en OTRO proveedor ya no se envía
  tal cual (evita 404 tipo "deepseek-v4-flash contra el endpoint de Gemini"), cae a default. El
  frontend ya no guarda turnos con error en la libreta ni los reinyecta como historial, y ofrece
  un botón ↻ Reintentar en la ficha.

## 2026-07-09

### Añadido
- **Perfil de estudio configurable** (nombre, rol, situación, disciplina, nivel, objetivo) que se
  inyecta en el tutor, el Quiz y las tarjetas. Editable desde el panel de Configuración o vía
  `perfil.json`. El README explica cómo dejar que un agente de IA te entreviste para rellenarlo.
- **Panel de Configuración** en la app (engranaje ⚙): pegar/probar claves API y editar el perfil sin
  tocar archivos. Wizard de primera ejecución cuando falta la clave de Gemini.
- **Fallbacks de visión** para describir figuras/tablas al ingerir: `VISION_PROVIDER=bedrock` (default,
  mejor calidad), `gemini` (free tier) o `manual` (lo hace tu agente de IA).
- **Embeddings configurables** (`EMBED_PROVIDER`) con `embed_manifest.json`: detecta y aborta si el
  modelo con que consultas no coincide con el que indexó el corpus (evita resultados basura silenciosos).
- **Onboarding con corpus vacío** y **seguridad mínima**: CORS restringido a loopback, `RAG_TOKEN`
  opcional generable desde el wizard, aviso al arrancar con host expuesto.
- **Medidor de recuperación RAG** (`eval/`): set de 25 preguntas reales + script que mide el recall
  por tipo (texto/figura/tabla) antes y después de cambiar la recuperación.
- **Empaquetado para distribuir:** `README.md` público (pensado para que lo siga un agente de IA),
  `LICENSE` (MIT), `run.bat` de doble clic, `requirements.txt` y un smoke test (`scripts/smoke_test.py`).

### Cambiado
- **El perfil por defecto ahora es NEUTRAL** (tutor genérico de "temas técnicos"). Antes venía
  preconfigurado para geología, y ese sesgo se filtraba a cualquiera que clonara el repo.
- **Panel de Configuración reordenado:** el perfil de estudio va primero y las claves API debajo.

### Descartado (probado y no incorporado)
- **Re-ranking con LLM** en la recuperación: se implementó y midió contra el set de evaluación; **no
  mejoró** (con documento seleccionado el recall ya es casi perfecto) y añadía latencia. Se revirtió;
  se conservó el medidor. Detalle en `eval/README.md`.

### Arreglado
- `.gitignore`: los comentarios al final de línea anulaban los patrones (Git solo trata `#` como
  comentario a inicio de línea). Datos sensibles habrían quedado sin ignorar.
