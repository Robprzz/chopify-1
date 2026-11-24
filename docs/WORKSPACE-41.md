# Anqora Media Studio 4.1

## Destinos de trabajo

| Destino | Responsabilidad |
| --- | --- |
| Centro de operaciones | Capturas activas, recomendaciones, clips recientes, alertas y cola técnica. |
| Live Studio | Nueva sesión, sesiones activas e históricas y reproducción de clips en Studio. |
| Proyectos | Creación, edición convencional, archivo y recuperación del proyecto activo. |
| Clips | Biblioteca de exportaciones de proyectos y de Live con reproducción, revisión y programación. |
| Revisión editorial | Ranking, propuestas, correcciones, aprobación y rechazo. |
| Publicaciones | Calendario, borradores y seguimiento con los controles de consentimiento existentes. |
| Estadísticas | Métricas operativas y confirmaciones de publicación. |
| Configuración | Reglas de Automation, Motor editorial, almacenamiento, papelera e integraciones. |

**Configuración → Inteligencia artificial → Motor editorial y modelos** abre la conexión con Ollama y los perfiles editoriales. **Configuración → Automation** abre los umbrales y límites. El procesamiento de la cola y su historial técnico se consultan en **Centro de operaciones**. **Revisión editorial** reúne las propuestas y el ranking para tomar decisiones.

## Proyectos, exportación y papelera

**Proyectos** conserva el proyecto seleccionado al cambiar de sección. **Continuar edición** recupera el contexto de ese proyecto y las selecciones que siguen abiertas en la pestaña. Al recargar se conserva el identificador del último proyecto y se consultan otra vez sus datos guardados.

Para exportar en vertical, selecciona **9:16** y el modo de encuadre y pulsa **Exportar selección**. Studio valida y guarda el formato visible junto con la entrada a la cola. Una prueba con Chromium y FFmpeg comprueba la salida final de 1080 × 1920 desde un proyecto que estaba guardado en 16:9.

Renombrar un proyecto conserva su identificador y sus rutas. Archivar permite separar los proyectos activos de los archivados con el filtro de la lista. Para retirar un clip o proyecto de la vista, usa **Enviar a la papelera**. **Configuración → Almacenamiento → Papelera y recuperación** permite restaurarlo.

La papelera registra referencias en `.anqora-trash.json` de cada espacio local. Los archivos, las transcripciones, las versiones y el historial se conservan en disco. Las rutas de medios dejan de servir los elementos retirados y su restauración devuelve el acceso. Restaurar un proyecto mantiene en la papelera los clips que se retiraron por separado. La papelera recuperable no libera espacio en disco. La entrega conserva los formatos de las bases de datos y sus historiales.

## Validación

Las pruebas de integración ejercitan el cambio de módulo, la recuperación del proyecto, un render convencional real, el reproductor de clips y la restauración a través de reinicios. La suite sigue usando fuentes HLS locales y servicios externos simulados cuando procede. La comprobación de la interfaz y de los flujos con datos reales en Windows sigue pendiente.

Capturas reales con un proyecto de demostración: [Centro de operaciones](../design/validation/workspace-41/centro-operaciones.png), [Proyectos en escritorio](../design/validation/workspace-41/proyectos-1366.png) y [Proyectos en móvil](../design/validation/workspace-41/proyectos-390.png).
