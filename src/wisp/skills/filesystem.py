"""Symlink-resistant filesystem primitives shared by Agent Skills operations."""

from __future__ import annotations

import errno
import importlib
import os
from pathlib import Path
from typing import Any

DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
SCANDIR_SUPPORTS_FD = os.scandir in os.supports_fd
USE_DESCRIPTOR_TRAVERSAL = (
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and OPEN_SUPPORTS_DIR_FD
    and SCANDIR_SUPPORTS_FD
)
PATH_FALLBACK_SUPPORTED = os.name == "nt"


def open_relative(
    name: str,
    flags: int,
    *,
    directory_fd: int | None,
    directory_path: Path,
) -> int:
    if directory_fd is not None and OPEN_SUPPORTS_DIR_FD:
        return os.open(name, flags, dir_fd=directory_fd)
    return os.open(directory_path / name, flags)


def is_link_like(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def open_path_file(path: Path) -> int:
    if os.name == "nt":
        return open_windows_file(path)
    return os.open(path, FILE_FLAGS)


def open_path_directory_guard(path: Path) -> int | None:
    if os.name != "nt":
        return None
    return open_windows_skill_directory_guard(path)


def resolved_open_file(file_fd: int, *, path: Path) -> Path:
    if os.name != "nt":
        raise RuntimeError("stable opened-file resolution is unavailable on this platform")
    msvcrt = importlib.import_module("msvcrt")
    return resolved_windows_handle(msvcrt.get_osfhandle(file_fd), path=path)


def open_windows_skill_directory_guard(path: Path) -> int:
    handle = open_windows_directory_guard(path)
    try:
        if windows_handle_is_reparse_point(handle, path=path):
            raise OSError(errno.ELOOP, "skill directory is a reparse point", path)
        if resolved_windows_handle(handle, path=path) != path.resolve(strict=False):
            raise OSError(errno.ELOOP, "skill directory changed while opening", path)
    except BaseException:
        close_windows_handle(handle)
        raise
    return handle


def open_windows_file(path: Path, *, writable: bool = False) -> int:
    handle = open_windows_file_handle(path, writable=writable)
    try:
        if windows_handle_is_reparse_point(handle, path=path):
            raise OSError(errno.ELOOP, "skill resource is a reparse point", path)
        return windows_handle_to_fd(handle, writable=writable)
    except BaseException:
        close_windows_handle(handle)
        raise


def open_windows_writable_file(path: Path) -> int:
    """Open an existing non-reparse file for in-place writes."""

    return open_windows_file(path, writable=True)


def open_windows_file_handle(path: Path, *, writable: bool = False) -> int:
    ctypes = importlib.import_module("ctypes")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x40000080 if writable else 0x80000000,  # write + attributes, or read
        0x3,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deliberately omit DELETE
        None,
        3,  # OPEN_EXISTING
        0x02200000,  # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise windows_error(ctypes, ctypes.get_last_error(), path)
    return int(handle)


def windows_handle_is_reparse_point(handle: int, *, path: Path) -> bool:
    ctypes = importlib.import_module("ctypes")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    file_attribute_tag_info = type(
        "FileAttributeTagInfo",
        (ctypes.Structure,),
        {
            "_fields_": [
                ("file_attributes", ctypes.c_uint32),
                ("reparse_tag", ctypes.c_uint32),
            ]
        },
    )
    info = file_attribute_tag_info()
    get_file_information = kernel32.GetFileInformationByHandleEx
    get_file_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    get_file_information.restype = ctypes.c_int
    if not get_file_information(
        ctypes.c_void_p(handle),
        9,  # FileAttributeTagInfo
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise windows_error(ctypes, ctypes.get_last_error(), path)
    return bool(info.file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def windows_handle_to_fd(handle: int, *, writable: bool = False) -> int:
    msvcrt = importlib.import_module("msvcrt")
    flags = (os.O_WRONLY | getattr(os, "O_BINARY", 0)) if writable else os.O_RDONLY
    return int(msvcrt.open_osfhandle(handle, flags))


def open_windows_directory_guard(path: Path) -> int:
    ctypes = importlib.import_module("ctypes")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80,  # FILE_READ_ATTRIBUTES
        0x3,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deliberately omit DELETE
        None,
        3,  # OPEN_EXISTING
        0x02200000,  # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise windows_error(ctypes, ctypes.get_last_error(), path)
    return int(handle)


def close_windows_handle(handle: int) -> None:
    ctypes = importlib.import_module("ctypes")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    close_handle(ctypes.c_void_p(handle))


def resolved_windows_handle(handle: int, *, path: Path) -> Path:
    ctypes = importlib.import_module("ctypes")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    get_final_path.restype = ctypes.c_uint32
    windows_handle = ctypes.c_void_p(handle)
    size = get_final_path(windows_handle, None, 0, 0)
    if size == 0:
        raise windows_error(ctypes, ctypes.get_last_error(), path)
    buffer = ctypes.create_unicode_buffer(size + 1)
    if get_final_path(windows_handle, buffer, len(buffer), 0) == 0:
        raise windows_error(ctypes, ctypes.get_last_error(), path)
    resolved = buffer.value
    if resolved.startswith("\\\\?\\UNC\\"):
        resolved = "\\\\" + resolved[8:]
    elif resolved.startswith("\\\\?\\"):
        resolved = resolved[4:]
    return Path(resolved)


def windows_error(ctypes: Any, error: int, path: Path) -> OSError:
    exc: OSError = ctypes.WinError(error)
    exc.filename = str(path)
    return exc


__all__ = [
    "DIRECTORY_FLAGS",
    "FILE_FLAGS",
    "PATH_FALLBACK_SUPPORTED",
    "USE_DESCRIPTOR_TRAVERSAL",
    "close_windows_handle",
    "is_link_like",
    "open_path_directory_guard",
    "open_path_file",
    "open_relative",
    "open_windows_directory_guard",
    "open_windows_writable_file",
    "resolved_open_file",
    "resolved_windows_handle",
    "windows_handle_is_reparse_point",
]
