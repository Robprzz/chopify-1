# Live Clipping 1.2

## Captura y producción

El flujo anterior esperaba hasta cinco segundos a la transcripción Live antes de exportar. Una cola de Whisper con 139 segundos de retraso podía dejar el MP4 sin subtítulos y los cortes dependían del preroll más cinco segundos de cierre. Live 1.2 separa el detector, la preservación de MPEG-TS, la transcripción de producción, la edición, el control de calidad y el ingreso a Automation.

| Perfil | Rango final | Selección |
| --- | --- | --- |
| 30–60 s | 30–60 s | Prefiere un cierre cerca de 45 s. |
| 60–90 s | 60–90 s, hasta 180 s | Predeterminado. Puede extenderse si el diálogo continúa o no hay un cierre apropiado en el rango preferido. |
| 90–180 s | 90–180 s | Prefiere un cierre cerca de 130 s. |
| Automática | 30–180 s | Prefiere 60 s y busca un cierre entre 30 y 90 s antes de considerar más contexto. |

Se conserva el score real del momento detectado. El mínimo de producción inteligente es 6/10. La detección usa el mínimo de Automation si el campo queda vacío. El extractor busca puntuación, pausas naturales y continuidad de vocabulario temático alrededor del momento, incluso con transcripciones sin puntuación. Puede rechazar un momento si no encuentra un cierre fiable. La selección temática usa heurísticas locales y puede requerir revisión en conversaciones superpuestas o con muchos cambios de interlocutor.

Studio conserva segmentos cerrados en el mismo volumen con enlaces físicos cuando el sistema lo permite y copia local cuando no puede enlazarlos. La captura sigue escribiendo mientras se trabaja. Los trabajos se guardan en `live/production.sqlite` y conservan el material por generación. Una reconexión cierra la ventana acumulada. Si la fuente anterior reunió el mínimo y contiene el momento, puede continuar en la cola. Si falta contexto o hay un salto, se registra el rechazo. El reinicio recupera trabajos y transcripciones ya guardadas. Un trabajo interrumpido puede tardar hasta 45 s en recuperarse por la concesión que impide procesarlo dos veces.

## Recursos y límites

- Hasta cuatro trabajos activos por carpeta de proyectos y un trabajador de producción. Cuando la cola está llena, el panel muestra el motivo y la captura continúa. Los límites predeterminados son seis admisiones por hora por sesión y sin tope por sesión. El valor cero en los campos de hora o sesión activa modo continuo sujeto a la cola, el disco, el cooldown y la deduplicación. El límite de recomendación de Automation y el consentimiento de publicación son independientes.
- Live conserva hasta 768 MiB de fuente por trabajo. La cuota global de 8 GiB incluye las ventanas Live conservadas y reserva espacio para el trabajo en curso. Si una fuente nueva compromete la cuota o hay menos de 512 MiB libres, se rechaza la admisión. Las salidas guardadas y diagnósticos quedan disponibles para el usuario. Se eliminan los segmentos intermedios de un trabajo exitoso tras guardar su MP4 fuente.
- El análisis continuo usa Whisper `small` CPU/int8 en modo inteligente. La producción usa `small` en un proceso local con un modelo CPU reutilizable. Faster-Whisper procesa la fuente acotada en sus ventanas de inferencia internas y devuelve palabras con timestamps. La primera carga del modelo `small` utiliza el mecanismo habitual de faster-whisper si el modelo aún no está en caché. La inferencia de producción tiene un tiempo límite de 240–600 s según la duración. Al expirar se termina ese trabajador y queda la fuente preservada.
- **Automático** comprueba CUDA con CTranslate2. Si `nvidia-smi` está disponible, exige 2 GiB libres para intentar cargar el modelo en GPU. Si CUDA falta o falla, reintenta una vez en CPU. Whisper de producción, Ollama y el renderizado comparten el turno de trabajo de Studio. Los pesos GPU de Whisper se liberan al devolver el turno y Ollama conserva su política existente de descargar pesos al acabar. La captura y los analizadores de audio/video no usan ese turno. Otras aplicaciones pueden seguir ocupando memoria GPU.
- El panel muestra el dispositivo informado por el backend y el tiempo de inferencia. RTF se calcula como tiempo de inferencia dividido entre duración analizada. Menos de 1 indica procesamiento más rápido que el material. Las lecturas de `nvidia-smi` corresponden a toda la GPU. También se muestran el retraso y los segmentos omitidos del transcriptor Live.

## Calidad y clips anteriores

La producción verifica el intervalo seleccionado, la reproducción íntegra con FFmpeg, los relojes de audio y video y la continuidad de paquetes. Comprueba la cobertura de voz y la sincronía de SRT y ASS. Si hay voz y los subtítulos faltan o presentan errores críticos, el MP4 queda fuera del catálogo de clips terminados. Un fragmento sin voz detectada va a revisión en Automation. Los resultados válidos conservan el formato elegido, la marca de agua configurada y los subtítulos incrustados. Automation vuelve a aplicar sus controles de duplicados, calidad y admisión editorial. La exportación manual de emergencia de 30, 60 o 90 s conserva su comportamiento y no recibe score de detección.

Para los diez clips anteriores, comprueba si cada registro conserva su MP4 limpio en `live/<sesión>/windows/`. Con una fuente local alineada, Studio puede transcribir el clip y crear una variante corregida desde Subtítulos o con la corrección de Automation si cumple sus requisitos. Esa variante conserva el original. El contexto fuera de la ventana ya guardada solo puede recuperarse si se conserva la grabación de la transmisión. Si falta la fuente limpia, se puede reconstruir un SRT a partir del audio del MP4, pero Studio exige una fuente alineada para volver a incrustar subtítulos.

## Validación en Windows

1. Actualiza `feature/anqora-media-studio-v2` en GitHub Desktop. Abre Studio e inicia tu transmisión con **60–90 s** y dispositivo **Automático**. Deja que se completen el contexto y la cola.
2. En **Producción Live**, compara el retraso de Whisper Live con la inferencia de producción. Si el dispositivo informado es CPU, comprueba `nvidia-smi` y las bibliotecas CUDA/cuDNN que requiere la versión instalada de CTranslate2 antes de probar de nuevo. La RTX y el driver por sí solos no confirman disponibilidad para Whisper.
3. Reproduce un clip en la pestaña nueva. Comprueba inicio y cierre, audio, subtítulos y marca de agua. Verifica en Automation que conserva el score y queda sujeto a revisión. Puedes consultar también el informe local `quality.json` de la producción.
4. Repite con 90–180 s, interrumpe brevemente la conexión y vuelve a entrar en Live. Comprueba que la captura se recupera, los trabajos conservados siguen en la cola y el reproductor de una sesión previa sigue accesible. Usa **Exportar últimos segundos** si necesitas una salida de emergencia.

## Pruebas controladas

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_live_production.py tests/test_live_recovery.py tests/test_live_integration.py
```

FFmpeg, el servidor HLS local, la captura MPEG-TS, el render vertical y el control de los MP4 de aproximadamente 60, 90 y 180 s fueron reales. Se simularon la detección, las palabras y la actividad de voz en esas tres pruebas para controlar los timestamps y representar 139 s de atraso. La simulación de cola comprobó 12 trabajos terminados en una sesión. Se probó por separado Faster-Whisper real con `tiny` y 65 s de voz sintética repetida en Linux. El backend informó CPU y reutilizó el proceso, con RTF de 0,24 y 0,33 en dos inferencias. Esa medición no estima la exactitud de subtítulos ni el rendimiento de `small` en la RTX de Windows.
