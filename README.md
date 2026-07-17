# GaIA — Tutor-IA local para leer papers técnicos

GaIA es un asistente de estudio que corre **en tu computador** y te ayuda a entender papers técnicos
densos (ciencias, ingeniería, geología, lo que sea que estés leyendo) sin tener que subirlos a ningún
servicio de terceros. Le das tus PDFs, GaIA los procesa —incluyendo figuras y tablas, que describe con
un modelo de visión— y luego puedes conversar con ellos como si tuvieras a alguien que ya se los leyó
todos y te los explica con paciencia.

El chat responde citando la fuente exacta (documento y sección), muestra las figuras del paper
incrustadas en la respuesta cuando son relevantes, y organiza tus PDFs en "cuadernos" temáticos. Además
incluye un modo **Quiz** que genera preguntas (opción múltiple, verdadero/falso, desarrollo) a partir de
lo que ya indexaste, con feedback y exportación de las preguntas falladas a **Anki** para repaso
espaciado.

Solo la base de datos vectorial (Qdrant, embebida) y el servidor web corren localmente; los modelos de
lenguaje y de embeddings se consumen en la nube (Google, DeepSeek, Ollama Cloud o AWS Bedrock, a tu
elección). Tus PDFs y tus claves de API nunca salen de tu máquina hacia este proyecto — solo viajan a
la API que tú elijas usar.

---

## Instalación

Este README sirve tanto a una persona como a su **asistente de IA** (Claude Code, Google Antigravity,
Codex, Cursor, etc.). La forma más simple:

> **Clona este repositorio y dile a tu agente de IA: "instala esta app siguiendo el README".**

El detalle técnico está más abajo (["Requisitos técnicos"](#requisitos-técnicos-referencia)). Lo que
sigue es el **guion que debe ejecutar el agente**.

---

## Guion para el asistente de IA (runbook)

> **Si eres un asistente de IA que está levantando este repo: este bloque es para ti.** La persona que
> te dio el repo probablemente **no es técnica** y no debería tener que preguntarle nada a quien se lo
> pasó. Sigue las fases en orden y **haz tú las preguntas** marcadas con **➤ PREGUNTA**. **No asumas la
> profesión ni la disciplina del usuario:** este sistema sirve para cualquiera (medicina, derecho,
> biología, geología, ingeniería, lo que sea). Al terminar, el usuario debe poder abrir la app y
> chatear con sus documentos.

### Fase 0 — Reconoce el estado del repo
Mira qué hay en la carpeta antes de actuar:
- ¿Existen `qdrant_data/`, `md/`, `descripciones/`? → **ya hay un corpus** (alguien lo compartió): NO
  hay que ingerir nada, solo levantar. Puedes omitir OCR y la parte pesada de ingesta.
- ¿No existen? → **corpus vacío**: el usuario partirá de cero e irá agregando sus PDFs.
- ¿Existe `.env`? Si no, lo crearás en la Fase 2. ¿Existe `perfil.json`? Si no, en la Fase 3.

### Fase 1 — Requisitos y dependencias
1. Verifica **Python 3.11+** (`python --version`).
2. `pip install -r requirements.txt`.
   - **Si el corpus ya viene dado y el usuario solo quiere chatear:** el backend NO usa
     `marker-pdf`/PyTorch, así que puedes seguir aunque esa dependencia pesada falle o demore; Poppler
     y Tesseract tampoco hacen falta.
   - **Si el usuario ingerirá PDFs nuevos:** hace falta la instalación completa **y** Poppler +
     Tesseract OCR en el `PATH` (ver ["Requisitos técnicos"](#requisitos-técnicos-referencia)).

### Fase 2 — Claves de API (➤ PREGUNTA al usuario)
- **➤ PREGUNTA:** *"¿Tienes una API key de Google AI Studio (Gemini)? Es gratis y es la única
  obligatoria."* Si no la tiene, guíalo a https://aistudio.google.com/apikey (2 minutos).
- **➤ PREGUNTA (opcional):** si quiere un chat alternativo, por una key de **DeepSeek** (de pago,
  barato) o **Ollama Cloud** (gratis). Para describir figuras con la mejor calidad, por una de
  **Bedrock** (de pago) — pero hay alternativas gratis (ver Fase 4).
- Crea `.env` copiando `.env.example` y rellena **solo** las claves que el usuario tenga. **Las
  instrucciones de cada clave están dentro de `.env.example`.** Nunca inventes, compartas ni subas
  claves a git. Genera un `RAG_TOKEN` aleatorio (o deja que el wizard lo haga al arrancar).

### Fase 3 — Perfil del usuario (➤ ENTREVISTA — lo más importante)
El tutor se adapta a cada usuario mediante un perfil que se inyecta en el chat, el quiz y las
tarjetas. **Entrevista al usuario** con preguntas cortas, una a una; con sus respuestas escribe
`perfil.json` en la raíz. Lo que quede en blanco → genérico (no lo inventes).

1. **Nombre** — ¿Cómo quieres que te llame?
2. **Profesión / rol** — ¿A qué te dedicas o qué estudias? (ej. médica, abogado, estudiante de biología)
3. **Disciplina / tema** — ¿De qué tratan los documentos que vas a leer? (ej. medicina interna,
   derecho penal, geotecnia)
4. **Nivel** — ¿Principiante, intermedio o avanzado en ese tema?
5. **Objetivo** — ¿Para qué lo usarás? (ej. preparar un examen, estar al día con papers, entender un informe)
6. **Instrucciones personalizadas para el chatbot** — ¿Cómo quieres que te responda? **Si no sabe qué
   poner, ofrécele 2-3 opciones por defecto y que elija o combine**, por ejemplo:
   - *"Define en una línea cada término técnico nuevo la primera vez que aparezca."*
   - *"Cierra cada concepto nuevo con una pregunta corta para comprobar que entendí."*
   - *"Usa analogías cotidianas para lo abstracto y evita tablas anchas."*

Escribe el resultado así (omite los campos vacíos):
```json
{"nombre":"…","rol":"…","disciplina":"…","nivel":"intermedio","objetivo":"…","instrucciones":"…"}
```
Alternativa: dejar que el usuario lo llene desde el panel **⚙ Configuración** de la app (se guarda solo).

### Fase 4 — Corpus de documentos
- **Si te compartieron un corpus:** copia dentro de la raíz del proyecto las carpetas recibidas
  (`qdrant_data/`, `md/`, `descripciones/`, `outlines/`, `embed_manifest.json`, `cuadernos.json`).
  **No cambies el modelo de embeddings:** el corpus está *casado* con el que registra
  `embed_manifest.json` (por defecto `gemini-embedding-2`); si intentas otro, el backend aborta con aviso.
- **Si el corpus está vacío:** el usuario agrega PDFs con `python ingesta.py "archivo.pdf"` (copiándolos
  antes a `raw/`). Para describir figuras/tablas **sin clave de pago**, usa `VISION_PROVIDER=manual` (o
  el selector *"Sin API / Agente IA"* en Configuración): la ingesta se detiene y **tú, el asistente,
  describes las imágenes** siguiendo el archivo `descripciones/<doc>_PENDIENTES/DESCRIBIR_FIGURAS.md`.

### Fase 5 — Levantar y verificar
1. Arranca: `python -m uvicorn backend:app --app-dir "." --port 8901` (o doble clic a `run.bat` en Windows).
2. Abre `http://127.0.0.1:8901/app`. Si faltan claves, la app abre sola el wizard de Configuración.
3. **Verifica de verdad:** si hay corpus, haz una consulta de prueba y confirma que responde **citando
   la fuente**. Si está vacío, confirma que la app carga y que el perfil quedó guardado.

Con esto, el usuario final ya puede usar GaIA sin depender de quien le pasó el repo.

---

## Requisitos técnicos (referencia)

- **Python 3.11 o superior.**
- Para **procesar PDFs nuevos** (no solo consultar): **Poppler** y **Tesseract OCR** en el `PATH`.
  En **Linux**: `sudo apt install libgomp1 poppler-utils tesseract-ocr ffmpeg libsm6 libxext6 -y`.
- Dependencias Python: `pip install -r requirements.txt`.
  > `marker-pdf` (el extractor de PDFs) arrastra **PyTorch**, pesado de instalar. Con GPU NVIDIA,
  > instala primero `torch` con CUDA (https://pytorch.org/get-started/locally/) antes del `pip install`,
  > o quedará en modo CPU (funciona, más lento). Si **solo vas a chatear** con un corpus ya dado, el
  > backend no usa `marker-pdf`/PyTorch y puedes prescindir de ellos.
- **Arrancar:** `python -m uvicorn backend:app --app-dir "." --port 8901` → `http://127.0.0.1:8901/app`.

---

## Personaliza tu tutor (para tu disciplina, no la de otro)

GaIA **no** viene amarrado a ninguna carrera: recién clonado es un tutor genérico. Tú lo adaptas a lo
que estudias mediante un **perfil** que se inyecta en todas las explicaciones, el quiz y las tarjetas.
Un geólogo, un médico y un docente de derecho obtienen tres tutores distintos con el mismo código.

El perfil tiene estos campos (todos opcionales; en blanco → tutor genérico):

| Campo | Qué es | Ejemplo |
|---|---|---|
| Nombre | Cómo quieres que te llame | *María* |
| Rol / profesión | Tu oficio o rol | *médica, geólogo, docente de sedimentología* |
| Disciplina / tema | El área de lo que lees | *medicina interna, geología, derecho penal* |
| Nivel | Calibra la profundidad | *principiante · intermedio · avanzado* |
| Objetivo | Para qué lo usas | *preparar un examen, entender papers densos* |

Dos formas de rellenarlo:

- **Que la IA te entreviste (recomendada):** dile a tu agente
  > *"Entrevístame con unas preguntas cortas para definir mi perfil de estudio en GaIA (nombre, rol,
  > disciplina, nivel y objetivo) y guárdalo."*

  El agente te hace 4-5 preguntas, completa **solo los campos que hagan falta** y escribe el archivo
  `perfil.json` en la raíz del proyecto (o lo guarda vía el panel de Configuración). Formato:
  ```json
  {"nombre": "María", "rol": "médica", "disciplina": "medicina interna",
   "nivel": "avanzado", "objetivo": "estar al día con papers clínicos"}
  ```
- **A mano:** abre la app, pulsa el engranaje **⚙ Configuración** (arriba a la derecha) y rellena la
  sección **"Tu perfil de estudio"**. Se guarda solo, sin reiniciar nada.

> El perfil vive **solo en tu máquina** (`perfil.json` está fuera del control de versiones). Nunca se
> sube al repositorio ni se comparte.

---

## Qué claves de API necesitas (y cuáles cuestan dinero)

| Clave | Para qué | Costo |
|---|---|---|
| `GEMINI_API_KEY` | Embeddings (obligatoria) + chat opcional | **Gratis** (Google AI Studio) |
| `DEEPSEEK_API_KEY` | Chat alternativo, muy barato | De pago (bajo costo) |
| `OLLAMA_API_KEY` | Chat alternativo | **Gratis** (Ollama Cloud, free tier) |
| `AWS_BEARER_TOKEN_BEDROCK` | Describir figuras/tablas al ingerir PDFs (Claude Sonnet en Bedrock, mejor calidad) | De pago |

Lo único **estrictamente obligatorio** es `GEMINI_API_KEY` (gratis) — con eso ya puedes chatear con
papers ya indexados. Para **ingerir PDFs nuevos** necesitas además un proveedor con visión para
describir figuras y tablas: Bedrock es la opción de mejor calidad (de pago, requiere cuenta AWS), pero
también puedes usar Gemini u Ollama Cloud (gratis, algo menos precisos) configurando `VISION_PROVIDER`
en el `.env`.

---

## Uso básico

### Chatear con papers ya indexados
Solo arranca el servidor (paso 4 arriba) y abre `http://127.0.0.1:8901/app`.

### Añadir un PDF propio
```bash
# 1. Copia el PDF a la carpeta raw/
# 2. Corre el orquestador de ingesta:
python ingesta.py "nombre del archivo.pdf"
```
El proceso extrae el texto, agrupa las figuras y **se detiene** (esto es esperado, no un error) para
que revises y apruebes las figuras antes de gastar créditos de API describiéndolas. Para eso, abre en
otra pestaña el editor visual:
```bash
python revisar_figuras.py "nombre_del_pdf_sin_extension"
```
y entra a `http://127.0.0.1:8902` para reorganizar, recortar o descartar figuras. Luego vuelve a correr
el comando de `ingesta.py` del paso 2 para terminar el proceso (describir figuras/tablas e indexar en
la base vectorial).

> El servidor bloquea la carpeta de la base de datos vectorial mientras corre: **apágalo (Ctrl+C)**
> antes de correr `ingesta.py` o cualquier re-indexación.

---

## Importante

- **Este repositorio no incluye papers.** Por derechos de autor, la carpeta `raw/` (PDFs) y la base de
  datos vectorial ya procesada no se distribuyen. Cada quien parte con un corpus vacío e ingiere sus
  propios PDFs siguiendo los pasos de arriba.
- **Todo corre local.** El servidor, la base de datos vectorial y tus PDFs viven en tu computador. Las
  claves de API y el contenido de tus documentos solo se envían al proveedor de IA que tú configures
  (Google, DeepSeek, Ollama o AWS), nunca a un servidor de este proyecto.

---

## Licencia

MIT.
