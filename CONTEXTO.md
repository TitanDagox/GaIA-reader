# GaIA — Arquitectura y diseño

> Cómo está construida GaIA y por qué (estado **actual**). Para **instalarla y usarla**, ver
> `README.md`; para el **detalle del pipeline, problemas conocidos y convenciones**, ver
> `NOTAS_TECNICAS.md`; para el **historial de cambios por fecha**, ver `CHANGELOG.md`.

## Qué es y para qué

GaIA es un asistente **local** para **leer y estudiar papers y PDFs técnicos densos** (geología,
geotecnia, yacimientos, etc.): documentos largos (60–120+ páginas) y llenos de figuras, gráficos y
tablas que hay que **entender**, no solo leer. La idea: abrir un documento y que un tutor-IA lo
explique —incluidas las figuras, con sentido técnico— con resumen por secciones, citas verificables
y autoevaluación (Quiz).

Está pensado como herramienta de aprendizaje: encaja con métodos de *active recall* + repetición
espaciada y exporta tarjetas a Anki. El tono "tutor" del chat es deseado, no un defecto.

## Arquitectura local-first

Todo corre en el PC del usuario salvo las APIs cloud de terceros:
- **Local (PC):** los PDFs, el índice vectorial **Qdrant embebido** (`QdrantClient(path=...)`, sin
  Docker ni servidor aparte), el backend FastAPI y la interfaz web.
- **Cloud (según el proveedor elegido):** embeddings, el LLM de chat (multi-proveedor) y el modelo de
  visión que describe las figuras al ingerir.
- **Los datos no salen del PC ni van a git.** Las API keys viven solo en `.env` (gitignored).

## Por qué RAG + resumen pre-computado (no "todo en contexto")

Un paper de 40 páginas (~45K tokens) cabría entero en el contexto del modelo, pero hay documentos de
120+ páginas (~150K tokens): reenviar eso en cada pregunta es caro, lento y pierde calidad
("lost in the middle"). Por eso el sistema es **híbrido**, con dos productos que se calculan UNA vez
al ingerir cada documento:

1. **Esquema/resumen por secciones** (`outlines/<doc>`): da el resumen por títulos al instante y
   resuelve la detección de títulos sin índice (donde el formato del PDF falla, un pase de LLM segmenta).
2. **Índice vectorial** (texto + figuras descritas + tablas) en Qdrant, para responder preguntas
   puntuales sobre documentos grandes sin reenviar todo el texto.

Ruteo de la consulta: "resúmeme el capítulo X" → sale del esquema pre-computado; "¿qué dice sobre X
en la página 30?" → recuperación RAG; "explícame esta figura" → punto de imagen descrito con sentido
técnico.

## Stack

| Capa | Qué se usa |
|---|---|
| Interfaz | Web app local **GaIA** (`app.html`, servida por el backend en `/app`). Chat con citas, figuras inline, pestañas Consulta/Quiz, libretas (conversaciones persistentes), panel de configuración |
| Backend | **FastAPI** (`backend.py`, puerto 8901). Endpoints principales: `/ask`, `/search`, `/outline`, `/documentos`, `/figura`, `/pdf`, `/anki`, `/quiz/*`, `/config`, `/conversacion(es)`, `/cuaderno(s)` |
| LLM de chat | Multi-proveedor: **DeepSeek** v4 flash/pro (texto, con razonamiento opcional), **Gemini** 3.5-flash (visión), **Gemma 4** vía Ollama Cloud (visión), **Claude Sonnet** vía Bedrock (visión) |
| Embeddings | **gemini-embedding-2** por defecto (dim 1536, Cosine, multilingüe: consulta en español recupera texto en inglés). Configurable por instalación con `EMBED_PROVIDER` |
| Visión (ingesta) | Describir figuras/tablas: **Bedrock/Claude Sonnet** por defecto (mejor calidad); fallbacks `VISION_PROVIDER=gemini` (free tier) o `manual` (lo hace tu agente de IA) |
| Vector DB | **Qdrant local embebido** (`qdrant_data/`, colección `papers`). Bloquea la carpeta → un solo proceso a la vez (parar el backend antes de reindexar) |
| Ingesta | `marker-pdf` (extrae texto y figuras; `disable_ocr` para PDFs digitales) → describir figuras/tablas por visión → esquema por secciones → chunking → indexar. Orquestado por `ingesta.py` |

## Pipeline de ingesta (resumen)

Un solo comando —`python ingesta.py "archivo.pdf"`— encadena: router texto/imagen → `marker-pdf` →
**plan de figuras (pausa para revisión humana)** → describir figuras y tablas por visión → esquema por
secciones → chunking → indexar en Qdrant. Es **reanudable** gracias a los caches: si un paso falla,
se re-corre el mismo comando. El detalle de cada paso, el checkpoint de figuras y los problemas
conocidos están en `NOTAS_TECNICAS.md` (§1 y §5).

## Decisiones técnicas vigentes

- **Modelo de embeddings y corpus están CASADOS.** El índice se vectoriza con un modelo
  (`gemini-embedding-2` por defecto); consultar con otro modelo da resultados basura silenciosos.
  `embed_manifest.json` registra el modelo usado y `indexar.py`/backend abortan si el `.env` pide
  otro. Cambiar de modelo ⇒ re-indexar todo el corpus.
- **Figuras y tablas se describen por VISIÓN al ingerir**, no a partir del texto que extrae marker
  (que desordena las tablas apaisadas): se renderiza la página y un modelo con visión reconstruye la
  descripción/Markdown con sentido técnico. Se indexa como un punto y se guarda la ruta a la imagen
  para mostrarla o pasarla a un modelo con visión al responder.
- **Breadcrumb `[Documento… · Sección…]`** se antepone a cada chunk ANTES de vectorizar (ancla de
  recuperación que desambigua chunks casi idénticos entre papers parecidos); el texto que se muestra
  queda limpio.
- **Los niveles de título de marker no son fiables** → `secciones.py` los reconstruye con un pase de LLM.
- **Checkpoint humano de figuras:** la ingesta se detiene tras extraer (exit code 3, no es error) para
  que el usuario revise/corrija el plan de figuras antes de gastar visión/embeddings. Es la red de
  seguridad ante heurísticas frágiles.
- **Perfil de estudio inyectado en los prompts.** El tutor, el Quiz y las tarjetas se adaptan a un
  perfil del usuario (nombre, rol, situación, disciplina, nivel, objetivo) guardado en `perfil.json`
  (local, gitignored) y editable desde el panel de Configuración. El **default es NEUTRAL** (un tutor
  genérico de "temas técnicos"): así el repositorio no arrastra el perfil de nadie y sirve para
  cualquier disciplina sin editar código.
- **Seguridad:** las API keys van solo en `.env` (gitignored), nunca en `.env.example` ni en el chat.
  El backend se sirve en `127.0.0.1` (CORS solo loopback) y admite un `RAG_TOKEN` opcional.
