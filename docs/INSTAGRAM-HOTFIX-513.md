# Anqora Media Studio 5.1.3 · Recuperación de importaciones de Instagram

`PinnedHTTPS` hereda de `http.client.HTTPSConnection`. Tiene un método `close()` pero carece de los métodos necesarios para usar la conexión directamente con `with`. La descarga fallaba al abrir la respuesta HTML, y la descarga del MP4 usaba el mismo patrón. La conexión y la respuesta ahora se cierran explícitamente al salir de cada etapa, incluidas las redirecciones, los errores HTTP, los tiempos de espera y las cancelaciones.

## Volver a descargar en el mismo proyecto

1. Actualiza la rama `feature/anqora-media-studio-v2`, reinicia Studio y abre **Proyectos**.
2. Selecciona el proyecto de Instagram existente y pulsa **Reintentar descarga**. Studio usa el enlace, la autorización, los límites y la configuración guardados. Los intentos anteriores figuran en **Errores de descargas anteriores** y el registro del trabajo se conserva.
3. Sigue el progreso en la pantalla del proyecto. Usa **Cancelar descarga** si es necesario. La limpieza se limita a los archivos parciales de la importación. Un MP4 verificado y reutilizable permanece disponible.
4. Cuando la descarga finaliza, Studio continúa con la transcripción y la detección de clips en ese proyecto. Puedes revisar los candidatos y exportarlos con el flujo habitual.

Studio distingue enlaces inválidos, contenido restringido, respuestas sin un MP4 público, errores HTTP, interrupciones, tiempos de espera y cancelaciones. Si ocurre un error inesperado de Python, el registro del trabajo muestra el tipo de excepción sin incluir credenciales ni URLs firmadas. Siguen aplicándose los controles existentes de HTTPS, TLS, DNS, destinos permitidos, redirecciones y límites de descarga.

## Alcance de la validación

La regresión reproduce el error con la clase real de Python y comprueba la descarga HTML y MP4 con respuestas controladas. También prueba el cierre de recursos, la cancelación, las redirecciones, HTTP 403, los tiempos de espera y la limpieza de archivos parciales. Una prueba con el servidor HTTP de Studio y Chromium verifica el reintento desde el proyecto fallido hasta la revisión. FFmpeg y FFprobe procesan medios locales reales. La red de Instagram y la transcripción se simulan en estas pruebas. Queda pendiente comprobar el Reel original en Windows.
