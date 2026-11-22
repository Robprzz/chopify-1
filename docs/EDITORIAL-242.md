# Automation 2.4.2 · Evidencia editorial verificable

## Causa y alcance de la reproducción

En Windows, según el informe del usuario, 2.4.1 tardó 90,16 s con Qwen3:4b y 160,67 s con Qwen3:8b. Ambos hicieron cinco llamadas, dos intentos de reparación temática y ninguna llamada de redacción. El resultado fue 0/10 y cero variantes válidas. El registro con 26,079 s restantes demuestra que esa ejecución terminó por una condición distinta al agotamiento de tiempo.

En 2.4.1, el validador de tema toma los pasajes de las citas estructuradas aceptadas como universo para comprobar los nombres. Si la respuesta deja fuera un pasaje válido, o la cita textual falla su esquema o coincidencia, un nombre visible en el clip puede quedar fuera de ese universo. La reparación volvía a exigir una cita completa, su posición y su categoría. El código `focus: missing_evidence` indicaba un fallo al validar ese objeto. No permitía distinguir el formato inválido, la selección de un pasaje no ofrecido y el texto no encontrado. El historial anterior tampoco guardaba qué candidato individual disparaba `unsupported_entity`. Para identificar si en ese resultado concreto falló el nombre de la persona, el del edificio o una coincidencia parcial del detector, hace falta la transcripción original o repetir el trabajo con la nueva traza.

Los tests usan una **reconstrucción parcial ficticia**. Incluyen una persona, un edificio numerado, un segundo acontecimiento sensible y un nombre que aparece solo fuera del clip. El contenido del test no reproduce los hechos ni el diálogo del video original.

## Flujo actualizado

- El registro asigna IDs de cláusula como `p1.0` estables para la misma captura de la transcripción. Cada entrada conserva el pasaje, la fuente, la modalidad y las entidades o categorías de afirmación que se respaldaron. Los hechos atribuidos, opiniones, preguntas, hipótesis, contradicciones e interpretaciones conservan categorías distintas.
- Los fragmentos anteriores y posteriores y los metadatos usan IDs separados y se marcan como fuentes auxiliares. Pueden orientar la interpretación temática. Los esquemas de citas y reparación excluyen esos IDs como evidencia de afirmaciones del clip.
- La validación distingue nombres que sí aparecen fuera de la selección actual de nombres que aparecen solo en contexto auxiliar y de nombres sin respaldo. Cuando encuentra una cláusula pertinente que respalda el tema, recupera su referencia sin repetir la llamada al modelo. El cotejo conserva las reglas de cifras y admite mayúsculas y tildes según la normalización existente.
- Dos acontecimientos sensibles deben estar vinculados a sus respectivos sujetos en los pasajes usados. La comprobación determinista cubre familias de expresiones frecuentes, como envenenamiento, fallecimiento y acusación. La revisión independiente sigue siendo necesaria para alias, referencias implícitas, atribuciones complejas o hechos fuera de esas familias.
- Las reparaciones reciben el campo, el texto rechazado, el motivo, las referencias permitidas y una instrucción específica. Si vuelve el mismo estado inválido, se detiene esa secuencia. Si el tema conserva un núcleo explícito, el motor puede usar una mención comprobada y una cita atribuida sin repetir el dato dudoso. Se mantienen los límites configurados, la revisión en tercera persona y la aprobación humana para publicar.

En **Diagnóstico de ejecución → Rechazos de evidencia** se pueden desplegar el campo, la entidad o afirmación, el fragmento, las citas disponibles, la regla y la sugerencia. Los códigos `outside_selected_evidence`, `auxiliary_only`, `name_not_attested` y `unknown_or_unoffered_id` distinguen problemas que antes se presentaban juntos.

## Resultados comparables

| Medición | 2.4.1 Windows, reportado | 2.4.2 con respuestas simuladas |
| --- | --- | --- |
| Modelos | Qwen3:4b y Qwen3:8b | Respuestas de prueba controladas |
| Duración | 90,16 s y 160,67 s | Sin estimación de latencia de inferencia |
| Llamadas | 5 por ejecución | 1 para recuperar un tema ya respaldado. 4 de extremo a extremo en la reconstrucción con reparación y redacción |
| Reparación repetida | 2 intentos temáticos por ejecución | 1 intento idéntico y luego reformulación conservadora en el caso reconstruido |
| Variantes válidas | 0 por ejecución | 3 en la reconstrucción de extremo a extremo. 28/29 clips elegibles en el corpus simulado de 32 entradas |

La regresión completa registró **662 pruebas aprobadas, 0 fallidas y 1 omitida**. La omitida requiere DPAPI nativo de Windows. Los ensayos controlados verifican la recuperación de un alcance demasiado estrecho, la detección de nombres auxiliares, el rechazo de IDs inválidos y la separación de dos acontecimientos. La precisión temática, los falsos positivos y el tiempo sobre el clip original se deben medir en Windows con ambos modelos reales.

## Repetir en Windows

1. Actualiza `feature/anqora-media-studio-v2` con **Fetch origin** y **Pull origin** en GitHub Desktop. Reinicia Studio y conserva el MP4, el SRT y los metadatos del proyecto.
2. Ejecuta `ollama list` y comprueba que estén disponibles `qwen3:4b` y `qwen3:8b`. En CMD desde el repositorio, usa el mismo MP4 y el mismo presupuesto en ambos comandos. El ejemplo usa 180 s para ambos. Si utilizaste otro valor en la comparación inicial, reemplázalo en ambos comandos.

   ```bat
   .venv\Scripts\python.exe tools\benchmark_editorial.py --provider ollama --model qwen3:4b --clip "C:\ruta\al\proyecto\clips\clip.mp4" --timeout 180 --output editorial-242-4b.json
   .venv\Scripts\python.exe tools\benchmark_editorial.py --provider ollama --model qwen3:8b --clip "C:\ruta\al\proyecto\clips\clip.mp4" --timeout 180 --output editorial-242-8b.json
   ```

3. Comprueba que ambos JSON tengan el mismo `transcript_id` y los mismos ajustes aparte del modelo. Anota tiempo, llamadas, reparaciones y variantes válidas. Lee las frases rechazadas junto a sus citas. Distingue los nombres respaldados, los rechazos que la transcripción sí justificaba, las afirmaciones sin apoyo y las asociaciones incorrectas entre acontecimientos.
4. Para comprobar Studio, selecciona el modelo y el mismo clip en **Automation → AI Engine**. Usa **Reprocesar resultados con puntuación cero** o **Regenerar** para repetir con el otro modelo. Compara los diagnósticos desde el historial. El benchmark y la revisión en AI Engine reutilizan el archivo local y no envían contenido a TikTok.
