# Automation 3.0 · Calidad, duplicados y selección

## Auditoría y cambios

| Componente | Control existente reutilizado | Cambio |
| --- | --- | --- |
| `automation.py` | Mínimo de detección, cupo y zona horaria validados | Producción lee estos ajustes al evaluar y recomendar. |
| `production.py` y `subtitle_quality.py` | Decodificación con FFmpeg, relojes A/V, subtítulos y cobertura de voz, reparación local con versiones separadas | Se exige un score real y se verifica también la continuidad de los paquetes de cada pista. Los cortes mayores de 0,50 s bloquean el clip. Si faltan datos para verificar una pista, el trabajo queda pendiente de revisión. |
| `score_clips.py` | Proporción de solapamiento respecto al intervalo más corto | Se usa un solapamiento superior al 55% para exportaciones de una fuente identificada como la misma. En Live la comparación se limita al reloj de la misma sesión. |
| `editorial_semantic.py` | Criterio de variante válida, puntuación editorial y contexto | La cola reutiliza el criterio vigente. Para recomendar se exige además contexto independiente y puntuaciones disponibles. La puntuación editorial se presenta separada de la detección. |
| `production_store.py` y Centro de operaciones | Cola, bloqueo por trabajador, historial y revisión manual en Publicaciones | SQLite guarda evidencia por trabajo y ejecución. El panel muestra decisión, motivo, prioridad y enlace al duplicado. |

## Evaluación y selección

1. Una detección por debajo del mínimo se descarta antes de abrir un trabajo editorial. Un clip de datos incompletos queda en revisión. Solo se recomienda un archivo con video y audio decodificables, relojes alineados dentro de 0,22 s, continuidad verificable, subtítulos válidos y evidencia de cobertura de voz.
2. Antes de enviar un clip técnicamente apto a análisis editorial, se compara el SHA-256 del MP4 y, cuando existe una variante corregida, el del original y el de esa variante. También se comparan intervalos de la misma fuente y transcripciones locales. La comparación textual exige al menos 24 palabras, muchos grupos distintos de cuatro palabras, longitudes comparables y la misma secuencia de palabras sustantivas. Solo se toleran pequeñas diferencias de vacilaciones. Un tema compartido, un nombre parecido o una cifra cambiada no bastan.
3. La primera exportación técnicamente verificada queda como referencia para comparaciones posteriores. En **Duplicados → Ver detalle** aparece el método y el clip relacionado. **Revisar como distinto** permite reevaluar ese par de archivos. Se mantiene la evidencia anterior y la excepción queda acotada a las huellas de ese par.
4. Se calcula prioridad solo tras aprobar los controles. La fórmula es 45% detección, 35% editorial, 10% contexto y 10% diferenciación. La diferenciación descuenta la mayor similitud de grupos de cuatro palabras frente a exportaciones verificadas previamente. Con evidencia textual insuficiente para comparar, se usa un valor neutral. Se desempatan por detección, puntuación editorial e ID.
5. Se recomiendan hasta tres clips completados en la fecha local configurada, respetando el cupo cuando sea menor. La lista puede cambiar si termina un clip de mayor prioridad. Los demás clips aptos siguen disponibles para revisión manual. La lista es de revisión, y sus entradas nunca consumen cupos de publicación ni programan envíos.

## Reutilización e historial

Se reutiliza el control técnico cuando el MP4 original, la variante validada y sus archivos asociados conservan el sello registrado. Un cambio retira el clip de recomendaciones hasta reevaluarlo. Se reutiliza el resultado semántico compatible con el contenido, idioma, perfil, pesos, identidad, versión y modelo guardado sin consultar Ollama. Si el catálogo local ya indica un digest distinto, se considera obsoleto. El historial previo permanece accesible. Los trabajos listos que carecen de evidencia de continuidad y deduplicación de 3.0 aparecen para reevaluación; se pueden comprobar con **Reevaluar**.

## Simulación y comprobación en Windows

El escenario reproducible de 20 clips usa MP4 sintéticos, FFmpeg y FFprobe reales, el servidor local y SQLite. La actividad de voz, la retranscripción y Ollama usan dobles controlados. Incluye archivos válidos, reparaciones, descartes, desfase A/V, errores editoriales y una cancelación.

| Decisión | Clips |
| --- | ---: |
| Recomendados | 3 |
| Aptos para revisión manual fuera del cupo | 7 |
| Duplicados | 4 |
| Revisión requerida | 3 |
| Descartados, incluida una cancelación | 3 |

Las cuatro variantes duplicadas se detuvieron antes del motor editorial. Las pruebas focalizadas también cubren mínimo de detección, MP4 defectuoso, subtítulos inválidos, huecos en la continuidad, falsos positivos textuales, zonas horarias, historial tras reinicio y reutilización con un proveedor que rechaza nuevas consultas. Chromium comprueba la tabla, el filtro de recomendados y el paso de revisión a Publicaciones.

```bat
.venv\Scripts\python.exe -m pytest -q tests/test_production.py tests/test_production_selection.py
```

1. En GitHub Desktop actualiza `feature/anqora-media-studio-v2` y abre Studio.
2. En **Automation**, comprueba el mínimo, el cupo y la zona horaria. Abre **Centro de operaciones**. Añade un clip terminado a producción o reevalúa uno del historial.
3. Usa **Recomendados**, **Revisión requerida**, **Duplicados** y **Descartados**. Abre el detalle para comparar puntuaciones y comprobar los motivos. En un duplicado, inspecciona el archivo asociado antes de solicitar revisión como distinto.
4. En un clip apto, abre **Revisar en Publicaciones** y comprueba que el consentimiento se solicite antes de cualquier envío. Puedes terminar la validación en esa pantalla.

El corpus es sintético. La comprobación A/V mide relojes y continuidad de pistas. La sincronía labial y la continuidad narrativa requieren revisión humana. La evaluación con exportaciones reales, el rendimiento en Windows y la calidad de sus propuestas con Ollama local quedan pendientes de validación.
