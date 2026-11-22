# Chopify Studio · Automation 2.4

[Actualización 2.4.1 y reprocesamiento en Windows](EDITORIAL-241.md)

AI Engine y el Centro de operaciones comparten el motor semántico local. La generación automática distingue la calidad del archivo, el resultado editorial y la revisión de publicación. Las propuestas aptas pasan a la revisión humana de Publicaciones. El envío al buzón de TikTok conserva su inspección de MP4 y el consentimiento por clip.

## Auditoría de la versión anterior

La auditoría partió de `43b4755efce066cdb20755b680edd2f4d5a94107`. En Windows se reportaron 4 trabajos completados y 11 fallidos, una latencia media aproximada de 22,4 s y un resultado de puntuación cero para el clip #15. No había una copia reproducible de ese clip, de los prompts completos ni de las respuestas del modelo. Los hallazgos siguientes se verificaron en el código y con casos reproducibles.

| Hallazgo | Causa demostrada | Componente | Impacto | Solución | Prueba |
| --- | --- | --- | --- | --- | --- |
| Títulos y captions formados por el mismo fragmento | Los botones principales enviaban `semantic=false`. El compositor convertía índices de extractos en los tres campos de texto; las reglas elegían con frecuencia el mismo índice. | `editorial.js`, `editorial.py`, `editorial_content.py` | Un resultado como «Muy importante» podía verse como redacción editorial. Con Ollama instalado, el modo extractivo también podía limitarse a elegir índices. | Los botones principales usan redacción semántica con el modelo configurado o Qwen3:4b instalado. **Generar extractos** sigue disponible con un rótulo propio. | HTTP 2.4 con generación predeterminada y Chromium con respaldo explícito. |
| Trabajos completados con puntuación cero | `done` reflejaba la terminación del ejecutor. El compositor podía devolver puntuación cero y ninguna recomendación en ese mismo estado. | `editorial_store.py`, `editorial_content.py` | El contador de completados podía confundirse con calidad editorial. | Estado editorial separado y evaluación de resultados previos al leer SQLite. La aptitud exige una variante válida con puntuación positiva. | Historial con cero, filtro de reprocesamiento y control de aptitud. |
| Se perdía contexto y el histórico no cuantificaba cuánto | El extractivo limitaba cada fragmento a 165 caracteres y usaba hasta 29 extractos. El semántico recortaba cada subtítulo a 300 caracteres, mezclaba pasajes cortos y muestreaba bajo un presupuesto de 6.200–6.500 caracteres contando sobrecarga. El histórico solo conservaba el material seleccionado. | `editorial_source.py`, `editorial.py` | La cantidad real perdida depende del clip. No puede reconstruirse para el incidente de Windows con la información disponible. | Registro de cobertura, ventanas cronológicas completas y síntesis de todas las ventanas. Los subtítulos se conservan en disco. | Ventanas largas, citas con tiempos, cobertura insuficiente y multilingüe. |
| Un nombre dudoso invalidaba las tres propuestas | La comprobación de personas usaba una lista global. Cualquier fallo determinista o semántico causaba otro borrador completo. La afinidad de hashtags se comprobaba en conjunto, de modo que un hashtag válido podía ocultar otro sin relación. | `editorial_semantic.py` 2.2 | Mezcla de metadatos y fuentes, repetición de fallos y pérdida de textos correctos. | Evidencia contigua con tiempos, verificación por nombre y variante, revisión independiente y parche por campo. | Tildes, nombre fragmentado, nombre solo externo, cifra, etiquetas y variantes repetidas. |
| Manual, Live y cola podían recorrer motores diferentes | Producción activaba 2.2 cuando tenía modelo. Los botones manuales y la entrada automática básica usaban el contrato extractivo salvo petición explícita. Si fallaba el proveedor extractivo, entraban las reglas. | `editorial.py`, `production.py` | El mismo clip podía recibir tratamientos distintos y el operador no veía todas las etapas. | Decisión compartida sobre el motor y la aptitud. El fallo de un modelo configurado se conserva como diagnóstico; el operador puede solicitar extractos. | API manual, producción convencional y Live, modelo no disponible y contingencia local. |

## Arquitectura y criterios de decisión

1. **Comprensión y fuentes.** Se valida el SRT asociado al MP4 terminado. Se conservan orden, tiempos y etiquetas de interlocutor cuando el subtítulo las incluye. Se filtran instrucciones, enlaces y patrones de credenciales. El título, la descripción, el canal y los pasajes vecinos disponibles entran como contexto auxiliar. Solo el texto del clip puede respaldar una afirmación.
2. **Evidencia.** Cada ventana produce tema, mensaje, intención, emoción, tono, idioma, audiencia, valor editorial, ambigüedades y citas sobre hechos atribuidos, opiniones, preguntas o interpretaciones. El registro asocia cada entrada con identificador, pasajes, tiempos, entidades, tipo y origen. Los nombres se cotejan como secuencias contiguas, con normalización Unicode de mayúsculas y tildes. Se respeta la `ñ`, y un apellido aproximado o comunicado por un interlocutor diferente no valida una identidad. Una interpretación se conserva con su categoría y no basta como soporte de una propuesta.
3. **Redacción.** Una petición corta redacta tres estrategias para el perfil activo de TikTok, Facebook Reels o YouTube Shorts. Devuelve título, caption, descripción, hashtags, CTA opcional, estrategia y referencias al registro. Conserva los límites configurados por plataforma. En clips largos, cada ventana de hasta unos 3,2 KB entra en la comprensión. Las síntesis agrupadas verifican los índices cubiertos, y las peticiones de redacción usan una selección acotada de citas del registro completo. Más de 20 ventanas, poca habla cubierta cuando se puede medir o evidencia demasiado escasa derivan a revisión o rechazo con diagnóstico.
4. **Validación.** Se comprueban estructura, campos obligatorios, longitud, frases vagas o repetidas, duplicación entre campos y entre variantes, etiquetas, cifras, nombres y atribución de afirmaciones sensibles. La consulta de revisión se construye por separado con únicamente las propuestas y las citas a las que apuntan. Evalúa fidelidad, originalidad, redacción, relevancia, contexto, ajuste a plataforma y otros criterios ponderados. La fidelidad debe ser al menos 7/10, cada criterio al menos 5/10 y la incertidumbre no puede ser alta. Para temas sensibles se exige atribución y se deriva a revisión si persiste incertidumbre. El resultado es una evaluación local, no una verificación externa de hechos.
5. **Autocorrección.** Un defecto se asocia a una variante y un campo. Solo se aplican valores nuevos que correspondan al parche pedido y pasen el esquema. Después se comprueba la variante completa con sus pruebas y citas; se conservan las demás propuestas que ya pasaron. Si queda al menos una variante válida, se recomienda la de mayor puntuación ponderada. Si no queda ninguna, se requiere revisión.
6. **Persistencia y publicación.** SQLite mantiene la ejecución, la decisión editorial, las etapas, el digest de modelo, el prompt, los tiempos, las llamadas, las incidencias y los campos corregidos. La selección de una propuesta permite llevar su descripción a Publicaciones. La revisión local exige el mismo MP4 compatible, y el consentimiento se abre sin marcar. Los perfiles de Reels y Shorts solo se aplican a la redacción.

El máximo de autocorrección es configurable entre 0 y 3 (2 por defecto). El presupuesto total de inferencia se comparte con los reintentos estructurales y cada petición recibe solo el tiempo restante. Se mantiene el transporte oficial local de Ollama, con contexto de 6.144 tokens, máximo de 1.600 tokens de salida por petición, sin herramientas, sin modelos remotos y con un solo trabajador. El flujo mínimo usa tres llamadas (comprensión con evidencia, redacción y revisión); dividir la revisión o sintetizar muchos pasajes añade llamadas. El transporte pide liberar memoria al terminar cada llamada. No hay una medición de RAM o coste de carga con Qwen en este entorno.

### Historial y métricas

**Opciones de generación y recuperación** permite reprocesar fallos o resultados de puntuación cero sin volver a renderizar ni retranscribir el MP4. Con un clip seleccionado se filtra ese clip; **Todos los clips** solicita un lote global. Cada lote usa la cola de 16 pendientes. Regenerar conserva los resultados previos. La vista muestra los IDs y la puntuación anterior. Se copian las ediciones vigentes desde el historial y se conserva cada versión original. La caducidad de la concesión de SQLite permite retomar un trabajo pendiente tras reiniciar. Para un resultado 2.4 puedes regenerar o corregir una variante sin repetir la comprensión si el clip, el perfil y el digest coinciden.

Las métricas usan hasta 1.000 trabajos terminales recientes. La finalización técnica se divide entre completados y fallidos. La tasa editorial se calcula entre resultados 2.4 con evaluación de variantes e incluye resultados derivados a revisión, excluyendo los casos de evidencia insuficiente. La tasa de reparación compara trabajos con algún campo revalidado contra trabajos que solicitaron un parche. Los avisos de fidelidad cuentan incidencias de controles o revisión, no hechos independientemente verificados. El p95 se muestra a partir de 20 tiempos medidos. La tasa de originalidad y la diferenciación se comparan mediante el benchmark independiente.

## Benchmark reproducible

La muestra incluye 32 transcripciones sintéticas, 29 elegibles y 3 con evidencia insuficiente. Incluye nombres ficticios, contenido sensible, ASR imperfecto, mezclas de idiomas y muestras en inglés, árabe, chino y japonés. El programa carga el código 2.2 desde la revisión auditada. El modo simulado usa las reglas extractivas y respuestas deterministas para los motores semánticos. Aplica a los dos motores un conjunto predefinido de defectos, incluidos casos en los que 2.2 corrige tras un reintento. La rúbrica de títulos, captions, descripciones, hashtags, diferenciación y gancho usa etiquetas del corpus y no usa las notas del generador. Sus controles de fidelidad cubren las cifras y la identidad ficticia inyectadas. No se realizó una evaluación humana independiente.

| Medición | Reglas extractivas | Semántico 2.2 | Semántico 2.4 |
| --- | ---: | ---: | ---: |
| Clips utilizables / 29 elegibles | 0 | 18 | 28 |
| Casos sin evidencia rechazados / 3 | 0 | 3 | 3 |
| Título / caption / descripción que pasan, al menos en una variante | 28 / 21 / 27 | 19 / 19 / 19 | 29 / 29 / 29 |
| Etiquetas / originalidad / diferenciación / contexto independiente | 0 / 0 / 0 / 27 | 19 / 19 / 19 / 19 | 29 / 29 / 29 / 29 |
| Errores de fidelidad detectados en variantes entregadas | 0 | 0 | 0 |
| Fallos técnicos por calidad devueltos por el motor | 0 | 13 | 0 |
| Clips utilizables después de reintento o reparación | 0 | 2 | 12 |
| Llamadas al proveedor simulado | 0 | 66 | 115 |
| Mediana / p95 local del benchmark, aprox. | 0,17 / 0,38 ms | 0,51 / 0,96 ms | 2,74 / 7,88 ms |

La rúbrica marcó como no utilizable una transcripción japonesa en 2.4 porque ninguna variante reunió simultáneamente sus longitudes mínimas y sus demás criterios. El 28/29 describe esta simulación. Estas latencias miden el código local con respuestas prefijadas; no estiman la inferencia, y 2.4 requiere más llamadas que 2.2. La meta del 90% queda pendiente de la prueba con Qwen3:4b y de revisión humana.

[Corpus etiquetado](evidence/editorial-24/corpus.tsv) · [Resultados por caso](evidence/editorial-24/benchmark-simulated.json)

### Ejemplos antes y después

- **Podcasts y nombres externos.** El resultado extractivo truncaba el título en «… perdió la parada del…». 2.2 descartó las tres variantes al encontrar el nombre ficticio de los metadatos. 2.4 registró `unsupported_entity`, reformuló el título y conservó la descripción que explicaba que el viajero perdió la parada por mirar el teléfono.
- **Taller sobre sombras.** El borrador 2.2 repetía «¿Qué detalle cambia la forma de ver el tema: sombra?» en título y caption. 2.4 señaló solo el caption en `duplicate_copy`; el título y la descripción siguieron idénticos en el ensayo.
- **Regresión en Studio.** El doble de Ollama rechazó el título de curiosidad de un clip sobre albahaca. Chromium mostró el campo reparado y su evidencia. A continuación abrió el mismo MP4 en Publicaciones con la descripción rellenada y el botón de envío deshabilitado.

[Centro de operaciones en Chromium](evidence/editorial-24/operations-chromium.png) · [Diagnóstico de la reparación en Chromium](evidence/editorial-24/semantic-chromium.png) · [Publicaciones y consentimiento](evidence/editorial-24/consent-chromium.png)

## Actualización y validación en Windows

1. Cierra Studio. En **GitHub Desktop**, selecciona `feature/anqora-media-studio-v2`. Pulsa **Fetch origin** y luego **Pull origin** si aparece. Confirma en **History** que está el commit de Automation 2.4.
2. Abre CMD en la carpeta del repositorio. Con el entorno virtual existente, ejecuta `"Setup Chopify Studio.cmd"` o abre el acceso directo habitual. Verifica que Studio abra en `http://127.0.0.1:8765`.
3. En CMD, ejecuta `ollama list`. Si Qwen3 4B falta, puedes descargar únicamente ese modelo de forma manual con `ollama pull qwen3:4b`. Abre Ollama o ejecuta `ollama serve` si aún no está iniciado. En Studio entra en **Automation → AI Engine**, pulsa **Comprobar conexión y modelos**, selecciona `qwen3:4b` y guarda. [Instalación oficial de Ollama para Windows](https://docs.ollama.com/windows)
4. Ejecuta el benchmark local con el modelo instalado. El programa compara las tres rutas con el mismo modelo, sin publicar, transcribir ni renderizar. Los 120 s de este ejemplo son el máximo por ejecución editorial en el benchmark. El informe mide las llamadas y las latencias reales para tu equipo.

   ```bat
   .venv\Scripts\python.exe tools\benchmark_editorial.py --provider ollama --model qwen3:4b --timeout 120 --output editorial24-qwen3-windows.json
   ```
5. Revisa los 3 casos rechazados y lee una muestra que incluya nombres, temas sensibles y el caso japonés. Contrasta los resultados con el corpus y verifica manualmente sentido, hechos, identidad y calidad de redacción, incluida la recomendación. Anota mediana, p95, número de reparaciones y casos que necesitan reescritura.
6. En AI Engine selecciona el clip histórico con puntuación cero. En **Opciones de generación y recuperación**, pulsa **Reprocesar resultados con puntuación cero**. Compara el trabajo nuevo con su versión anterior y verifica que el MP4 siga disponible. Si detectas un campo dudoso, usa **Corregir variante**, **Regenerar variante** o edítalo.
7. Para comprobar la barrera de consentimiento, pulsa **Revisar en Publicaciones** en una propuesta válida. Verifica el vídeo, el texto precargado y la casilla desmarcada. Termina la prueba en esa pantalla y verifica que no haya un envío nuevo en el historial.

La regresión completa obtuvo **634 pruebas aprobadas, 0 fallidas y 1 omitida** con Python 3.11.18 en Linux. Las pruebas de interfaz usaron Chromium y el servidor HTTP real, SQLite local, FFmpeg/FFprobe reales y un servicio Ollama simulado. La prueba nativa de protección DPAPI requiere Windows. El servicio Ollama real no estaba disponible en el entorno de desarrollo. La aceptación en Windows y la evaluación con Qwen3:4b real siguen pendientes. No se enviaron vídeos a TikTok.
