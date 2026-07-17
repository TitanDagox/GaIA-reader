# eval/rag_eval.jsonl

Set de evaluación de recuperación RAG: 25 preguntas de estudio (18 texto, 5 figura, 2 tabla) sobre
8 papers reales del corpus (Sillitoe 2010, Hoek-Brown 2018, Marinos-Hoek 2007, Camus 2005, Hoek 2009,
Kruse 2011, Bar-Barton 2017, Niemeyer 1996). Cada línea es un JSON:

```json
{"pregunta": "...", "doc": "<source exacto de /documentos>", "debe_recuperar": ["..."], "tipo": "texto|figura|tabla"}
```

`debe_recuperar` son términos/secciones/figuras que deberían aparecer en los resultados de `/search`
(no es un gold-chunk exacto, es una heurística de verificación manual o automática por substring).

Uso: correr estas 25 queries contra `POST /search` antes/después de un cambio de recuperación
(re-ranking, chunking, embeddings) y comparar qué fracción de `debe_recuperar` aparece en el top-k.
No modifica ni depende del backend; solo lo consulta.

## Medidor: `run_eval.py`

Con el backend corriendo:

```bash
python eval/run_eval.py                       # set normal, acotado al doc de cada pregunta
python eval/run_eval.py --corpus              # buscar en TODO el corpus (más difícil)
python eval/run_eval.py eval/rag_eval_hard.jsonl --corpus   # set "duro" (figuras con frases vagas)
```

Imprime recall por tipo (texto/figura/tabla) y, para figuras, el rank de la figura esperada.

## Resultado del experimento de RE-RANKING (2026-07-09)

Se probó un re-ranking con LLM (traer un pool grande por tipo y reordenarlo con Gemini flash) y se
midió contra estos sets. **No mejoró la recuperación** en ningún escenario: por-doc ya es casi
perfecto (figuras 5/5 en rank 1) y el único fallo whole-corpus es una consulta que no especifica el
paper y compite contra cartas de GSI casi idénticas de otros papers (algo que el re-ranking no puede
ni debería resolver desde la sola query). Se descartó el re-ranking por no aportar y añadir latencia.
Palancas con retorno real pendientes: mejorar los **captions de tablas** en la ingesta (el fallo de la
tabla Q-slope viene de ahí) y activar el lookup determinista de figuras también en `/search`.
