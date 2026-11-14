"""DPAPI key custody and legacy AES-GCM snapshot migration helpers."""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"MEMORY-ENGINE-1\x00"
_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


def protect(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("The personal vault requires Windows DPAPI.")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def atomic_write(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + ".pending")
    with temp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


@contextlib.contextmanager
def file_lock(path: Path):
    with _guard:
        lock = _locks.setdefault(str(path.resolve()), threading.RLock())
    with lock:
        with path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                deadline = time.monotonic() + 30
                while True:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise TimeoutError("Memory is busy; retry in a moment.")
                        time.sleep(.05)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


class Cipher:
    def __init__(self, home: Path, test_key: bytes | None = None):
        keyfile = home / "key.dpapi"
        if test_key is not None:
            self.key = test_key
        elif keyfile.exists():
            self.key = protect(keyfile.read_bytes(), decrypt=True)
        elif any((home / name).exists() for name in ('memory.bb', 'memory.db', 'memory.storage.json', 'memory.migrating.db')):
            raise RuntimeError("The vault key is missing. Restore the original key; it was not replaced.")
        else:
            self.key = AESGCM.generate_key(bit_length=256)
            atomic_write(keyfile, protect(self.key))
        if len(self.key) != 32:
            raise ValueError('The database key must be exactly 32 bytes; it was not replaced.')
        self.aes = AESGCM(self.key)

    def encrypt(self, raw: bytes) -> bytes:
        nonce = os.urandom(12)
        return MAGIC + nonce + self.aes.encrypt(nonce, raw, MAGIC)

    def decrypt(self, raw: bytes) -> bytes:
        if not raw.startswith(MAGIC):
            raise ValueError("Unsupported vault format; existing data was not changed.")
        start = len(MAGIC)
        return self.aes.decrypt(raw[start:start + 12], raw[start + 12:], MAGIC)
