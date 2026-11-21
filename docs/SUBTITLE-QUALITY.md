# Automation 2.1 · Calidad y reparación de subtítulos

## Auditoría del incidente

El punto de partida fue `2dde238`. El reporte de Windows encontró tres subtítulos en el clip convencional `007-17c884f9-yo-le-había-dado-un-poco.mp4`. El segundo, «de agua», cubría 68,44 s. Las tres variantes tenían 0/10 y el panel eligió una. Los tiempos de esa exportación se reprodujeron en un SRT sintético. Para verificar la procedencia de los tiempos del clip de Windows hacen falta su MP4, su SRT y el `transcript.json` original.

| Etapa | Hallazgo verificable |
| --- | --- |
| Descarga y audio | Los proyectos convencionales cortos descargan el video completo. Los largos pueden transcribir una descarga de audio y obtener el video por tramos al exportar. `download_and_transcribe.py` usa Faster-Whisper directamente sobre la fuente corta. Para fuentes largas extrae audio por bloques solapados. |
| VAD y tiempos | Faster-Whisper recibe `word_timestamps=True` y `vad_filter=True`. En modo bloque se suma el desplazamiento del bloque a los segundos obtenidos y se filtran duplicados en la unión. `transcript.json` guarda tiempos de palabras y segmentos en segundos absolutos. Esas etapas también pueden producir omisiones o tiempos anómalos. El caso sintético reproduce el defecto aunque las palabras de entrada tengan tiempos correctos. |
| Intervalo de clip | La selección guarda los límites absolutos. Al usar un tramo de video, FFmpeg busca desde el inicio del clip menos el inicio del tramo. Si se eliminaron silencios con `tighten`, las palabras se remapean a los intervalos conservados. Para los subtítulos se resta el comienzo del clip o se usa el reloj de ese montaje. |
| SRT y ASS anteriores | El SRT agrupaba por cantidad fija de palabras y tomaba el inicio del primer token y el final del último, incluidos los huecos entre ambos. El ASS prolongaba el resaltado de cada palabra hasta el inicio de la siguiente del grupo. Con «agua» de 1,05 a 1,55 s y la palabra siguiente a 69,54 s, el ASS anterior la resaltó hasta 69,54 s y el SRT produjo un bloque de 0,84 a 70,10 s. |
| FFmpeg y archivos asociados | FFmpeg quemaba los eventos ASS tal como se le entregaban. Los clips anteriores no guardaban el ASS temporal. Las nuevas exportaciones guardan ambos archivos de subtítulos y hashes SHA-256 del SRT, el ASS y el MP4 en los metadatos de cada exportación. Una edición posterior de `transcript.json` se señala en el diagnóstico cuando el hash de palabras cambió. |
| AI Engine | Aceptaba un SRT con tiempos válidos sintácticamente aunque una sola frase durase 68,44 s. El ensamblador elegía el máximo incluso si todas las variantes valían cero. Ahora valida la pista antes de usar la caché y comprueba el MP4 y la cobertura de voz en el trabajador antes de invocar Ollama o las reglas locales. Si el mejor resultado, la evaluación editorial o la confianza son cero, el panel muestra «No hay una propuesta editorial válida para recomendar». El historial y las ediciones se conservan. |

## Generación y validación

El SRT y el ASS usan un mismo conjunto de palabras y límites. El ajuste inicial apunta a 500 ms de mínima, 4.000 ms de máxima, dos líneas y un nuevo subtítulo tras una pausa de 650 ms. Esos valores se ajustan en **Captions** o al regenerar subtítulos en **Automation**. La mínima es una meta de legibilidad. Un token breve, una pausa o el borde del clip pueden acortarla. El resaltado usa el fin real de cada palabra y puede cubrir transiciones de hasta 150 ms con la siguiente. Afronta los cortes y las pausas sin añadir diálogo. Un tiempo de palabra anómalo se señala para revisión.

**Calidad de subtítulos** informa el modelo cuando se conoce, origen y versiones, duración del MP4, cantidad de bloques SRT y palabras, subtítulo más largo, cobertura de la voz detectada y tiempo de diagnóstico. Verifica orden, solapamientos, tiempos negativos, duración fuera del MP4 y eventos prolongados. En un SRT anterior a esta versión, superar 4 s genera un aviso de legibilidad y superar 8 s bloquea el análisis. Los nuevos usan el máximo configurado en su manifiesto. La cobertura se calcula sobre intervalos detectados por Silero VAD en audio mono de 16 kHz. Señala 1,5 s seguidos de voz sin texto, o cobertura inferior al 65 % si detectó al menos 2 s de voz y falta al menos 1 s. Un silencio genuino entre frases puede ocupar la mayor parte del clip. Si el VAD no confirma voz, el panel indica que hay que cotejar el audio y el texto. Un clip con errores de tiempos se bloquea antes de medir cobertura.

La caché de render tiene una versión nueva y conserva las descargas, transcripciones y la caché de descubrimiento. La caché editorial usa la versión de la validación y la identidad del archivo. Para trabajos antiguos que quedaron pendientes, vuelve a solicitar el análisis si Studio pide verificar el clip con la versión actual.

## Actualizar y reparar en Windows

Cierra el proceso de Studio con Ctrl+C. Confirma que GitHub Desktop muestra la rama de desarrollo y usa **Fetch origin** y **Pull origin**. En PowerShell, desde la carpeta del repositorio, el equivalente es:

```powershell
git switch feature/anqora-media-studio-v2
git pull --ff-only origin feature/anqora-media-studio-v2
.\.venv\Scripts\python.exe dashboard.py
```

1. Abre `http://127.0.0.1:8765` con la misma carpeta de proyectos. En **Automation → AI Engine**, selecciona el MP4 afectado y abre **Calidad de subtítulos**. Pulsa **Diagnosticar subtítulos**. Si el SRT original coincide con el reportado, debe marcar el bloque de 68,44 s e impedir generar propuestas con ese archivo.
2. Selecciona un modelo de **Faster-Whisper** ya descargado, por ejemplo el utilizado en ese proyecto, y pulsa **Retranscribir audio final**. Es un modelo distinto de Qwen en Ollama. Abre el texto con sus tiempos, compáralo con el audio y descarga el SRT si lo necesitas. Si cambias los ajustes de tiempos, genera otro borrador. Studio guarda la última retranscripción de cada clip localmente.
3. Pulsa **Previsualizar versión corregida**. El video de previsualización se crea desde la fuente limpia con la pista de audio original. Si falta el video limpio, conserva el borrador y recupera la fuente. Para proyectos largos, puedes dejar una copia local completa con el nombre `source_full.mp4` en la carpeta del proyecto correspondiente usando el mismo contenido y la misma línea de tiempo. Comprueba que coincidan el principio, «de agua», el contenido hablado posterior y el final.
4. Pulsa **Exportar nueva versión**. Studio genera otro nombre y coloca el clip convencional en **Publicaciones** y **AI Engine**. El clip anterior y su SRT siguen disponibles. El export de una ventana Live conservada se descarga desde el panel de reparación. El flujo habitual de TikTok exige la revisión y el consentimiento por separado.
5. Selecciona la versión nueva en AI Engine. Comprueba primero la cobertura y los tiempos. Después solicita propuestas para esa versión y revisa las puntuaciones por separado. Si una propuesta se completó técnicamente con puntuación cero, comprueba que el panel indique que no hay una recomendación válida.

Si no hay una fuente limpia verificable, o si la duración del clip revela cortes cuyos tiempos no se pueden reconstruir, Studio permite recuperar el SRT y pide reexportar desde el proyecto. Volver a quemar texto sobre el MP4 anterior conservaría el subtítulo congelado en la imagen. La reparación usa la marca de agua configurada actualmente. En clips antiguos sin un manifiesto de render, usa el estilo del proyecto actual. Clips de más de diez minutos pueden recibir diagnóstico de tiempos, con comprobación de voz y retranscripción local limitadas a diez minutos.

## Evidencia y recursos

La regresión completa en Python 3.11 y Linux aprobó 592 pruebas, con 0 fallidas. Once pruebas nuevas reproducen los tiempos del reporte y cubren la reparación. Las pruebas verifican que ni Ollama simulado ni las reglas locales reciben esa transcripción y prueban español, conversación continua, cambios de interlocutor, solapamientos, límites del recorte y silencio. El fixture audiovisual usa FFmpeg y FFprobe reales. El audio de la versión corregida tiene el mismo hash SHA-256 de pista que el original, y ambas pistas del MP4 nuevo duran tres segundos con una diferencia menor de 50 ms. En la pausa a 1,60 s, el fixture original todavía tiene texto y el corregido queda en negro. A 2,50 s vuelven a aparecer subtítulos. Chromium recorrió el diagnóstico, la retranscripción simulada, la recuperación del borrador, la vista previa y la exportación sobre el servidor HTTP real.

- [Diagnóstico en Chromium](evidence/subtitle-quality/diagnostico-chromium.png)
- [Reparación en Chromium](evidence/subtitle-quality/reparacion-chromium.png)
- [Fotograma original durante la pausa](evidence/subtitle-quality/antes-silencio.png) · [Fotograma corregido durante la pausa](evidence/subtitle-quality/despues-silencio.png) · [Fotograma corregido durante el diálogo](evidence/subtitle-quality/despues-dialogo.png)

La prueba local con VAD real y un clip sintético de 9 s midió 0,370 s de tiempo transcurrido, 0,214 s de CPU del proceso Python y 125,3 MiB de memoria residente máxima del proceso, incluida la carga de Faster-Whisper y ONNX Runtime. El valor de memoria y CPU excluye el proceso hijo FFmpeg. Se detectó voz entre 1,136 y 4,560 s. Son mediciones en Linux de la comprobación de voz, con independencia de la inferencia de Whisper y el render.

Studio admite una reparación a la vez. Para la retranscripción carga en un proceso separado un modelo local, en CPU con INT8 y dos hilos, con un plazo máximo de cinco minutos. FFprobe y FFmpeg tienen plazos independientes durante el diagnóstico y la reparación. Se reutilizan FFmpeg, FFprobe y Faster-Whisper del entorno existente. Las pruebas de Ollama y la retranscripción de la interfaz usaron dobles, y la comprobación de voz, el render, la sincronización y Chromium se ejecutaron localmente. Queda por verificar la correspondencia completa con el audio hablado del incidente en Windows y medir allí el tiempo y la RAM de la retranscripción.
