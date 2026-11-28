# Content Discovery 6.0

Content Discovery organiza publicaciones recientes de canales de YouTube para revisión editorial. Usa el mismo monitor local y la misma base `.youtube-monitor.sqlite` que **Proyectos → Canales monitoreados**. La primera apertura de Studio aplica una migración aditiva y conserva los canales, los videos, los estados de producción y los vínculos a proyectos que ya existen.

## Primer uso en Windows

1. Cierra Studio. En GitHub Desktop, selecciona `feature/anqora-media-studio-v2` y pulsa **Fetch origin** y **Pull origin**. Abre Studio con el acceso directo habitual.
2. Ve a **Content Discovery → Fuentes**. Agrega un canal mediante su ID `UC…`, su `@handle` o una URL HTTPS oficial de su canal.
3. Deja la modalidad **Solo descubrir** y la frecuencia de **180 minutos**. Puedes habilitar o pausar cada fuente y ajustar su categoría y frecuencia.
4. Pulsa **Actualizar fuentes** si quieres adelantar una consulta elegible. Mantén Studio abierto y revisa **Descubrir** cuando llegue el feed. La consulta se ejecuta en segundo plano y respeta las esperas vigentes.
5. Busca por palabras o filtra por fecha de publicación (días UTC), canal, categoría, estado y derechos. Usa **Vista previa y revisión** para consultar la descripción, abrir el video original en una pestaña nueva y registrar tus notas.
6. Guarda, aprueba o descarta la oportunidad. Las guardadas y aprobadas aparecen en **Biblioteca**. El filtro **Descartada** en Descubrir permite volver a revisar y recuperar una oportunidad.
7. Reinicia Studio y comprueba que tus fuentes, reglas, guardados y notas se mantienen. En **Rendimiento** verás los recuentos actuales, los proyectos existentes vinculados al monitor y el tiempo registrado al guardar revisiones.

Con **Solo descubrir**, el monitor almacena título, fecha y descripción pública cuando están disponibles. La consulta no inicia yt-dlp, Whisper, FFmpeg ni crea proyectos. Los canales existentes mantienen su modalidad de producción hasta que la cambies. Si eliges **Descubrir y producir** para un canal, se aplican las reglas y la autorización de **Canales monitoreados**. Activar esta modalidad no encola retroactivamente lo descubierto.

## Ranking editorial

En **Fuentes → Reglas de ranking y temas**, escribe un tema por línea con sus palabras o frases clave, por ejemplo `Energía: energía solar, baterías`. El ranking compara esas expresiones con el título y la descripción, sin distinguir mayúsculas o tildes. Actualidad usa la fecha de publicación y baja linealmente a cero en 14 días. El potencial de conversación y la narrativa usan marcadores observables en los metadatos, como una pregunta en el título o las expresiones `entrevista` y `cronología`.

| Dimensión | Peso inicial |
| --- | ---: |
| Afinidad temática | 30% |
| Actualidad | 25% |
| Potencial de conversación | 20% |
| Narrativa | 15% |
| Información verificable | 10% |

Los pesos deben sumar 100%. Cada ficha muestra el porcentaje de peso que se pudo evaluar y explica sus señales. El resultado es el promedio ponderado de las dimensiones evaluadas. Si el peso evaluado es cero, la ficha aparece sin puntuación. La verificación de afirmaciones queda pendiente hasta disponer de evidencia externa. Las tarjetas muestran relevancia editorial aproximada a partir de metadatos públicos.

## Alcance de estas fases

- YouTube puede entregar solo una parte de sus publicaciones recientes o devolver errores temporales. Los registros locales siguen disponibles sin conexión. **Fuentes** muestra el error y el siguiente intento. La actualización manual respeta la espera de YouTube y el intervalo mínimo de 30 segundos entre consultas manuales.
- El monitor consulta las fuentes una a una mientras Studio está abierto. Admite hasta 32 canales en la base compartida. Cada consulta usa el feed Atom público de YouTube y las páginas de canal solo cuando hace falta resolver un `@handle`.
- El tiempo de revisión acumula los segundos con el diálogo visible en revisiones guardadas, hasta una hora por acción. Aprobar una oportunidad registra una decisión editorial. Confirmar derechos requiere una referencia explícita.
- **Enviar a proyecto** se reserva para la fase siguiente. El servicio interno prepara un plan únicamente con aprobación editorial y derechos documentados. También verifica los vínculos y el identificador de video en proyectos locales. El plan todavía no se puede solicitar desde Studio y no dispara descargas.

## Pruebas

Las pruebas de Content Discovery usan bases temporales, respuestas Atom controladas y Chromium contra el servidor HTTP real de Studio. La conexión de prueba con el feed oficial no obtuvo respuesta desde el entorno de desarrollo. En Windows, comprueba la conexión con un canal público propio o autorizado. Verifica que los videos aparezcan y que **Proyectos** conserve su listado durante **Solo descubrir**.
