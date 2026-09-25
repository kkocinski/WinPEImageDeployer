"""In-process coordination for disk operations and short editor I/O."""

from __future__ import annotations

import ctypes
import threading
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Callable, Iterator


class OperationLocks:
    def __init__(self) -> None:
        self._mutex = threading.RLock()
        self._global: str | None = None
        self._disks: dict[int, str] = {}
        self._unknown: str | None = None
        self._tokens: dict[int, tuple[str, set[int] | None, bool]] = {}
        self._next_token = 0
        self._editor_io = 0

    @property
    def active(self) -> bool:
        with self._mutex:
            return self._global is not None or bool(self._disks) or self._unknown is not None

    @property
    def global_active(self) -> bool:
        with self._mutex:
            return self._global is not None or self._unknown is not None

    def acquire(self, name: str, disks: set[int] | None = None, *, unknown: bool = False) -> int:
        with self._mutex:
            if disks is None:
                blocker = self._global or next(iter(self._disks.values()), None) or self._unknown
            elif unknown:
                blocker = self._global or next(iter(self._disks.values()), None) or self._unknown
            else:
                blocker = self._global or self._unknown or next(
                    (self._disks[disk] for disk in disks if disk in self._disks), None
                )
            if blocker:
                raise RuntimeError(f"{name} is blocked while {blocker} is in progress.")
            if self._editor_io:
                raise RuntimeError(f"{name} is blocked while text editor I/O is in progress.")
            if disks is None:
                self._global = name
            elif unknown:
                self._unknown = name
            else:
                for disk in disks:
                    self._disks[disk] = name
            self._next_token += 1
            self._tokens[self._next_token] = (name, disks, unknown)
            return self._next_token

    def release(self, token: int) -> None:
        with self._mutex:
            name, disks, unknown = self._tokens.pop(token)
            if disks is None:
                self._global = None
            elif unknown:
                self._unknown = None
            else:
                for disk in disks:
                    del self._disks[disk]

    @contextmanager
    def editor_access(self, path: Path, resolve: Callable[[Path], int | None]) -> Iterator[None]:
        # Hold the mutex across resolution and I/O: no operation may start
        # between checking the physical disk and opening/writing the file.
        with self._mutex:
            if self.active:
                try:
                    disk = resolve(path)
                except (OSError, RuntimeError, ValueError):
                    disk = None
                if disk is None:
                    raise RuntimeError(f"Cannot safely identify the disk for {path} while a disk operation is active.")
                if self._global or self._unknown or disk in self._disks:
                    blocker = self._global or self._unknown or self._disks.get(disk)
                    raise RuntimeError(f"Access to {path} is blocked while {blocker} is in progress.")
            self._editor_io += 1
            try:
                yield
            finally:
                self._editor_io -= 1


def path_disk_number(path: Path, disk_number_for_drive: Callable[[str], int]) -> int | None:
    """Resolve a local mounted path; refuse remote, RAM, and folder-mounted volumes."""
    try:
        if not path.is_absolute():
            return None
        # Resolve existing junctions/symlinks, including those in the parent of a new file.
        resolved = path.resolve(strict=False)
        probe = resolved if resolved.exists() else resolved.parent
        kernel32 = ctypes.windll.kernel32
        buffer = ctypes.create_unicode_buffer(32768)
        if not kernel32.GetVolumePathNameW(str(probe), buffer, len(buffer)):
            return None
        mount = buffer.value
        if len(mount) != 3 or mount[1:] != ":\\":
            return None
        # DRIVE_REMOTE=4, DRIVE_RAMDISK=6, DRIVE_FIXED=3, DRIVE_REMOVABLE=2.
        if kernel32.GetDriveTypeW(mount) not in (2, 3):
            return None
        return disk_number_for_drive(mount[:2])
    except (AttributeError, OSError, RuntimeError, ValueError):
        return None