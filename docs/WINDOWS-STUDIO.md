# Anqora Media Studio · Inicio desde Windows

## Primera configuración

1. Cierra el servidor anterior y abre GitHub Desktop en `feature/anqora-media-studio-v2`. Usa **Fetch origin** y **Pull origin**. Conserva el repositorio y la carpeta de proyectos que ya utilizas.
2. En GitHub Desktop, abre **Repository → Show in Explorer**. Haz doble clic en **Setup Anqora Media Studio.cmd**. Este paso usa el Python de `.venv`. Si todavía falta el entorno, créalo una vez desde la carpeta del repositorio con el Python que ya tengas instalado.

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. En el navegador, sigue **Primera configuración**. Ejecuta el diagnóstico, confirma el entorno virtual y, si Studio pide FFmpeg, **elige la carpeta que contiene tanto `ffmpeg.exe` como `ffprobe.exe`**. Guarda la carpeta.
4. Comprueba Ollama si lo utilizas. Marca **Autorizar el inicio de Ollama local con Studio** si quieres ese comportamiento y guarda la preferencia. Comprueba que el modelo `qwen3:4b` aparece en la lista. Selecciónalo para guardarlo en la configuración de AI Engine de la carpeta de proyectos activa. Puedes usar Studio con las reglas locales mientras Ollama esté detenido o el modelo no esté instalado.
5. Conserva la carpeta de proyectos existente. Si antes usabas `--projects-dir`, indica esa ruta absoluta en el asistente. Cambiarla afecta el próximo inicio y deja los proyectos anteriores en su carpeta.
6. Si vas a usar TikTok, elige **Sandbox** o **Production** como perfil activo. En **Credenciales de la aplicación**, selecciona el perfil correspondiente, pega el Client Key y el Client Secret y pulsa **Guardar en Windows**. Después pulsa **Conectar cuenta** y autoriza la aplicación en la página oficial de TikTok. [Configuración de TikTok y URI de retorno](TIKTOK.md)
7. Pulsa **Crear o recrear acceso directo** cuando llegues a esa etapa. El asistente usa el Escritorio que Windows devuelve para el usuario, incluido el Escritorio redirigido. El acceso directo se llama **Anqora Media Studio** y apunta al `pythonw.exe` del entorno con la ruta del launcher y el directorio de trabajo de la instalación.
8. Pulsa **Terminar configuración**. Usa **Configuración → Diagnósticos → Cerrar al terminar los trabajos activos** para aplicar las preferencias de inicio y abre el acceso directo. En adelante usa ese acceso para abrir Studio.

Si iniciaste Studio con el navegador desactivado, la dirección para abrirlo es `http://127.0.0.1:8765/`. Si moviste la carpeta del repositorio, vuelve a ejecutar el asistente en la carpeta nueva. Un acceso directo existente de otra instalación conserva su destino hasta que el usuario lo retire. Para esta instalación, **Configuración → Diagnósticos** permite recrear o eliminar el acceso directo.

## Texto multilingüe y arranque

Anqora inicia los procesos Python del launcher y Studio en UTF-8 y transmite la configuración a los procesos de transcripción, descarga y procesamiento. Las transcripciones, los subtítulos SRT y ASS y los registros nuevos conservan los caracteres Unicode. El launcher solo abre el navegador predeterminado tras verificar la respuesta de Studio a un desafío local. Respeta la preferencia de apertura automática.

Después de actualizar, cierra la instancia anterior desde **Configuración → Diagnósticos → Cerrar al terminar los trabajos activos** y abre el acceso directo habitual. Con el navegador abierto, vuelve a hacer doble clic. Debe abrir la misma instancia, aunque aparezca otra pestaña. En `launcher.log` debe figurar una sola creación del proceso para esas dos aperturas.

Si falla el arranque, el cuadro de Windows indica la carpeta exacta de registros. Normalmente se encuentra en `%LOCALAPPDATA%\Chopify Studio\<instalación>\logs`. Consulta `launcher.log` si falló la comprobación inicial del puerto o del entorno. Consulta `studio.log` si el proceso salió antes de tiempo o superó la espera después de iniciarse. El código de salida identifica una terminación prematura y la salida del proceso queda registrada desde el inicio. Si Studio verifica el servidor pero Windows no abre el navegador, puedes usar `http://127.0.0.1:8765/`.

Si un doble clic no genera una entrada nueva en `launcher.log`, abre **Propiedades** del acceso directo. El destino debe usar `.venv\Scripts\pythonw.exe` y apuntar al `studio_launcher.py` de ese mismo repositorio. **Iniciar en** debe apuntar a la carpeta del repositorio. Puedes reparar el acceso directo de esa instalación con **Configuración → Diagnósticos → Crear o recrear acceso directo**.

En **Configuración → Diagnósticos → Dependencias**, pulsa **Elegir carpeta** o pega la ruta local de la carpeta `bin` que contiene ambos ejecutables. **Ejecutar diagnóstico** comprueba la ruta del campo y muestra el resultado y la versión de cada programa. **Guardar ubicación** valida los dos ejecutables y aplica la carpeta a los próximos trabajos sin reiniciar Studio. La ruta queda guardada para futuros inicios. Deja el campo vacío y guarda para volver a la detección automática.

Si Windows no abre el selector, puedes pegar la ruta desde el Explorador. Prepara y exporta un video local corto para validar el flujo. Solo cambia el PATH del proceso de Studio y de sus procesos secundarios. La apariencia de los subtítulos incrustados también depende de las fuentes que FFmpeg encuentra en Windows.

## Credenciales y autorización

Studio elige pares completos por perfil. La precedencia es **variables de entorno → credenciales temporales introducidas en Studio → credenciales protegidas almacenadas**. Un par incompleto de variables se ignora. Se mantienen `CHOPIFY_TIKTOK_CLIENT_KEY` y `CHOPIFY_TIKTOK_CLIENT_SECRET` para sesiones temporales. Para asociar ese par con Sandbox, define `CHOPIFY_TIKTOK_PROFILE=sandbox` en la sesión que abre Studio. Si omites el perfil temporal, Studio lo interpreta como Production. Las credenciales temporales y su autorización viven en memoria.

El almacén persistente usa **Windows DPAPI con el usuario actual**. Las preferencias generales están en `settings.json`, y las credenciales y los registros OAuth se escriben en archivos cifrados independientes por perfil en `%LOCALAPPDATA%\Chopify Studio\<instalación>\protected`. Fin de acceso, fin de renovación, ámbitos, identidad de la cuenta y vinculación con la aplicación forman parte del registro cifrado. Las escrituras se reemplazan de manera atómica. La interfaz muestra origen, disponibilidad, perfil, cuenta y vencimiento. Una página de TikTok necesita el identificador público de la aplicación como parte de OAuth. Studio lo incluye en la redirección oficial en lugar de devolver las credenciales almacenadas en JSON.

Studio comprueba la cuenta al recuperar la autorización y renueva el acceso cuando está a punto de vencer. Si TikTok rota el refresh token, el almacén guarda el valor nuevo. Cuando la sesión caduca o TikTok la revoca, conecta la cuenta de nuevo desde Studio. **Desconectar y revocar** solicita la revocación a TikTok. Si el servicio remoto no responde, **Eliminar autorización local** borra la copia del equipo y puedes revocar el permiso desde la cuenta de TikTok. **Eliminar credenciales guardadas** borra localmente las credenciales de aplicación y la autorización del perfil. La eliminación local y la revocación remota son acciones distintas.

Si DPAPI no está disponible, usa **Usar ahora temporalmente** o una sesión con variables de entorno. El almacén protegido pertenece al usuario y al equipo de Windows. Una instalación en otro equipo puede necesitar configurar las credenciales y autorizar la cuenta de nuevo. Un envío al buzón de TikTok sigue requiriendo revisión y consentimiento explícito.

## Dependencias, registros y mantenimiento

- **FFmpeg y FFprobe.** Studio prueba primero la ubicación guardada, después las carpetas del PATH disponible y ubicaciones convencionales de Windows, incluidas instalaciones de usuario y enlaces de WinGet. Verifica ambos ejecutables con `-version`. El servidor incorpora la carpeta validada a su entorno y la propaga a los procesos de video. Puedes configurar la carpeta en System Settings cuando tus binarios estén en otra ubicación.
- **Ollama.** El diagnóstico consulta versión y modelos en `127.0.0.1:11434`. Studio puede iniciar un proceso propio con `OLLAMA_HOST=127.0.0.1:11434` y `OLLAMA_NO_CLOUD=1` cuando lo autorizas. Para una instancia iniciada por otra herramienta, el diagnóstico indica que su privacidad no está verificada. Studio deja fuera los modelos de nube y el cierre solo actúa sobre el proceso de Ollama que inició.
- **Segundo inicio.** El launcher comprueba el servicio con un desafío HTTP ligado a esta instalación. Dos aperturas simultáneas se serializan. Cuando reconoce la instancia, vuelve a abrir el navegador en ella. Si otro programa ocupa el puerto, abre el diagnóstico y deja ese proceso en ejecución.
- **Cierre.** Desde System Settings puedes esperar a los trabajos ya activos o solicitar su cancelación. El cierre detiene la captura Live, espera la transferencia de TikTok o la verificación OAuth que ya esté en curso y conserva las tareas pendientes de Automation. Los proyectos convencionales interrumpidos se pueden reanudar desde Studio. Algunas etapas de render terminan en su siguiente punto seguro. Cerrar la pestaña deja el servidor disponible.
- **Actualización.** **Comprobar actualizaciones** muestra rama, commit instalado, commit remoto y cambios locales. Para aplicar una actualización, confirma el SHA y deja terminar los trabajos activos. Solo avanza la rama autorizada con un fast-forward desde el repositorio configurado en el código. Si cambia el SHA remoto, aparecen cambios locales o Git no puede avanzar, Studio pide revisar el estado. Al terminar, abre de nuevo el acceso directo. Si una versión trae nuevas dependencias, revisa sus instrucciones de instalación.
- **Diagnóstico.** El launcher avisa mediante un cuadro de Windows cuando no puede iniciar. System Settings muestra el directorio de registros y lo abre en el explorador. `launcher.log` registra la comprobación del puerto, la creación del proceso y el resultado de la verificación de identidad. `studio.log` recoge la salida del proceso hijo. Si Studio inició Ollama, su salida se guarda en `ollama.log`. Los registros de proyectos y Ollama pueden incluir detalles locales de los trabajos. Revisa esos archivos antes de compartirlos.

## Evidencia y alcance de la validación

La revisión del launcher reprodujo un caso que explicaba el mensaje genérico y la falta de `studio.log`. Si un socket aceptaba conexiones pero seguía sin responder a HTTP durante la comprobación inicial, se agotaba la espera antes de crear el proceso y su registro. El mensaje anterior indicaba consultar `studio.log` incluso en esa ruta. El servidor ejecutado directamente empieza en `dashboard.py` y omite esa comprobación previa. También se reprodujo una terminación prematura del proceso y se comprobó que su salida y código quedan disponibles. Estos resultados identifican fallos del launcher. La causa concreta del incidente en Windows con el puerto inicialmente libre queda pendiente de confirmación en ese equipo.

La regresión completa en Linux registró 609 pruebas aprobadas y una omitida, la de DPAPI nativo en Windows. Tras los ajustes finales de OAuth, navegación y registro, las pruebas focalizadas del launcher y TikTok registraron 48 aprobadas y la misma omisión. En el lote no se enviaron clips reales.

Las pruebas usaron el servidor HTTP real de Studio y Chromium sobre Linux. Ejecutaron dos aperturas del launcher sobre un mismo proceso, detección de un servicio ajeno en el puerto, persistencia de ajustes, FFmpeg fuera del PATH, el flujo guiado y el cierre con recuperación de la cola. Un repositorio Git local permitió probar un fast-forward y proteger cambios locales. La renovación OAuth y los estados de Ollama usaron dobles de prueba. La prueba de DPAPI nativo se ejecuta bajo un perfil de Windows y queda pendiente, junto con la creación física del acceso directo y el arranque después de reiniciar Windows. [Captura de System Settings](evidence/windows-launcher/system-settings-chromium.png)
