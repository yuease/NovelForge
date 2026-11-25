"""Private, atomic storage for credentials belonging to this workbench."""

import base64
import ctypes
import json
import os
import stat
from ctypes import wintypes
from pathlib import Path
from tempfile import NamedTemporaryFile


_TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")


def _dpapi(value: bytes, *, decrypt: bool) -> bytes:
    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_pointer = ctypes.POINTER(DataBlob)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [blob_pointer, ctypes.c_void_p, blob_pointer, ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    incoming, outgoing = DataBlob(len(value), source), DataBlob()
    if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        if decrypt:
            ctypes.memset(outgoing.data, 0, outgoing.size)
        kernel32.LocalFree(outgoing.data)


class SiwcVault:
    def __init__(self, directory: Path):
        self.path = directory / "openai-siwc" / "credentials.json"

    def _check_permissions(self):
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise OSError("SIWC credentials cannot be stored through symlinks")
        if os.name != "nt":
            for path in (self.path.parent, self.path):
                if path.exists() and (path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077):
                    raise OSError("SIWC credential storage must be private")

    def _profile(self, value, *, decrypt):
        result = dict(value)
        if os.name == "nt":
            for key in _TOKEN_FIELDS:
                if not result.get(key):
                    continue
                if decrypt:
                    ciphertext = result[key].removeprefix("dpapi:")
                    if ciphertext == result[key]:
                        raise ValueError("Unprotected SIWC credential")
                    result[key] = _dpapi(base64.b64decode(ciphertext, validate=True), decrypt=True).decode("utf-8")
                else:
                    encoded = _dpapi(result[key].encode("utf-8"), decrypt=False)
                    result[key] = "dpapi:" + base64.b64encode(encoded).decode("ascii")
        return result

    def read(self):
        self._check_permissions()
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("profile", {}), dict):
            raise ValueError("Invalid SIWC vault")
        return {**value, "profile": self._profile(value.get("profile", {}), decrypt=True)}

    def write(self, value):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_permissions()
        disk = {**value, "profile": self._profile(value.get("profile", {}), decrypt=False)}
        temporary = None
        try:
            with NamedTemporaryFile(dir=self.path.parent, mode="w", encoding="utf-8", delete=False) as file:
                temporary = Path(file.name)
                if os.name != "nt":
                    os.fchmod(file.fileno(), 0o600)
                json.dump(disk, file, ensure_ascii=False, allow_nan=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
