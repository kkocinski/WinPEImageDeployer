from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PureWindowsPath
import re


class FirmwareType(str, Enum):
    UEFI = "UEFI (GPT)"
    BIOS = "BIOS (MBR)"


class CaptureCompression(str, Enum):
    MAXIMUM = "Maximum (LZX)"
    FAST = "Fast (XPRESS)"
    NONE = "None"

    @property
    def dism_value(self) -> str:
        return {
            CaptureCompression.MAXIMUM: "max",
            CaptureCompression.FAST: "fast",
            CaptureCompression.NONE: "none",
        }[self]


@dataclass(frozen=True)
class DiskInfo:
    number: int
    model: str
    size_bytes: int
    bus_type: str = "Unknown bus"
    serial_number: str = ""
    volume_size_bytes: int = 0
    volume_free_bytes: int = 0
    unallocated_bytes: int = 0

    @property
    def size_gib(self) -> float:
        return self.size_bytes / (1024**3)

    @property
    def volume_used_bytes(self) -> int:
        return max(0, self.volume_size_bytes - self.volume_free_bytes)

    @property
    def volume_used_gib(self) -> float:
        return self.volume_used_bytes / (1024**3)

    @property
    def volume_free_gib(self) -> float:
        return self.volume_free_bytes / (1024**3)

    @property
    def unallocated_gib(self) -> float:
        return self.unallocated_bytes / (1024**3)

    def display_name(self) -> str:
        identity = f"Disk {self.number}: {self.model} | {self.bus_type} | {self.size_gib:.1f} GiB"
        serial = f" | S/N: {self.serial_number}" if self.serial_number else ""
        usage = (
            f" | Volumes: {self.volume_used_gib:.1f} GiB used, {self.volume_free_gib:.1f} GiB free"
            if self.volume_size_bytes else " | Volume usage: unavailable"
        )
        usage += f" | Unallocated: {self.unallocated_gib:.1f} GiB"
        return identity + serial + usage


@dataclass(frozen=True)
class WimImageInfo:
    index: int
    name: str
    description: str


@dataclass(frozen=True)
class VolumeInfo:
    drive_letter: str
    label: str
    file_system: str
    size_bytes: int
    free_bytes: int

    @property
    def used_bytes(self) -> int:
        return max(0, self.size_bytes - self.free_bytes)

    @property
    def free_gib(self) -> float:
        return self.free_bytes / (1024**3)

    @property
    def used_gib(self) -> float:
        return self.used_bytes / (1024**3)

    @property
    def size_gib(self) -> float:
        return self.size_bytes / (1024**3)

    def display_name(self) -> str:
        label = self.label or "No label"
        filesystem = self.file_system or "Unknown FS"
        return f"{self.drive_letter}  |  {label}  |  {filesystem}  |  {self.free_gib:.1f} GiB free of {self.size_gib:.1f} GiB"


@dataclass(frozen=True)
class PartitionInfo:
    disk_number: int
    number: int
    partition_type: str
    size_bytes: int
    offset_bytes: int = 0

    @property
    def size_gib(self) -> float:
        return self.size_bytes / (1024**3)

    @property
    def offset_gib(self) -> float:
        return self.offset_bytes / (1024**3)

    def display_name(self) -> str:
        kind = self.partition_type or "Unknown type"
        return f"Partition {self.number} | {kind} | {self.size_gib:.1f} GiB | Offset {self.offset_gib:.1f} GiB"


def normalize_drive_letter(value: str) -> str:
    candidate = value.strip().upper().rstrip("\\/")
    if len(candidate) == 1 and candidate.isalpha():
        candidate += ":"
    if len(candidate) != 2 or candidate[1] != ":" or not candidate[0].isalpha():
        raise ValueError("A volume must be a drive letter, for example D:.")
    return candidate


def validate_volume_label(value: str) -> str:
    label = value.strip()
    if len(label) > 32:
        raise ValueError("The volume label must be 32 characters or fewer.")
    if any(character in '\\/:*?"<>|' for character in label):
        raise ValueError("The volume label contains unsupported characters.")
    return label


def validate_wim_path(path: str) -> str:
    # Tk file dialogs can return a Windows path with forward slashes (for
    # example D:/Images/reference.wim). DISM in WinPE must receive native
    # Windows separators in option values so the slash is never interpreted
    # as part of its command-line option syntax.
    candidate = path.strip().strip('"').replace("/", "\\")
    if not candidate:
        raise ValueError("Select a WIM image path.")
    if PureWindowsPath(candidate).suffix.lower() != ".wim":
        raise ValueError("The image file must have a .wim extension.")
    return candidate


def validate_image_index(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)(?:\s*(?:—|-)\s*.*)?\s*", value)
    if not match:
        raise ValueError("The image index must be a positive integer.")
    try:
        index = int(match.group(1))
    except ValueError as error:
        raise ValueError("The image index must be a positive integer.") from error
    if index < 1:
        raise ValueError("The image index must be a positive integer.")
    return index


def validate_relative_image_destination(value: str) -> str:
    candidate = value.strip().replace("/", "\\")
    if not candidate:
        raise ValueError("Enter a destination directory inside the image.")
    path = PureWindowsPath(candidate)
    if path.drive.upper() == "C:":
        path = PureWindowsPath(*path.parts[1:])
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Use a relative image path without '..', for example Install\\Packages.")
    return str(path)