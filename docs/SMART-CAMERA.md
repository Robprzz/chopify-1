# Smart Camera Engine 1.0

## Diagnóstico

El render anterior elegía en cada muestra el rostro de mayor área ponderada, aunque no hubiera evidencia de que hablara. Emitía órdenes discretas de recorte a partir de muestras de 5 Hz y trataba un cambio grande de centro como un salto, aun sin evidencia de corte de escena. Además, escalar un recorte de 202 × 360 a 1080 × 1920 podía conservar un SAR de 404:405. Ambos comportamientos se reprodujeron con una muestra controlada y el render del commit `a0efb33`.

El archivo `5ff813a67df4-clip.mp4`, su fuente y una exportación horizontal del mismo intervalo no estaban disponibles. Los tres intervalos de 66,7 ms y los tramos con poco movimiento del informe externo siguen sin atribución verificada. Un desplazamiento de recorte, un salto de PTS, un cuadro repetido durante captura y una escena inmóvil son señales distintas.

La captura HLS mantiene su reloj y su cola de producción independiente. La normalización de entrega ocurre después de ensamblar el fragmento preservado. El motor usa muestras de baja resolución para detección y compila el encuadre antes de renderizar. Conserva la exclusión de recursos compartidos que usa el pipeline Live para edición. La detección y el análisis del render vertical se ejecutan localmente en CPU. Smart Camera vertical usa YuNet si ya está instalado y Haar en caso contrario.

## Modos

| Modo | Comportamiento |
| --- | --- |
| Automática | Usa seguimiento suave si hay evidencia suficiente. Si verifica dos posiciones estables puede usar dos paneles. Si la evidencia es débil, mantiene un encuadre estable. |
| Smart Camera | Sigue la posición del sujeto usando continuidad espacial, zona muerta y límites de velocidad y aceleración. Mantiene el sujeto ante oclusiones breves y exige evidencia persistente antes de cambiarlo. |
| Split Screen | Usa dos posiciones estables identificadas en la muestra o centros configurados manualmente. Si no hay dos posiciones verificadas ni centros explícitos, usa un encuadre estable. |
| Encuadre estable | Conserva el video completo sobre fondo desenfocado en fuentes de otra proporción. La configuración de contención existente conserva sus barras. |

El selector se encuentra en Live Studio y en la edición de proyectos. La geometría manual de dos paneles y el montaje de juegos siguen disponibles bajo Configuración avanzada de encuadre. En CLI está disponible `--camera-mode auto|smart|split|stable`. Los proyectos y trabajos Live anteriores usan el modo automático cuando carecen de ese campo. Los modos heredados de montaje guardados se conservan en modo Automática.

El seguimiento estima continuidad de posición, no identidad biométrica ni quién está hablando. Un cambio grande de luminancia y de su distribución permite reiniciar la cámara ante un corte visual corroborado. La salida interpola el origen de recorte entre muestras y separa esos cortes del movimiento de cámara. La cobertura, cambios de sujeto, inversiones rápidas y picos de movimiento quedan en los metadatos.

## Verificación de entrega

Para vertical final, se comprueban 1080 × 1920, SAR 1:1, DAR 9:16, H.264 y yuv420p. Si existe audio, debe ser AAC y sus límites de pista deben estar a menos de 220 ms de los del video. Los PTS se comprueban en orden de presentación y el DTS en orden de decodificación. Se conserva la cadencia CFR medida en fuentes regulares. Con VFR se selecciona una tasa racional próxima al intervalo mediano observado. FFmpeg puede repetir o descartar muestras para distribuirlas a esa tasa, sin crear imágenes intermedias. La medición de velocidad de origen usa hasta 241 paquetes próximos al inicio del intervalo.

La inspección de imagen distingue pausas de 600 ms o más y las compara con la fuente preservada. Una pausa que coincide con la fuente se conserva. Ante una pausa introducida por Smart Camera o Split Screen se prueba una sola vez un encuadre estable y se vuelve a verificar. Si persiste el defecto, se bloquea el MP4 antes de publicar el archivo en la carpeta de destino. La prueba de continuidad audiovisual y subtítulos de Automation sigue activa. Los clips nuevos incluyen métricas de movimiento en los metadatos y en el diagnóstico de Automation. Cuando no hay acceso al original, el motor no puede distinguir de manera concluyente una imagen naturalmente estática de una pausa en la propia captura. Los defectos más breves que el umbral pueden requerir revisión manual.

Los subtítulos usan los mismos tiempos de la transcripción y la misma composición ASS. La marca de agua respeta la configuración. El control añade una lectura local de baja resolución y un sondeo de paquetes. Un render con pausas sospechosas puede requerir una segunda codificación. Se limita a dos hilos de codificación para vertical y no se reservan recursos adicionales de GPU para este motor.

## Comparativa reproducible

Un clip de cuatro segundos contiene un fondo de luminancia creciente y un marcador que se desplaza a velocidad conocida. Se inyectaron las mismas observaciones de posición a 5 Hz en ambos motores. Las pruebas del algoritmo incluyen dos posiciones, oclusión, cortes, cambios de sujeto y congelamientos de origen frente a pausas introducidas. FFmpeg real valida 24, 25, 30 y 60 FPS, VFR sin interpolación de imágenes, 9:16 y 16:9. Las pruebas Live usan fuentes HLS locales, combinadas y separadas. Las observaciones de posición de la comparativa son controladas.

| Medición | Exportación anterior | Smart Camera |
| --- | ---: | ---: |
| Cuadros y duración | 120 / 4 s | 120 / 4 s |
| SAR y DAR | 404:405 / 101:180 | 1:1 / 9:16 |
| Intervalo de cuadro máximo | 33,334 ms | 33,334 ms |
| Desfase A/V de inicio y fin | 0 ms | 0 ms |
| Intervalos con cambio de luminancia en la franja superior | 20 / 119 | 101 / 119 |
| Salto máximo de luminancia en esa franja | 7,25 | 3,5 |
| Tiempo de render, controles y poster en una ejecución Linux | 4,25 s | 6,39 s |

La métrica de luminancia es un indicador de desplazamiento de recorte para esta muestra, no una métrica general de calidad subjetiva. Los tiempos se midieron en el entorno de pruebas y no representan rendimiento en la RTX 2000 Ada.

[Fuente horizontal](../design/validation/smart-camera/fuente-horizontal.mp4) · [Vertical anterior](../design/validation/smart-camera/vertical-antes.mp4) · [Vertical Smart Camera](../design/validation/smart-camera/vertical-smart-camera.mp4)

## Validación pendiente en Windows

Exporta el mismo intervalo del archivo fuente del incidente en 16:9, 9:16 con Smart Camera y 9:16 con Encuadre estable. Conserva la exportación inicial y sus archivos laterales. Compara los PTS en las posiciones reportadas, revisa la reproducción local con y sin aceleración por hardware y comprueba el movimiento de cámara, el audio y los subtítulos en un directo representativo. La comparación de esta incidencia y el rendimiento en la RTX 2000 Ada siguen pendientes.
