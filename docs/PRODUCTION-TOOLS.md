# Herramientas de producción de Anqora Media Studio

## Plantilla global

En **Configuración → Apariencia y marca → Plantilla de exportación**, elige la relación de aspecto, marco, estilo de subtítulos, marca guardada, encuadre, uso de la marca de agua y perfil. Activa **Aplicar plantilla a todos los proyectos** y guarda. Studio guarda la configuración versionada en `.export-template.json` dentro de la carpeta de proyectos. La próxima exportación usa los ajustes vigentes. La configuración aplica también a campañas que exportan mediante el pipeline de proyectos y a nuevas exportaciones de Live Studio.

En **Proyectos → Ajustes de exportación**, activa **Usar ajustes propios de este proyecto** y guarda para crear una excepción. El resumen indica los ajustes efectivos antes de exportar. En el editor de un clip puedes elegir el formato y perfil de esa exportación. Signature usa sus franjas oficiales en 9:16. En otros formatos el resumen advierte que omitirá las franjas y, si es necesario, usará un solo encuadre. Los archivos y metadatos de exportaciones anteriores se conservan.

## Exportación y rendimiento

**Alta calidad** conserva el perfil anterior de FFmpeg, libx264 `veryfast` y CRF 20. **Rápido** usa libx264 `ultrafast` y CRF 20. Ambos conservan resolución, encuadre, composición, subtítulos y audio configurados. Los archivos del perfil Rápido pueden ocupar más espacio.

En el editor selecciona **Exportar a Clips**. Si la edición coincide, Studio verifica el archivo y lo reutiliza en la Biblioteca con el mismo nombre. La caché valida la identidad de la fuente, el intervalo, los subtítulos relevantes, la composición, el perfil y los archivos generados. Una variante con ajustes distintos se guarda con otro nombre. En proyectos antiguos con un MP4 local íntegro también se puede reutilizar la versión actual. Para aplicar una plantilla nueva a un clip antiguo tiene que estar disponible su fuente original.

Studio muestra la etapa, el avance informado por FFmpeg y los tiempos de etapas completadas. El tiempo restante aparece cuando hay suficientes muestras para estimarlo. **Cancelar exportación** detiene la codificación cuando está en curso o descarta el resultado antes de su traslado. Una comprobación puntual de fuente o cámara puede terminar antes de que se refleje la cancelación. La exportación por proyecto muestra además **Registro del trabajo**, con el diagnóstico local de FFmpeg.

**Comprobar NVENC**, en Apariencia, inicializa el codificador con un cuadro sintético y muestra el resultado del FFmpeg instalado. Los dos perfiles usan libx264. La incompatibilidad NVENC 13.1 frente a 13.0 aportada por el usuario se cubre con una respuesta simulada del codificador.

## Medición de CPU

AMD EPYC 9V74 con tres CPU lógicas disponibles en el entorno Linux, Python 3.11 y FFmpeg 7.1.5. Misma fuente sintética 640 × 360 con movimiento y audio, exportación 1920 × 1080 a 30 FPS con subtítulos incrustados. La tabla usa la mediana de tres repeticiones, en segundos. La base es `4333fb8e2914d3fb483827550bd4e7fa017d8d3d`.

| Clip | Base, nueva exportación | Actual, Alta calidad | Actual, Rápido | Base, misma edición desde Clips | Actual, misma edición desde Clips |
| --- | ---: | ---: | ---: | ---: | ---: |
| 6 s | 3,118 | 3,059 | 1,588 | 3,085 | 0,080 |
| 18 s | 8,623 | 8,268 | 4,574 | 8,800 | 0,079 |

Los tiempos de nuevas exportaciones incluyen inspección, preparación de filtros, codificación, miniatura y metadatos. Una ejecución de Alta calidad de 18 s registró 0,066 s de inspección, 0,002 s de filtros, 7,470 s de codificación y 0,728 s de metadatos. La fuente ya coincidía con el encuadre estable y no necesitó detección de sujetos. En este material, el clip de 18 s ocupó 8,44 MB en Alta calidad y 21,48 MB en Rápido. Las cifras corresponden a este entorno y a esta fuente.

Para reproducir en Windows desde la raíz del repositorio, puedes crear un checkout separado de la base y ejecutar el benchmark:

```powershell
git worktree add --detach ..\anqora-export-base 4333fb8e2914d3fb483827550bd4e7fa017d8d3d
.\.venv\Scripts\python.exe .\tools\benchmark_exports.py ..\anqora-export-base --runs 3
```

## Borradores del calendario

En **Publicaciones**, el filtro **Solo borradores sin programar** y el filtro de plataforma acotan las tarjetas. Usa **Eliminar borrador** en una tarjeta o marca varios borradores. **Seleccionar borradores filtrados** incluye solo los borradores elegibles e indica cuántos seleccionaste. La confirmación muestra la cantidad y el resultado identifica cualquier plan cuya revisión cambió o cuya entrega ya lo protege. La acción solo elimina los datos locales del plan. Conserva el historial de las publicaciones confirmadas y los MP4.

## Canales monitoreados de YouTube

Para clasificar oportunidades a partir de metadatos públicos, abre **Content Discovery**. Los canales registrados allí comienzan en **Solo descubrir**, con una frecuencia de 180 minutos. [Guía de Content Discovery](CONTENT-DISCOVERY.md)

Abre **Proyectos → Canales monitoreados**. Puedes agregar un identificador `UC…`, un `@handle` o una URL HTTPS oficial con `/channel/UC…` o `/@handle`. El monitor registra las entradas de la primera consulta como referencia. Elige **Importar último video** si también quieres incorporar el más reciente de esa referencia inicial. **Revisar ahora** solicita una consulta y respeta el intervalo mínimo manual y el periodo de espera impuesto por YouTube.

La detección consulta el feed Atom público de YouTube. El flujo básico no requiere una clave. Cuando dispones de derechos, marca la autorización y documenta su origen. Al incorporar un video, Studio comprueba con yt-dlp sin cookies ni configuración de usuario que pertenece al canal, es público y está disponible como grabación. La ingestión conserva Whisper Automático con respaldo CPU. Puedes limitar el número de clips, elegir duración, plantilla y categoría, y decidir si quieres exportar automáticamente los clips que superen el umbral. Los resultados quedan para revisión editorial.

SQLite guarda identificadores, fechas, intentos, estados y vínculos con proyectos. La cola comparte la ejecución local de Studio. Ante un fallo guarda el intento y aplica espera progresiva. Comprueba el error del canal y la fecha del próximo intento para diagnosticar rate limits. Puedes pausar el canal para evitar nuevos trabajos. Si se suspende Windows o cierras Studio, se reanuda la consulta al volver a abrir. El alcance de recuperación depende de los videos que el feed todavía muestre y de los trabajos que ya se hayan guardado en SQLite.

En este entorno se usaron feeds controlados para verificar la recuperación y la deduplicación. La conexión de prueba al feed oficial falló antes de recibir datos. En Windows, prueba con tu canal, espera a que se guarde su referencia e importa un video autorizado. Para probar la deduplicación, reinicia Studio y consulta la lista del canal y la de proyectos. El adaptador público actual no incorpora YouTube Data API.

## Mejores candidatos

Si el proyecto tiene ventanas válidas pero ninguna supera el umbral, Studio muestra **Mejores candidatos para revisión** cuando hay opciones con suficiente contexto y cierre. Se muestran hasta cinco ventanas diferenciadas con su puntuación real, intervalo y motivo. Cada una requiere selección explícita. Activa **Filtro estricto de puntuación** en el proyecto para mostrar solo los resultados que superen el umbral. Si también falta evidencia contextual, la lista de recomendaciones puede quedar vacía.

## Detener y reanudar un proyecto

**Detener proyecto** aparece mientras Studio tenga una tarea del proyecto en cola o en curso. El estado **Cancelando** se muestra hasta que el trabajo confirma la cancelación. Después, **Reanudar proyecto** recupera el modo, el formato y la selección de la ejecución anterior. Una reanudación conserva la fuente terminada, la transcripción completa o sus bloques guardados, los checkpoints de descubrimiento y los MP4 válidos. Puede repetir el bloque de Whisper que quedó en curso.

La cancelación de FFmpeg y yt-dlp se limita a los procesos de la tarea. Whisper espera un límite seguro de inferencia antes de parar, tanto en CPU como en CUDA. La espera puede durar hasta que termine el segmento actual; también puede esperar a que devuelva el control una carga de modelo. El editor de Clips mantiene su propio botón de cancelación para exportaciones individuales.
