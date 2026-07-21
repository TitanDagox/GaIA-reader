# Changelog — GaIA

Cambios notables del proyecto, por fecha, en lenguaje conciso. **Sin números de versión** por ahora;
se etiquetará `v1.0` cuando el repositorio se haga público. Lo más reciente va arriba.

> Convención: cada cambio hecho se anota aquí en una línea. El *porqué* y el diseño vivo están en
> `CONTEXTO.md`; el detalle técnico y los bugs resueltos, en `NOTAS_TECNICAS.md`.

## 2026-07-21

### Motores de chat — MiniMax M3 (razonamiento) y Bedrock fuera del selector
- **MiniMax M3 añadido** como motor de chat vía Ollama Cloud (`minimax-m3:cloud`, configurable con
  `OLLAMA_REASON_MODEL`). Etiqueta *"MiniMax M3 · razonamiento"*. En una prueba de Investigación
  cross-paper (GSI/Hoek-Brown) razonó **más profundo que DeepSeek V4 Flash** (extrajo fórmulas de Cai
  2004 que Flash omitió; mejor manejo del caso de relleno arcilloso), a costa de ~3x más lento y
  verboso. Contras: puede filtrar algún token en chino → conviene reforzar "solo español" en `sys_chat`.
- **Investigación habilitada para Ollama** (`AGENT_ADAPTERS += "ollama"`, usa `_investigar_ollama`
  no-streaming): validado con 24 tool-calls limpios de MiniMax.
- **Bedrock (Claude Sonnet 4.6) fuera del selector de chat**: se reserva SOLO para describir figuras
  al ingerir (`describir_figuras.py`). Sus keys siguen en el panel de config.

### Orden de lectura — Esperanza (Perello 2004) reconstruido
- **Fix del desorden de columnas de marker** en `md/…Esperanza…(Perello et al. 2004).md`: marker
  entrelazó las dos columnas del PDF (páginas 9-16), dejando frases partidas, títulos sueltos y
  ruido de ejes de figura. Reconstruido con orden geométrico de PyMuPDF (por columnas) reusando
  títulos y refs de figura de marker; re-`secciones` + re-`indexar` (69 puntos). Backup del original
  en `…(Perello et al. 2004).md.marker_bak`. Detalle y método → `NOTAS_TECNICAS.md`.
- **Barrido del corpus**: el problema resultó ser un **outlier** de Esperanza; ningún otro paper lo
  sufre a nivel apreciable (verificado con detector objetivo PyMuPDF-vs-marker + ojo). El informe
  delegado a Antigravity (`Hallazgos.md`) resultó **no confiable** (evidencia alucinada) — no usar.

## 2026-07-20

### Investigación/Chat — fidelidad de fórmulas, anti-truncación y más profundidad
- **Anti-truncación en respuestas largas**: `CHAT_MAX_TOKENS=16384` en `.env` (era 8192; con
  `thinking` los tokens de razonamiento agotaban el cupo y cortaban el texto a mitad de tabla).
  No aplica al motor `claude_cli` (Claude Code gestiona su propio tope de salida).
- **Fidelidad de fórmulas** en `sys_chat()`: instrucción de copiar coeficientes TEXTUALMENTE (no
  "simetrizar" ni redondear; si no está en el CONTEXTO, decirlo). Nace del error `GSI=JCond89/2` vs
  el correcto `GSI=1.5·JCond89+RQD/2` (Hoek 2013). Solo en `sys_chat`, no en los prompts JSON.
- **Más profundidad panorámica**: `AGENT_MAX_ROUNDS` 5→10 — más vueltas de herramientas para
  preguntas que cruzan muchos papers (validado: 13 docs inspeccionados, 6 secciones leídas). El
  bucle corta apenas el modelo deja de pedir herramientas → sin costo en preguntas simples. Aplica a
  DeepSeek/Gemini/Ollama; el motor `claude_cli` NO lo usa (acota sus propias vueltas).

### Chat — motor por SUSCRIPCIÓN vía Claude Code CLI (Fase 1)
- **Nuevo proveedor `claude_cli`**: motor de chat que delega en el CLI de Claude Code headless
  (`claude -p … --system-prompt … --output-format json`, prompt del usuario por STDIN para no chocar
  con el tope de argumentos de Windows). Usa la **suscripción Pro/Max** del usuario, no una API key
  (el coste sale de la cuota del plan). Se auto-detecta el binario y aparece en el selector como
  "Claude Sonnet · tu suscripción"; desactivable con `CLAUDE_CLI_ENABLED=0`. Chat puro (herramientas
  de código desactivadas). **Streaming token-a-token** vía `stream-json` (base compartida
  `_claude_cli_run`: STDIN por hilo alimentador + guardián de timeout).

### Investigación por SUSCRIPCIÓN vía MCP (Fase 2)
- **`claude_cli` ahora hace Investigación agéntica** (fiel a la semántica, no léxica): nuevo servidor
  MCP `mcp_corpus.py` expone tus 3 herramientas (buscar_en_corpus/ver_esquema/leer_seccion), que
  llaman al nuevo endpoint `/agent_tool` (envuelve `_ejecutar_tool` → texto idéntico al de
  DeepSeek/Gemini). El adaptador `_investigar_claude_cli` corre `claude -p --mcp-config …`, parsea
  los eventos stream-json (tool_use → trazas en vivo; tool_result → evidencia) y devuelve
  `(evidencia, trazas)`, mismo contrato que los otros adaptadores → se enchufa en `AGENT_ADAPTERS` y
  el botón se auto-activa (`agentic_capaz`). Dep nueva: `mcp`. Pendiente: Codex.

### Quiz — figura rota en preguntas
- **Fix imagen rota en el quiz**: el LLM ponía en `figura` una referencia ("Fig. 3", "3", el nombre
  sin extensión) en vez del archivo exacto → `/figura` 404 → `<img>` roto. `generar_quiz` ahora
  normaliza el campo contra los archivos reales (`_mapa_figuras`/`_resolver_figura`): recupera el
  archivo por nº de figura si puede, o lo deja en `null`. Red de seguridad en el front: `onerror`
  oculta el recuadro.

## 2026-07-18

### App — Investigación busca en todo el cuaderno + swap de botón enviar/stop
- **Investigación profunda ya cruza papers**: con un doc abierto en el lector, el botón Investigación
  amplía el alcance al cuaderno completo (o al corpus si el selector está en "Todos"), ignorando el
  candado de un solo doc; la Consulta normal sigue enfocada en el paper abierto. Antes, tener un paper
  abierto encerraba también a Investigación en ese único paper (por eso no encontraba papers hermanos
  como "Quantification of the GSI Chart (Hoek 2013)" estando ya indexado). Solo frontend (`app.html`).
- **Botón de envío/stop no se muestran a la vez**: mientras el modelo piensa/redacta, el botón enviar
  se oculta y en su lugar aparece el de detener (antes salía enviar en gris + stop juntos).
- **Investigación ancla en el paper abierto (`doc_foco`)**: el frontend manda el paper que el lector
  tiene abierto como pista (no restringe el alcance); el prompt del agente lo usa como sujeto por
  defecto de "este paper"/"el paper". Así "compárame este paper con Hoek 2013" se ciñe a esos dos y no
  arrastra papers hermanos (p.ej. Li 2026), mientras que una pregunta abierta ("qué otros métodos de
  GSI existen") sí explora todo el cuaderno. La amplitud la decide el modelo según la pregunta, sin
  heurísticas de palabras clave. Toca `app.html` y `backend.py` (`AskReq.doc_foco` + Fase A).

### App — fixes de UX en el chat: desborde y saltos de línea
- **Fórmulas y bloques `<pre>` ya no se salen del recuadro**: `.cuerpo pre` y `.cuerpo .katex-display`
  hacen scroll horizontal interno (`overflow-x:auto`, `max-width:100%`); `.cuerpo` con
  `overflow-wrap:break-word` corta palabras kilométricas. Antes la regla de KaTeX estaba scopeada solo
  al lector (`.md-reader`) y `<pre>` no tenía estilo, así que rompían el ancho del hilo. Solo CSS.
- **El prompt respeta los saltos de línea (shift+Enter)**: `.pregunta` con `white-space:pre-wrap` — antes
  se mostraba todo junto aunque el textarea sí insertaba los saltos.

### App — lector central del `.md` y portada del cuaderno
- **El panel central pasa de "visor de PDF" a LECTOR del `.md`**: renderiza el markdown de marker
  (fórmulas en KaTeX, figuras vía `/figura`, texto seleccionable) con el mismo motor `render()` del
  chat. Nuevo endpoint `GET /md?doc=`. El PDF original queda a un clic (botón `▦ PDF ⇄ ▤ Texto`);
  si un paper no tiene `.md`, cae al PDF (nunca peor que antes).
- **Clic en una cita `[n]` → salta a la fuente en el lector**: si es texto, carga el `.md` de ESE
  paper (aunque sea otro del cuaderno) y hace scroll + resalta el párrafo citado; si es figura/tabla,
  salta a la imagen y la destella. Convierte las citas en navegación verificable (RAG con evidencia
  a la vista). El scroll usa `setTimeout` (no `requestAnimationFrame`, que se congela en segundo plano).
- **Portada del cuaderno** (estado vacío del modo cuaderno, antes un placeholder muerto): grilla de
  tarjetas, una por paper (autor-año + título + conteos de figuras/tablas/secciones). Clic en una =
  la selecciona (carga su `.md` + índice). `/documentos` ahora devuelve los conteos por tipo.
- **Citas inline estilo Wikipedia `[1,2]`**: se corrigió el bug de que en una cita multi-fuente
  ("Fuentes 1, 3 y 8") solo se enlazaba el PRIMER número; ahora cada número es su propio enlace
  (superíndice, hover = preview), y se absorben los paréntesis que envolvían la cita.
- **Marcadores `[[FIG:…]]` mal formados ya no se filtran**: si el modelo escribe texto en vez del N de
  Fuente (`[[FIG:<doc> — Figure 5]]`), se resuelve la figura por documento+número si está entre las
  fuentes, y si no, se borra el marcador (nunca queda texto crudo). Detalle en `NOTAS_TECNICAS.md`.
- **Fix: clic en cita a veces no resaltaba y saltaba al inicio del `.md`**: `resaltarPasaje()` comparaba
  los primeros 45 chars del chunk contra el DOM, pero el texto guardado conserva marcado crudo de marker
  (anclas `<span id="page-N">`, `<sup>`, imágenes, links `[t](u)`, math `$…$`) que no sobrevive al render.
  Ahora limpia ese marcado y prueba ventanas de ~8 palabras desde varios offsets. Medido en Cai 2004:
  9/34 chunks fallaban → 0/34. Detalle en `NOTAS_TECNICAS.md`.

## 2026-07-17

### App — UI
- **Selector de documento: nombres largos ya no desbordan el popup**: el desplegable nativo se
  estiraba hasta la opción más larga (llegaba al borde de la pantalla; no acotable por CSS). Ahora
  `etiquetaDoc()` acorta el texto de cada `<option>` a ~62 chars con elipsis a la mitad conservando el
  `(Autor Año)` final; el nombre completo queda en `value` y en `title` (tooltip). El `<select>` sigue
  siendo nativo (teclado/accesibilidad intactos).

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
