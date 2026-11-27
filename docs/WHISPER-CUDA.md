# Transcripción local con NVIDIA CUDA en Anqora Media Studio

En **Proyectos → Nuevo proyecto**, selecciona **Aceleración de transcripción**. **Automático** intenta CUDA cuando CTranslate2 detecta un dispositivo. El diagnóstico solo registra CUDA como dispositivo real cuando una transcripción del medio ha terminado en ese backend. **NVIDIA CUDA con respaldo CPU** también muestra el motivo del respaldo si CUDA no está operativa. **CPU** usa int8. El modelo predeterminado de proyectos es **Medium**. **Small (Turbo)** es el modelo Whisper Small existente y emplea menos recursos. Live Studio conserva su modelo Small y su propio selector.

En Windows, Studio registra los directorios `bin` de `cuda_runtime`, `cublas` y `cudnn` de las ruedas NVIDIA de la `.venv` activa. El acceso directo habitual abre el dashboard con ese intérprete. El registro y el PATH se aplican al proceso y la configuración se hereda en sus procesos hijos. FFmpeg conserva libx264. La aceleración descrita aquí corresponde a Whisper.

Los errores reconocidos de CUDA permiten un reintento en CPU/int8. Si falla un bloque largo, se repite solo ese bloque y se mantienen los checkpoints finalizados. Las transcripciones anteriores se reutilizan con las reglas de caché existentes. Solo se muestra un dispositivo o una duración cuando los datos guardados permiten determinarlo. Si hay bloques con diferentes backends, se muestran ambos. Live puede usar CPU mientras otro trabajo ocupa el recurso compartido local y puede conservar el modelo CPU en memoria. En reparación de subtítulos solo se usan modelos ya instalados.

## Verificar el entorno en Windows

Abre PowerShell en el repositorio. Usa el mismo entorno virtual que el acceso directo.

```powershell
$p = ".\.venv\Scripts\python.exe"
& $p --version
& $p -m pip show ctranslate2 nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 nvidia-cudnn-cu12
& $p -c "import gpu_runtime as g; print(g.configure() or 'Directorios NVIDIA registrados en el proceso'); print(g.candidate() or 'CUDA detectada, falta una inferencia')"
```

La única evidencia NVIDIA previa a la integración es el resultado comunicado por el usuario de una inferencia independiente con Tiny/float16, `WHISPER CUDA OK`. El equipo reportado usó la RTX 2000 Ada, el driver 573.44, Python 3.11, CTranslate2 4.9.0 y las ruedas CUDA Runtime 12.8.90, cuBLAS 12.8.4.1 y cuDNN 9.10.2.21 dentro de la `.venv`. La aplicación usa las bibliotecas que ya estén instaladas en ese entorno. La instalación de dependencias opcionales queda a cargo del usuario conforme a su entorno corporativo.

Para verificar la integración, cierra Studio normalmente, vuelve a abrirlo desde el acceso directo y prepara un **proyecto nuevo** con un MP4 local con voz. Selecciona **Automático** y revisa el diagnóstico debajo del estado del proyecto o en **Configuración → Diagnósticos**. Una ejecución confirmada en GPU mostrará el modelo, `cuda`, el tipo efectivo y los segundos de transcripción. El archivo `transcript.json` contiene el mismo registro en `whisper_runtime`. Si figura `cpu`, sigue el motivo que se muestra en Studio o en el registro del trabajo.

## Comparar tiempos con el mismo video

Usa un MP4 local y el mismo modelo en ambas ejecuciones. Si aún no tienes el modelo, prepáralo antes y comienza la medición con la caché del modelo disponible. Los directorios independientes impiden que la segunda ejecución reutilice la primera transcripción.

```powershell
$p = ".\.venv\Scripts\python.exe"
$video = (Resolve-Path "C:\Videos\muestra.mp4").Path
$bench = Join-Path $env:TEMP ("anqora-whisper-" + [Guid]::NewGuid())
foreach ($backend in @("cpu", "cuda")) {
    $folder = Join-Path $bench $backend
    & $p .\download_and_transcribe.py $video --model medium --device $backend --workdir $folder
    if ($LASTEXITCODE -ne 0) { throw "Falló la ejecución $backend" }
    (Get-Content (Join-Path $folder "transcript.json") -Encoding UTF8 -Raw |
        ConvertFrom-Json).whisper_runtime | Format-List
}
```

Compara `elapsed_seconds` para carga del modelo más transcripción e `inference_seconds` para la inferencia. Verifica primero que los campos `device` muestran `cpu` y `cuda` respectivamente. Si CUDA recurrió a CPU, esa ejecución no sirve como medición de GPU. Quedan pendientes ambos tiempos y la primera transcripción desde Studio en el equipo Windows. La detección del dispositivo y las pruebas simuladas no proporcionan esa comparación.
