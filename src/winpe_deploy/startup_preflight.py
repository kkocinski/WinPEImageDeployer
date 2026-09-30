"""Non-GUI WinPE startup work that must complete before Tk is created."""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from pathlib import PureWindowsPath

from .command_runner import CommandRunner
from .media_content import MEDIA_CONTENT_DIR
from .models import DiskInfo, validate_wim_path
from .network_adapters import adapter_names_for_mac
from .services import DeploymentService, NetworkService, WinPEPowerService
from .startup_config import AutoDeployStartupConfig, StartupConfig, load_startup_config, select_auto_deploy_target, unc_share_root


STARTUP_LOG_PATH = Path(r"X:\Windows\Temp\WinPEImageDeployer-startup.log")
DRIVER_SETTLE_SECONDS = 10
ETHERNET_SETTLE_SECONDS = 10
SMB_CONNECT_ATTEMPTS = 6
SMB_CONNECT_RETRY_SECONDS = 5


def configure_startup_logger(log_path: Path = STARTUP_LOG_PATH) -> logging.Logger:
    """Create an independent persistent log; this must not depend on Tk."""
    logger = logging.getLogger("winpe_deployer.startup")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    logger.addHandler(console_handler)
    logger.propagate = False
    return logger


def boot_media_roots() -> list[Path]:
    """Return every mounted drive root except WinPE's X: RAM disk."""
    try:
        drive_mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except (AttributeError, OSError):
        return []
    roots: list[Path] = []
    for index in range(26):
        if not drive_mask & (1 << index):
            continue
        letter = chr(ord("A") + index)
        if letter == "X":
            continue
        root = Path(f"{letter}:\\")
        if root.is_dir():
            roots.append(root)
    return roots


def run_startup_preflight(
    *,
    roots: Iterable[Path] | None = None,
    logger: logging.Logger | None = None,
    deployment: DeploymentService | None = None,
    network: NetworkService | None = None,
    power: WinPEPowerService | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Run startup work and return whether automatic deployment completed."""
    logger = logger or configure_startup_logger()
    runner = CommandRunner(logger)
    deployment = deployment or DeploymentService(runner, logger)
    network = network or NetworkService(runner)
    power = power or WinPEPowerService(runner)
    scanned_roots = list(roots) if roots is not None else boot_media_roots()
    logger.info("=== WinPE Image Deployer startup preflight started ===")
    logger.info("Scanned drive roots: %s", ", ".join(str(root) for root in scanned_roots) or "none")

    # Only scan the designated media folder; never load arbitrary INF files
    # or startup configurations found elsewhere on mounted volumes.
    content_roots = [root / MEDIA_CONTENT_DIR for root in scanned_roots if (root / MEDIA_CONTENT_DIR).is_dir()]
    logger.info("Found %s folder(s): %s", MEDIA_CONTENT_DIR, ", ".join(str(path) for path in content_roots) or "none")

    total_loaded = 0
    total_failures = 0
    for content_root in content_roots:
        drivers = content_root / "Drivers"
        if not drivers.is_dir():
            logger.info("No Drivers folder at %s.", drivers)
            continue
        inf_files = sorted((path for path in drivers.rglob("*.inf") if path.is_file()), key=lambda path: str(path).casefold())
        logger.info("Driver folder %s contains %d INF file(s).", drivers, len(inf_files))
        if not inf_files:
            continue
        try:
            loaded, failures = deployment.load_session_drivers(str(drivers), logger.info)
        except Exception as error:
            logger.exception("Could not load drivers from %s: %s", drivers, error)
            continue
        total_loaded += loaded
        total_failures += len(failures)
    logger.info("Driver preflight completed: %d loaded, %d failed.", total_loaded, total_failures)
    if total_loaded:
        logger.info("Waiting %d seconds for newly loaded drivers and network adapters to initialize.", DRIVER_SETTLE_SECONDS)
        sleeper(DRIVER_SETTLE_SECONDS)

    configuration_paths = [content_root / "startup-config.ini" for content_root in content_roots if (content_root / "startup-config.ini").is_file()]
    logger.info("Found active startup configuration file(s): %s", ", ".join(str(path) for path in configuration_paths) or "none")
    if len(configuration_paths) != 1:
        if len(configuration_paths) > 1:
            logger.warning("Startup configuration was skipped because more than one startup-config.ini was found.")
        logger.info("=== WinPE Image Deployer startup preflight finished ===")
        return False

    configuration_path = configuration_paths[0]
    try:
        configuration = load_startup_config(configuration_path)
    except ValueError as error:
        logger.error("Startup configuration %s is invalid; automatic deployment was not started and the GUI will open: %s", configuration_path, error)
    else:
        ethernet_configured = _apply_ethernet_configuration(configuration_path, configuration, network, logger)
        if ethernet_configured and (configuration.share is not None or (configuration.auto_deploy is not None and unc_share_root(configuration.auto_deploy.wim_path))):
            logger.info("Waiting %d seconds after static IPv4 configuration before SMB mapping.", ETHERNET_SETTLE_SECONDS)
            sleeper(ETHERNET_SETTLE_SECONDS)
        _connect_share_with_retry(configuration_path, configuration, network, logger, sleeper)
        if configuration.auto_deploy is not None:
            if _run_automatic_deployment(configuration_path, configuration.auto_deploy, deployment, power, logger, network, sleeper):
                logger.info("=== WinPE Image Deployer startup preflight finished after successful automatic deployment ===")
                return True
    logger.info("=== WinPE Image Deployer startup preflight finished ===")
    return False


def _run_automatic_deployment(
    configuration_path: Path,
    configuration: AutoDeployStartupConfig,
    deployment: DeploymentService,
    power: WinPEPowerService,
    logger: logging.Logger,
    network: NetworkService,
    sleeper: Callable[[float], None],
) -> bool:
    """Deploy before Tk is created; any validation or runtime failure opens GUI instead."""
    try:
        logger.info("[auto_deploy] enabled: validating WIM, image index, and target disk.")
        share_root = unc_share_root(configuration.wim_path)
        if share_root:
            _connect_auto_deploy_unc_with_retry(share_root, configuration, network, logger, sleeper)
        disks = deployment.list_disks_for_auto_deploy() if (configuration.expected_disk_serial or configuration.source_disk_serial) else deployment.list_disks()
        source_disk = None
        if configuration.source_disk_serial:
            wim_path, source_disk = _resolve_source_wim(configuration, disks, deployment)
        else:
            wim_path = _validate_existing_wim_path(configuration.wim_path)
        images = deployment.inspect_wim(wim_path)
        _require_wim_image_index(images, configuration.image_index)
        protected_disks = _auto_deploy_protected_disk_numbers(configuration_path, wim_path, deployment, logger)
        if source_disk is not None:
            protected_disks.add(source_disk.number)
            logger.info("Auto-deploy protected Disk %d as the WIM source disk.", source_disk.number)
        logger.info(
            "Auto-deploy evaluation: protected disks=%s; detected disks=%s",
            sorted(protected_disks),
            "; ".join(disk.display_name() for disk in disks) or "none",
        )
        target = select_auto_deploy_target(disks, protected_disks, configuration)
        if source_disk is not None:
            # Recheck the mounted letter just before clean, not only the metadata snapshot.
            if deployment.disk_number_for_drive(PureWindowsPath(wim_path).drive) != source_disk.number:
                raise ValueError("WIM drive no longer belongs to the selected source disk.")
        logger.info("Auto-deploy selected eligible target: %s", target.display_name())
        logger.info("Starting automatic deployment to Disk %d. All existing data on this disk will be erased.", target.number)
        deployment.deploy_image(wim_path, configuration.image_index, target.number, configuration.firmware, logger.info)
        logger.info("Automatic deployment completed successfully. Restarting WinPE now.")
        power.restart()
        return True
    except Exception as error:
        # Native SMB errors can echo command arguments: never log the password.
        safe_error = str(error).replace(configuration.password, "********") if configuration.password else str(error)
        logger.error("Automatic deployment was not started or failed: %s. Opening the GUI instead.", safe_error)
        return False


def _resolve_source_wim(configuration: AutoDeployStartupConfig, disks: list[DiskInfo], deployment: DeploymentService) -> tuple[str, DiskInfo]:
    matches = [disk for disk in disks if disk.serial_number.strip() == configuration.source_disk_serial]
    if len(matches) != 1:
        raise ValueError(f"WIM source serial must identify exactly one physical disk; found {len(matches)}.")
    source_disk = matches[0]
    found: list[str] = []
    for _, letter in source_disk.partition_letters:
        # Resolve only the configured relative filename, not every WIM on the disk.
        candidate = str(PureWindowsPath(letter + "\\") / configuration.wim_path)
        if Path(candidate).is_file():
            if deployment.disk_number_for_drive(letter) != source_disk.number:
                raise ValueError(f"WIM drive {letter} does not belong to the selected source disk.")
            found.append(candidate)
    if len(found) != 1:
        raise ValueError(f"Expected exactly one accessible WIM at {configuration.wim_path} on source disk; found {len(found)}.")
    return _validate_existing_wim_path(found[0]), source_disk


def _connect_auto_deploy_unc_with_retry(
    share_root: str, configuration: AutoDeployStartupConfig, network: NetworkService,
    logger: logging.Logger, sleeper: Callable[[float], None],
) -> None:
    for attempt in range(1, SMB_CONNECT_ATTEMPTS + 1):
        try:
            network.connect_unc_share(share_root, configuration.username, configuration.password)
            logger.info("Authenticated SMB share %s without mapping a drive.", share_root)
            return
        except Exception as error:
            safe_error = str(error).replace(configuration.password, "********")
            if attempt == SMB_CONNECT_ATTEMPTS:
                raise RuntimeError(f"SMB authentication to {share_root} failed after {attempt} attempts: {safe_error}") from None
            logger.warning("SMB authentication to %s failed on attempt %d/%d: %s. Retrying in %d seconds.",
                           share_root, attempt, SMB_CONNECT_ATTEMPTS, safe_error, SMB_CONNECT_RETRY_SECONDS)
            sleeper(SMB_CONNECT_RETRY_SECONDS)


def _auto_deploy_protected_disk_numbers(
    configuration_path: Path, wim_path: str, deployment: DeploymentService, logger: logging.Logger
) -> set[int]:
    protected: set[int] = set()
    _protect_path_disk(configuration_path, protected, deployment, logger, required=True)
    # Remote WIM storage has no local physical disk; never resolve a UNC share as a target disk.
    if not unc_share_root(wim_path):
        _protect_path_disk(Path(wim_path), protected, deployment, logger, required=False)
    return protected


def _protect_path_disk(
    path: Path, protected: set[int], deployment: DeploymentService, logger: logging.Logger, *, required: bool
) -> None:
    drive = path.drive.upper()
    if not drive:
        if required:
            raise ValueError(f"Startup configuration path {path} has no drive letter.")
        return
    if _drive_type(Path(f"{drive}\\")) not in {2, 3}:
        logger.info("Auto-deploy protection skipped for %s because %s is not local/removable media.", path, drive)
        return
    try:
        disk_number = deployment.disk_number_for_drive(drive)
    except Exception as error:
        raise ValueError(f"Could not protect the physical disk containing {path}.") from error
    protected.add(disk_number)
    logger.info("Auto-deploy protected Disk %d because it contains %s.", disk_number, path)


def _drive_type(root: Path) -> int:
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(str(root)))
    except (AttributeError, OSError):
        return 0


def _validate_existing_wim_path(path: str) -> str:
    wim_path = validate_wim_path(path)
    if not Path(wim_path).is_file():
        raise ValueError(f"Configured WIM image does not exist or is not accessible: {wim_path}")
    return wim_path


def _require_wim_image_index(images: list, image_index: int) -> None:
    if not any(image.index == image_index for image in images):
        available_indexes = ", ".join(str(image.index) for image in images) or "none"
        raise ValueError(f"Configured WIM image index {image_index} was not found. Available indexes: {available_indexes}.")


def _apply_ethernet_configuration(path: Path, configuration: StartupConfig, network: NetworkService, logger: logging.Logger) -> bool:
    ethernet = configuration.ethernet
    if ethernet is None:
        logger.info("No [ethernet] section in %s.", path)
        return False
    try:
        adapters = network.list_adapters()
        logger.info("Network adapters available before Ethernet configuration: %s", ", ".join(adapters) or "none")
        adapter = ethernet.adapter
        if ethernet.mac:
            # IP Helper also enumerates filter/virtual interfaces sharing a physical
            # MAC; only netsh-visible aliases can actually receive IPv4 settings.
            matches = [name for name in adapter_names_for_mac(ethernet.mac) if name in adapters]
            if len(matches) != 1:
                raise ValueError(f"Configured MAC {ethernet.mac} matched {len(matches)} network adapters; expected exactly one.")
            if adapter and adapter != matches[0]:
                raise ValueError(f"Configured adapter {adapter!r} does not match MAC {ethernet.mac} ({matches[0]!r}).")
            adapter = matches[0]
        if adapter not in adapters:
            raise ValueError(f"Configured adapter {adapter!r} was not found.")
        network.configure_ipv4(adapter, ethernet.address, ethernet.mask, ethernet.gateway, ethernet.dns_servers)
        logger.info("Applied static IPv4 configuration to adapter %r from %s.", adapter, path)
        return True
    except Exception as error:
        logger.exception("Static Ethernet configuration from %s failed: %s", path, error)
        return False


def _connect_share_with_retry(
    path: Path, configuration: StartupConfig, network: NetworkService, logger: logging.Logger, sleeper: Callable[[float], None]
) -> None:
    share = configuration.share
    if share is None:
        logger.info("No [share] section in %s.", path)
        return
    for attempt in range(1, SMB_CONNECT_ATTEMPTS + 1):
        try:
            network.connect_share(share.drive_letter, share.unc_path, share.username, share.password)
            logger.info("Mapped SMB share %s to %s from %s.", share.unc_path, share.drive_letter, path)
            return
        except Exception as error:
            if attempt == SMB_CONNECT_ATTEMPTS:
                logger.exception(
                    "SMB mapping of %s to %s failed after %d attempts: %s",
                    share.unc_path,
                    share.drive_letter,
                    attempt,
                    error,
                )
                return
            logger.warning(
                "SMB mapping of %s to %s failed on attempt %d/%d: %s. Retrying in %d seconds.",
                share.unc_path,
                share.drive_letter,
                attempt,
                SMB_CONNECT_ATTEMPTS,
                error,
                SMB_CONNECT_RETRY_SECONDS,
            )
            sleeper(SMB_CONNECT_RETRY_SECONDS)