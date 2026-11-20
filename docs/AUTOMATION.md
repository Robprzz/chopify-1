# Chopify Automation v1.0

## Fase 1 · Configuración local

Abre Studio en `http://127.0.0.1:8765`, entra en **Automation**, edita tus preferencias y pulsa **Guardar ajustes**. Studio guarda la configuración por carpeta de proyectos en `.chopify-automation.sqlite3`. Al reiniciar con la misma carpeta, recupera la última revisión guardada. **Recargar ajustes** devuelve los valores guardados, incluidos los cambios hechos en otra pestaña.

| Ajuste | Valor inicial | Valores admitidos |
| --- | --- | --- |
| Recibir clips de Live clipping | Sí | Editable |
| Generar textos de publicación con IA | Sí | Editable |
| Corregir subtítulos | Sí | Editable |
| Forzar exportación vertical | No | Editable |
| Puntuación mínima del clip | 7,0 | De 7 a 10 |
| Límite diario | 3 | De 1 a 3 |
| Zona horaria | `America/Santo_Domingo` | Zona IANA válida |
| Prevención de duplicados | Sí | Obligatoria |
| Motor y proveedor de IA | Local, Ollama | Política fija de v1.0 |
| Consentimiento de publicación | Sí | Obligatorio para cada envío |

El panel de la Fase 1 guarda preferencias. La ejecución de estas preferencias se incorpora en las fases siguientes. En Windows, actualiza las dependencias con el Python del entorno virtual e inicia Studio desde la carpeta del repositorio.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe dashboard.py
```

Se usa Python 3.11 o superior con `sqlite3` y `zoneinfo` de la biblioteca estándar. En Windows, `requirements.txt` incluye `tzdata` para las zonas IANA. Esta fase utiliza una sola fila de configuración y conexiones SQLite breves al abrir o guardar el panel. El consumo adicional se limita a una base de unos pocos KiB y el intercambio local de un documento pequeño por operación. No se necesita un modelo de IA para configurar el panel.

## Auditoría de los puntos de integración

| Componente existente | Cómo se reutilizará |
| --- | --- |
| `dashboard.py` y `assets/studio/` | La navegación existente sirve el panel. Los cambios de configuración usan la protección de host, origen y token de Studio. Los trabajos de proyectos tienen su propio ejecutor serial. |
| `live.py` y `live_clips.py` | Los clips terminados se identifican en `live_session.json`. Allí se registran intervalos, señales y puntuación de origen. El render ya genera MP4, SRT y metadatos, con el aspecto seleccionado para la sesión. |
| `projects.py`, `captions.py` y `render_clips.py` | Los proyectos conservan transcripciones con timestamps. Sus funciones de transcripción, edición y render son los puntos de reutilización para corrección, subtítulos y verificación de archivos. |
| `score_clips.py` y `campaign.py` | Ya existen llamadas a Ollama para ranking y briefs. Automation puede reutilizar el transporte local mediante un contrato de generación propio. Usará un fallback determinista local identificado en el resultado. |
| `tiktok.py` y Publicaciones | El flujo existente inspecciona, revisa y sube al buzón con consentimiento y `video.upload`. Conserva `publish_id` y reconcilia intentos pendientes. La integración futura se apoya en ese mismo flujo. |

## Contrato de configuración

`GET /api/automation/settings` devuelve `{revision, settings}`. Si todavía no hay ajustes guardados, devuelve la revisión `0` con los valores iniciales. `POST` recibe ese mismo objeto con todos los ajustes y la revisión que estaba abierta en el editor. La API valida los parámetros antes de escribir.

SQLite confirma cada cambio en una transacción. Una segunda escritura con una revisión antigua devuelve HTTP 409 cuando difiere de los valores guardados. Repetir un guardado idéntico devuelve la revisión vigente. La interfaz conserva los cambios sin guardar si se rechaza el guardado y permite recargar la versión vigente. Si la base no se puede leer, el panel informa del error y conserva el archivo.

La API se sirve en el loopback de Studio. La base contiene preferencias y su revisión. Las credenciales y los tokens de TikTok siguen bajo el flujo de Publicaciones.

## Arquitectura para las fases siguientes

1. **Captions con Ollama.** Tomar el texto y los tiempos del clip a partir de la transcripción existente o el SRT del clip Live. Pedir al modelo tres alternativas estructuradas con título, descripción, llamada a la acción y hashtags. Validarlas, elegir una y conservar la procedencia. El fallback trabaja sobre el mismo texto. El usuario elige un modelo ya instalado. Automation limitará el trabajo de IA a una tarea a la vez, con entradas acotadas y tiempo de espera. El consumo de RAM dependerá del modelo y se medirá al integrar Ollama.
2. **Subtítulos, calidad y duplicados.** Guardar el original junto a una revisión separada. Conservar timestamps y validar sentido, orden y rangos de subtítulos antes de usar la revisión. Mantener la puntuación de detección y la revisión editorial en campos distintos. La admisión automática exige la puntuación mínima configurada y que pasen las comprobaciones de audio, duración, continuidad, contexto, subtítulos y archivo final. Comparar SHA-256, intervalos de la misma fuente y similitud textual con alcance por cuenta para detectar duplicados.
3. **Cola y calendario.** Ampliar SQLite con registros de clips, cola, consentimientos, intentos e historial. Usar transacciones e identificadores de idempotencia para reservar cupos por día local, con un máximo de tres. Pausar o reanudar la preparación y recuperar el trabajo tras reinicios. Un intento ambiguo conserva su reserva hasta reconciliarlo.
4. **Live, TikTok y seguimiento.** Consumir manifiestos de clips terminados y respetar el aspecto de la sesión cuando el forzado vertical esté desactivado. El consentimiento liga el archivo revisado, la cuenta de destino y el intento. La interfaz distingue preparación, aprobación, subida en curso, llegada al buzón y publicación confirmada por TikTok. Mostrar clips, las dos puntuaciones, alternativas, cola, calendario, historial y diagnósticos de Ollama.

## Verificación de la Fase 1

Las pruebas unitarias y de integración cubren los valores aprobados, persistencia, reintentos, concurrencia, validación y rechazo de solicitudes sin autorización. Chromium abre el panel sobre el servidor HTTP real, guarda ajustes, verifica la recuperación tras reiniciar Studio y comprueba la navegación a Live clipping y Publicaciones. Las comprobaciones de esta fase se ejecutan localmente y no requieren conectarse a Ollama, TikTok ni YouTube.
