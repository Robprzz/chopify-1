# Automation 2.2 · Producción supervisada

## Uso en Studio

Abre **Automation → Centro de operaciones**. Los clips terminados de proyectos y de Live clipping entran automáticamente en la cola. Studio también recupera los clips registrados en exportaciones anteriores del proyecto y en los manifiestos de Live. Para incorporar otro clip que ya esté en el catálogo, selecciónalo en **AI Engine** y pulsa **Añadir a producción**.

Un clip listo ofrece **Ver propuestas** para revisar o editar el texto y **Revisar en Publicaciones** para abrir ese archivo en el flujo de revisión. El estado **Listo para aprobación** significa que el control técnico y la revisión editorial local terminaron. El envío al buzón de TikTok requiere la aprobación explícita de la persona que revisó el archivo y la descripción. Cada plataforma conserva su flujo autorizado.

| Control | Comportamiento |
| --- | --- |
| Pausar y reanudar | Guarda la pausa en SQLite. El trabajo activo puede terminar. Los pendientes empiezan al reanudar. |
| Cancelar | Los trabajos en espera se cancelan de inmediato. En un trabajo activo, Studio cancela la decodificación de integridad, Whisper u Ollama cuando corresponde. Si FFmpeg ya está renderizando, deja acabar la etapa acotada y comprueba la cancelación antes de promover el archivo. |
| Reintentar | Abre un nuevo ciclo para trabajos con incidencias o cancelados y conserva el historial, el original y el archivo reparado si existe. |
| Intentos de reparación | Dos por defecto, configurable entre cero y cinco. Cero permite diagnosticar y aislar el clip para revisión. |
| Revisar detalle | Muestra tiempos, diagnóstico, intentos, incidencias y transiciones del trabajo. |
| Contingencia | **Calidad de subtítulos** y **Generar propuestas semánticas** mantienen las opciones manuales. **Generar propuestas** también conserva el modo de extractos y reglas de la fase anterior. |

## Control técnico y reparación local

1. Studio registra el clip al terminar una exportación convencional o al confirmarse el clip Live. Si los tiempos de palabra impiden generar subtítulos, puede conservar la primera versión del MP4 con el defecto registrado para que la cola la procese. Conserva los tramos locales de exportaciones por secciones de Studio para poder reparar el clip.
2. FFprobe lee duración y relojes de audio y video. FFmpeg verifica la decodificación de ambas pistas. La cola señala diferencias superiores a 220 ms al inicio o al fin de las pistas. La medición comprueba el reloj de cada pista. La coincidencia entre voz y movimientos de labios sigue siendo una revisión perceptual.
3. Reutiliza el diagnóstico de 2.1 para verificar SRT, ASS, versión del MP4 y cobertura de voz con Silero VAD. Si el detector no confirma voz o faltan relojes de pista, solicita revisión excepcional. Un clip correcto pasa a AI Engine sin retranscribirse. Un clip ilegible o claramente desalineado se rechaza y se conserva.
4. Para reparar subtítulos, la cola comprueba primero si existe video limpio del mismo montaje. Cuando corresponda, intenta reutilizar las palabras y sus tiempos desde `transcript.json`. Si la cobertura es insuficiente o ese intento falla, retranscribe el audio final con un modelo de Faster-Whisper ya instalado. Usa la caché local y evita repetir una retranscripción completada para el mismo intento y contenido. Separa cada intento e impide ciclos de reparación.
5. Genera otro archivo desde la fuente limpia verificada, conserva la pista de audio original y revalida el resultado. La versión corregida usa la relación de aspecto del original y la configuración de marca de agua de Studio. La variante apta de un clip convencional o Live aparece en el catálogo. Si falta el video limpio, el montaje no puede reconstruirse o se agotan los intentos, conserva los archivos y el siguiente clip sigue en producción.

Los clips quedan aislados del flujo automático cuando hay defectos. El catálogo manual conserva acceso al original para revisar o reparar. La generación editorial automática y la recepción de clips Live respetan los controles de Automation existentes.

## AI Engine semántico

Elige **qwen3:4b** en **AI Engine** para fijar un modelo local. Cuando el selector está en su configuración inicial, la cola detecta y usa `qwen3:4b` si ya está instalado. Si no encuentra un modelo local, deja el resultado de contingencia para revisión. Una ejecución con un modelo configurado que falle se marca para reintento.

- El modelo recibe pasajes con timestamps como evidencia del clip. Usa toda la transcripción cuando cabe en el contexto y una muestra distribuida para clips largos. El título, descripción, canal y contexto anterior o posterior provienen de los archivos locales del proyecto cuando están disponibles. Esos campos se presentan por separado como contexto. Los manifiestos Live antiguos pueden aportar únicamente los subtítulos. Studio descarta texto que parece una instrucción y elimina patrones de credenciales y direcciones web de la entrada.
- Ollama devuelve un tema, mensaje central, intención, emoción, audiencia potencial, elemento de interés, contexto independiente y las personas o acontecimientos mencionados. Escribe tres variantes originales, **Curiosidad**, **Interacción** y **Contextual**, con título, caption, descripción, hashtags y, cuando aporta, llamada a la acción. Cada propuesta cita pasajes del clip.
- Studio aplica límites, evita variantes repetitivas y mantiene referencias verificables. Rechaza cifras ausentes de los pasajes citados. Un segundo prompt pide al modelo revisar fidelidad, atribución, sensacionalismo, coherencia, título, descripción, hashtags y contexto. Si falla, envía instrucciones correctivas para un segundo intento dentro del tiempo configurado. La puntuación mide la evaluación editorial local. Una propuesta con puntuación cero queda fuera de las recomendaciones. Sigue siendo necesario revisar afirmaciones sensibles antes de aprobar un envío.

## Arquitectura y recuperación

`dashboard.py`, `projects.py` y `live_clips.py` conectan los exportadores con `production.py`. `production_store.py` agrega las tablas `production_jobs`, `production_events` y `production_config` a la base existente. Una restricción única evita duplicar un clip por proyecto y nombre de exportación. El historial conserva los eventos por ciclo. Una reserva renovable permite reanudar un trabajo si Studio se cierra. El identificador de producción y el número de ciclo impiden duplicar el trabajo editorial si el cierre ocurre entre registrarlo y guardar su enlace. Los nombres de la reparación son deterministas por intento para revalidar una versión ya escrita.

`subtitle_quality.py`, `subtitle_repair.py`, `render_clips.py` y `local_process.py` reutilizan FFmpeg y Faster-Whisper para reparar fuera del hilo de Studio. `editorial_source.py` y `editorial_semantic.py` preparan evidencia y propuestas. `editorial.py` conserva el historial editable y usa el transporte de Ollama existente en loopback. Los dos prompts editoriales son locales y usan límites de contexto y tiempo. El Centro de operaciones consulta el estado con `assets/studio/production.js`. El flujo de aprobación de Publicaciones sigue separado.

Un trabajador de producción procesa un clip a la vez. Comparte acceso exclusivo a trabajos pesados dentro de la misma instancia de Studio con las exportaciones convencionales, los renders Live, las reparaciones manuales y la generación editorial. La adquisición y los analizadores en tiempo real de Live siguen funcionando en paralelo. La comprobación de integridad y la retranscripción limitan hilos a dos. Ollama conserva su contexto de 6.144 tokens, 1.600 de respuesta y `keep_alive: 0`. La prueba local no estima el consumo simultáneo de un modelo real y una captura Live en Windows. Se recomienda una instancia de Studio por carpeta de proyectos.

## Validación

La regresión completa en Python 3.11 y Linux aprobó 598 pruebas, con cero fallidas. Las seis pruebas nuevas de 2.2 volvieron a pasar tras los últimos ajustes de recuperación y contexto.

El lote de 20 clips usó MP4 reales de prueba, FFmpeg, FFprobe y el servidor de Studio. Para reproducir resultados editoriales y de voz se usaron dobles de Ollama, VAD y Faster-Whisper en ese lote. También se probó el transporte HTTP de Ollama contra un servidor simulado en loopback y se verificó VAD real por separado con audio sintético hablado. Chromium comprobó pausa, límite, reanudación, diagnóstico, acceso a las propuestas y el estado del consentimiento en Publicaciones.

| Casos | Cantidad | Resultado |
| --- | ---: | --- |
| Clips válidos, incluido uno Live | 8 | Llegaron a aprobación sin reparación. |
| Subtítulos congelados con transcripción local válida | 3 | Regeneraron subtítulos y llegaron a aprobación. |
| Subtítulos congelados que necesitaron retranscripción, incluido uno Live | 2 | Regeneraron el clip desde la fuente limpia y llegaron a aprobación. |
| Primera propuesta editorial deficiente | 1 | Se regeneró automáticamente y llegó a aprobación. |
| Fuente de video limpia ausente | 1 | Requiere recuperar la fuente local. |
| Modelo Whisper no disponible | 1 | Requiere instalar o recuperar un modelo local. |
| Editorial con cifras sin respaldo en ambos intentos | 1 | Requiere revisión de las propuestas. |
| Relojes de A/V desalineados y MP4 ilegible | 2 | Se rechazaron. Para recuperarlos se necesita una exportación multimedia válida. |
| Cancelado por el operador | 1 | Se detuvo antes de la generación editorial. |

Hubo **14 listos para aprobación, 3 en revisión, 2 rechazados y 1 cancelado**. Cinco reparaciones que llegaron a aprobación cuentan como intervenciones de reparación evitadas. El lote comprobó que las notificaciones repetidas producen un solo trabajo y que los originales conservan sus bytes. La prueba de reinicio provocó un corte entre el registro editorial y el enlace en producción. Studio recuperó el mismo identificador.

En ese lote de Linux, el tiempo promedio por trabajo cerrado y no cancelado fue **0,82 s** y el promedio de siete ciclos con reparación fue **1,05 s**. El promedio de quince trabajos editoriales fue **0,074 s** con el proveedor simulado. Una comprobación separada sobre nueve segundos de voz sintética obtuvo 100 % de cobertura VAD y 0 ms de diferencia entre los relojes de pista. Tardó **0,619 s** y consumió **0,215 s de CPU** del proceso Python. Su pico residente fue **126,8 MiB**, incluidos Python y el VAD, con FFmpeg ejecutándose en un proceso aparte.

- [Resultados individuales del lote de 20](evidence/production/production-20.json)
- [Centro de operaciones en Chromium](evidence/production/operations-chromium.png)
- [Propuestas semánticas en Chromium](evidence/production/semantic-chromium.png)

No se encontró una instancia local de Ollama ni pesos instalados de Faster-Whisper en el entorno de validación. El rendimiento, la calidad semántica con Qwen3 y la retranscripción real quedan pendientes de medir en la computadora Windows. La duración máxima del control técnico y de la retranscripción es de diez minutos por clip. La reparación de montajes con cortes exige una fuente local que coincida con el audio exportado.

## Actualizar y validar en Windows

Cierra Studio con Ctrl+C. Desde la misma carpeta del repositorio, actualiza la rama de desarrollo e inicia Studio con la carpeta de proyectos habitual.

```powershell
git switch feature/anqora-media-studio-v2
git pull --ff-only origin feature/anqora-media-studio-v2
ollama list
.\.venv\Scripts\python.exe dashboard.py
```

Si necesitas instalar Qwen3 4B, abre Ollama y ejecuta manualmente `ollama pull qwen3:4b`. Selecciónalo desde AI Engine. La reparación usa modelos Faster-Whisper que ya estén en la caché, por ejemplo el modelo **small** con el que se preparó un proyecto. La transcripción inicial de un proyecto puede descargar el modelo si el operador lo selecciona. La cola de reparación usa exclusivamente modelos locales ya disponibles.

Exporta un clip con diálogo y abre **Automation → Centro de operaciones**. Comprueba que llega a **Listo para aprobación**. En otro clip de prueba que puedas reparar, encola el archivo desde AI Engine y revisa el historial. Espera **Reparando → Revalidando → Analizando contenido** y abre la variante corregida. Finalmente, verifica que **Revisar en Publicaciones** abre el archivo con la confirmación de envío pendiente.
