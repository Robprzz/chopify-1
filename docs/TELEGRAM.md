# Telegram Control Center 1.0

La integración usa la Bot API oficial por HTTPS. Studio consulta mensajes mediante long polling y envía avisos desde un trabajador independiente. Mantén Studio abierto y el PC conectado para operar desde el móvil. El servidor de Studio sigue en loopback. No necesita una IP pública, puertos abiertos ni un servicio de pago. [Telegram Bot API](https://core.telegram.org/bots/api)

## Vincular en Windows

1. En GitHub Desktop, selecciona `feature/anqora-media-studio-v2`, ejecuta **Fetch origin** y **Pull origin** si aparece. Abre Studio con el mismo perfil de Windows que utilizarás cada día.
2. Abre **Telegram** en la barra lateral. Pega localmente el token del bot que creaste con BotFather en **Bot Token**. Marca **Activar Telegram**. Revisa **Avisar cuando un clip supere los controles de Automation** y **Recibir alertas operativas** antes de guardar. Con los avisos de clips activados, Telegram recibirá copias de revisión automáticamente después de vincular tu chat. Deja el campo vacío en guardados posteriores para conservar el token protegido.
3. Pulsa **Probar conexión**. Comprueba que Telegram confirme el token. Si el registro indica otro poller o un webhook activo, detén la integración anterior que use ese bot antes de reintentar. Si el token fue rechazado, verifica con BotFather y guarda el token vigente.
4. Pulsa **Generar código de vinculación**. Abre el chat **privado** de tu bot desde la app de Telegram y envía exactamente `/pair CÓDIGO`. El código dura cinco minutos y admite un solo uso. Studio debe mostrar **Cuenta vinculada**. Envía `/start` para volver al menú.
5. Pulsa **Enviar notificación de prueba**. Comprueba que el mensaje llegue a tu chat. La entrega respeta el horario silencioso y el máximo por hora. Si se queda en cola, revisa ambos ajustes y el registro.
6. Para probar captura remota, guarda en **Fuentes autorizadas para captura remota** el enlace de un directo que tengas permiso de grabar. Envía `/capture ENLACE`, consulta el ID con `/live` y usa `/stop ID` para ver la confirmación. Pulsa **Confirmar detención** solo cuando quieras finalizar esa sesión. La confirmación expira en 90 segundos.

La aceptación de URLs remotas se limita a videos de YouTube y canales de Twitch o Kick autorizados en Studio. Los enlaces directos y fuentes de red interna se siguen gestionando desde Studio. Cada carpeta de proyectos tiene su propia configuración y vinculación. Al cambiar de carpeta, vuelve a configurar el bot para ese espacio. **Revocar vinculación** corta el acceso remoto y cancela las entregas pendientes. Genera otro código para renovar el acceso. Al guardar un token distinto se revoca la vinculación anterior. Los mensajes ya enviados se gestionan en Telegram.

## Operaciones y revisión

| Comando | Función |
| --- | --- |
| `/start` | Abre el menú con botones. |
| `/status`, `/live` | Consulta Studio y captura activa. |
| `/clips`, `/pending` | Abre clips elegibles recientes y pendientes de decisión. Los fallos técnicos o editoriales se contabilizan y se revisan en Studio. |
| `/queue`, `/stats` | Consulta estados de producción Live y Automation. |
| `/capture ENLACE`, `/stop ID` | Inicia una fuente autorizada o pide confirmación para detener una sesión. |
| `/edit TEXTO`, `/help` | Guarda el campo seleccionado o muestra la ayuda. |

La notificación inmediata usa la evidencia vigente de Automation. El score de detección mínimo es 6. Un mínimo superior en Automation prevalece. **Prioridad mínima** filtra por el score global que ya calcula Automation. El ranking y el límite diario se mantienen como referencia. La antigüedad se calcula desde la detección cuando Live conserva ese dato. Si falta, se indica el tiempo desde el procesamiento cuando está disponible. Los textos largos se abrevian en el video y **Detalles** muestra la descripción completa. **Ver video** permite pedir una copia desde el detalle.

Aprobar, rechazar, solicitar corrección y posponer se registran en **Automation → Centro de operaciones**. Usa **Ver detalle** para ver la revisión y el historial. Una corrección solicitada queda pendiente del operador. Usa **Reevaluar** o edita la propuesta desde Studio. Las ediciones de título o descripción crean una revisión y requieren una nueva decisión. Los controles de TikTok siguen vigentes. Aprobar en Telegram deja una decisión editorial en Automation.

Studio valida el par de usuario y chat privado y asocia cada botón del clip a la vinculación, el trabajo, su ejecución y la revisión editorial. Cambiar la versión invalida los botones anteriores. Los clips que exceden 49 000 000 bytes se convierten en una copia temporal de revisión con FFmpeg y duración comprobada. El archivo original se conserva.

## Entrega y diagnóstico

Los avisos incluyen inicio de Studio, interrupción y recuperación de captura, fallos persistentes de transcripción, saturación, disco insuficiente, fallos de producción y recuperación de Telegram. Los eventos repetidos se agrupan por condición y respetan un intervalo entre avisos. El horario silencioso usa la zona de Automation. El máximo por hora se aplica a clips, previsualizaciones y avisos salientes en cola. Las respuestas directas a comandos se muestran en el momento de la consulta.

El token se guarda mediante DPAPI en el perfil del usuario Windows. La cola SQLite conserva la vinculación, el cursor de lectura, el estado de entrega y el registro local. Los reintentos confirmados son acotados. Si se pierde la respuesta después de enviar o Studio se cierra antes de confirmar, el envío queda **sin confirmación**. Telegram no ofrece deduplicación para estos envíos, por lo que conviene consultar primero el chat y pedir **Ver video** si hace falta otra copia. Los comandos consumidos se registran antes de ejecutarse para evitar repetir una captura o detención tras un cierre. Si se interrumpe un comando, consulta el estado antes de repetirlo.

Las pruebas automatizadas usan respuestas simuladas de Telegram y DPAPI simulado en Linux. Chromium usa el servidor real de Studio. FFmpeg comprueba la compresión de una copia real. La prueba con tu bot y DPAPI nativo se realiza en Windows siguiendo los pasos anteriores.
