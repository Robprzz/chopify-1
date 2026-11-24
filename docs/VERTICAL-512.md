# Anqora Media Studio 5.1.2 · Validación vertical

## Causa reproducida

La comprobación anterior exigía el nombre exacto yuv420p. En una fuente sintética, FFmpeg recibió -pix_fmt yuv420p y señalización explícita de rango completo. FFprobe informó H.264, 1080 × 1920, SAR 1:1, DAR 9:16, yuvj420p y rango pc. El validador anterior rechazó esa combinación exacta. FFprobe puede representar así el YUV 4:2:0 de 8 bits de rango completo.

La validación comprueba resolución, SAR, DAR, códecs, formato y rango, duración, FPS, continuidad de PTS y DTS y sincronía audiovisual por separado. Acepta yuv420p limitado, completo o sin etiqueta y yuvj420p de rango completo o sin etiqueta adicional. Una etiqueta de rango limitado contradice yuvj420p y se rechaza. El motor conserva el control de congelamientos de Smart Camera.

La corrección conserva los filtros de escala, composición, subtítulos y marca de agua. Forzar rango limitado para cambiar el nombre del formato podría alterar los niveles de la imagen. Studio registra los valores de FFprobe antes y después del grafo, así como las mediciones de validación.

## Recuperación

Si el MP4 codificado falla una comprobación técnica, el intento más reciente se conserva localmente en clips/.vertical-diagnostic/last-rejected.mp4 para diagnóstico. Esa carpeta queda fuera del catálogo. Si el fallo ocurrió en un modo complejo, Studio puede ofrecer reexportar con encuadre estable mediante el mecanismo de confirmación existente. Con una salida ya estable, el mensaje indica la comprobación fallida y puedes reexportar tras revisar el Registro del trabajo. Se conservan el original y las exportaciones anteriores. Studio registra el comando con rutas de medios redactadas.

Los archivos rechazados por versiones anteriores que fueron eliminados al cerrar sus carpetas temporales requieren una nueva exportación. Para reutilizar un proyecto, abre el proyecto existente y pulsa Exportar con sus candidatos guardados.

## Verificación

Se utilizaron MP4 locales sintéticos, FFmpeg y FFprobe reales y Chromium. Las pruebas de interfaz simulan el reconocimiento de voz, usan el flujo de preparación y render existentes y verifican la reproducción y edición. Las regresiones de proveedores usan sus fixtures controlados. La grabación original de Windows no estaba disponible. Su validación específica sigue pendiente.

| Fuente local de prueba | Resolución de salida | SAR | DAR | Códec | FFprobe |
| --- | --- | --- | --- | --- | --- |
| Horizontal, rango limitado | 1080 × 1920 | 1:1 | 9:16 | H.264 | yuv420p · tv |
| Vertical, rango completo | 1080 × 1920 | 1:1 | 9:16 | H.264 | yuvj420p · pc |
