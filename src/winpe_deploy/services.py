from __future__ import annotations

import ctypes
import json
import logging
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from ctypes import wintypes

from .command_runner import CommandExecutionError, CommandRunner
from .models import (
    CaptureCompression,
    DiskInfo,
    FirmwareType,
    PartitionInfo,
    VolumeInfo,
    WimImageInfo,
    normalize_drive_letter,
    validate_relative_image_destination,
    validate_volume_label,
)

ProgressCallback = Callable[[str], None]


class DeploymentService:
    def __init__(self, runner: CommandRunner, logger: logging.Logger) -> None:
        self._runner = runner
        self._logger = logger

    def list_disks(self) -> list[DiskInfo]:
        """Use DiskPart first because it is present and reliable in minimal WinPE."""
        disks = self._list_disks_with_diskpart()
        if disks:
            return disks
        self._logger.warning("DiskPart returned no disks; trying PowerShell Storage cmdlets as a fallback.")
        return self._list_disks_with_powershell()

    def list_disks_for_auto_deploy(self) -> list[DiskInfo]:
        """Require rich bus/serial metadata; automatic clean must never guess."""
        disks = self._list_disks_with_powershell()
        if not disks:
            raise RuntimeError("Automatic deployment requires PowerShell disk metadata, but no detailed physical disks were returned.")
        return disks

    def disk_number_for_drive(self, drive_letter: str) -> int:
        """Resolve a mounted local drive to its physical disk number for protection.

        PowerShell Storage cmdlets are preferred, but some WinPE builds expose
        Get-Disk while not resolving Get-Partition for removable media. DiskPart
        is available in the supported WinPE image and provides a safe fallback.
        """
        drive = normalize_drive_letter(drive_letter)
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"$ErrorActionPreference='Stop'; (Get-Partition -DriveLetter '{drive[0]}' | Select-Object -First 1 -ExpandProperty DiskNumber)",
        ]
        try:
            output = self._runner.run(command).stdout.strip()
            return int(output)
        except (CommandExecutionError, ValueError) as error:
            self._logger.warning("PowerShell could not resolve %s to a disk number: %s. Trying DiskPart.", drive, error)
        try:
            output = self._run_diskpart_output(f"select volume {drive[0]}\r\ndetail volume\r\n")
            disk_numbers: set[int] = set()
            for line in output.splitlines():
                match = re.match(r"^\s*\*?\s*(?:disk|dysk)\s+(\d+)\s+", line, re.IGNORECASE)
                if match:
                    disk_numbers.add(int(match.group(1)))
            if len(disk_numbers) == 1:
                return disk_numbers.pop()
            if len(disk_numbers) > 1:
                raise RuntimeError(f"Volume {drive} spans several physical disks; single-disk mapping is unsafe.")
        except CommandExecutionError as error:
            raise RuntimeError(f"Could not resolve {drive} to a physical disk number.") from error
        raise RuntimeError(f"Could not resolve {drive} to a physical disk number using PowerShell or DiskPart.")

    def single_disk_number_for_drive(self, drive_letter: str) -> int:
        """Lock mapping: require DiskPart to report exactly one physical extent."""
        drive = normalize_drive_letter(drive_letter)
        output = self._run_diskpart_output(f"select volume {drive[0]}\r\ndetail volume\r\n")
        numbers = {
            int(match.group(1))
            for line in output.splitlines()
            if (match := re.match(r"^\s*\*?\s*(?:disk|dysk)\s+(\d+)\s+", line, re.IGNORECASE))
        }
        if len(numbers) != 1:
            raise RuntimeError(f"Cannot safely map {drive} to exactly one physical disk.")
        return numbers.pop()

    def _list_disks_with_powershell(self) -> list[DiskInfo]:
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            (
                "$ErrorActionPreference='Stop'; "
                "$result = foreach ($disk in Get-Disk) { "
                "$partitions = @(Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue); "
                "$volumes = @($partitions | ForEach-Object { $partition = $_; $volume = $partition | Get-Volume -ErrorAction SilentlyContinue; if ($null -ne $volume -and $null -ne $volume.Size) { $volume } }); "
                "$volumeSize = [int64](@($volumes | Measure-Object -Property Size -Sum).Sum); "
                "$volumeFree = [int64](@($volumes | Measure-Object -Property SizeRemaining -Sum).Sum); "
                "$partitionSize = [int64](@($partitions | Measure-Object -Property Size -Sum).Sum); "
                "[pscustomobject]@{Number=$disk.Number;FriendlyName=$disk.FriendlyName;BusType=$disk.BusType;SerialNumber=$disk.SerialNumber;Size=[int64]$disk.Size;VolumeSize=$volumeSize;VolumeFree=$volumeFree;Unallocated=[int64][math]::Max(0,([int64]$disk.Size-$partitionSize))} "
                "}; $result | ConvertTo-Json -Compress"
            ),
        ]
        try:
            output = self._runner.run(command).stdout.strip()
            raw_disks = json.loads(output) if output else []
            if isinstance(raw_disks, dict):
                raw_disks = [raw_disks]
            return [
                DiskInfo(
                    number=int(item["Number"]),
                    model=item.get("FriendlyName") or "Unknown physical disk",
                    size_bytes=int(item["Size"]),
                    bus_type=item.get("BusType") or "Unknown bus",
                    serial_number=(item.get("SerialNumber") or "").strip(),
                    volume_size_bytes=int(item.get("VolumeSize") or 0),
                    volume_free_bytes=int(item.get("VolumeFree") or 0),
                    unallocated_bytes=int(item.get("Unallocated") or 0),
                )
                for item in raw_disks
            ]
        except (CommandExecutionError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            self._logger.error("PowerShell disk discovery failed: %s", error)
            return []

    def _list_disks_with_diskpart(self) -> list[DiskInfo]:
        """Read physical disk capacity and free/unallocated space from DiskPart."""
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="ascii", newline="\r\n") as handle:
                handle.write("list disk\r\n")
                script_path = handle.name
            try:
                output = self._runner.run(["diskpart.exe", "/s", script_path]).stdout
            finally:
                Path(script_path).unlink(missing_ok=True)
        except CommandExecutionError as error:
            self._logger.error("DiskPart disk discovery failed: %s", error)
            return []

        disks: list[DiskInfo] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith("disk ") or "---" in stripped:
                continue
            fields = stripped.split()
            if len(fields) < 4 or not fields[1].isdigit():
                continue
            try:
                measurements = re.findall(r"\b(\d+(?:\.\d+)?)\s+(KB|MB|GB|TB)\b", stripped, re.IGNORECASE)
                if not measurements:
                    raise ValueError("No disk size was found")
                size_bytes = self._diskpart_measurement_to_bytes(*measurements[0])
                unallocated_bytes = self._diskpart_measurement_to_bytes(*measurements[1]) if len(measurements) > 1 else 0
                disks.append(DiskInfo(int(fields[1]), "Physical disk (DiskPart)", size_bytes, unallocated_bytes=unallocated_bytes))
            except (KeyError, ValueError):
                self._logger.warning("Could not parse DiskPart disk entry: %s", stripped)
        return disks

    @staticmethod
    def _diskpart_measurement_to_bytes(value: str, unit: str) -> int:
        multiplier = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit.upper()]
        return int(float(value) * multiplier)

    def list_volumes(self) -> list[VolumeInfo]:
        """List volumes through Windows APIs; results are independent of UI language."""
        try:
            drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
        except AttributeError as error:
            self._logger.error("Windows volume APIs are unavailable: %s", error)
            return []

        volumes: list[VolumeInfo] = []
        for index in range(26):
            if not drive_mask & (1 << index):
                continue
            drive = f"{chr(ord('A') + index)}:"
            if drive == "X:":
                continue  # WinPE RAM disk is not persistent WIM storage.
            try:
                volumes.append(self._get_windows_volume_info(drive))
            except OSError as error:
                self._logger.warning("Could not read volume information for %s: %s", drive, error)
        return volumes

    def list_partitions(self, disk_number: int) -> list[PartitionInfo]:
        """List partitions using DiskPart, accepting localized partition type labels."""
        if disk_number < 0:
            raise ValueError("Select a valid physical disk.")
        output = self._run_diskpart_output(f"select disk {disk_number}\r\nlist partition\r\n")
        partitions: list[PartitionInfo] = []
        for line in output.splitlines():
            # DiskPart headers and the word before a partition number are
            # localized, but data rows carry a number followed by size/offset.
            match = re.match(
                r"^\s*\*?\s*(?:[^\d]+?\s+)?(\d+)\s+(.+?)\s+(\d+(?:\.\d+)?)\s+(KB|MB|GB|TB)\s+(\d+(?:\.\d+)?)\s+(KB|MB|GB|TB)\s*$",
                line,
                re.IGNORECASE,
            )
            if match is None:
                continue
            number, partition_type, size, size_unit, offset, offset_unit = match.groups()
            partitions.append(
                PartitionInfo(
                    disk_number,
                    int(number),
                    partition_type.strip(),
                    self._diskpart_measurement_to_bytes(size, size_unit),
                    self._diskpart_measurement_to_bytes(offset, offset_unit),
                )
            )
        return partitions

    @staticmethod
    def _get_windows_volume_info(drive_letter: str) -> VolumeInfo:
        """Read size, free space, label, and filesystem through kernel32 Unicode APIs."""
        root = normalize_drive_letter(drive_letter) + "\\"
        kernel32 = ctypes.windll.kernel32
        free_available = ctypes.c_ulonglong()
        total_bytes = ctypes.c_ulonglong()
        total_free = ctypes.c_ulonglong()
        if not kernel32.GetDiskFreeSpaceExW(
            root,
            ctypes.byref(free_available),
            ctypes.byref(total_bytes),
            ctypes.byref(total_free),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        label = ctypes.create_unicode_buffer(261)
        file_system = ctypes.create_unicode_buffer(261)
        serial_number = wintypes.DWORD()
        maximum_component_length = wintypes.DWORD()
        file_system_flags = wintypes.DWORD()
        if not kernel32.GetVolumeInformationW(
            root,
            label,
            len(label),
            ctypes.byref(serial_number),
            ctypes.byref(maximum_component_length),
            ctypes.byref(file_system_flags),
            file_system,
            len(file_system),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return VolumeInfo(
            normalize_drive_letter(drive_letter),
            label.value,
            file_system.value,
            int(total_bytes.value),
            int(free_available.value),
        )

    def capture_image(
        self, source_volume: str, image_path: str, name: str, description: str,
        compression: CaptureCompression, progress: ProgressCallback,
    ) -> None:
        volume = normalize_drive_letter(source_volume)
        if not name.strip():
            raise ValueError("An image name is required.")
        output = Path(image_path)
        if output.exists():
            raise ValueError("The selected WIM file already exists. Choose a new filename to avoid overwriting it.")
        if not output.parent.exists():
            raise ValueError("The WIM output directory does not exist.")
        destination_drive = normalize_drive_letter(output.drive or "")
        destination_volume = self._get_windows_volume_info(destination_drive)
        if destination_volume.file_system.upper() in {"FAT", "FAT12", "FAT16", "FAT32"}:
            raise ValueError(
                f"{destination_drive} uses {destination_volume.file_system}. A single WIM can exceed the 4 GiB FAT32 file limit. "
                "Use an NTFS or exFAT destination volume."
            )
        if destination_volume.free_bytes == 0:
            raise ValueError(f"{destination_drive} reports no available free space. Refresh volumes or select another destination.")
        source_volume_info = self._get_windows_volume_info(volume)
        minimum_scratch_bytes = 1024**3
        if destination_volume.free_bytes < minimum_scratch_bytes:
            raise ValueError(
                f"{destination_drive} has only {destination_volume.free_gib:.1f} GiB free. "
                "At least 1 GiB is required for the DISM scratch directory."
            )

        scratch_directory = Path(tempfile.mkdtemp(prefix="WinPEImageDeployerScratch-", dir=f"{destination_drive}\\"))
        self._logger.info(
            "Capture storage: source %s has %.1f GiB used; destination %s has %.1f GiB free; scratch directory: %s",
            volume,
            source_volume_info.used_gib,
            destination_drive,
            destination_volume.free_gib,
            scratch_directory,
        )
        try:
            dism_environment = {"TEMP": str(scratch_directory), "TMP": str(scratch_directory)}
            progress(f"Capturing {volume} to {output}. DISM TEMP/TMP: {scratch_directory}.")
            self._run_dism_with_progress([
                "dism.exe", "/Capture-Image", f"/ImageFile:{output}", f"/CaptureDir:{volume}\\",
                f"/Name:{name.strip()}", f"/Description:{description.strip()}", f"/Compress:{compression.dism_value}",
                f"/ScratchDir:{scratch_directory}",
                "/CheckIntegrity", "/Verify",
            ], progress, environment=dism_environment)
            progress("Image capture completed successfully.")
        finally:
            try:
                shutil.rmtree(scratch_directory, ignore_errors=True)
                self._logger.info("Removed DISM scratch directory: %s", scratch_directory)
            except OSError as error:
                self._logger.warning("Could not remove DISM scratch directory %s: %s", scratch_directory, error)

    def inspect_wim(self, image_path: str) -> list[WimImageInfo]:
        result = self._runner.run(["dism.exe", "/Get-WimInfo", f"/WimFile:{image_path}", "/English"])
        images: list[WimImageInfo] = []
        index: int | None = None
        name = ""
        description = ""
        for line in result.stdout.splitlines():
            key, separator, value = line.partition(":")
            if not separator:
                continue
            normalized_key = key.strip().lower()
            value = value.strip()
            if normalized_key == "index":
                if index is not None:
                    images.append(WimImageInfo(index, name, description))
                index, name, description = int(value), "", ""
            elif normalized_key == "name":
                name = value
            elif normalized_key == "description":
                description = value
        if index is not None:
            images.append(WimImageInfo(index, name, description))
        return images

    def deploy_image(
        self, image_path: str, image_index: int, disk_number: int, firmware: FirmwareType, progress: ProgressCallback
    ) -> None:
        system_volume, windows_volume = self._select_free_deployment_drive_letters(firmware)
        layout_description = f"EFI={system_volume}, Windows={windows_volume}" if system_volume else f"Windows={windows_volume}"
        progress(f"5% Preparing Disk {disk_number}. All existing data will be erased. Temporary drive letters: {layout_description}.")
        self._run_diskpart(self._partition_script(disk_number, firmware, system_volume, windows_volume))
        scratch_directory = Path(tempfile.mkdtemp(prefix="WinPEImageDeployerScratch-", dir=f"{windows_volume}\\"))
        try:
            dism_environment = {"TEMP": str(scratch_directory), "TMP": str(scratch_directory)}
            progress(f"10% Applying the Windows image. This can take several minutes. DISM TEMP/TMP: {scratch_directory}.")
            self._run_dism_with_progress([
                "dism.exe", "/Apply-Image", f"/ImageFile:{image_path}", f"/Index:{image_index}",
                f"/ApplyDir:{windows_volume}\\", "/CheckIntegrity", "/Verify",
            ], lambda message: progress(self._map_deploy_dism_progress(message)), environment=dism_environment)
            progress("95% Creating boot files.")
            if firmware is FirmwareType.UEFI:
                assert system_volume is not None
                self._runner.run(["bcdboot.exe", f"{windows_volume}\\Windows", "/s", system_volume, "/f", "UEFI", "/c"])
                progress("97% Registering the deployed disk with UEFI firmware.")
                self._prefer_deployed_uefi_disk(system_volume)
            else:
                self._runner.run(["bcdboot.exe", f"{windows_volume}\\Windows", "/s", windows_volume, "/f", "BIOS", "/c"])
            boot_mode_guidance = "UEFI firmware with a GPT disk" if firmware is FirmwareType.UEFI else "legacy BIOS firmware with an MBR disk"
            hyper_v_generation = "Generation 2" if firmware is FirmwareType.UEFI else "Generation 1"
            progress(
                f"100% Deployment completed successfully using {firmware.value}. "
                f"Boot this disk using {boot_mode_guidance}. For Hyper-V, use {hyper_v_generation}."
            )
        finally:
            shutil.rmtree(scratch_directory, ignore_errors=True)
            self._logger.info("Removed DISM deployment scratch directory: %s", scratch_directory)

    def _prefer_deployed_uefi_disk(self, system_volume: str) -> None:
        """Prioritize Windows Boot Manager pointing to the target ESP in UEFI NVRAM."""
        esp = normalize_drive_letter(system_volume)
        boot_file = Path(f"{esp}\\EFI\\Microsoft\\Boot\\bootmgfw.efi")
        if not boot_file.is_file():
            raise RuntimeError(f"UEFI boot file is missing on target ESP {esp}; refusing automatic reboot.")

        try:
            firmware_before = self._runner.run(["bcdedit.exe", "/enum", "firmware"]).stdout
            if not re.search(r"(?im)^\s*identifier\s+\{bootmgr\}\s*$", firmware_before):
                raise RuntimeError("Windows Boot Manager firmware entry is missing; cannot safely prioritize the target ESP.")
            # /sysstore is not persistent across separate BCDEdit processes.
            # Address the target ESP explicitly via device instead.
            self._runner.run(["bcdedit.exe", "/set", "{bootmgr}", "device", f"partition={esp}"])
            self._runner.run(["bcdedit.exe", "/set", "{bootmgr}", "path", r"\EFI\Microsoft\Boot\bootmgfw.efi"])
            self._runner.run(["bcdedit.exe", "/set", "{fwbootmgr}", "displayorder", "{bootmgr}", "/addfirst"])
            firmware_entries = self._runner.run(["bcdedit.exe", "/enum", "firmware"]).stdout
            sections = re.split(r"(?im)^\s*identifier\s+", firmware_entries)
            firmware_section = next((section for section in sections if section.lstrip().casefold().startswith("{fwbootmgr}")), "")
            boot_section = next((section for section in sections if section.lstrip().casefold().startswith("{bootmgr}")), "")
            order = re.search(r"(?im)^\s*displayorder\s+(\{[^}]+\})", firmware_section)
            device = re.search(r"(?im)^\s*device\s+partition=([^\s]+)", boot_section)
            path = re.search(r"(?im)^\s*path\s+(\S+)", boot_section)
            if (order is None or order.group(1).casefold() != "{bootmgr}"
                    or device is None or not self._firmware_device_matches_volume(device.group(1), esp)
                    or path is None or path.group(1).casefold() != r"\EFI\Microsoft\Boot\bootmgfw.efi".casefold()):
                raise RuntimeError("UEFI firmware did not report the deployed ESP as its first boot entry.")
        except (CommandExecutionError, RuntimeError) as error:
            self._logger.error("Could not make the deployed disk the preferred UEFI boot target: %s", error)
            raise RuntimeError("UEFI boot priority could not be verified; automatic restart was stopped.") from error
        self._logger.info("UEFI firmware reports deployed ESP %s first in boot priority.", esp)

    @staticmethod
    def _firmware_device_matches_volume(device: str, volume: str) -> bool:
        """BCDEdit may render the device as a DOS letter or an NT device path."""
        if device.casefold() == volume.casefold():
            return True
        if not device.casefold().startswith("\\device\\harddiskvolume"):
            return False
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if not kernel32.QueryDosDeviceW(volume, buffer, len(buffer)):
                return False
            return device.casefold() in (entry.casefold() for entry in buffer[:].split("\0") if entry)
        except (AttributeError, OSError):
            return False

    def _run_dism_with_progress(
        self, command: list[str], progress: ProgressCallback, *, environment: dict[str, str] | None = None
    ) -> None:
        streaming_runner = getattr(self._runner, "run_streaming", None)
        if streaming_runner is None:
            self._runner.run(command, environment=environment)
            return
        streaming_runner(command, output_callback=progress, environment=environment)

    @staticmethod
    def _map_deploy_dism_progress(message: str) -> str:
        """Map DISM apply progress into the 10-95% deployment workflow range."""
        match = re.search(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%", message)
        if match is None:
            return message
        dism_percent = min(100.0, float(match.group(1)))
        overall_percent = 10.0 + dism_percent * 0.85
        return message[:match.start()] + f"{overall_percent:.1f}%" + message[match.end():]

    def inject_files(
        self, image_path: str, image_index: int, mount_directory: str, source_paths: Iterable[str], destination: str,
        progress: ProgressCallback,
    ) -> None:
        mount = Path(mount_directory)
        sources = [Path(path) for path in source_paths]
        destination_relative = validate_relative_image_destination(destination)
        if not sources:
            raise ValueError("Add at least one file or directory to inject.")
        if mount.exists() and any(mount.iterdir()):
            raise ValueError("The mount directory must be empty before mounting an image.")
        if any(not source.exists() for source in sources):
            raise ValueError("One or more selected injection sources no longer exist.")
        mount.mkdir(parents=True, exist_ok=True)
        mounted = False
        try:
            progress("Mounting the WIM image read/write.")
            self._runner.run([
                "dism.exe", "/Mount-Image", f"/ImageFile:{image_path}", f"/Index:{image_index}",
                f"/MountDir:{mount}",
            ])
            mounted = True
            target = mount / destination_relative
            target.mkdir(parents=True, exist_ok=True)
            for source in sources:
                progress(f"Copying {source.name} into the mounted image.")
                self._copy_source(source, target)
            progress("Committing and unmounting the image.")
            self._runner.run(["dism.exe", "/Unmount-Image", f"/MountDir:{mount}", "/Commit"])
            mounted = False
            progress("Image servicing completed successfully.")
        finally:
            if mounted:
                self._logger.warning("Image remains mounted at %s after a failed servicing operation.", mount)

    def discard_mounted_image(self, mount_directory: str) -> None:
        self._runner.run(["dism.exe", "/Unmount-Image", f"/MountDir:{mount_directory}", "/Discard"])

    def format_volume(self, drive_letter: str, file_system: str, label: str) -> None:
        drive = self._validate_manageable_volume(drive_letter)
        filesystem = file_system.strip().upper()
        if filesystem not in {"NTFS", "EXFAT", "FAT32"}:
            raise ValueError("Select NTFS, exFAT, or FAT32.")
        volume_label = validate_volume_label(label)
        label_argument = f' label="{volume_label}"' if volume_label else ""
        self._run_diskpart(f"select volume={drive[0]}\r\nformat quick fs={filesystem}{label_argument}\r\n")

    def change_volume_letter(self, current_letter: str, new_letter: str) -> None:
        current = self._validate_manageable_volume(current_letter)
        new = self._validate_manageable_volume(new_letter)
        if current == new:
            raise ValueError("Choose a different drive letter.")
        if self._is_drive_letter_in_use(new):
            raise ValueError(f"Drive letter {new} is already in use.")
        self._run_diskpart(f"select volume={current[0]}\r\nremove letter={current[0]}\r\nassign letter={new[0]}\r\n")

    def change_volume_label(self, drive_letter: str, label: str) -> None:
        drive = self._validate_manageable_volume(drive_letter)
        volume_label = validate_volume_label(label)
        if not volume_label:
            raise ValueError("Enter a volume label.")
        self._run_diskpart(f"select volume={drive[0]}\r\nlabel=\"{volume_label}\"\r\n")

    def create_primary_partition(
        self, disk_number: int, size_mib: int | None, file_system: str, label: str, drive_letter: str | None
    ) -> None:
        self._validate_disk_number(disk_number)
        if size_mib is not None and size_mib < 1:
            raise ValueError("Partition size must be at least 1 MiB, or leave it blank to use all unallocated space.")
        filesystem = file_system.strip().upper()
        if filesystem not in {"NTFS", "EXFAT", "FAT32"}:
            raise ValueError("Select NTFS, exFAT, or FAT32.")
        volume_label = validate_volume_label(label)
        if drive_letter:
            drive = self._validate_manageable_volume(drive_letter)
            if self._is_drive_letter_in_use(drive):
                raise ValueError(f"Drive letter {drive} is already in use.")
            assign_command = f"assign letter={drive[0]}\r\n"
        else:
            assign_command = ""
        size_command = f" size={size_mib}" if size_mib is not None else ""
        label_command = f' label="{volume_label}"' if volume_label else ""
        self._run_diskpart(
            f"select disk {disk_number}\r\ncreate partition primary{size_command}\r\n"
            f"format quick fs={filesystem}{label_command}\r\n{assign_command}"
        )

    def delete_partition(self, disk_number: int, partition_number: int) -> None:
        self._validate_disk_number(disk_number)
        if partition_number < 1:
            raise ValueError("Select a valid partition number.")
        self._run_diskpart(f"select disk {disk_number}\r\nselect partition {partition_number}\r\ndelete partition override\r\n")

    def resize_volume(self, drive_letter: str, *, extend: bool, size_mib: int | None = None) -> None:
        drive = self._validate_manageable_volume(drive_letter)
        if size_mib is not None and size_mib < 1:
            raise ValueError("Size must be at least 1 MiB, or leave it blank only when extending to all contiguous free space.")
        if not extend and size_mib is None:
            raise ValueError("Enter how many MiB to shrink the volume.")
        command = "extend" if extend else "shrink"
        size_argument = f" desired={size_mib}" if not extend and size_mib is not None else (f" size={size_mib}" if size_mib is not None else "")
        self._run_diskpart(f"select volume={drive[0]}\r\n{command}{size_argument}\r\n")

    def clean_disk(self, disk_number: int) -> None:
        self._validate_disk_number(disk_number)
        self._run_diskpart(f"select disk {disk_number}\r\nclean\r\n")

    def inject_drivers(
        self, image_path: str, image_index: int, mount_directory: str, driver_directory: str, progress: ProgressCallback
    ) -> None:
        mount = Path(mount_directory)
        drivers = Path(driver_directory)
        if not drivers.is_dir():
            raise ValueError("Select an existing folder containing extracted driver .inf files.")
        if not any(drivers.rglob("*.inf")):
            raise ValueError("The selected driver folder does not contain any .inf files.")
        if mount.exists() and any(mount.iterdir()):
            raise ValueError("The mount directory must be empty before mounting an image.")
        mount.mkdir(parents=True, exist_ok=True)
        mounted = False
        try:
            progress("Mounting the WIM image read/write for driver injection.")
            self._runner.run(["dism.exe", "/Mount-Image", f"/ImageFile:{image_path}", f"/Index:{image_index}", f"/MountDir:{mount}"])
            mounted = True
            progress(f"Adding drivers recursively from {drivers}.")
            self._run_dism_with_progress(["dism.exe", f"/Image:{mount}", "/Add-Driver", f"/Driver:{drivers}", "/Recurse"], progress)
            progress("Committing driver changes and unmounting the image.")
            self._runner.run(["dism.exe", "/Unmount-Image", f"/MountDir:{mount}", "/Commit"])
            mounted = False
            progress("Driver injection completed successfully.")
        finally:
            if mounted:
                self._logger.warning("Image remains mounted at %s after a failed driver injection operation.", mount)

    def load_session_drivers(self, driver_directory: str, progress: ProgressCallback) -> tuple[int, list[str]]:
        """Load extracted INF packages into the current WinPE session only."""
        drivers = Path(driver_directory)
        if not drivers.is_dir():
            raise ValueError("Select an existing folder containing extracted driver .inf files.")
        inf_files = sorted((path for path in drivers.rglob("*.inf") if path.is_file()), key=lambda path: str(path).casefold())
        if not inf_files:
            raise ValueError("The selected driver folder does not contain any .inf files.")

        progress(f"Loading {len(inf_files)} driver INF file(s) into the current WinPE session.")
        loaded = 0
        failures: list[str] = []
        for inf_file in inf_files:
            try:
                self._runner.run(["drvload.exe", str(inf_file)])
            except CommandExecutionError as error:
                failures.append(f"{inf_file}: {error}")
                self._logger.warning("Could not load session driver %s: %s", inf_file, error)
                progress(f"Could not load {inf_file.name}; continuing with remaining drivers.")
            else:
                loaded += 1
                progress(f"Loaded {inf_file.name} into the current WinPE session.")
        progress(f"Session driver loading finished: {loaded} loaded, {len(failures)} failed. Refresh disks or network adapters now.")
        return loaded, failures

    def _run_diskpart(self, script: str) -> None:
        self._run_diskpart_output(script)

    def _run_diskpart_output(self, script: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="ascii", newline="\r\n") as handle:
            handle.write(script)
            script_path = handle.name
        try:
            return self._runner.run(["diskpart.exe", "/s", script_path]).stdout
        finally:
            Path(script_path).unlink(missing_ok=True)

    @staticmethod
    def _validate_disk_number(disk_number: int) -> None:
        if disk_number < 0:
            raise ValueError("Select a valid physical disk.")

    @classmethod
    def _validate_manageable_volume(cls, drive_letter: str) -> str:
        drive = normalize_drive_letter(drive_letter)
        if drive == "X:":
            raise ValueError("The WinPE X: RAM disk cannot be managed.")
        if drive == "C:" and not cls._is_winpe_environment():
            raise ValueError("C: is protected when WinPE X: is not present. Start the application from WinPE to manage C:.")
        return drive

    @staticmethod
    def _is_winpe_environment() -> bool:
        """Use the WinPE RAM-disk drive letter as the conservative environment signal."""
        try:
            drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
        except AttributeError:
            return False
        if drive_mask == 0:
            return False
        return bool(drive_mask & (1 << (ord("X") - ord("A"))))

    @staticmethod
    def _is_drive_letter_in_use(drive_letter: str) -> bool:
        drive = normalize_drive_letter(drive_letter)
        try:
            drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
        except AttributeError as error:
            raise RuntimeError("Windows drive-letter APIs are unavailable; cannot safely change the drive letter.") from error
        if drive_mask == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(drive_mask & (1 << (ord(drive[0]) - ord("A"))))

    @staticmethod
    def _select_free_deployment_drive_letters(firmware: FirmwareType) -> tuple[str | None, str]:
        """Reserve currently unused letters without ever using WinPE's X: RAM disk."""
        try:
            drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
        except AttributeError as error:
            raise RuntimeError("Windows drive-letter APIs are unavailable; cannot safely prepare the deployment disk.") from error
        if drive_mask == 0:
            raise ctypes.WinError(ctypes.get_last_error())

        occupied = {chr(ord("A") + index) for index in range(26) if drive_mask & (1 << index)}
        preferred_letters = ("W", "S", "V", "T", "U", "R", "Q", "P", "O", "N", "M", "L", "K", "J", "I", "H", "G", "F", "E", "D", "C", "B", "A", "Y", "Z")
        required_count = 2 if firmware is FirmwareType.UEFI else 1
        available = [f"{letter}:" for letter in preferred_letters if letter not in occupied and letter != "X"]
        if len(available) < required_count:
            raise RuntimeError("Not enough free drive letters are available to safely prepare the deployment disk.")
        if firmware is FirmwareType.UEFI:
            return available[0], available[1]
        return None, available[0]

    @staticmethod
    def _partition_script(
        disk_number: int, firmware: FirmwareType, system_volume: str | None = "S:", windows_volume: str = "W:"
    ) -> str:
        common = [f"select disk {disk_number}", "clean"]
        if firmware is FirmwareType.UEFI:
            if system_volume is None:
                raise ValueError("UEFI deployment requires a temporary EFI partition drive letter.")
            lines = common + [
                "convert gpt", "create partition efi size=100", "format quick fs=fat32 label=System", f"assign letter={system_volume[0]}",
                "create partition msr size=16", "create partition primary", "format quick fs=ntfs label=Windows", f"assign letter={windows_volume[0]}",
            ]
        else:
            lines = common + [
                "convert mbr", "create partition primary", "format quick fs=ntfs label=Windows", f"assign letter={windows_volume[0]}", "active",
            ]
        return "\r\n".join(lines) + "\r\n"

    @staticmethod
    def _copy_source(source: Path, target: Path) -> None:
        destination = target / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)


class WinPEPowerService:
    """Restart or shut down the current Windows PE session."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def restart(self) -> None:
        self._runner.run(["wpeutil.exe", "Reboot"])

    def shutdown(self) -> None:
        self._runner.run(["wpeutil.exe", "Shutdown"])


class NetworkService:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def list_adapters(self) -> list[str]:
        output = self._runner.run(["netsh.exe", "interface", "show", "interface"]).stdout
        adapters: list[str] = []
        for line in output.splitlines():
            columns = line.split()
            if len(columns) >= 4 and columns[0].lower() in {"enabled", "disabled"}:
                adapters.append(" ".join(columns[3:]))
        return adapters

    def configure_ipv4(self, adapter: str, address: str, mask: str, gateway: str, dns_servers: str) -> None:
        if not all((adapter.strip(), address.strip(), mask.strip())):
            raise ValueError("Adapter, IPv4 address, and subnet mask are required.")
        self._runner.run([
            "netsh.exe", "interface", "ipv4", "set", "address", f"name={adapter}", "source=static",
            f"address={address}", f"mask={mask}", f"gateway={gateway or 'none'}",
        ])
        for index, server in enumerate(filter(None, (part.strip() for part in dns_servers.split(","))), start=1):
            self._runner.run([
                "netsh.exe", "interface", "ipv4", "add" if index > 1 else "set", "dnsservers",
                f"name={adapter}", "source=static", f"address={server}", f"index={index}",
            ])

    def enable_dhcp(self, adapter: str) -> None:
        if not adapter.strip():
            raise ValueError("Select a network adapter before enabling DHCP.")
        self._runner.run(["netsh.exe", "interface", "ipv4", "set", "address", f"name={adapter}", "source=dhcp"])
        self._runner.run(["netsh.exe", "interface", "ipv4", "set", "dnsservers", f"name={adapter}", "source=dhcp"])

    def connect_share(self, drive_letter: str, unc_path: str, username: str, password: str) -> None:
        drive = normalize_drive_letter(drive_letter)
        if not unc_path.strip().startswith("\\\\"):
            raise ValueError("The network path must start with \\ (for example \\server\\share).")
        command = ["net.exe", "use", drive, unc_path.strip(), password, f"/user:{username}", "/persistent:no"]
        self._runner.run(command, secrets=(password,))

    def disconnect_share(self, drive_letter: str) -> None:
        self._runner.run(["net.exe", "use", normalize_drive_letter(drive_letter), "/delete", "/y"])