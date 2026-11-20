# Anqora Media Studio 2.0

Auditoría inicial de `Robprzz/chopify-1`, rama
`feature/anqora-media-studio-v2`, revisión base `c1e4a28`.

## Flujo auditado

| Código | Comportamiento verificado |
| --- | --- |
| `dashboard.py` y `assets/studio/app.js` | **Find clips** encola `chopify.py --action prepare --no-input`. Studio muestra y permite editar `segments.json`. La cola serializa las operaciones dentro de la instancia. |
| `chopify.py` y `agent_ops.py` | CLI, Studio y agentes delegan en `projects.run_project`. `render` lee la selección guardada, incluidas las ediciones manuales. |
| `projects.py` | La caché de transcripción depende de la identidad del medio, la versión de transcripción y la configuración de Whisper. Los modos de búsqueda y el modo heurístico compartían archivos con claves independientes. |
| `score_clips.py` y `semantic.py` | El descubrimiento genera ventanas deterministas o ventanas a partir de búsqueda. Los bloques de VOD persistían resultados previos sin identificar la versión del algoritmo. La selección opcional con Ollama reescribía los candidatos que luego consumía el modo heurístico. |
| `ingest.py`, `download_and_transcribe.py` y `sections.py` | Los proyectos largos pueden conservar medios de descubrimiento, transcripciones parciales y secciones para render. Estos artefactos tienen reglas de reutilización propias. |
| `video_types.py`, `evidence.py` y `visual.py` | La receta resuelta aporta el perfil y el intervalo preferido de duración. Las mediciones locales tienen sus propias versiones e identidades de caché. |
| `branding.py`, `render_clips.py` y `assets/studio/` | Los presets de marca ya admiten un logotipo de exportación. La identidad de la interfaz vive en los recursos de Studio y puede evolucionar de forma independiente. |

## Corrección inicial

- Solo la ejecución activa de descubrimiento y selección puede reutilizar los archivos compartidos. El proyecto invalida las claves dependientes antes de escribir y registra los pasos completados. Si una ejecución se interrumpe, la próxima preparación vuelve a generar los resultados pendientes.
- La clave de descubrimiento incluye `DISCOVERY_VERSION` y el perfil y el intervalo de la receta resuelta. Los bloques también usan `DISCOVERY_VERSION`. Al cambiar la construcción de ventanas, la puntuación determinista o los metadatos del descubrimiento, incrementa esa versión.
- El proyecto vuelve a descubrir si falta el archivo de candidatos o se reemplaza. En modo automático, si solo faltan los archivos de selección o son ilegibles, vuelve a seleccionar a partir de los candidatos disponibles. Un acierto completo de caché conserva los segmentos editados.
- Los valores guardados que difieren de los predeterminados se respetan cuando Studio ejecuta la CLI sin flags de longitud. En el modo automático, el umbral, el límite de clips y el modelo de reranking afectan a la selección. El resultado de Ollama se aplica a los segmentos y deja intactos los candidatos de descubrimiento.
- Los proyectos existentes regeneran candidatos y selección en su primera preparación con el cambio. La descarga y la transcripción se reutilizan mientras sus entradas coincidan.

## Verificación y hallazgos pendientes

- Las regresiones automatizadas cubren el cambio de modo, la edición del texto, el reemplazo de archivos, el reintento después de una escritura parcial, las opciones guardadas, el cambio de perfil y versión, el reranking y los bloques largos. Las pruebas de un proyecto URL comprueban que la descarga y la transcripción se reutilizan, incluidas las ediciones del usuario.
- En `c0958ce`, `TestArc.test_grow_window_backs_off_mid_sentence_end` reproducía un final cortado que también aceptaba el descubrimiento activo. El cambio de selección de finales descrito abajo corrige ambos casos.
- Studio guarda formatos personalizados en la raíz de proyectos. La resolución de formatos por nombre en `projects.py` usa el directorio predeterminado de `video_types.py`. Queda por alinear esas rutas y cubrir el flujo de creación y uso de un formato con una prueba de integración.
- El clasificador opcional de `video_types.py` construye su muestra con `text` en los registros de palabras. Whisper guarda esos registros con la clave `word`. Queda por verificar y corregir la muestra que recibe el clasificador local.

## Selección de finales de clip

El descubrimiento comprueba cada posible final de frase antes de puntuarlo. Un guion de corte o una palabra claramente pendiente, como `the` o `porque`, descarta ese punto incluso si Whisper añadió puntuación. Una pausa real o un último tramo plausible del texto siguen siendo válidos sin puntuación. En vídeos largos, se consulta el inicio de la frase siguiente si cae fuera del contexto del bloque. El descubrimiento conserva los límites de duración configurados.

`DISCOVERY_VERSION` sube a `2`. La siguiente preparación vuelve a calcular los candidatos y la selección, con reutilización de la descarga y la transcripción si sus entradas coinciden. La comprobación usa señales de texto y tiempos, así que una frase que se corta sin dejar una señal reconocible puede seguir siendo elegible.

Validación en Linux con Python 3.12. La revisión `c0958ce` reproducía el fallo original. Pasaron las 28 pruebas dirigidas de límites de frase, bloques y caché y las 454 pruebas de la suite completa. Las nuevas regresiones cubren cierres incompletos, pausas, español sin puntuación y el contexto entre bloques. También pasaron la compilación de Python y la construcción del sitio de documentación.

## Rebranding

Cuando esté disponible el logotipo, actualizar el nombre visible y los recursos de `assets/studio/`, comprobar la legibilidad en escritorio y pantallas pequeñas y revisar los textos de CLI y documentación. Conservar la compatibilidad de comandos, rutas de proyectos, API local y presets de exportación existentes.

## Anqora Score 2.0

Introducir primero un perfil opcional de gaming en español. Reutilizar las ventanas, la evidencia audiovisual y los motivos de selección existentes. Separar las señales de gancho, reacción, desenlace, intensidad audiovisual y contexto suficiente. Las señales léxicas deben cubrir expresiones regionales y cambios de idioma, y aportar valor aun cuando la transcripción sea escasa.

Evaluar el perfil actual y el nuevo sobre los mismos VOD anotados. Medir precisión entre los primeros clips, cobertura de momentos marcados y proporción de recortes que interrumpen la acción o la frase. Incorporar el perfil por etapas tras revisar los resultados. El flujo base seguirá usando procesamiento local.
