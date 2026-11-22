# Automation 2.4.1 · Comprensión temática e identidad editorial

## Auditoría del incidente de Windows

Qwen3:4b devolvió «muy importante. Qué tal, hermano?» como tema principal, con 154 de 154 subtítulos leídos, 17 pasajes y 93% de cobertura del habla. La ejecución registró incertidumbre alta, puntuación cero, nueve campos pendientes y ningún campo corregido. El informe incluye una incidencia de cifra sin respaldo. La cobertura mide cuánto diálogo se procesó. La interpretación se comprueba por separado.

| Hallazgo en 2.4 | Efecto | Cambio en 2.4.1 |
| --- | --- | --- |
| La síntesis dependía de los temas generados para cada ventana. Antes de redactar se exigía alguna cita respaldada, aunque la cita fuera incidental o no justificara el tema. | Un saludo podía aparecer como tema principal mientras el asunto seguía en el cuerpo del clip. | Se filtran señales conversacionales, se contrasta tema y mensaje con los pasajes citados y se revisan la recurrencia, las entidades y el asunto narrativo. Una reparación temática usa citas del clip y debe verificarse antes de redactar. |
| Los metadatos entraban como contexto auxiliar, sin un diagnóstico de qué coincidía con el diálogo. | Un resumen podía mezclar un tema corroborable con detalles que solo aparecían en el título original. | El análisis registra las coincidencias temáticas y el contexto auxiliar sin corroborar. Los metadatos solo orientan hipótesis. La redacción y la revisión reciben citas del clip. |
| Las cifras se cotejaban como una bolsa de números de los pasajes. | Los dígitos de un edificio podían confundirse con una cantidad o una cifra independiente. | Se verifica el nombre completo y se distingue de cantidades, estadísticas, fechas, direcciones e identificadores. La revisión independiente sigue comprobando el sentido y la atribución. |
| El diagnóstico contabilizaba reintentos y campos cambiados sin la traza de cada parche. | El historial del incidente no permite saber si se agotó el tiempo, si se consumieron los intentos en otra etapa o si Ollama respondió con un parche incompleto o inválido. | Cada intento registra ámbito, diagnóstico, tiempo restante, respuesta estructurada segura, errores y resultado. El motor indica el motivo cuando ya no puede intentar una reparación. |

El MP4 y la transcripción del incidente no estaban disponibles en el entorno de desarrollo. Los tests usan un relato ficticio con cobertura y síntomas equivalentes. El relato y los nombres del test son datos sintéticos.

## Identidad editorial

En **Automation → AI Engine → Identidad editorial** puedes editar nombre, organización, perspectiva, idioma, estilo, tono, objetividad y creatividad. Pulsa **Guardar configuración de AI Engine**. Los ajustes se guardan en SQLite y se aplican a los proyectos de la carpeta activa, incluidos clips convencionales y Live, tanto desde la cola como en generación manual.

La configuración inicial es **Anqora Media Studio**, medio digital, tercera persona, español, estilo informativo, natural y atractivo, tono adaptado al contenido, objetividad alta y creatividad moderada-alta. También se puede elegir voz impersonal y conservar el idioma del clip. El análisis coteja el tema en el idioma del diálogo. Si el idioma editorial es distinto, se localizan el tema y el mensaje después de verificarlos y se conservan las citas en su idioma original. Fidelidad y prohibición de sensacionalismo engañoso son controles obligatorios.

El medio narra desde fuera del relato. El motor verifica las citas atribuidas en primera persona contra el pasaje correspondiente. Los extractos de respaldo siguen sirviendo como material de revisión; se distinguen en el historial de la redacción semántica. Cambiar la identidad invalida la caché editorial para el nuevo resultado. El historial y las ediciones previas se conservan.

## Diagnóstico y límites

- **Evaluación y evidencia** muestra el tema, el mensaje, las citas y los campos de contexto auxiliar. Una coincidencia temática indica que el diálogo incluye un asunto del metadato. Las afirmaciones concretas siguen requiriendo citas.
- **Diagnóstico de ejecución → Intentos de reparación** muestra si el motor reparó primero la comprensión o un campo de redacción. Se separan el límite de intentos, el tiempo consumido, los errores de proveedor y el esquema. Solo se presenta la parte estructurada, segura y acotada de la respuesta. Si Ollama no entrega JSON utilizable, se registra el error sin guardar el cuerpo bruto.
- Cuando no se puede verificar el tema, el trabajo queda en revisión y no genera propuestas a partir del resumen dudoso. Los tiempos y el límite de reintentos se comparten entre análisis, generación y correcciones.
- El cotejo numérico reconoce formas frecuentes con dígitos. La revisión semántica complementa este control en otras expresiones, reformulaciones o contextos ambiguos. Una transcripción incorrecta conserva su carácter de testimonio y puede requerir revisión humana.

## Reprocesar el mismo clip en Windows

1. Cierra Studio. En GitHub Desktop selecciona `feature/anqora-media-studio-v2`. Pulsa **Fetch origin**, luego **Pull origin**, y comprueba el commit publicado en **History**.
2. Abre Chopify con el acceso directo habitual. Si usas CMD, ejecuta `"Setup Chopify Studio.cmd"` desde el repositorio. Comprueba `ollama list` y selecciona `qwen3:4b` en **Automation → AI Engine**.
3. Revisa y guarda tu **Identidad editorial**. Selecciona el mismo MP4 en **Propuestas editoriales → Clip terminado**. Abre **Opciones de generación y recuperación** y pulsa **Reprocesar resultados con puntuación cero**. El trabajo nuevo conserva la referencia de la versión anterior. Si el resultado anterior está fallido, usa **Reprocesar trabajos fallidos**.
4. Abre el resultado nuevo. Comprueba tema, mensaje, contexto auxiliar, nombres con números y citas contra el clip. Si queda en revisión, anota el motivo en **Diagnóstico de ejecución**. Si se agotó el tiempo, ajusta **Tiempo máximo** dentro del rango permitido y repite el análisis del clip. El flujo usa el MP4 existente.
5. Para medir el modelo, ejecuta el benchmark local con Ollama activo. El benchmark conserva el idioma original para comparar las rutas con las mismas condiciones. Usa clips reales y revisión humana adicional para aceptar la calidad editorial de producción.

   ```bat
   .venv\Scripts\python.exe tools\benchmark_editorial.py --provider ollama --model qwen3:4b --timeout 120 --output editorial241-qwen3-windows.json
   ```

6. Si quieres comprobar el paso a Publicaciones, abre la propuesta y verifica que el consentimiento esté desmarcado. Puedes detener la prueba en esa pantalla sin enviar el video.

La regresión completa obtuvo **654 pruebas aprobadas, 0 fallidas y 1 omitida** con Python 3.11.18 en Linux. La omitida requiere DPAPI nativo de Windows. Las regresiones de desarrollo usan respuestas simuladas de Ollama, SQLite y el servidor HTTP real de Studio. Chromium verifica que la identidad persista tras reiniciar. El benchmark simulado de 32 transcripciones produjo 28 resultados utilizables de 29 elegibles y rechazó los tres casos insuficientes. No se ejecutó Qwen3:4b real en este entorno. La aceptación del clip reportado queda para la prueba en Windows.
