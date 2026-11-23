# Configuración y marca de agua en Studio 4.0.1

**Configuración** muestra nueve filas de ancho completo. El buscador filtra las categorías por nombre, descripción y controles. **General** y **Publicaciones** se despliegan en la misma página. Las demás filas abren las herramientas existentes.

## Cambiar la marca de agua

1. Abre **Configuración → Edición y subtítulos → Marca de agua**.
2. Pulsa **Cambiar imagen** y selecciona un PNG de hasta 2 MiB y 2048 × 2048 píxeles. Studio comprueba el tamaño, la cabecera y la decodificación antes de mostrarlo. **Imagen lista** indica que el archivo está preparado en el formulario.
3. Elige **16:9**, **9:16** o **1:1** para simular la composición. Los valores guardados de posición, tamaño, opacidad y margen se conservan al cambiar la imagen. El margen está en **Ajustes avanzados**.
4. Marca **Activar marca de agua** si quieres mostrarla en los próximos clips y pulsa **Guardar cambios**. Espera el mensaje **Marca de agua guardada**. Este mensaje confirma la respuesta del servidor.
5. Para desactivar temporalmente la marca, quita la marca de la casilla y guarda. Para retirar la imagen, usa **Quitar imagen**, confirma y guarda.

Una selección inválida conserva la vista previa anterior. Si falla la escritura de la configuración, Studio conserva el PNG activo y muestra un error. La marca de agua se configura por carpeta de proyectos y se aplica a exportaciones nuevas. El logotipo de Studio y los clips ya exportados conservan sus propios archivos.

## Validación

Las pruebas en Chromium ejercitan el selector de archivos, los PNG válidos, el límite de 2 MiB, el límite por dimensiones, una imagen dañada, la persistencia y la confirmación de retirada. Las pruebas de FFmpeg inspeccionan píxeles de la imagen nueva y verifican SAR, duración y sincronización en los cuatro modos Smart Camera y una salida horizontal. Los rostros de la prueba de composición son observaciones controladas.

| Vista de Chromium | Antes | Después |
| --- | --- | --- |
| Configuración 1920 × 1080 | [Captura](../design/validation/settings-401/settings-antes-1920x1080.png) | [Captura](../design/validation/settings-401/settings-despues-1920x1080.png) |
| Configuración 1366 × 768 | [Captura](../design/validation/settings-401/settings-antes-1366x768.png) | [Captura](../design/validation/settings-401/settings-despues-1366x768.png) |
| Configuración 390 × 844 | [Captura](../design/validation/settings-401/settings-antes-390x844.png) | [Captura](../design/validation/settings-401/settings-despues-390x844.png) |
| Marca de agua 1366 × 768 | [Captura](../design/validation/settings-401/watermark-antes-1366x768.png) | [Captura](../design/validation/settings-401/watermark-despues-1366x768.png) |
| Marca de agua 390 × 844 | [Captura](../design/validation/settings-401/watermark-antes-390x844.png) | [Captura](../design/validation/settings-401/watermark-despues-390x844.png) |

En Windows, vuelve a abrir Studio tras actualizar la rama de desarrollo. Selecciona un PNG distinto del guardado, guarda y recarga. Exporta un clip corto en 9:16 y 16:9 para comprobar el logo nuevo. La experiencia del selector nativo, el clip real que motivó Smart Camera y el rendimiento con la RTX 2000 Ada requieren validación en ese equipo.
