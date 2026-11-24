# Anqora Media Studio 4.2

## Trabajar desde Clips

1. Abre Studio en [127.0.0.1:8765](http://127.0.0.1:8765). **Clips** es el punto de entrada. El logotipo vuelve a la biblioteca. Usa los filtros para ver clips por proyecto, origen, formato y fecha.
2. Abre **Editar clip**. Escribe título, caption, descripción y hashtags para la plataforma seleccionada. **Guardar** conserva una versión local. Puedes consultar o restaurar versiones recientes.
3. **Regenerar caption con IA** utiliza el motor editorial y el modelo local configurado. Revisa el texto antes de guardarlo. El ajuste **Configuración → Automatización → Captions con IA** controla la generación inicial. Si hay una propuesta previa válida para la plataforma, el editor puede mostrarla como borrador inicial. La edición manual está disponible en todo momento.
4. El **caption de publicación** acompaña al contenido en la plataforma. **Subtítulos del video** abre las herramientas de diagnóstico y reparación de la pista de texto del clip.
5. En **Proyectos**, abre un proyecto y elige **Editar clip** en la biblioteca de exportaciones. El reproductor y el caption aparecen dentro del proyecto. Desde una sesión de **Live Studio**, la acción del clip abre el mismo editor.

## Portada y exportación

En **Portada y marca de agua**, puedes subir un PNG de hasta 2 MiB y 2048 × 2048 píxeles. La portada se utiliza en Studio. Por clip puedes heredar la marca global, elegir una imagen personalizada o desactivarla. Conserva los archivos originales y crea un nuevo MP4 mediante **Formato y nueva exportación**. La vista previa de la marca es orientativa. La posición final depende de la relación de aspecto y del tamaño de la imagen.

La nueva versión se reconstruye desde la fuente limpia disponible, con los subtítulos recuperados del proyecto o de la sesión Live. Si la fuente se perdió o el intervalo original no se puede reconstruir, Studio muestra un error y conserva el clip anterior.

## Publicar o programar

**Publicar ahora** y **Programar** se pueden iniciar desde el clip o desde Publicaciones. Studio vincula el plan a la versión del archivo, comprueba su reproducción y detecta planes duplicados en la misma plataforma. Un resultado editorial previo es opcional. El envío a un servicio requiere consentimiento.

| Destino | Acción disponible |
| --- | --- |
| TikTok | Entrega al buzón con OAuth y permiso `video.upload`. La persona titular de la cuenta completa la edición, privacidad y publicación en TikTok. Direct Post permanece deshabilitado. |
| YouTube Shorts | Paquete local de MP4 y texto. Subida y publicación manuales. |
| Facebook Reels | Paquete local de MP4 y texto. Subida y publicación manuales. |
| Instagram Reels | Paquete local de MP4 y texto. Subida y publicación manuales. |

Una entrega al buzón no se contabiliza como publicación. El calendario sincroniza el estado desde el historial de TikTok si se obtiene confirmación del proveedor. Para las otras plataformas, el estado **Publicado** requiere que el usuario confirme que ya publicó.

Para una entrega programada al buzón, conecta tu cuenta y marca el consentimiento dentro del plan. La autorización queda vinculada a la cuenta, fecha y versión del plan. Mantén Studio en ejecución a la hora indicada. Si la hora autorizada pasó hace más de diez minutos, la cuenta cambió o el resultado de un intento quedó incierto, revisa el historial antes de intentar de nuevo. Studio no repite automáticamente un envío ambiguo. Sin consentimiento programado, el evento pasa a **Requiere acción**.

## Papelera y configuración

**Enviar a la papelera** oculta el clip o proyecto de las bibliotecas. Puedes restaurarlo tras reiniciar. En **Configuración → Almacenamiento y papelera** se muestran los archivos y el espacio reclamable. La eliminación definitiva exige escribir la confirmación solicitada. Studio bloquea el borrado cuando detecta archivos compartidos, enlaces, trabajos activos o planes relacionados que siguen pendientes. Al borrar un clip Live se conserva la sesión. Conserva aparte cualquier original necesario antes de confirmar.

**Configuración → Diagnósticos → Diagnósticos de procesamiento** reúne los historiales técnicos y controles de la cola. Esta cola sigue ejecutándose en segundo plano. Las preferencias de video se aplican a trabajos nuevos. Los proyectos ya creados mantienen sus propias opciones.

## Arquitectura y verificación

El servicio de clips y el calendario reutilizan el servidor HTTP local, SQLite, FFmpeg, el motor editorial, la marca de agua y el flujo de TikTok existente. La migración añade tablas locales para captions, arte y entrega al buzón. Conserva los planes e historiales anteriores. La papelera registra la retirada de archivos antes de borrar para poder completar o revertir una operación interrumpida al reiniciar.

Las pruebas usan Chromium sobre el servidor HTTP real y FFmpeg/FFprobe con medios de prueba locales. Las pruebas de TikTok y Ollama usan cuentas y proveedores simulados. No se subió contenido real.

Para validar en Windows, actualiza la rama de desarrollo y abre Studio con el acceso directo habitual. Elige un clip de prueba, guarda un caption, recarga, exporta una versión vertical con una marca personalizada y crea una programación sin autorizar el envío. En otra prueba, usa exclusivamente un proyecto desechable para verificar restauración y borrado. La autenticación real y la entrega a un buzón de prueba requieren una cuenta autorizada.

### Capturas de Chromium

| Vista | 1920 × 1080 | 1366 × 768 | 390 × 844 |
| --- | --- | --- | --- |
| Biblioteca de Clips | [Escritorio](../design/validation/workspace-42/clips-1920x1080.png) | [Portátil](../design/validation/workspace-42/clips-1366x768.png) | [Móvil](../design/validation/workspace-42/clips-390x844.png) |
| Editor dentro de Proyectos | [Escritorio](../design/validation/workspace-42/editor-1920x1080.png) | [Portátil](../design/validation/workspace-42/editor-1366x768.png) | [Móvil](../design/validation/workspace-42/editor-390x844.png) |

[Programación guardada](../design/validation/workspace-42/programado-1366x768.png) · [Configuración integrada](../design/validation/workspace-42/configuracion-1366x768.png)
