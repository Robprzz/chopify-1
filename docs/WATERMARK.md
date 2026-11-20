# Marca de agua y estabilización de Live clipping

## Actualizar Studio en Windows

Cierra Studio con **Ctrl+C**. Desde la carpeta del repositorio ejecuta lo siguiente.

```powershell
git switch feature/anqora-media-studio-v2
git pull --ff-only origin feature/anqora-media-studio-v2
.\.venv\Scripts\python.exe dashboard.py
```

Abre `http://127.0.0.1:8765`. Si usabas `--projects-dir`, vuelve a iniciar Studio con la misma carpeta para recuperar tus proyectos y ajustes. Python, FFmpeg, FFprobe y OpenCV son dependencias existentes. Esta entrega tampoco cambia la instalación de `tzdata` de Automation.

## Configurar y comprobar el PNG

1. Abre **Marca de agua**, elige un PNG de hasta 2 MiB y 2048 × 2048 y marca **Activar marca de agua**. La imagen puede tener transparencia.
2. Selecciona ancho, opacidad, margen y posición. El ancho se expresa como porcentaje del video y el margen como porcentaje de su lado más corto. La marca se reduce cuando hace falta para que quepa entera. La opción Centro coloca su caja en el centro del fotograma.
3. Elige 16:9, 9:16 o 1:1 en **Formato de vista previa**. Esta selección cambia solo la vista previa de colocación. El formato real se elige en el proyecto o al iniciar Live. Guarda los ajustes.
4. Usa la vista previa de un clip del proyecto o expórtalo. Reproduce el MP4, preferiblemente también con subtítulos activos. En **Publicaciones** elige la versión que quieres revisar. La vista previa, el clip y el archivo disponible para Publicaciones usan la misma composición.
5. Reinicia Studio y comprueba que los ajustes se recuperan. Para sustituir la imagen, elige otro PNG y guarda. **Quitar imagen** la retira de la configuración y desactiva la marca en futuras exportaciones. Las copias administradas de imágenes anteriores se conservan para que los renders en curso puedan terminarlas.

La configuración y el PNG importado se guardan en `.chopify-watermark/` dentro de la carpeta de proyectos. El servidor sirve solo la imagen seleccionada a través de la ruta local de vista previa. Si el archivo activo falta o cambia, la exportación falla con un mensaje antes de generar un MP4 nuevo. Cambiar los ajustes permite generar versiones nuevas sin reemplazar versiones anteriores.

## Auditoría y diseño

El logo anterior se cargaba desde un preset por proyecto. Guardar el propio preset solo seleccionaba su nombre en el formulario. Hacía falta guardar además los ajustes del proyecto. El diálogo de Live carecía de esa selección y su escritor pasaba la ruta del preset directamente a un render que esperaba un diccionario. El filtro anterior fijaba la posición y el margen.

La marca global se lee al renderizar. El compositor aplica PNG, escalado proporcional y opacidad sobre el fotograma ya encuadrado, y compone los subtítulos por encima. La segunda entrada recibe una ruta de archivo como argumento de FFmpeg, apta para nombres con espacios. El filtro se ejecuta dentro de la codificación existente y el MP4 se publica en su carpeta final cuando FFmpeg completa la exportación. El preset anterior sigue disponible para los proyectos que lo usan. Cuando la marca global está activa, solo se dibuja esa marca y se conservan los estilos de subtítulos del preset.

## Verificar Live clipping

1. Abre **Live clipping**, pega la URL de un directo que tengas autorización para capturar y elige 720p o 1080p y el aspecto de salida.
2. En un directo con audio y video separados, deja que Studio capture varios segmentos. Cuando se genere un clip, reprodúcelo y comprueba movimiento, audio, subtítulos y marca. Confirma que aparece en **Publicaciones** y detén la sesión.
3. Si inspeccionas el MP4 desde PowerShell, comprueba sus pistas con el siguiente comando.

```powershell
ffprobe -v error -show_entries stream=codec_type,codec_name,width,height -of json "ruta\al\clip.mp4"
```

Si falta el códec de una pista seleccionada, Studio confirma su tipo con FFprobe y con sus propios encabezados HTTP. Dos entradas que no se puedan verificar como una pista de video y otra de audio se rechazan. El enlace de YouTube indicado para esta tarea devolvió HTTP 403 desde el proxy del entorno de desarrollo. La comprobación del directo en Windows sigue pendiente. [Guía de Live clipping](LIVE.md)

## Evidencias sintéticas

Las pruebas extraen fotogramas del MP4 y comparan los píxeles de la marca con la posición esperada, su tamaño, su opacidad y sus zonas transparentes. También verifican que los subtítulos se dibujan. Se cubrieron las cinco posiciones y los diseños de una cámara, pantalla dividida y cámara sobre gameplay.

| Evidencia | Resultado |
| --- | --- |
| [PNG de prueba](evidence/watermark/test-logo.png) | Imagen sintética con zonas opaca, semitransparente y transparente |
| [Horizontal 720p](evidence/watermark/horizontal-720p.png) | Fotograma 1280 × 720 con marca superior izquierda |
| [Horizontal 1080p](evidence/watermark/horizontal-1080p.png) · [MP4](evidence/watermark/horizontal-1080p.mp4) | Fotograma 1920 × 1080 con marca inferior derecha y subtítulos |
| [Vertical 1080p](evidence/watermark/portrait-1080p.png) | Fotograma 1080 × 1920 de pantalla dividida con marca superior derecha |
| [Cuadrado 1080p](evidence/watermark/square-1080p.png) | Fotograma 1080 × 1080 con marca inferior izquierda |
| [Live cuadrado](evidence/watermark/live-square-frame.png) · [MP4](evidence/watermark/live-square.mp4) | Clip de dos entradas HLS locales con marca verde y subtítulos. FFprobe identificó video y audio en tres segmentos y en el clip final. |

La prueba HLS simuló la respuesta de yt-dlp con la pista 234 sin `acodec`, además de la detección y el reconocimiento de voz. La consulta FFprobe, la captura HTTP, el mux, el render y la vista previa en Chromium se ejecutaron de verdad. La diferencia medida entre destello y tono fue de 18,58 ms en el segmento y 58,58 ms en el MP4. Las pruebas de Publicaciones inspeccionaron archivos locales y no enviaron videos a TikTok.

La regresión completa obtuvo 564 resultados aprobados con Python 3.11, FFmpeg 7.1.5 y Chromium en Linux. Incluye el panel de Automation Fase 1 y la persistencia, sustitución y retirada de la marca en Studio. El MP4 horizontal de evidencia tiene H.264 y AAC a 1920 × 1080 y 30 FPS. El clip Live cuadrado tiene H.264 y AAC a 1080 × 1080 y 25 FPS.

## Coste medido

En un render sintético de 2 s a 1920 × 1080, la mediana de tres ejecuciones sin y con marca fue de 0,96 s y 1,47 s de tiempo real, y 0,97 s y 1,74 s de CPU total en los procesos hijos. El pico de memoria residente de esos procesos rondó los 318 MiB en ambos casos. Son medidas de un clip pequeño en Linux. El coste variará con la resolución, el contenido y el equipo. La marca añade una composición al render existente y la verificación de Live solo lanza FFprobe para pistas con metadatos incompletos.
