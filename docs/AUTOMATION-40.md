# Anqora Media Studio · Automation 4.0

## Centro de operaciones

**Inicio** reúne capturas activas, exportaciones del día, revisiones pendientes y recomendaciones de Automation 3.1. El progreso utiliza la fecha local y el límite configurados en Automation. La lista de clips usa la prioridad temporal existente y muestra la antigüedad solo si hay un acontecimiento fechado. La búsqueda busca clips, sesiones y programaciones. Puedes abrirla con **Ctrl+K** y cerrarla con **Esc**.

La barra lateral permite contraer el menú y recuerda la última sección en ese navegador. **Live Studio** abre con URL, formato y duración. En instalaciones sin preferencias previas, usa 9:16, 60–90 segundos, máximo 720p, Whisper automático y umbral de detección 6. La configuración avanzada conserva el resto de controles y diagnósticos. **Clips** incluye la biblioteca, **Nuevo proyecto** y **Proyectos guardados**. **Configuración** reúne los ajustes y las integraciones. **Estadísticas** distingue registros de producción, confirmaciones manuales y confirmaciones verificadas en TikTok.

### Identidad visual

La pantalla conserva la estructura de la maqueta aprobada, con barra lateral clara, controles azul marino, cuatro tarjetas, banner de captura, lista de prioridades y columna de operaciones. Los contadores, miniaturas y actividad proceden de los datos locales. El nombre de perfil aparece cuando el operador lo configura. Los estados vacíos reflejan un espacio de trabajo nuevo.

Studio utiliza el logotipo horizontal oficial en la barra lateral expandida y el símbolo oficial en la vista compacta, el favicon y el icono de escritorio. Los originales y la maqueta aprobada se conservan en [Referencias de diseño](../design/references/README.md).

## Calendario y ruta urgente

1. Abre **Publicaciones** y elige día, semana o mes. Puedes filtrar por plataforma y estado, o abrir el detalle de una tarjeta sin salir del calendario.
2. Para planificar una exportación existente, pulsa **Nueva programación** o **Programar** en la biblioteca. Guarda un borrador sin fecha mientras el clip espera su evaluación.
3. Aprueba en **Automation** la versión vigente del archivo. Si el borrador se creó antes de que existiera un trabajo de producción, usa **Actualizar a la versión vigente del clip** para vincularlo. Si hubo otra edición o revisión editorial, actualiza la referencia y revisa la nueva versión. Con la aprobación vigente puedes elegir una fecha y hora, sugerir una franja o guardar el plan como listo.
4. **Configuración → Publicaciones** permite cambiar la zona IANA, cantidad deseada por día, separación mínima en minutos y horas preferidas. Los límites se aplican por plataforma. La zona inicial sigue Automation, cuyo valor predeterminado es `America/Santo_Domingo`. Un cambio de visualización conserva los instantes guardados. Si una hora se repite o no existe por el cambio de horario, elige otra franja o utiliza UTC.
5. En **Clips prioritarios** puedes revisar de inmediato un urgente. **Preparar ahora** abre su detalle sin fecha obligatoria. La revisión humana y los controles técnicos y editoriales también se aplican a esta ruta.

La sugerencia busca huecos dentro de los próximos 14 días. Usa horas preferidas, cantidad diaria, separación y prioridad. Con cinco o más planes fechados y confirmados localmente para esa plataforma, puede favorecer las horas ya utilizadas. La sugerencia es una heurística de agenda. La frecuencia y el límite de recomendaciones de Automation 3.1 son independientes.

## Cola y estados

La cola usa `.chopify-publishing.sqlite3` dentro de la carpeta de proyectos. Es una base adicional a las de Automation y la producción Live. Guarda identificadores, referencias de archivo e integridad, versión de producción, revisión editorial, zona, instante UTC, copia, prioridad, estado, intentos, aprobación y consentimiento, auditoría y referencias externas. Las escrituras se ejecutan en transacciones SQLite. La creación es idempotente por identificador y las ediciones usan una revisión para detectar conflictos entre pestañas. Los planes activos de una misma plataforma se comparan por archivo y, si está disponible, por el SHA-256 existente de Automation. La producción mantiene sus propios controles de similitud y duplicados.

| Estado | Evidencia |
|---|---|
| Borrador | Exportación local seleccionada. |
| Pendiente de revisión | Trabajo asociado sin aprobación editorial vigente. |
| Aprobado editorialmente | Aprobación de la versión vigente y controles de Automation superados. |
| Listo para programar | Plan sin fecha marcado expresamente tras la aprobación. |
| Programado | Fecha futura y franja aceptada. |
| Requiere acción | Vencimiento, versión que necesita revisión, paquete manual preparado, envío al buzón o resultado externo incierto. |
| En proceso | Operación registrada con estado efectivo de subida o procesamiento. |
| Publicado | Estado `PUBLISH_COMPLETE` confirmado en el historial de TikTok o confirmación manual explícita identificada como tal. |
| Fallido | El historial de la operación confirma un fallo. |
| Cancelado | Cancelación solicitada antes de enlazar un intento externo. |

La vista detecta las tareas vencidas al volver a abrir Studio y durante su actualización. El vencimiento plantea una revisión a cargo del operador. Las operaciones ambiguas conservan su referencia para revisar el historial o el buzón. Los reintentos se limitan a fallos confirmados sin identificador externo y necesitan volver a pasar por consentimiento.

### TikTok y publicación manual

Para TikTok, **Abrir revisión de TikTok** abre el flujo existente con vista previa. El envío requiere la cuenta conectada, permisos, inspección del archivo exacto y consentimiento explícito. Subir al buzón requiere completar la revisión en la aplicación de TikTok. Cuando exista evidencia nueva, consulta el historial de TikTok para actualizar el estado de la programación. El calendario guarda la referencia del intento y la evidencia que el flujo existente ya registró.

Para YouTube Shorts, Facebook Reels e Instagram Reels, **Preparar paquete manual** permite descargar los metadatos en JSON y el MP4 original. Una vez publicado el clip por los medios oficiales de la plataforma, puedes usar **Confirmar publicación manual**. Estadísticas muestra la fuente de estas confirmaciones.

### Instalación, seguridad y recuperación

El servidor sigue escuchando en `127.0.0.1`. Las escrituras de la interfaz usan la autorización local existente. Las credenciales conservan su almacén y protección en Windows. La cola nueva contiene referencias y decisiones locales. Los originales, las transcripciones, las revisiones y los historiales permanecen en sus ubicaciones actuales. Para respaldar o trasladar el calendario junto con sus referencias, cierra Studio y copia la carpeta de proyectos completa. Las preferencias del perfil y de interfaz se guardan en el navegador.

Automation 4.0 reutiliza el ranking, las cinco recomendaciones, las prioridades, la deduplicación, el control de versiones y la cola de producción existentes. La agenda usa la evidencia ya calculada. No requiere modelos adicionales. El fallo de Telegram se muestra en **Configuración → Integraciones** y las alertas de clips urgentes o tareas pendientes también aparecen en Studio.

## Validación

Ejecuta `python -m pytest -q -rs` desde el checkout. Las pruebas de calendario cubren operaciones transaccionales, DST, conflictos, duplicados, versiones obsoletas, caducidad, reapertura, confirmación manual y recuperación de un envío incierto. Las pruebas de navegador ejercitan navegación, teclado, borradores, calendario y tres anchos desde el servidor HTTP real. Las pruebas de Live emplean FFmpeg y HLS controlado. La prueba del calendario con TikTok utiliza un MP4 H.264/AAC generado localmente y una respuesta simulada del proveedor.

Capturas reales de Chromium con un espacio local vacío: [1920 × 1080](../design/validation/studio-1920x1080.png), [1366 × 768](../design/validation/studio-1366x768.png), [390 × 844](../design/validation/studio-390x844.png) y [calendario](../design/validation/calendario-1366x768.png). También se muestran [la lista de prioridades](../design/validation/studio-pruebas-1920x1080.png) y [una franja guardada](../design/validation/calendario-programado-1366x768.png), con medios generados por FFmpeg y datos de una instalación aislada de pruebas.

### Comprobación en Windows

1. Cierra Studio cuando terminen los trabajos activos. En GitHub Desktop selecciona `feature/anqora-media-studio-v2`, elige **Fetch origin** y luego **Pull origin**. Conserva la carpeta de proyectos de la instalación.
2. Abre **Setup Anqora Media Studio.cmd**. Para crear el acceso directo con el nombre actualizado, abre **Configuración → Diagnósticos**. El comando anterior sigue abriendo la misma instalación.
3. Abre **Publicaciones**, crea un borrador con una exportación local y recarga. Repite después de cerrar y volver a abrir Studio.
4. Aprueba un clip en Automation. Actualiza el borrador, guárdalo en una hora futura, reprograma el mismo elemento y verifica el historial. Comprueba también una franja en otra zona horaria.
5. Para validar TikTok Sandbox, conecta la cuenta de prueba según [la guía de TikTok](TIKTOK.md), abre un plan aprobado y comprueba la vista previa y la casilla de consentimiento. Envía únicamente si autorizas esa prueba. Comprueba la aparición en el buzón y consulta el historial antes de intentar otro envío.

La prueba de aceptación en Windows con GPU, Ollama, una transmisión externa y una cuenta real de TikTok queda pendiente. En Automation 5.0 se pueden añadir conectores supervisados cuando existan permisos oficiales, más historial de horarios basado en datos observados y notificaciones nativas opcionales.
