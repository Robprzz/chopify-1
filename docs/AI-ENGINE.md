# Automation Fase 2 · AI Engine local

## Actualizar y conectar en Windows

Cierra Studio con Ctrl+C y ejecuta desde el repositorio.

```powershell
git switch feature/anqora-media-studio-v2
git pull --ff-only origin feature/anqora-media-studio-v2
.\.venv\Scripts\python.exe dashboard.py
```

Inicia Studio con la misma carpeta de proyectos que usabas. AI Engine funciona con las dependencias Python existentes. Si todavía falta `tzdata` para Automation Fase 1, instálalo cuando el equipo pueda verificar correctamente los certificados de tu red.

Para usar un modelo, instala Ollama con el instalador oficial, ábrelo y descarga manualmente una de estas variantes desde PowerShell. Qwen3 4B es el punto de partida con captura activa. Qwen3 8B es una alternativa para comparar la calidad. Los modelos locales compatibles de Llama, Gemma y otras familias pueden seleccionarse una vez instalados. [Instalación oficial de Ollama en Windows](https://docs.ollama.com/windows) · [Modelos Qwen3](https://ollama.com/library/qwen3)

```powershell
ollama pull qwen3:4b
ollama list
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

Para probar el modelo mediano usa `ollama pull qwen3:8b` en lugar del primero. Si el instalador no inició el servicio, abre Ollama o ejecuta `ollama serve`. En Studio, abre **Automation → AI Engine**, pulsa **Comprobar conexión y modelos**, elige el modelo y guarda. **Ejecutar prueba de generación** procesa un texto sintético. Revisa el proveedor y el diagnóstico de esa ejecución antes de evaluar el resultado.

| Estado | Qué comprobar |
| --- | --- |
| Ollama no disponible | Comprueba que el servicio esté iniciado y que el puerto local 11434 responda. El aviso de PATH distingue si Studio detecta el ejecutable. |
| Ollama sin modelos locales | Instala manualmente un modelo y actualiza la lista. Las variantes cloud se excluyen. |
| Modelo seleccionado ausente o cambiado | Actualiza la lista y vuelve a generar. El historial conserva el digest de la versión utilizada cuando Ollama completó el análisis. |
| Tiempo agotado | Prueba un modelo más pequeño o ajusta el tiempo máximo entre 5 y 180 s. |
| Respuesta inválida | El motor admite hasta un reintento dentro del mismo plazo. Si falla, el resultado se identifica como generado mediante reglas locales. |
| Clip sin diálogo | Revisa el SRT o vuelve a preparar el fragmento. El motor necesita texto verificable. |

El respaldo determinista se usa si eliges **Reglas locales** o si Ollama no puede completar el trabajo. Puedes regenerar con Ollama más adelante. Studio consulta y usa modelos instalados desde `http://127.0.0.1:11434`. El panel permite seleccionar modelos y configurar tiempos. La instalación y las descargas se hacen manualmente.

## Uso editorial

Elige un clip terminado y uno de los perfiles de **TikTok**, **Facebook Reels** o **YouTube Shorts**, y pulsa **Generar propuestas**. También puedes abrir AI Engine desde un clip de proyecto, Live o Publicaciones. Hay tres variantes con título, caption, descripción, hashtags, llamada a la acción opcional, puntuación y justificación. Puedes seleccionar una, editar sus campos, copiar el texto completo, restaurar su original o solicitar una versión nueva. El selector de historial permite volver a versiones anteriores.

El panel conserva el resultado generado y los cambios humanos por separado. Al encolar una regeneración, copia las ediciones guardadas y la selección de la última versión del mismo clip y plataforma. Restaurar elimina la edición de la versión abierta y recupera su texto original. Los campos editados por una persona se deben revisar antes de reutilizarlos.

Los límites de título, caption y hashtags son preferencias editoriales configurables. La puntuación pondera atención 25%, emoción 20%, relevancia 15%, curiosidad 15%, contexto 15% e interacción 10% al instalar. Los pesos se pueden cambiar y deben sumar 100. La confianza y la puntuación editorial son ayudas para revisión. La puntuación de detección se muestra por separado. El umbral de admisión de fases futuras y los controles de consentimiento, duplicados y límite diario permanecen en Automation Fase 1.

La casilla existente **Generar captions automáticamente para nuevas exportaciones** activa el procesamiento al terminar de exportar en Studio. Recibir clips Live depende también de la preferencia **Recibir clips de Live clipping**. Las exportaciones anteriores se pueden seleccionar manualmente. Las propuestas se copian como texto. El envío al buzón de TikTok sigue su flujo oficial de revisión y consentimiento. Los perfiles de Facebook y YouTube solo preparan texto.

## Arquitectura

- **Fuente** en `editorial_source.py`. Se reutiliza el resolver de exportaciones terminadas de Studio. El SRT tiene prioridad porque conserva los tiempos del MP4, incluidos los recortes. En proyectos con intervalo guardado y sin SRT se usa la porción correspondiente de `transcript.json`. Los clips Live leen también su puntuación del manifiesto. Corregir el texto del proyecto requiere regenerar su SRT para que la exportación existente utilice esa corrección. El análisis agrupa frases y muestrea a lo largo del intervalo sin exceder 29 extractos ni 4.800 caracteres de diálogo. Conserva tiempos y calcula un hash del texto del intervalo y otro de la muestra.
- **Proveedores** en `editorial_providers.py`. El contrato expone el catálogo y una generación estructurada con tiempo máximo y cancelación. El transporte HTTP apunta directamente al loopback, sin proxy ni redirecciones. Se comprueban los modelos mediante `/api/tags` y `/api/show`. Se pide un esquema en `/api/chat`, sin herramientas ni ejecución de comandos. La petición limita el contexto a 6.144 tokens, la respuesta del modelo a 1.600 y el cuerpo HTTP a 512 KiB. Cerrar el socket cancela la espera de Studio. El tiempo de liberación del cálculo depende del servidor Ollama. `keep_alive: 0` solicita descargar de memoria el modelo al terminar una respuesta. [Contrato de chat](https://docs.ollama.com/api/chat) · [Respuestas estructuradas](https://docs.ollama.com/capabilities/structured-outputs)
- **Composición** en `editorial_content.py`. El modelo elige índices de extractos y emite una evaluación con valores acotados. La aplicación ensambla los textos a partir de frases reales, vocabulario de la transcripción y plantillas del perfil. El fallback elige los extractos por frecuencia de términos y señales de puntuación. Los dos proveedores reutilizan el mismo esquema y el mismo compositor. Se eliminan URLs, patrones reconocibles de credenciales y fragmentos con apariencia de instrucciones. La entrada se separa del mensaje de sistema y la validación rechaza campos inesperados o índices fuera de la evidencia. La filtración es una defensa adicional. La validación estructurada impide que la respuesta se convierta en un comando o una llamada de red.
- **Persistencia** en `editorial_store.py`. La migración añade `editorial_config` y `editorial_jobs` a la base SQLite de Automation. El historial conserva estados, orígenes, hashes, plataforma, configuración aplicada, resultados, ediciones, revisiones, versión del prompt, modelo, tiempos y errores. La caché usa el clip, la transcripción, el perfil, las instrucciones, los pesos, el modelo y su digest, y la versión del prompt. Una regeneración explícita crea otra versión. Las transacciones impiden duplicar el mismo trabajo pendiente. Una reserva temporal permite que, tras una interrupción brusca, otro proceso recupere trabajos pendientes cuando expire. Al cerrar Studio normalmente, devuelve el trabajo activo a la cola. Las versiones previas de Fase 1 conservan su esquema.
- **Ejecución** en `editorial.py` y `dashboard.py`. Hay un trabajo editorial activo por carpeta de proyectos, hasta 16 trabajos en cola incluyendo el activo, y notificaciones de exportación acotadas. Una cola automática llena informa del problema. Los demás clips siguen disponibles para análisis manual. La cancelación y el trabajador editorial son independientes de FFmpeg y la captura. El proceso editorial lee texto y SQLite. Los MP4, subtítulos y publicaciones quedan bajo los flujos existentes.

El compositor recibe un contrato de extractos con tiempos, independiente del catálogo de Studio. Eso permite reutilizarlo cuando se incorpore Channel Scanner. Un futuro proveedor puede suministrar planes bajo el mismo esquema y añadirse con otra política de destino y presupuesto. El motor actual expone Ollama y reglas locales.

## Validación y recursos

La regresión completa ejecutó 581 pruebas con 581 aprobadas y 0 fallidas, usando Python 3.11 en Linux. Las pruebas de Ollama usan un servidor HTTP simulado en loopback. Chromium ejecutó el panel de Studio y verificó configuración, generación de tres variantes, selección, edición, portapapeles y recuperación del historial tras reiniciar. La prueba de Live usó HLS sintético y el fallback. Se verificaron el catálogo de Publicaciones, la migración de SQLite, el control de concurrencia, la caducidad de trabajos, la caché, la cancelación y la continuidad de la Fase 1, la marca de agua y Live clipping.

[Captura de Chromium con el servicio Ollama simulado](evidence/ai-engine/chromium-editorial.png)

En una medición de 12 trabajos por modo con un fragmento sintético de 8 s, la mediana del tiempo editorial fue de aproximadamente 0,36 ms para reglas y 2,76 ms para el servidor doble. El proceso de medición alcanzó alrededor de 25 MiB de memoria residente, incluida la infraestructura de prueba. Esos valores miden Studio y el contrato simulado. Inferencia, RAM y calidad con un modelo instalado se deben medir en el equipo que lo ejecuta. En esta validación el puerto habitual de Ollama estaba cerrado. La prueba con un modelo real en Windows queda pendiente.

Ejemplos del respaldo local con el perfil TikTok y la transcripción sintética de semillas.

| Variante | Caption |
| --- | --- |
| Curiosidad · recomendada | 👀 ¿Cómo guardar las semillas después de la cosecha? |
| Interacción | 💬 ¿Cómo guardar las semillas después de la cosecha? ¿Qué opinas? |
| Contextual | Deja secar las semillas y guárdalas en un frasco etiquetado. |
