"""Read mounted-volume facts on macOS straight from the kernel.

``diskutil info`` asks Disk Arbitration to describe a volume, and on a slow
external disk that request blocks until the writes already in flight settle.
During a sync that regularly takes longer than the 5 second write-safety
timeout, so the revalidation before a copy fails even though nothing about the
volume changed.

``statfs(2)`` and ``getattrlist(2)`` answer from the mounted filesystem's own
state and return immediately, so callers should read the mount facts here and
keep ``diskutil`` as a fallback for anything the kernel cannot report.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

_MNT_RDONLY = 0x00000001

# Subset of <sys/mount.h> MNT_* flags spelled the way ``mount(8)`` prints them.
_MOUNT_FLAG_NAMES: tuple[tuple[int, str], ...] = (
    (0x00000001, "read-only"),
    (0x00000002, "synchronous"),
    (0x00000004, "noexec"),
    (0x00000008, "nosuid"),
    (0x00000010, "nodev"),
    (0x00000020, "union"),
    (0x00000040, "asynchronous"),
    (0x00001000, "local"),
    (0x00002000, "quota"),
    (0x00004000, "rootfs"),
    (0x00200000, "noowners"),
    (0x00800000, "journaled"),
    (0x01000000, "nouserxattr"),
    (0x02000000, "defwrite"),
    (0x08000000, "noatime"),
)

_ATTR_BIT_MAP_COUNT = 5
_ATTR_CMN_RETURNED_ATTRS = 0x80000000
_ATTR_VOL_INFO = 0x80000000
_ATTR_VOL_UUID = 0x00040000
_ATTRIBUTE_SET_SIZE = 4 * _ATTR_BIT_MAP_COUNT
_UUID_SIZE = 16


@dataclass(frozen=True, slots=True)
class MacOSVolumeFacts:
    """What the kernel reports about the mounted volume containing a path."""

    mount_path: str
    device_node: str
    filesystem_type: str
    read_only: bool
    mount_options: tuple[str, ...]
    block_size: int
    volume_uuid: str

    @property
    def device_id(self) -> str:
        """Return the ``diskNsM`` identifier that Disk Arbitration would report."""
        return self.device_node.removeprefix("/dev/")


class _Statfs(ctypes.Structure):
    # struct statfs with 64-bit inodes (sys/mount.h, _DARWIN_FEATURE_64_BIT_INODE).
    _fields_ = (
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    )


class _Attrlist(ctypes.Structure):
    _fields_ = (
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    )


@cache
def _libc() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("c")
    if not name:
        return None
    try:
        return ctypes.CDLL(name, use_errno=True)
    except OSError:
        return None


def _statfs_function(libc: ctypes.CDLL) -> Callable[..., int] | None:
    # x86_64 keeps the legacy 32-bit-inode layout under the plain symbol and
    # exposes the layout above as ``statfs$INODE64``; arm64 only has the latter
    # layout and exports it under the plain name.
    for symbol in ("statfs$INODE64", "statfs"):
        try:
            function = getattr(libc, symbol)
        except AttributeError:
            continue
        function.argtypes = (ctypes.c_char_p, ctypes.POINTER(_Statfs))
        function.restype = ctypes.c_int
        return function
    return None


def mount_flag_names(flags: int) -> tuple[str, ...]:
    """Return the ``mount(8)`` option names set in a ``statfs`` flag word."""
    return tuple(name for bit, name in _MOUNT_FLAG_NAMES if flags & bit)


def read_mounted_volume(path: str | os.PathLike[str]) -> MacOSVolumeFacts | None:
    """Return kernel-reported facts for the volume containing *path*, or ``None``."""
    if sys.platform != "darwin":
        return None
    libc = _libc()
    if libc is None:
        return None
    statfs = _statfs_function(libc)
    if statfs is None:
        return None

    encoded = os.fsencode(os.fspath(path))
    stats = _Statfs()
    if statfs(encoded, ctypes.byref(stats)) != 0:
        return None
    mount_path = os.fsdecode(stats.f_mntonname)
    device_node = os.fsdecode(stats.f_mntfromname)
    filesystem_type = stats.f_fstypename.decode("ascii", "replace").strip().casefold()
    if not mount_path or not device_node or not filesystem_type:
        return None
    return MacOSVolumeFacts(
        mount_path=mount_path,
        device_node=device_node,
        filesystem_type=filesystem_type,
        read_only=bool(stats.f_flags & _MNT_RDONLY),
        mount_options=mount_flag_names(stats.f_flags),
        block_size=int(stats.f_bsize),
        volume_uuid=_volume_uuid(libc, os.fsencode(mount_path)),
    )


def _volume_uuid(libc: ctypes.CDLL, mount_path: bytes) -> str:
    """Return the volume UUID as ``diskutil`` prints it, or ``""`` if unsupported."""
    try:
        getattrlist = libc.getattrlist
    except AttributeError:
        return ""
    getattrlist.argtypes = (
        ctypes.c_char_p,
        ctypes.POINTER(_Attrlist),
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
    )
    getattrlist.restype = ctypes.c_int
    request = _Attrlist(
        bitmapcount=_ATTR_BIT_MAP_COUNT,
        commonattr=_ATTR_CMN_RETURNED_ATTRS,
        volattr=_ATTR_VOL_INFO | _ATTR_VOL_UUID,
    )
    buffer = ctypes.create_string_buffer(4 + _ATTRIBUTE_SET_SIZE + _UUID_SIZE)
    if getattrlist(mount_path, ctypes.byref(request), buffer, len(buffer), 0) != 0:
        return ""
    (length,) = struct.unpack_from("<I", buffer.raw, 0)
    returned = struct.unpack_from(f"<{_ATTR_BIT_MAP_COUNT}I", buffer.raw, 4)
    if length < len(buffer) or not returned[1] & _ATTR_VOL_UUID:
        return ""
    start = 4 + _ATTRIBUTE_SET_SIZE
    return str(uuid.UUID(bytes=buffer.raw[start : start + _UUID_SIZE])).upper()
