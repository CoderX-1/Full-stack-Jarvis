"""The small, explicitly scoped shelf of documents created by JARVIS."""

from __future__ import annotations

import ctypes
import os
import uuid
from pathlib import Path


FOLDERS = ("reports", "presentations")
SUFFIXES = {".pdf", ".pptx", ".docx"}


class CreatedFileError(ValueError):
    pass


def _folder(root: Path, section: str) -> Path:
    root = Path(root).resolve()
    folder = root / "output" / section
    if section not in FOLDERS or folder.is_symlink() or not folder.is_dir():
        raise CreatedFileError("created-file folder is unavailable")
    if not folder.resolve().is_relative_to(root):
        raise CreatedFileError("created-file folder is outside JARVIS")
    return folder


def _entry(root: Path, file_id: str) -> Path:
    if not isinstance(file_id, str) or "/" not in file_id or len(file_id) > 300:
        raise CreatedFileError("invalid created-file ID")
    section, name = file_id.split("/", 1)
    if (not name or name in {".", ".."} or "/" in name or "\\" in name
            or ":" in name or "\x00" in name or Path(name).suffix.casefold() not in SUFFIXES):
        raise CreatedFileError("invalid created-file ID")
    folder = _folder(root, section)
    path = folder / name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(folder.resolve()):
        raise CreatedFileError("created file is no longer on the output shelf")
    return path


def _version(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def list_created_files(root: Path, limit: int = 80) -> list[dict]:
    items = []
    for section in FOLDERS:
        try:
            folder = _folder(root, section)
            candidates = folder.iterdir()
        except (CreatedFileError, OSError):
            continue
        for path in candidates:
            if path.suffix.casefold() not in SUFFIXES or path.is_symlink():
                continue
            try:
                file_id = f"{section}/{path.name}"
                valid = _entry(root, file_id)
                stat = valid.stat()
                items.append({"id": file_id, "name": path.name, "kind": section,
                              "bytes": stat.st_size,
                              "modified": stat.st_mtime, "version": _version(valid)})
            except (CreatedFileError, OSError):
                continue
    items.sort(key=lambda item: item["modified"], reverse=True)
    return items[:max(1, min(int(limit), 80))]


def resolve_created_file(root: Path, file_id: str, version: str = "") -> Path:
    path = _entry(root, file_id)
    if version and _version(path) != version:
        raise CreatedFileError("created file changed; refresh the Files view")
    return path


def _send_to_recycle_bin(path: Path) -> None:
    if os.name != "nt":
        raise CreatedFileError("Windows Recycle Bin is unavailable on this system")

    class GUID(ctypes.Structure):
        _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                    ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

        @classmethod
        def from_string(cls, value: str) -> "GUID":
            return cls.from_buffer_copy(uuid.UUID(value).bytes_le)

    def call(interface, index, *types):
        table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[index])

    ole = ctypes.windll.ole32
    shell = ctypes.windll.shell32
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)  # single-threaded COM apartment
    if initialized not in (0, 1):
        raise CreatedFileError("Windows Recycle Bin COM initialization failed")
    operation = ctypes.c_void_p()
    item = ctypes.c_void_p()
    try:
        ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        ole.CoCreateInstance.restype = ctypes.c_long
        clsid = GUID.from_string("3ad05575-8857-4850-9277-11b85bdb8e09")
        iid_operation = GUID.from_string("947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8")
        iid_item = GUID.from_string("43826d1e-e718-42ee-bc55-a1e261c37bfe")
        hresult = ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid_operation),
                                       ctypes.byref(operation))
        if hresult != 0:
            raise CreatedFileError(f"Windows Recycle Bin file operation is unavailable (0x{hresult & 0xffffffff:08x})")
        shell.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                                      ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        shell.SHCreateItemFromParsingName.restype = ctypes.c_long
        if shell.SHCreateItemFromParsingName(str(path), None, ctypes.byref(iid_item),
                                           ctypes.byref(item)) != 0:
            raise CreatedFileError("created file could not be prepared for recycling")
        # FOFX_RECYCLEONDELETE explicitly requests recycling; never fall back
        # to the legacy SHFileOperation path that may silently delete forever.
        flags = 0x00080000 | 0x20000000 | 0x00100000 | 0x0010 | 0x0400 | 0x0004
        if call(operation, 5, ctypes.c_uint)(operation, flags) != 0:
            raise CreatedFileError("Windows refused Recycle Bin operation flags")
        if call(operation, 18, ctypes.c_void_p, ctypes.c_void_p)(operation, item, None) != 0:
            raise CreatedFileError("Windows refused to queue the Recycle Bin move")
        result = call(operation, 21)(operation)
        aborted = ctypes.c_int()
        aborted_result = call(operation, 22, ctypes.POINTER(ctypes.c_int))(
            operation, ctypes.byref(aborted))
        if result != 0 or aborted_result != 0 or aborted.value or path.exists():
            raise CreatedFileError("Windows did not confirm the move to Recycle Bin")
    finally:
        if item:
            call(item, 2)(item)
        if operation:
            call(operation, 2)(operation)
        ole.CoUninitialize()


def recycle_created_file(root: Path, file_id: str, version: str) -> dict:
    if not isinstance(version, str) or not version:
        raise CreatedFileError("file version required; refresh the Files view")
    try:
        path = resolve_created_file(root, file_id, version)
        _send_to_recycle_bin(path)
    except OSError as exc:
        raise CreatedFileError("Windows could not recycle this file; original may still be on the shelf") from exc
    return {"id": file_id, "recycled": True}
