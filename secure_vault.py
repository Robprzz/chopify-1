"""Chopify secrets protected by Windows DPAPI in the current user's profile."""
import ctypes
import json
import os
from pathlib import Path
import tempfile


PROFILES = ("sandbox", "production")
SLOTS = ("application", "authorization")
MAX_SIZE = 64 * 1024


class VaultError(ValueError):
    pass


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="chopify-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class DPAPI:
    def __init__(self):
        self.available = os.name == "nt" and hasattr(ctypes, "WinDLL")

    def transform(self, data, label, protect):
        if not self.available:
            raise VaultError("El almacén protegido de Windows no está disponible.")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        def blob(value):
            array = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
            return Blob(len(value), array)

        source, entropy, result = blob(data), blob(label), Blob()
        api = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = api.CryptProtectData if protect else api.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR if protect else ctypes.c_void_p,
                       ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(Blob)]
        fn.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        # No UI, no machine-wide flag. Windows binds protection to the current user.
        if not fn(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(result)):
            raise VaultError("Windows no pudo acceder al almacén protegido de Chopify.")
        try:
            return ctypes.string_at(result.data, result.size)
        finally:
            if not protect:
                ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)


class Vault:
    def __init__(self, directory, protector=None):
        self.directory = Path(directory)
        self.protector = protector or DPAPI()

    def _file(self, profile, slot):
        if profile not in PROFILES or slot not in SLOTS:
            raise VaultError("Perfil de TikTok no válido.")
        return self.directory / f"{profile}-{slot}.dpapi"

    def _transform(self, data, profile, slot, protect):
        label = f"Chopify Studio credential v1/{profile}/{slot}".encode()
        return self.protector.transform(data, label, protect)

    def available(self):
        if not self.protector.available:
            return False
        try:
            encoded = self._transform(b"probe", "sandbox", "application", True)
            return self._transform(encoded, "sandbox", "application", False) == b"probe"
        except (VaultError, OSError):
            return False

    def read(self, profile, slot):
        path = self._file(profile, slot)
        if not self.protector.available or not path.is_file():
            return None
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_SIZE + 1)
            if len(data) > MAX_SIZE:
                raise ValueError
            result = json.loads(self._transform(data, profile, slot, False))
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (OSError, UnicodeError, ValueError):
            raise VaultError("No se pudo recuperar la configuración protegida. Revisa tu perfil de Windows.") from None

    def write(self, profile, slot, value):
        if not self.protector.available:
            raise VaultError("El almacén protegido de Windows no está disponible. Usa credenciales temporales.")
        path = self._file(profile, slot)
        try:
            encoded = self._transform(json.dumps(value, allow_nan=False).encode(), profile, slot, True)
            if len(encoded) > MAX_SIZE:
                raise ValueError
            atomic_bytes(path, encoded)
        except (OSError, TypeError, ValueError):
            raise VaultError("No se pudo guardar la configuración en el almacén protegido de Windows.") from None

    def delete(self, profile, slot):
        try:
            self._file(profile, slot).unlink(missing_ok=True)
        except OSError:
            raise VaultError("No se pudo eliminar la configuración protegida.") from None
