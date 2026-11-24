# Anqora Media Studio 5.0.2 · Render Engine Reliability

## Qué se reprodujo

FFmpeg devuelve un error de argumento inválido cuando el recorte recibe un ancho de cero. La fórmula anterior podía producirlo al redondear dimensiones pequeñas hacia números pares. Se reprodujo además un recorte inválido en composición apilada al usar las dimensiones codificadas de un MP4 rotado. FFmpeg aplica su matriz de rotación antes del grafo. El motor usa ahora las dimensiones visibles para rotaciones de 90° y 270°. También se reprodujo un fallo de libass al intentar abrir un archivo de subtítulos inexistente. El render ahora acota los recortes al origen y aísla el ASS y los comandos temporales por intento.

El entero de Windows 4294967274 corresponde a −22. El video ScreenRecording_11-24-2026 09-24-42_1.mp4 y el stderr de esa ejecución no estuvieron disponibles. La causa específica de ese archivo sigue sin determinarse.

## Diagnóstico y recuperación

- Exporta con los ajustes habituales. Studio muestra un resumen y conserva el stderr completo de los fallos de codificación en el registro del proyecto. La interfaz muestra el tramo final. El archivo job.log local contiene el registro del trabajo.
- Si FFmpeg identifica un fallo de subtítulos, marca de agua o composición vertical, Studio ofrece la alternativa correspondiente. La alternativa exige confirmación y utiliza la selección fallida. Conserva los ajustes del proyecto y crea una exportación separada. Si el diagnóstico es ambiguo, revisa el registro.
- Exportar sin subtítulos incrustados conserva el SRT y señala la omisión en los metadatos. Los reintentos no autorizan publicaciones externas.
- En Windows comprueba la compilación activa con PowerShell. El resultado de 'where.exe ffmpeg' identifica los ejecutables en PATH. La lista de 'ffmpeg -hide_banner -filters' debe incluir subtitles, crop, scale, overlay y vstack para la composición apilada. El filtro subtitles requiere libass. El render registra el error cuando la compilación rechaza un filtro.

## Validación

Las pruebas generan fuentes temporales y comprueban con FFmpeg y FFprobe el render apilado a 1080 × 1920, SAR 1:1, 60 FPS y sincronía de las pistas. También cubren rutas con caracteres especiales, la ausencia del ASS, recuperación explícita y preservación de versiones previas. Se simula la representación sin signo del código de salida de Windows.

Para completar la validación de la incidencia en Windows, actualiza la rama de desarrollo, usa el mismo video y vuelve a intentar la exportación con los ajustes originales. Si falla, conserva el primer error de FFmpeg y su contexto desde el registro antes de confirmar una alternativa.
