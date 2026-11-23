"""Windows desktop integration without registry changes or elevation."""
import ctypes
import os
from pathlib import Path
import subprocess

from studio_settings import HIDDEN, venv_python


def _powershell(script, variables=None, timeout=12):
    if os.name != "nt":
        raise ValueError("Esta acción está disponible en Windows.")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        result = subprocess.run([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
                                 "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                                 "$ErrorActionPreference='Stop'; " + script],
                                env={**os.environ, **(variables or {})}, capture_output=True, check=True,
                                encoding="utf-8", timeout=timeout, creationflags=HIDDEN)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ValueError("Windows no pudo completar esta acción de escritorio.") from None


def shortcut(checkout, action):
    path = Path(checkout).resolve()
    pythonw = venv_python(path, windowless=True)
    launcher = path / "studio_launcher.py"
    previous_args = '"' + str(launcher) + '"'
    args = "-X utf8 " + previous_args
    if action == "create" and not pythonw.is_file():
        raise ValueError("Falta pythonw.exe en el entorno .venv de Anqora.")
    result = _powershell("""
        $desk = [Environment]::GetFolderPath('DesktopDirectory')
        if (-not $desk) { throw 'desktop unavailable' }
        $link = Join-Path $desk 'Anqora Media Studio.lnk'
        $shell = New-Object -ComObject WScript.Shell
        $exists = Test-Path -LiteralPath $link -PathType Leaf
        $shortcut = $shell.CreateShortcut($link)
        $ours = $exists -and $shortcut.TargetPath -eq $env:CS_DESKTOP_TARGET -and
          ($shortcut.Arguments -eq $env:CS_DESKTOP_ARGS -or $shortcut.Arguments -eq $env:CS_DESKTOP_PREVIOUS_ARGS)
        if ($exists -and -not $ours -and $env:CS_DESKTOP_ACTION -ne 'status') {
          Write-Output 'occupied'; exit
        }
        if ($env:CS_DESKTOP_ACTION -eq 'create') {
          $shortcut.TargetPath = $env:CS_DESKTOP_TARGET
          $shortcut.Arguments = $env:CS_DESKTOP_ARGS
          $shortcut.WorkingDirectory = $env:CS_DESKTOP_WORKDIR
          $shortcut.IconLocation = $env:CS_DESKTOP_ICON
          $shortcut.Description = 'Anqora Media Studio'
          $shortcut.Save()
          Write-Output 'created'
        } elseif ($env:CS_DESKTOP_ACTION -eq 'delete') {
          if ($ours) { Remove-Item -LiteralPath $link }
          Write-Output 'missing'
        } elseif ($ours) { Write-Output 'created' }
        elseif ($exists) { Write-Output 'occupied' }
        else { Write-Output 'missing' }
    """, {"CS_DESKTOP_ACTION": action, "CS_DESKTOP_TARGET": str(pythonw),
          "CS_DESKTOP_ARGS": args, "CS_DESKTOP_PREVIOUS_ARGS": previous_args,
          "CS_DESKTOP_WORKDIR": str(path),
          "CS_DESKTOP_ICON": str(path / "assets" / "studio" / "anqora.ico") + ",0"})
    if result not in {"created", "missing", "occupied"}:
        raise ValueError("Windows no confirmó el estado del acceso directo.")
    if result == "occupied" and action != "status":
        raise ValueError("Ya existe un acceso directo con ese nombre que pertenece a otra instalación.")
    return result


def choose_folder():
    return _powershell("""
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object Windows.Forms.FolderBrowserDialog
        $dialog.Description = 'Selecciona una carpeta para Anqora Media Studio'
        try {
          if ($dialog.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) { Write-Output $dialog.SelectedPath }
        } finally { $dialog.Dispose() }
    """, timeout=180)


def message(text):
    if os.name == "nt":
        box = ctypes.WinDLL("user32", use_last_error=True).MessageBoxW
        box.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        box(None, text, "Anqora Media Studio", 0x10)


def managed_job():
    """Keep the Windows Job handle open for the dashboard and its children."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                    ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                    ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io_counters", ctypes.c_uint64 * 6),
                    ("memory", ctypes.c_size_t * 4)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        return None
    if not (kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            and kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
        kernel.CloseHandle(handle)
        return None
    # The handle belongs to dashboard.py until its process exits.
    return handle
