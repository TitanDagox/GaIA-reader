# NOTAS_TECNICAS.md — Papers_Asistente

Detalle técnico del pipeline, **problemas conocidos y cómo resolverlos**, features de la app y
convenciones. Arquitectura y diseño → `CONTEXTO.md`. Instalación y uso → `README.md`.

> Si algo del pipeline falla, el mapa de problemas está en §5 (cada uno con su causa y su solución
> o alternativa).

---

## 1. Pipeline de ingesta (qué produce cada paso y dónde)

| Paso | Script | Entrada → Salida |
|---|---|---|
| Router + Extraer | `extraer.py` (vía `ingesta.py`) | `raw/<doc>.pdf` → `md/<doc>.md` + `md/<doc>_figs/*.jpeg` |
| **Plan de figuras** ⏸ | `describir_figuras.py` (1ª pasada) | md → `descripciones/<doc>_figuras_plan.json` y PAUSA (exit 3) |
| **Revisión humana** | `revisar_figuras.py` (puerto 8902) | el usuario revisa/corrige/aprueba el plan (visual, sin Qdrant) |
| Describir figuras | `describir_figuras.py` (2ª pasada) | plan aprobado → `descripciones/<doc>.jsonl` (Bedrock/Sonnet, type-aware; ver nota de MENCIONES abajo) |
| Explicar tablas (**por VISIÓN**) | `describir_tablas.py` | localiza la tabla en el PDF, **renderiza la página** (la endereza si es apaisada) → Sonnet/Gemma **reconstruye el Markdown fiel** → `descripciones/<doc>_tablas.jsonl` (+ `_tablas_revisar.txt` con las dudosas, revisión OPCIONAL) |
| Esquema secciones | `secciones.py` | md → `outlines/<doc>.md` + `.jsonl` (jerarquía + resúmenes) |
| Chunking | `chunk.py` | md → chunks en memoria; **antepone `[Documento: <source> · Sección: <título>]` al texto ANTES de vectorizar** (ancla de recuperación, ver §4) |
| Indexar | `indexar.py` | texto+figuras+tablas → Qdrant local `qdrant_data/`, colección `papers` |

**⏸ Punto de control humano (desde 2026-07-05):** tras extraer, la ingesta genera el PLAN de figuras
(`descripciones/<doc>_figuras_plan.json`) y SE DETIENE **antes de gastar Sonnet/embeddings** (exit
code 3, NO es un error). El usuario lo revisa con el editor visual:
```bash
python revisar_figuras.py "<doc_stem>"   # puerto 8902, NO usa Qdrant (corre en paralelo al backend)
```
Ahí puede **arrastrar imágenes entre grupos** (unir/separar mosaicos), **editar los pies** como texto,
**descartar** logos (🗑), **recortar** con el mouse (✂, con respaldo `.bak` y restaurar), **subir
capturas propias** (⇪ reemplazar una imagen mal extraída o ⇪ subir una nueva al grupo — p.ej. desde
Recortes de Windows), y **✔ APROBAR**. Recortar/reemplazar invalida el cache de descripción de ESA
imagen (y la firma de los mosaicos incluye tamaños → se regeneran solos). Luego **re-ejecuta el mismo
comando de ingesta** para continuar. `REVISION_FIGURAS=off` salta la pausa. Para regenerar un plan
desde cero: borrar el `_figuras_plan.json` y re-correr la ingesta.

**Menciones distantes en figuras (desde 2026-07-17):** al describir cada figura, `describir_figuras.py`
además del pie y el contexto local (~900 chars contiguos) recoge las **oraciones de TODO el paper que
citan esa figura por número** ("…como muestra la Fig. 6…", a menudo lejos, en Discusión/Resultados donde
el autor dice qué CONCLUIR) y se las pasa a Sonnet como bloque `=== MENCIONES EN EL TEXTO ===`
(`_num_de_caption` + `_menciones_en_texto`, con el punto de "Fig." protegido para no partir la oración; no
confunde "Fig. 6" con "Fig. 60"). Es automático para toda figura que se describa de aquí en adelante.
**OJO caché:** las figuras ya descritas ANTES de este cambio conservan su descripción vieja (el cache va
por nombre de imagen); para que una se beneficie hay que **borrar su `.md` en `descripciones/_cache_<doc>/`
y re-correr la ingesta** (gasta Sonnet). Las figuras de papers NUEVOS ya salen mejoradas sin hacer nada.

Convención de nombres: **stem limpio** = nombre del PDF sin extensión (sin sufijos como `_noocr`).

Carpetas auxiliares (2026-07-07): `_descargas/` = staging de PDFs bajados por agentes ANTES de
validarlos (que sean `%PDF` reales) y moverlos a `raw/`; `_archivo/` = material obsoleto guardado por
si acaso (NO se usa en el pipeline).

---

## 2. Features de la app (interfaz web "GaIA")

Registro de secciones (clic = resumen instantáneo, sin LLM) + chat con citas y figuras inline. El
diseño ("GaIA"/"TESTIGO", drill-log, paleta atacamita/calcopirita) sigue la skill `frontend-design`
instalada en `.agents/skills/`. `app.html` se relee en cada `GET /app` → los cambios de frontend NO
requieren reiniciar el backend; los de `backend.py` SÍ.

**Panel central = LECTOR del `.md`** (desde 2026-07-18, antes solo visor de PDF). Renderiza el markdown
de marker con el motor `render()` (KaTeX + figuras vía `/figura`, reescribiendo las refs de imagen).
Endpoint `GET /md?doc=`. Botón `▦ PDF ⇄ ▤ Texto` para el original; sin `.md` cae al PDF. **Clic en una
cita inline `[n]`** (`cargarLector(f.source, {resaltar|img})`) carga el `.md` de esa fuente y hace
scroll+resalta el pasaje (match normalizado sobre bloques `p/li/td/h*`) o la figura. El scroll va en
`setTimeout` (rAF se congela con la pestaña en segundo plano).

> **Bug resuelto (2026-07-18): la cita a veces no resaltaba y saltaba al inicio del `.md`.**
> `resaltarPasaje()` tomaba los primeros 45 chars normalizados del texto guardado del chunk y buscaba
> ese prefijo dentro del `textContent` de un bloque del DOM. Pero el `texto`/`preview` del chunk conserva
> **marcado crudo de marker que NO sobrevive al render**: anclas de página `<span id="page-N-0"></span>`
> (marker las inserta al inicio de cada página, ~11 en Cai 2004), `<sup>`, imágenes `![](…)`, links
> `[t](u)` y math `$…$` (que `render()` convierte en KaTeX, sin texto plano). Cuando esa basura caía en
> los primeros ~45 chars, el prefijo normalizado ("span id page 5 0 …") no existía en el DOM → no había
> match → `scrollTop=0` **en silencio** (parecía intermitente: fallaba solo cuando el chunk citado abría
> justo en un salto de página, una fórmula o un link). Medido: **9 de 34 chunks de texto** de Cai 2004
> fallaban. Fix (frontend, sin re-indexar): `_limpiaMarcado()` quita ese marcado igual que el render, y
> el match ya no usa un prefijo fijo sino **ventanas de ~8 palabras desde varios offsets** (0,3,8,15,25),
> así una ventana más adentro empareja aunque el arranque siga sucio → 0/34. **Medido en todo el corpus
> (42 papers, 1774 chunks de texto): 240 fallos (14%) → 6 (0.3%).** Los 6 residuales son front-matter
> (pie de revista "journal homepage:", listas de autores con afiliaciones, bloques de keywords) y una
> línea de OCR corrupto (Hoek-Brown 2018) — bloques que casi nunca se citan como evidencia; no se
> persiguen a propósito (emparejar tokens cortos saltaría al párrafo equivocado, peor que ir arriba). **Estado vacío del modo cuaderno =
portada** (`mostrarPortada()`): tarjetas por paper (autor-año + conteos que da `/documentos`); clic =
`seleccionarDoc()`. El `<select>` de documento acorta nombres largos con elipsis a la mitad
(`etiquetaDoc`, ver 2026-07-17) porque el popup nativo no es acotable por CSS.

El panel derecho tiene **dos pestañas ◎ CONSULTA / ❓ QUIZ** (la pestaña activa persiste en localStorage).

- **Consulta (chat):** **📎 imágenes adjuntas** (botón o Ctrl+V; hasta 3; requiere motor '·visión'; el
  modelo LEE la captura —p.ej. una tabla real del PDF— y responde combinándola con el contexto RAG).
  **Selector de modelos** por proveedor (DeepSeek V4 Flash **default**, DeepSeek V4 Pro, Gemini 3.5
  Flash, Gemma 4) + **toggle 🧠 PENSAMIENTO** aparte (solo DeepSeek); motor y toggle persisten en
  localStorage. El panel de pensamiento nace **colapsado** y con animación "Pensando…" mientras razona
  (pasa a "Pensamiento" estático al terminar).
- **🔬 INVESTIGACIÓN PROFUNDA (agéntica, 2026-07-17):** toggle aparte (off por defecto), habilitado solo
  en motores con `agentic_capaz=True` (derivado de `AGENT_ADAPTERS`; hoy DeepSeek). Con él, `/ask` corre
  una **FASE A** previa: el modelo usa herramientas para indagar el corpus antes de responder. Herramientas
  (`backend.py`, `AGENT_TOOLS`): `buscar_en_corpus(consulta, documento?)`, `ver_esquema(documento)`,
  `leer_seccion(documento, titulo)` — reutilizan `buscar` / `outlines` / `_texto_seccion`, todas acotadas
  al **scope** (`_scope_sources`: doc, cuaderno o todo el corpus). El bucle es **no-streaming** (adaptador
  por proveedor en `AGENT_ADAPTERS`, p.ej. `_investigar_deepseek`, formato OpenAI, tope `AGENT_MAX_ROUNDS=5`)
  → esquiva los bugs conocidos de *streaming + tool_calls* de Gemini/Gemma. La evidencia recolectada se
  antepone como bloque `=== INVESTIGACIÓN ===` y la **FASE B** (respuesta) se transmite con los `_chat_*`
  de siempre. Si la Fase A lanza, se ignora y se responde con el RAG de un tiro (el CONTEXTO normal sigue
  ahí → nunca peor). El evento `done` trae `agentic: bool` y `trazas` (herramientas llamadas). Coste/latencia:
  varias llamadas al modelo por pregunta (~50–120 s en el cuaderno GSI). Adaptadores implementados:
  `_investigar_deepseek` (formato OpenAI), `_investigar_gemini` (`generateContent` no-streaming; round-trip
  functionCall role=model → functionResponse role=**function**), `_investigar_ollama` (Ollama `/api/chat`
  nativo; tool-result `{role:tool, content, tool_name}`, args ya son objeto). **Gating por evaluación**
  (2026-07-17, cuaderno GSI, misma pregunta): registrados en `AGENT_ADAPTERS` solo DeepSeek y Gemini
  (rindieron bien); **Gemma NO** (1 sola tool-call, el más lento, mezcló métodos → su adaptador queda
  definido pero sin registrar). **Progreso en vivo:** la Fase A corre DENTRO de `stream()`; los adaptadores
  son generadores que hacen `yield` de un paso legible (`_paso_legible`) por cada tool-call y `return`
  de `(evidencia, trazas)` (se captura vía `StopIteration.value`). `ask()` emite eventos `{"status":…}`
  (aviso inicial + pasos + "✍️ Redactando…"); la UI los pinta en un panel colapsable "Investigando…"
  (clases `pensamiento-details`/`.status-linea`) que al terminar se reetiqueta "Investigación" y colapsa.
  **Pendiente (opcional):** adaptador Sonnet (Anthropic/Bedrock, tool_use nativo).
- **Cuaderno y paper persisten** entre recargas (`testigo-cuaderno` / `testigo-doc` en localStorage;
  se validan al cargar y caen a "Todos" si el cuaderno/paper ya no existe).
- **MAPA DEL CUADERNO (2026-07-08):** si el alcance es "— Todo el cuaderno —" (llega `docs` sin `doc`),
  el prompt lleva SIEMPRE el listado completo de papers del cuaderno con su ficha (primer resumen con
  contenido del outline `.jsonl`, ~700 chars c/u, cache por mtime en `backend.py`). Así las preguntas
  panorámicas ("¿por cuál paper parto?", "¿qué doc cubre X?") consideran TODOS los papers, no solo los
  2-3 que tocan los `top_k=6` chunks recuperados. A propósito SIN detección de intención (heurísticas
  de intención = frágiles, ver P16): va siempre, cuesta ~1.8k tokens para 10 papers.
- **Citas textuales en español:** el `SYSTEM` prompt pide traducir al español los fragmentos que se
  transcriban aunque el paper esté en inglés (conservando términos técnicos y siglas/fórmulas).
- **Quiz (autoevaluación, Tarea 5):** mezcla configurable MC / V-F / desarrollo **sobre TODO el paper**
  (ya NO por sección), dificultad, selector de modelo propio; genera en lotes paralelos por tipo
  (`/quiz/generar`), **feedback diferido al final**, califica desarrollo con crédito parcial
  (`/quiz/calificar`), y exporta las falladas → Anki. Spec en `_archivo/PLAN_QUIZME.md`. La generación NO se
  cancela al cambiar de pestaña (flag `QUIZ_GENERANDO` mantiene la pantalla de carga al volver).
- **Gestor de libretas (2026-07-08):** botón **✎** junto al selector abre un modal para **cargar o
  borrar conversaciones**. **✎ editar** → cada fila muestra un círculo vacío; al seleccionar se rellena
  (teal `--meta`); el **🗑** borra las marcadas (con confirmación). Alcance **Este cuaderno / Todas**.
  Reusa `DELETE /conversacion?id=` (una llamada por libreta). Fuera de edición, clic en una fila carga.

- **Panel de Configuración ⚙ (2026-07-09):** modal para gestionar keys API (enmascaradas, botón
  "Probar" por proveedor, etiquetas GRATIS/DE PAGO) y el **perfil del tutor** (nombre/rol/disciplina/
  objetivo → `perfil.json`, inyectado en los prompts de chat/Anki/Quiz). Endpoints `GET/POST /config`
  y `POST /config/probar`; las keys se escriben al `.env` local. Wizard automático si falta
  GEMINI_API_KEY (el backend arranca en "modo configuración", ya no aborta). Onboarding si
  `corpus_vacio` y banner rojo si `embed_mismatch` (ver `embed_manifest.json`). OJO: los listeners
  se registran al final de `app.html` — un clic automatizado inmediatamente tras recargar puede
  llegar antes de que existan.
- **Instrucciones al tutor (2026-07-10):** campo de texto libre del perfil (`perfil.json` →
  `instrucciones`), estilo *custom instructions*. Se inyecta SOLO en `sys_chat()` (al final del
  system prompt, tope 2000 chars) y las reglas de FUENTES/citas de arriba siempre ganan si chocan.
  NO se pasa a `sys_quiz()`/`sys_anki()`/`sys_quiz_grade()`: esos generan JSON y texto libre del
  usuario podría romper el formato.

La revisión/recorte de figuras se hace SOLO con `revisar_figuras.py` (puerto 8902) durante la ingesta;
el botón in-app "▦ figuras" se eliminó (el modal quedó como código muerto, inofensivo).

---

## 3. Verificar que un PDF quedó bien indexado

Tras la ingesta, correr una **consulta de prueba** (embeddings `RETRIEVAL_QUERY` + búsqueda en Qdrant)
con una pregunta cuya respuesta esté en el doc, y confirmar que recupera texto/figuras relevantes.
Revisar también `outlines/<doc>.md` (resumen por secciones) a ojo.

---

## 4. Convenciones (detalle)

- 1 punto Qdrant = **unidad natural** (sección de texto, figura, o tabla) + payload rico (`source`,
  `breadcrumb`, `type`). Colección `papers`, 1536 dims, Cosine.
- Chunking: por sección, alineado a párrafos (NUNCA corta a mitad); tablas aisladas. **Breadcrumb
  enriquecido (2026-07-07):** antes de vectorizar, cada chunk de texto lleva `[Documento: <source> ·
  Sección: <título hoja>]` (`_embed_head()` en `chunk.py`). Esto ANCLA el chunk a su documento y
  desambigua chunks casi-idénticos entre papers parecidos (p.ej. ediciones Hoek-Brown 2002 vs 2018).
  NO se reconstruye jerarquía profunda a propósito (niveles de título de marker no fiables, ver P3). El
  `texto` del payload queda LIMPIO; el breadcrumb solo moldea el vector. Cambiarlo obliga a re-indexar
  (solo re-embed, ~3 min todo el corpus, sin marker/Sonnet). Es **general**, no por documento.
- Modelos: embeddings `gemini-embedding-2` (INAMOVIBLE: el corpus ya está vectorizado con él);
  **describir figuras/tablas al ingerir = Bedrock/Claude Sonnet** (`us.anthropic.claude-sonnet-4-6`,
  inference profile con prefijo `us.` obligatorio; `VISION_PROVIDER=gemini` fuerza Gemini); resúmenes
  de secciones `gemini-3.5-flash`; chat multi-proveedor (DeepSeek V4 default, solo texto NO visión;
  Gemini 3.5 Flash y Gemma 4 con visión).
- Techo de salida del chat/quiz: `CHAT_MAX_TOKENS` (env, default 8192) en `_chat_deepseek`/`_chat_bedrock`.

---

## 5. Problemas conocidos y cómo resolverlos (P1–P18, aprendidos 2026-07-02 → 2026-07-08)

1. **Extracción lenta / OCR redundante.** `marker` corría OCR completo incluso en PDFs digitales
   (~42 min/40pág). Solución YA aplicada: para PDFs **digitales** se usa `disable_ocr=True`
   (~14 min/40pág, texto igual o MEJOR — el OCR metía typos). El router de `ingesta.py` lo decide
   solo. Si un PDF digital tarda muchísimo, verificar que el router lo clasificó bien.
2. **Tiempos.** 40pág ≈ 14 min; 120pág ≈ 40 min (CPU; el cuello es la detección de layout, no el
   OCR). Correr en **background**. La GPU GTX 1050 (2GB) NO ayuda (OOM). Es normal, avisar y esperar.
3. **Niveles de título de marker NO son fiables** (pone "Introduction" como `####`). `secciones.py`
   los reconstruye con un pase de LLM e **incluye títulos-contenedor sin cuerpo** para que las
   subsecciones aniden bien. No revertir esto.
4. **Tablas por VISIÓN (2026-07-06, REEMPLAZÓ al enfoque de texto).** Antes se pasaba el *texto* de la
   tabla (marker DESORDENA las celdas apaisadas) a un modelo. AHORA `describir_tablas.py` **localiza la
   tabla en el PDF, renderiza la página** (pymupdf; la rota si el texto va vertical) y **Sonnet/Gemma
   reconstruye un Markdown fiel** desde la imagen — mucho más exacto en tablas densas/apaisadas.
   Heurística de modelo: Gemma por defecto; fuerza **Sonnet** si la tabla es apaisada, tiene ≥7 columnas
   o celdas largas (`TABLES_FORCE_SONNET=1` fuerza Sonnet siempre). Las dudosas se listan en
   `descripciones/<doc>_tablas_revisar.txt` (revisión **OPCIONAL**, NO bloquea como el checkpoint de
   figuras). Último recurso si una tabla crítica aún sale mal: adjuntar una captura en el chat (📎/Ctrl+V,
   motor con ·visión).
5. **Errores 503/429 de Gemini (transitorios).** Hay reintento automático con backoff. Si persiste,
   **re-correr** el paso: los caches (`descripciones/_cache_*`, `outlines/_cache_*`) hacen que
   retome sin rehacer lo bueno.
6. **`GEMINI_API_KEY` inválida.** Si los embeddings o descripciones fallan con `400 "API key not
   valid"`, la key en `.env` es un placeholder o está mal. Verificar `.env` (sin imprimir el valor).
   La misma key sirve para embeddings Y para los describidores (Gemini).
7. **Seguridad de API keys.** Van SOLO en `.env` (gitignored). NUNCA en `.env.example` (plantilla
   pública) ni pegadas en el chat. Si se pega una key en el lugar equivocado, moverla al `.env`,
   limpiar la plantilla, y avisar que considere rotarla si se expuso.
8. **Qdrant local bloquea `qdrant_data/`.** Un solo proceso a la vez. Si más adelante hay un backend
   corriendo, **pararlo antes de reindexar**.
9. **Cross-lingual funciona.** `gemini-embedding-2` es multilingüe: consultas en español recuperan
   contenido en inglés. No hace falta traducir nada.
10. **Abstract perdido/agrupado en el título.** Ocurre cuando `marker` no genera la cabecera `# Abstract` en el markdown. Solución: `chunk.py` autodetecta e inyecta la cabecera `# Abstract` al inicio del documento en los primeros 10,000 caracteres.
11. **Tablas vacías (ghost tables).** Ocurre cuando `marker` extrae la cabecera de la tabla como un bloque de tabla independiente sin datos. Solución: `chunk.py` las filtra con `es_tabla_valida()` y las convierte a prosa para no ensuciar la base de datos de tablas.
12. **Pies de figura cruzados (captions robados).** La búsqueda simple hacia adelante se confundía con imágenes contiguas o captions previos. Solución: buscador bidireccional acotado (3 líneas antes/después) y barrera que detiene la búsqueda si cruza otra etiqueta de imagen, reconociendo negritas (`^[\s*_]*(fig|table|tabla|cuadro|gráfico|...)\b`).
13. **Logos y banners de editoriales.** Pequeños logos o banners alargados de Springer/editoriales se indexaban como figuras. Solución: filtro por dimensiones en `describir_figuras.py` (`w < 250 or h < 100` con Pillow) para omitirlos del procesamiento visual y del índice.
14. **Mosaicos/Paneles fragmentados.** Subfiguras consecutivas en la misma página de una sola figura real se separaban. Solución: agrupamiento por página física (si no hay divisores o pies de otras figuras intermedias) y fusión física mediante Pillow (`combinar_imagenes`) en grillas de 2 columnas o pilas verticales.
15. **`fig\b` NO matchea "Figure"** (2026-07-05). Los regex de pies con alternativas truncadas
   (`fig|table`) + `\b` fallan con la palabra completa ("Fig**ure** 5"): el `\b` corta dentro de la
   palabra. Por eso se fusionaron mal las Figuras 5 y 6 de Bezie. Usar SIEMPRE `_KW_CAP` de
   `describir_figuras.py` (cubre fig/figs/figure(s)/figura(s)/table(s)/tabla(s)/etc. + negritas).
   Si aparece otro formato de pie no reconocido, ampliar ESE regex, no crear otro paralelo.
16. **Las heurísticas de figuras fallan por variantes de formato** (negritas, pies antes de la
   imagen, mosaicos, etc. — ya van 3 iteraciones). La red de seguridad es el **punto de control
   humano** del §1: el usuario revisa el plan ANTES de gastar Sonnet/embeddings. Ante un caso raro nuevo,
   corregir el plan a mano es más barato y seguro que apilar otra heurística.
17. **Front-matter de revistas colado como secciones** (2026-07-07). `marker` extrae el título con
   hyperlinks y los metadatos de revista (Frontiers, etc.: "Edited by", "Reviewed by",
   "Correspondence", "Specialty section", "Citation") como si fueran secciones → ensucian la LEYENDA
   y el índice (visto en Chen 2021). Solución: `es_excluida()` en `chunk.py` (compartida por índice y
   outline) filtra esos rótulos, y `_limpiar_titulo()`/`limpiaTit` (frontend) quitan links markdown
   `[texto](url)`. Si aparece otro rótulo de front-matter, AMPLIAR la tupla `SECCIONES_EXCLUIDAS`, no
   crear otro filtro. Para aplicarlo a un doc ya procesado: re-correr `secciones.py "<stem>"` (usa
   caché, no toca Qdrant) y, si se quiere limpiar el índice, re-indexar.
18. **Quiz: "El LLM no generó un JSON válido en ninguno de los lotes"** (2026-07-08). La salida del
   modelo se **truncaba** por `max_tokens` (era 4096) con lotes grandes (p.ej. 10 MC ricas sobre todo
   el paper) → JSON cortado a media pregunta → el parser fallaba ENTERO y perdías hasta las 8 preguntas
   que sí venían completas. Solución doble en `backend.py`: (a) `CHAT_MAX_TOKENS` (env, default **8192**,
   antes 4096 hardcoded) en `_chat_deepseek`/`_chat_bedrock` — también evita que respuestas largas del
   chat se corten; (b) `_rescatar_items_quiz()` en `_parse_quiz` rescata los objetos `{...}` completos de
   un array truncado (descarta el último a medias) como última red. Diagnóstico: la salida cruda fallida
   queda en `debug_quiz_error.txt`. Si vuelve a pasar con lotes enormes, subir `CHAT_MAX_TOKENS` o bajar
   la cantidad de preguntas por tipo.
19. **`.gitignore` con comentarios inline NO ignoraba nada** (2026-07-09). Git solo trata `#` como
   comentario al INICIO de línea; `raw/    # PDFs de entrada` busca literalmente una carpeta llamada
   `raw/    # PDFs...` → `raw/`, `md/`, `qdrant_data/`, `conversaciones/`, etc. habrían entrado al
   repo (con data potencialmente confidencial) en el primer `git add .`. Detectado ANTES del `git init`
   real (2026-07-09). Solución: comentarios en línea propia; además se agregaron
   `descripciones/`, `_descargas/`, `_archivo/`, `*.bak*` y
   `.claude/settings.local.json`, que faltaban. Verificación: `git check-ignore -v <ruta>` por cada
   carpeta sensible y revisar `git status` antes del primer commit. Repo: privado,
   `github.com/TitanDagox/GaIA-reader`. Smoke test post-cambios: `python scripts/smoke_test.py`
   (5 checks sin gastar tokens LLM; requiere el backend corriendo).
   **Corolario (2026-07-16), al hacer el repo público:** `.gitignore` se publica, así que cada patrón
   DELATA el nombre de lo que oculta (una regla `mi_empresa/` cuenta dónde trabajas). Convención:
   en `.gitignore` va solo lo que necesita CUALQUIER instalación de GaIA (`raw/`, `qdrant_data/`,
   `conversaciones/`…); lo personal de tu PC va en **`.git/info/exclude`**, que ignora igual y nunca
   se commitea. Verifica ambos con `git check-ignore -v <ruta>`.
20. **`model` de OTRO proveedor colado → 404 contra el endpoint equivocado** (2026-07-10). El front
   persiste el `model` seleccionado; si el usuario cambia de motor pero el valor viejo queda pegado
   (o llega un `model` inconsistente por cualquier vía), se enviaba tal cual al proveedor resuelto —
   p.ej. `deepseek-v4-flash` contra el endpoint de Gemini → `404 generativelanguage.googleapis.com`.
   Encima el frontend guardaba ese texto de error como si fuera la respuesta del tutor: quedaba en la
   libreta y se reinyectaba como "conversación previa" en turnos futuros. Solución: `_resuelve_motor()`
   en `backend.py` (usa `_motores()`, la misma lista de `/providers`) detecta cuando el `model` pedido
   pertenece a OTRO proveedor y lo descarta (`None`) para que cada `_chat_*` caiga a su default en vez
   de reventar; se usa en `/ask`, `/anki`, `/quiz/generar` y `/quiz/calificar`. En `app.html`, `enviar()`
   ya NO hace `CONV.turnos.push(...)`/`guardarConv()` cuando hubo error (sí sigue guardando el caso
   "(detenido)" por Stop), y la ficha muestra un botón **↻ Reintentar** que repone la pregunta y
   reintenta con el motor actualmente seleccionado.
21. **Símbolos matemáticos de los PIES OCR-eados mal por marker** (2026-07-17). En pies con notación
   (σ, τ, φ, `|…|`, subíndices) marker a veces lee los glifos como letras: σ→`s`, las barras `|…|`→`j`
   (visto en Hoek-Brown 2018 Fig. 5: `σci/|σt|` quedó `sci/jstj`). El pie llega así al plan y de ahí a
   Sonnet como "pie de figura". NO se intenta corregir por heurística (adivinar que "jstj" era `|σt|` es
   justo el tipo de regla frágil que el §1/P16 nos dice evitar): la red correcta es el **editor de
   figuras** (`revisar_figuras.py`, 8902), donde el pie es un `<textarea>` editable — se corrige a mano
   y al re-describir (`--rehacer`) Sonnet recibe el pie bueno. Síntoma a cazar al revisar: "sci", "jstj",
   "st", letras sueltas donde debería haber griegas. `_limpiar_caption` sí limpia lo mecánico (etiquetas
   HTML, enlaces `[texto](#ancla)`, brackets escapados `\[6\]`), pero no puede reconstruir un glifo mal leído.
22. **Marcador `[[FIG:…]]` mal formado se filtraba como texto crudo** (2026-07-18). El backend pide al
   modelo insertar la figura con `[[FIG:N]]` (N = número de `[Fuente N]`), pero a veces —sobre todo en
   modo cuaderno— escribe texto: `[[FIG:Quantification of the GSI Chart (Hoek 2013) — Figure 5]]`. La
   regex de `finalizar` solo sustituía `[[FIG:\d+]]`, así que ese marcador ni traía imagen ni se borraba
   → quedaba visible. Solución en `app.html`: (a) `render()` oculta `\[\[FIG:[^\]]*\]\]` (cualquier
   marcador) durante el streaming; (b) `finalizar` acepta ref numérico O descriptivo — si es texto,
   `resolverFigDescrita()` busca la figura entre las fuentes por documento + número de pie; si la
   encuentra la muestra, y si no (no se recuperó), **borra el marcador** (nunca deja texto crudo). No se
   intenta traerla del índice si no está entre las fuentes (sería otra capa; el borrado es la red segura).
23. **Citas cruzan papers en modo cuaderno (no es bug)** (2026-07-18). Al consultar "todo el cuaderno"
   las `[Fuente N]` provienen de varios papers; una cita `[n]` puede abrir un paper distinto al del
   LECTOR, y el modelo a veces **mis-atribuye** (cita una Fuente que no sostiene la afirmación, p.ej.
   citó la Tabla de σci de Hoek-Brown 1997 para el "Joint Condition JCond₈₉", que son cosas distintas).
   El clic hace lo correcto (abre la Fuente citada); el LECTOR + el tooltip (nombre del paper) son la
   herramienta para **verificar** y cazar la mala cita. Mitigable endureciendo el prompt de citación,
   no eliminable. Es, de hecho, el valor del RAG con evidencia a la vista.
