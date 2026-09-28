"""Optional, removable-media startup configuration for WinPE networking."""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .models import DiskInfo, FirmwareType, normalize_drive_letter, validate_image_index, validate_wim_path
from .network_adapters import normalize_mac


@dataclass(frozen=True)
class EthernetStartupConfig:
    adapter: str
    address: str
    mask: str
    gateway: str
    dns_servers: str
    mac: str = ""


@dataclass(frozen=True)
class ShareStartupConfig:
    drive_letter: str
    unc_path: str
    username: str
    password: str


@dataclass(frozen=True)
class DiskMappingStartupConfig:
    serial_number: str
    partitions: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class AutoDeployStartupConfig:
    wim_path: str
    image_index: int
    firmware: FirmwareType
    minimum_disk_size_gib: int
    expected_disk_serial: str
    maximum_disk_size_gib: int | None = None
    username: str = ""
    password: str = ""


def unc_share_root(wim_path: str) -> str | None:
    """Extract the SMB share root from a WIM UNC path; reject incomplete paths."""
    path = PureWindowsPath(wim_path)
    if not wim_path.startswith("\\\\"):
        return None
    if not path.drive or len(path.parts) < 2 or any(part in {".", ".."} for part in path.parts[1:]):
        raise ValueError("[auto_deploy] UNC wim_path must include a server, share, and WIM file without '..'.")
    return path.drive


@dataclass(frozen=True)
class StartupConfig:
    ethernet: EthernetStartupConfig | None = None
    share: ShareStartupConfig | None = None
    auto_deploy: AutoDeployStartupConfig | None = None
    disk_mapping: DiskMappingStartupConfig | None = None


def load_startup_config(path: Path) -> StartupConfig:
    """Load a deliberately optional INI file stored beside WinPE USB drivers."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as error:
        raise ValueError(f"Cannot read startup configuration {path}: {error}") from error

    unknown_sections = set(parser.sections()) - {"ethernet", "share", "auto_deploy", "disk_mapping"}
    if unknown_sections:
        raise ValueError(f"Unsupported startup configuration section(s): {', '.join(sorted(unknown_sections))}.")

    ethernet = _load_ethernet(parser)
    share = _load_share(parser)
    auto_deploy = _load_auto_deploy(parser)
    disk_mapping = _load_disk_mapping(parser)
    if ethernet is None and share is None and auto_deploy is None and disk_mapping is None:
        raise ValueError("Startup configuration must contain an [ethernet], [share], [disk_mapping], or enabled [auto_deploy] section.")
    return StartupConfig(ethernet=ethernet, share=share, auto_deploy=auto_deploy, disk_mapping=disk_mapping)


def _load_disk_mapping(parser: configparser.ConfigParser) -> DiskMappingStartupConfig | None:
    if not parser.has_section("disk_mapping"):
        return None
    section = parser["disk_mapping"]
    serial_number = section.get("serial_number", "").strip()
    if not serial_number:
        raise ValueError("[disk_mapping] requires serial_number=.")
    partitions: list[tuple[int, str]] = []
    for key, value in section.items():
        if key == "serial_number":
            continue
        match = re.fullmatch(r"partition_(\d+)", key)
        if not match:
            raise ValueError(f"Unsupported [disk_mapping] option: {key}.")
        number = int(match.group(1))
        if number < 1 or any(existing == number for existing, _ in partitions):
            raise ValueError("[disk_mapping] partition numbers must be unique positive integers.")
        try:
            letter = normalize_drive_letter(value)
        except ValueError as error:
            raise ValueError(f"[disk_mapping] {key} must be a drive letter such as R:.") from error
        partitions.append((number, letter))
    if not partitions:
        raise ValueError("[disk_mapping] requires at least one partition_N = X: mapping.")
    letters = [letter for _, letter in partitions]
    if len(set(letters)) != len(letters) or any(letter == "X:" for letter in letters):
        raise ValueError("[disk_mapping] drive letters must be unique and cannot use X:.")
    return DiskMappingStartupConfig(serial_number, tuple(sorted(partitions)))


def _load_ethernet(parser: configparser.ConfigParser) -> EthernetStartupConfig | None:
    if not parser.has_section("ethernet"):
        return None
    _validate_keys(parser, "ethernet", {"adapter", "mac", "address", "mask", "gateway", "dns"})
    adapter = parser.get("ethernet", "adapter", fallback="").strip()
    mac = parser.get("ethernet", "mac", fallback="").strip()
    if not adapter and not mac:
        raise ValueError("[ethernet] requires adapter= or mac=.")
    if mac:
        mac = normalize_mac(mac)
    address = _required_value(parser, "ethernet", "address")
    mask = _required_value(parser, "ethernet", "mask")
    return EthernetStartupConfig(adapter, address, mask, parser.get("ethernet", "gateway", fallback="").strip(), parser.get("ethernet", "dns", fallback="").strip(), mac)


def _load_share(parser: configparser.ConfigParser) -> ShareStartupConfig | None:
    if not parser.has_section("share"):
        return None
    _validate_keys(parser, "share", {"drive_letter", "unc_path", "username", "password"})
    drive_letter = normalize_drive_letter(_required_value(parser, "share", "drive_letter"))
    unc_path = _required_value(parser, "share", "unc_path")
    if not unc_path.startswith("\\\\"):
        raise ValueError("The share unc_path must start with \\ (for example \\server\\share).")
    return ShareStartupConfig(drive_letter, unc_path, _required_value(parser, "share", "username"), _required_value(parser, "share", "password"))


def _load_auto_deploy(parser: configparser.ConfigParser) -> AutoDeployStartupConfig | None:
    if not parser.has_section("auto_deploy"):
        return None
    _validate_keys(
        parser,
        "auto_deploy",
        {"enabled", "wim_path", "image_index", "firmware", "minimum_disk_size_gib", "maximum_disk_size_gib", "expected_disk_serial", "username", "password"},
    )
    try:
        enabled = parser.getboolean("auto_deploy", "enabled", fallback=False)
    except ValueError as error:
        raise ValueError("[auto_deploy] enabled must be true or false.") from error
    if not enabled:
        return None
    try:
        firmware = FirmwareType(_required_value(parser, "auto_deploy", "firmware"))
    except ValueError as error:
        raise ValueError("[auto_deploy] firmware must be UEFI (GPT) or BIOS (MBR).") from error
    try:
        minimum_disk_size_gib = int(_required_value(parser, "auto_deploy", "minimum_disk_size_gib"))
    except ValueError as error:
        raise ValueError("[auto_deploy] minimum_disk_size_gib must be a positive integer.") from error
    if minimum_disk_size_gib < 1:
        raise ValueError("[auto_deploy] minimum_disk_size_gib must be a positive integer.")
    maximum_value = parser.get("auto_deploy", "maximum_disk_size_gib", fallback="").strip()
    maximum_disk_size_gib = None
    if maximum_value:
        try:
            maximum_disk_size_gib = int(maximum_value)
        except ValueError as error:
            raise ValueError("[auto_deploy] maximum_disk_size_gib must be a positive integer.") from error
        if maximum_disk_size_gib < 1:
            raise ValueError("[auto_deploy] maximum_disk_size_gib must be a positive integer.")
        if maximum_disk_size_gib < minimum_disk_size_gib:
            raise ValueError("[auto_deploy] maximum_disk_size_gib must be at least minimum_disk_size_gib.")
    wim_path = validate_wim_path(_required_value(parser, "auto_deploy", "wim_path"))
    share_root = unc_share_root(wim_path)
    username = parser.get("auto_deploy", "username", fallback="").strip()
    password = parser.get("auto_deploy", "password", fallback="").strip()
    if share_root and (not username or not password):
        raise ValueError("[auto_deploy] UNC wim_path requires username= and password=.")
    if not share_root and (username or password):
        raise ValueError("[auto_deploy] username/password are only supported for a UNC wim_path.")
    return AutoDeployStartupConfig(
        wim_path=wim_path,
        image_index=validate_image_index(_required_value(parser, "auto_deploy", "image_index")),
        firmware=firmware,
        minimum_disk_size_gib=minimum_disk_size_gib,
        expected_disk_serial=parser.get("auto_deploy", "expected_disk_serial", fallback="").strip(),
        maximum_disk_size_gib=maximum_disk_size_gib,
        username=username,
        password=password,
    )


def select_auto_deploy_target(
    disks: list[DiskInfo], protected_disk_numbers: set[int], configuration: AutoDeployStartupConfig
) -> DiskInfo:
    """Return exactly one non-protected physical disk within the size bounds or fail."""
    # A unique exact serial can select a target among several disks, but must
    # never override protection of the configuration or local WIM disk.
    candidates = [
        disk
        for disk in disks
        if disk.number not in protected_disk_numbers
        and disk.size_gib >= configuration.minimum_disk_size_gib
        and (configuration.maximum_disk_size_gib is None or disk.size_bytes <= configuration.maximum_disk_size_gib * 1024**3)
        and (not configuration.expected_disk_serial or disk.serial_number.strip() == configuration.expected_disk_serial)
    ]
    if len(candidates) != 1:
        raise ValueError(
            "Auto-deploy requires exactly one eligible physical disk after protecting WinPE/configuration media; "
            f"found {len(candidates)}."
        )
    return candidates[0]


def _validate_keys(parser: configparser.ConfigParser, section: str, allowed_keys: set[str]) -> None:
    unknown_keys = set(parser[section]) - allowed_keys
    if unknown_keys:
        raise ValueError(f"Unsupported [{section}] option(s): {', '.join(sorted(unknown_keys))}.")


def _required_value(parser: configparser.ConfigParser, section: str, option: str) -> str:
    value = parser.get(section, option, fallback="").strip()
    if not value:
        raise ValueError(f"[{section}] requires {option}=.")
    return value