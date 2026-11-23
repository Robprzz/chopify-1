# Live Clipping 1.1 · Estabilidad y recuperación continua

## Qué fallaba

En 1.0, una excepción en `BaseAnalyzer.run` terminaba el hilo del analizador. Captura podía seguir en ejecución, aunque las señales y la transcripción de ese analizador ya no avanzaran. El código de Windows `4294967294` corresponde a `-2` como entero de 32 bits con signo. El código por sí solo no identifica el motivo del fallo del segmento 1. La versión anterior no retenía el stderr necesario para determinarlo. Además, la reconexión borraba segmentos y volvía a usar los mismos nombres mientras podían seguir en uso.

## Comportamiento nuevo

- FFmpeg registra los segmentos cerrados en su lista CSV. Análisis, verificación del formato y exportación adquieren una referencia temporal que impide que la limpieza borre lo que están leyendo. Se descartan segmentos ilegibles y se vuelve a intentar la captura si se encadenan tres fallos de verificación o pasan 30 s sin segmentos cerrados. La resolución adaptable sigue disponible si el video supera el máximo seleccionado.
- Cada analizador tiene un reintento por segmento. Si vuelve a fallar, registra el código, el tiempo límite o el stderr depurado y avanza al siguiente. Esas métricas y el último fallo se conservan en el panel. Se limita el trabajo atrasado a los 24 segmentos más recientes por pasada.
- La reconexión renueva las entradas y abre un directorio distinto. Empiezan de nuevo los índices, las señales y las palabras de análisis. La cronología visible mantiene su desplazamiento de sesión. YouTube vuelve a intentar tras finalizar o perder la entrada, con espera progresiva de hasta 30 s mientras Studio esté activo. Un HLS directo finito puede terminar normalmente.
- Se mide el score contra muestras anteriores y se ponderan únicamente señales recientes disponibles. YouTube funciona sin IRC de chat. El panel distingue ausencia de señales de un score medido de cero.
- El mínimo predeterminado es **6/10** en Live y Automation. Al iniciar desde Studio, dejar vacío el campo Live usa el ajuste guardado de Automation. Introducir un valor en Live aplica ese umbral a la sesión. Las preferencias guardadas se respetan. Automation vuelve a aplicar su propio mínimo al revisar exportaciones.
- La exportación manual admite **30, 60 o 90 segundos** del tramo continuo disponible. Si hay menos, muestra la duración guardada. Usa el último tramo cerrado, reutiliza los subtítulos ya analizados, respeta el formato elegido y aplica la marca de agua configurada. Puede guardar el MP4 con subtítulos pendientes si la voz aún no se transcribió. La exportación manual tiene score nulo y entra en Automation para revisión. El consentimiento humano sigue siendo obligatorio.
- Solo se procesa una exportación Live a la vez. Se acotan las muestras, los textos en memoria, el historial visible y el búfer. Al guardar 10 clips en una sesión, Live sigue capturando y mantiene disponible la exportación manual. Para cambiar ese límite de exportación automática desde la CLI, usa `--live-max-clips`. Los MP4 y las fuentes de las exportaciones guardadas se conservan. Si el disco baja de la reserva, Live se detiene con un diagnóstico. El resumen guarda los últimos 100 registros y los clips válidos más antiguos siguen disponibles en el catálogo. Las pruebas no cubren reinicio del sistema operativo, reautenticación del proveedor ni una transmisión real de 24 horas.

## Validación en Windows

1. En GitHub Desktop actualiza `feature/anqora-media-studio-v2` y abre Studio con el acceso habitual.
2. En **Live clipping**, selecciona el enlace activo, el máximo 720p o 1080p y el formato de exportación. Deja el umbral vacío para usar Automation o indica el valor de esta sesión.
3. Comprueba los indicadores **Captura, Audio, Video, Transcripción, Detección y Exportación**. Después de una interrupción, comprueba que se incremente el contador de reconexiones y que vuelvan a avanzar los segmentos procesados. Si un analizador falla, revisa el segmento, el código y el stderr mostrado.
4. Selecciona 30, 60 o 90 segundos y pulsa **Exportar últimos segundos**. Reproduce el clip y revisa audio, subtítulos y marca de agua. En **Automation → Centro de operaciones**, localiza el clip manual y confirma que aparece para revisión sin puntuación de detección. Puedes terminar ahí sin enviar nada a TikTok.

## Pruebas reproducibles

```bat
.venv\Scripts\python.exe -m pytest -q tests/test_live*.py tests/test_automation.py tests/test_production_selection.py
```

Las pruebas en Linux sirvieron HLS real en loopback y usaron FFmpeg y FFprobe para decodificar MPEG-TS, cambiar de generación, comprobar pistas y renderizar MP4. Se probó tanto HLS combinado como audio y video separados, con verificación audiovisual en la pista y en el clip. Se entregó un segmento corrupto real al analizador y se verificó la recuperación con el siguiente. Chromium usó Studio, el MP4 y el SRT reales. La inferencia de voz y la respuesta de yt-dlp para el enlace de YouTube se simularon. El DWORD de Windows y el tiempo límite se reprodujeron con respuestas de subprocess simuladas. La transmisión real de YouTube en Windows sigue pendiente.
