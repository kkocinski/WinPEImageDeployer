"""Optional, removable-media startup configuration for WinPE networking."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

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
class AutoDeployStartupConfig:
    wim_path: str
    image_index: int
    firmware: FirmwareType
    minimum_disk_size_gib: int
    expected_disk_serial: str
    maximum_disk_size_gib: int | None = None


@dataclass(frozen=True)
class StartupConfig:
    ethernet: EthernetStartupConfig | None = None
    share: ShareStartupConfig | None = None
    auto_deploy: AutoDeployStartupConfig | None = None


def load_startup_config(path: Path) -> StartupConfig:
    """Load a deliberately optional INI file stored beside WinPE USB drivers."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as error:
        raise ValueError(f"Cannot read startup configuration {path}: {error}") from error

    unknown_sections = set(parser.sections()) - {"ethernet", "share", "auto_deploy"}
    if unknown_sections:
        raise ValueError(f"Unsupported startup configuration section(s): {', '.join(sorted(unknown_sections))}.")

    ethernet = _load_ethernet(parser)
    share = _load_share(parser)
    auto_deploy = _load_auto_deploy(parser)
    if ethernet is None and share is None and auto_deploy is None:
        raise ValueError("Startup configuration must contain an [ethernet], [share], or enabled [auto_deploy] section.")
    return StartupConfig(ethernet=ethernet, share=share, auto_deploy=auto_deploy)


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
        {"enabled", "wim_path", "image_index", "firmware", "minimum_disk_size_gib", "maximum_disk_size_gib", "expected_disk_serial"},
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
    return AutoDeployStartupConfig(
        wim_path=validate_wim_path(_required_value(parser, "auto_deploy", "wim_path")),
        image_index=validate_image_index(_required_value(parser, "auto_deploy", "image_index")),
        firmware=firmware,
        minimum_disk_size_gib=minimum_disk_size_gib,
        expected_disk_serial=parser.get("auto_deploy", "expected_disk_serial", fallback="").strip(),
        maximum_disk_size_gib=maximum_disk_size_gib,
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