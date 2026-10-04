"""Runtime secret values, kept out of workflows and journals.

Workflows store a ``secret_ref`` name only. Values live in a separate store,
encrypted at rest with the Windows DPAPI (per user) where available.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from pathlib import Path
from typing import Protocol


class UnknownSecret(KeyError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


_ENTROPY = b"recorded-automation.secret-store.v1"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Call CryptProtectData / CryptUnprotectData; returns a new bytes buffer."""
    library = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    func = library.CryptProtectData if protect else library.CryptUnprotectData
    in_blob = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    entropy = _Blob(len(_ENTROPY), ctypes.cast(ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY)), ctypes.POINTER(ctypes.c_char)))
    out_blob = _Blob()
    if not func(ctypes.byref(in_blob), None, ctypes.byref(entropy), None, None, 0, ctypes.byref(out_blob)):
        raise OSError(f"DPAPI {'protect' if protect else 'unprotect'} failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


class SecretStore(Protocol):
    def get(self, reference: str) -> str:
        """Resolve a secret at execution time."""


class FileSecretStore:
    """Per-user secret value store implementing the SecretStore protocol."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._values: dict[str, str] = {}
        # References whose ciphertext exists on disk but cannot be decrypted by *this*
        # Windows account (written by another user, copied between machines, or damaged).
        # They are deliberately not treated as available values.
        self._unreadable: set[str] = set()
        self._uses_dpapi = os.name == "nt"
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict):
            return
        for ref, blob in raw.items():
            try:
                self._values[str(ref)] = self._decode(blob)
            except Exception:
                # An unusable value must read as *missing*: otherwise a run would fill the
                # field with an empty string and still call the step verified.
                self._unreadable.add(str(ref))

    def _decode(self, blob: str) -> str:
        data = base64.b64decode(str(blob))
        if self._uses_dpapi:
            data = _dpapi(data, protect=False)
        return data.decode("utf-8")

    def _encode(self, value: str) -> str:
        data = value.encode("utf-8")
        if self._uses_dpapi:
            data = _dpapi(data, protect=True)
        return base64.b64encode(data).decode("ascii")

    def _flush(self) -> None:
        payload = {ref: self._encode(value) for ref, value in self._values.items()}
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def get(self, reference: str) -> str:
        with self._lock:
            if reference not in self._values:
                raise UnknownSecret(reference)
            return self._values[reference]

    def set(self, reference: str, value: str) -> None:
        with self._lock:
            self._values[reference] = value
            self._unreadable.discard(reference)        # rewriting it makes it usable again
            self._flush()

    def has(self, reference: str) -> bool:
        with self._lock:
            return reference in self._values

    def locked(self) -> list[str]:
        """Names of references present on disk but not decryptable here — never their values."""
        with self._lock:
            return sorted(self._unreadable)

    def refs(self) -> list[str]:
        with self._lock:
            return sorted(self._values)

    def preview(self) -> list[dict]:
        """Names and length only — never the value."""
        with self._lock:
            rows = [{"ref": ref, "length": len(self._values[ref])} for ref in sorted(self._values)]
            rows += [{"ref": ref, "length": 0, "locked": True} for ref in sorted(self._unreadable)]
            return rows

    def delete(self, reference: str) -> None:
        with self._lock:
            self._values.pop(reference, None)
            self._unreadable.discard(reference)
            self._flush()


class StaticSecrets:
    """Test helper implementing SecretStore without touching disk."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, reference: str) -> str:
        if reference not in self.values:
            raise UnknownSecret(reference)
        return self.values[reference]
