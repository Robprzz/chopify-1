# Anqora Media Studio 5.0

En Publicaciones, **Preparar varios clips** guarda borradores por plataforma con su caption correspondiente. Una selección con duplicados se rechaza completa. Se puede editar y programar cada plan en el calendario existente.

Configuración → Publicaciones permite elegir Manual, Asistido o Automatización autorizada. Asistido es el modo inicial. Selecciona las plataformas para activar la preparación automática. Studio verifica un elemento por ciclo cuando hay recursos y reutiliza las propuestas editoriales compatibles disponibles. El editor permite regenerar y guardar captions por separado. Los modos conservan el consentimiento por operación y no autorizan envíos por sí solos.

| Destino | Acción disponible | Pendiente |
| --- | --- | --- |
| TikTok | Entrega al buzón y consulta de estado con video.upload | Direct Post requiere aprobación, permisos y una prueba autorizada |
| YouTube Shorts | Paquete manual | Integración OAuth y subida autorizada |
| Facebook Reels | Paquete manual | Integración y permisos oficiales de una página |
| Instagram Reels | Paquete manual | Integración y permisos para cuenta profesional |

Studio debe estar disponible para la entrega local programada al buzón. Una entrega incierta requiere verificación. La entrega al buzón conserva su estado separado de la publicación confirmada.

## Métricas

En Estadísticas, selecciona plataforma y cuenta, carga un CSV y confirma la vista previa. Las columnas requeridas son post_id, published_at, observed_at, views, likes, comments y shares. Las fechas requieren zona horaria ISO 8601. Los datos se conservan con procedencia de importación. Los totales usan la última observación por publicación.

Para sugerir un horario con datos, escribe el mismo identificador de cuenta en el formulario del plan. El recomendador compara la mediana de reacciones, comentarios y compartidos. Exige cinco publicaciones por franja en al menos dos franjas. Cuando faltan datos usa las horas configuradas y los huecos del calendario.

## Persistencia y alcance

Las tablas de métricas, configuración y preparación se añaden al SQLite de 4.2. Los planes, captions y recibos existentes conservan su formato. La preparación reutiliza el catálogo y las verificaciones de medios. El registro de resultados evita volver a preparar una versión después de reiniciar.

La preparación automática reutiliza captions disponibles. La generación completa por lote, la evaluación editorial ampliada, la ingesta oficial de métricas y el análisis por tema o duración quedan pendientes.

[Capturas de Chromium](../design/validation/workspace-50)
