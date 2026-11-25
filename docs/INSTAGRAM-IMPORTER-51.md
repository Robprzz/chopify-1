# Anqora Media Studio 5.1 · Importación de Instagram

## Importar y preparar un clip

1. Abre **Proyectos → Nuevo proyecto**. Pega el enlace de Instagram en el mismo campo que admite YouTube y archivos locales. Studio detecta Instagram en los dominios oficiales admitidos.
2. Confirma que tienes los derechos o autorización para usar el video. Los límites iniciales son 256 MiB y 1 200 segundos. Puedes ajustarlos por solicitud.
3. Crea el proyecto. Studio conserva la URL canónica y la fecha de confirmación. Muestra título, autor y duración cuando la página o el MP4 proporcionan esa información. Puedes cancelar durante la descarga.
4. Selecciona y exporta los clips usando el flujo habitual, incluidos Smart Camera, subtítulos y marca de agua. Desde el clip exportado, usa **Programar** para abrir su revisión en el calendario. La importación no autoriza envíos.
5. Cuando un enlace ya corresponde a un proyecto activo o histórico visible, Studio reutiliza ese proyecto. Para recuperar una descarga fallida, cancelada o interrumpida en Anqora 5.1.3, abre ese proyecto y pulsa **Reintentar descarga**. Consulta el historial en **Errores de descargas anteriores**. [Notas de la corrección 5.1.3](INSTAGRAM-HOTFIX-513.md)

## Compatibilidad y límites

El importador acepta la sintaxis HTTPS de enlaces directos a Reels y publicaciones con video. Para descargar, la página pública del mismo enlace debe anunciar un MP4 directo en sus metadatos. El transporte limita redirecciones a páginas de Instagram y el video a dominios CDN permitidos. Cada conexión verifica los destinos DNS, se conecta a una IP pública validada y conserva la validación TLS del nombre de host. No usa cookies, contraseñas, sesiones de navegador ni configuración de yt-dlp para Instagram. No elimina marcas o créditos incrustados.

Un Reel público puede seguir sin ser descargable. Iniciar sesión, abrir una publicación privada, consultar publicaciones eliminadas, descargar carruseles o extraer video de un manifiesto quedan fuera de este flujo. Si Instagram no ofrece el MP4, usa el archivo original autorizado o un archivo que hayas obtenido mediante las opciones oficiales de tu cuenta. La API de medios de Meta requiere acceso autorizado. oEmbed permite incrustar publicaciones y no proporciona un mecanismo general de descarga. [Referencia de medios de Meta](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/v21.0/?locale=es_LA) · [Referencia oEmbed](https://developers.facebook.com/docs/graph-api/reference/oembed-post/)

Los archivos parciales se escriben fuera del catálogo de medios. Solo se incorpora el MP4 después de verificar el tamaño, la duración y que FFprobe reconozca una pista de video. Los errores y cancelaciones limpian el archivo temporal. Después de un cierre abrupto, el siguiente intento limpia el temporal abandonado.

## Verificación

Las pruebas de Reel y publicación usan respuestas HTTP simuladas. FFmpeg y FFprobe procesan MP4 locales reales. La transcripción está simulada. Chromium comprueba la detección de origen, la autorización y la reutilización de proyectos. No se descargaron publicaciones reales de Instagram. La compatibilidad con cuentas, publicaciones o respuestas reales concretas queda pendiente de una prueba autorizada.
