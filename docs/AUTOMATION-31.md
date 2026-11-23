# Automation 3.1 · Real-Time Operations

Automation actualiza el panel mientras está abierto. Cuando llega un clip elegible, vuelve a ordenar el conjunto y calcula las recomendaciones del día en la zona horaria configurada. El Centro de operaciones conserva la fórmula y el orden de prioridad de 3.0 para consultar la referencia original. El panel nuevo muestra ambos scores y conserva por separado detección y editorial.

## Puntuación temporal

| Factor | Peso inicial | Evidencia |
| --- | --- | --- |
| Calidad y potencial general | 40 % | Detección original y evaluación editorial. Cuando los criterios independientes están completos, el componente editorial excluye interacción para evitar contarla dos veces. |
| Oportunidad | 30 % | Edad desde el acontecimiento, con una fecha y hora verificadas. |
| Potencial de interacción | 20 % | Criterio editorial de interacción cuando está separado de los demás criterios. |
| Diferenciación | 10 % | Evidencia de diversidad de Automation 3.0. |

La vigencia inicial del acontecimiento es 100 % hasta cinco minutos, 85 % a los 15, 65 % a los 30, 40 % a los 60, 20 % a los 120 y 10 % desde los 180. La curva interpola entre los puntos. Es una heurística configurable. Los umbrales iniciales de **Urgente**, **Alta** y **Normal** son 8,5, 7,5 y 6,0. Por debajo del último umbral se muestra **Baja**.

Los registros distinguen Live, convencional y atemporal. Introduce una hora verificable desde **Hora del acontecimiento** si conoces el instante original. Studio usa la zona del navegador al registrar esa hora y la conserva como un instante absoluto. Las fuentes Live actuales conservan cuándo detectó Studio el momento. Ese dato puede incluir latencia del proveedor y se usa para medir el flujo interno. El renderizado, la llegada del segmento y el reloj del servidor tampoco establecen la fecha del acontecimiento. Los clips sin referencia muestran **Sin verificar**. Atemporal y sin fecha se evalúan repartiendo el peso entre las señales disponibles. Ambos pueden llegar a **Alta**. **Urgente** requiere una referencia verificable.

La detección sigue requiriendo al menos 6/10 o el mínimo mayor configurado. Los controles de artefactos, subtítulos y calidad editorial, las revisiones vigentes y el consentimiento de publicación siguen aplicándose. El cupo nuevo es cinco recomendaciones al día y puede ajustarse entre uno y cinco. Los valores guardados se conservan. Cuando faltan clips aptos y diferenciados, quedan plazas vacías. La selección compara SHA-256, intervalos solapados, equivalencia textual y una similitud contextual conservadora basada en secuencias de palabras. Las recomendaciones son una lista para revisión. Producción y publicación conservan sus propios controles.

## Colas y mediciones

- **Live** ordena el trabajo ya preservado según detección y recencia operativa. **Automation** favorece eventos verificados recientes y luego las entradas Live recientes cuya hora original se desconoce. La cola **editorial** favorece trabajos de producción con detección conocida. Después de 15 minutos, cada cola despacha por antigüedad entre los trabajos que superaron esa espera. El trabajo activo termina con la misma política de recursos, reintentos y recuperación.
- Los puntos auditados incluyen preservación en Live, las colas de producción y editorial, el turno compartido de Whisper, Ollama y FFmpeg y la validación de subtítulos. Una cola saturada sigue respetando su capacidad. La captura continua y la exportación manual permanecen disponibles según las condiciones de Live 1.2.
- Las mediciones nuevas registran detección, preservación, espera, transcripción, render, control técnico y disponibilidad. Automation guarda la hora de su primera toma del trabajo y el tiempo de validación. La cola editorial conserva el inicio real y la evaluación observada. Los clips convencionales nuevos también guardan los tiempos de su render y de la preparación de la fuente cuando esa medición existe. Las aprobaciones se registran por versión. Para TikTok se muestra el consentimiento local y el momento en que Studio observó `PUBLISH_COMPLETE` en el historial de la cuenta conectada. No se infiere la hora efectiva a la que el video se hizo visible.
- El panel muestra mediana y P95 descriptivos con el número de muestras, de hasta 120 clips con evidencia vigente. Los tiempos históricos faltantes permanecen vacíos. La estimación de cola requiere cinco duraciones observadas para el servicio correspondiente. Puede quedar vacía si la cola se pausa o el trabajo activo ya superó la mediana. Las estimaciones son para iniciar el trabajo y cambian cuando llegan otros clips.

Telegram Control Center conserva sus controles y funciona por separado. Automation y sus decisiones en Studio se pueden utilizar con Telegram desactivado o inaccesible.

## Validación en Windows

1. Actualiza `feature/anqora-media-studio-v2` en GitHub Desktop y abre Studio. En **Automation**, revisa **Recomendaciones diarias**. Ajusta a cinco si tu carpeta tenía guardado un cupo de tres.
2. Deja terminar dos clips diferentes. Comprueba que se incorporen al panel sin recargar la página. Abre **Hora del acontecimiento** y guarda las horas originales verificables. Observa cómo cambia **Prioridad temporal** mientras se conservan los scores de detección, editorial y calidad original. Prueba también un clip atemporal y otro con hora desconocida.
3. Abre **Detalles** para consultar el desglose y los tiempos. Reproduce un clip y registra una decisión editorial. La decisión aparece en el historial de Automation. Una solicitud de corrección queda disponible para el operador. El envío a TikTok sigue requiriendo revisión y consentimiento explícito en Publicaciones.
4. Revisa la cola y el panel **Latencias medidas** durante un Live de prueba. Verifica si hay muestras suficientes para mostrar una espera estimada. Comprueba que Automation siga disponible con Telegram desactivado. Repite tras reiniciar Studio para comprobar que se conservan referencias, decisiones y trabajos.

Las pruebas de ranking y de tiempos usan datos controlados. Chromium comprueba la actualización y las decisiones con el servidor HTTP real. La regresión de Live usa HLS local y FFmpeg real. Las mediciones obtenidas aquí no representan la latencia de tu equipo Windows, la de YouTube ni un resultado real de publicación.
