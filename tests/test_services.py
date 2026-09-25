import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from winpe_deploy.command_runner import CommandExecutionError, CommandResult, CommandRunner
from winpe_deploy.models import CaptureCompression, DiskInfo, FirmwareType, VolumeInfo
from winpe_deploy.services import DeploymentService, NetworkService, WinPEPowerService
from winpe_deploy.startup_config import AutoDeployStartupConfig, load_startup_config, select_auto_deploy_target


class FakeRunner:
    def __init__(self, outputs=None) -> None:
        self.commands = []
        self.outputs = list(outputs or [])

    def run(self, command, *, secrets=(), environment=None):
        self.commands.append((tuple(command), tuple(secrets), dict(environment or {})))
        stdout = self.outputs.pop(0) if self.outputs else ""
        return CommandResult(tuple(command), stdout, "", 0)

    def run_streaming(self, command, *, output_callback, secrets=(), environment=None):
        result = self.run(command, secrets=secrets, environment=environment)
        if result.stdout:
            output_callback(result.stdout)
        return result


class DeploymentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = FakeRunner()
        self.service = DeploymentService(self.runner, logging.getLogger("test"))

    def test_uefi_partition_script_has_efi_and_windows_volumes(self) -> None:
        script = self.service._partition_script(3, FirmwareType.UEFI)
        self.assertIn("select disk 3", script)
        self.assertIn("convert gpt", script)
        self.assertIn("assign letter=S", script)
        self.assertIn("assign letter=W", script)

    def test_bios_partition_script_marks_windows_partition_active(self) -> None:
        script = self.service._partition_script(1, FirmwareType.BIOS)
        self.assertIn("convert mbr", script)
        self.assertIn("active", script)

    def test_deploy_runs_apply_and_uefi_bcdboot(self) -> None:
        self.service._run_diskpart = lambda script: self.runner.commands.append((("diskpart-script", script), ()))
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch("winpe_deploy.services.tempfile.mkdtemp", return_value=str(Path(temporary) / "scratch")), mock.patch.object(self.service, "_select_free_deployment_drive_letters", return_value=("Y:", "Z:")), mock.patch.object(self.service, "_prefer_deployed_uefi_disk") as prefer:
                progress_messages = []
                self.service.deploy_image("Z:\\base.wim", 2, 4, FirmwareType.UEFI, progress_messages.append)
        prefer.assert_called_once_with("Y:")
        flat_commands = [entry[0] for entry in self.runner.commands]
        self.assertTrue(any(command[0] == "dism.exe" and "/Apply-Image" in command for command in flat_commands))
        apply_command = next(command for command in flat_commands if command[0] == "dism.exe" and "/Apply-Image" in command)
        self.assertFalse(any(part.startswith("/ScratchDir:") for part in apply_command))
        self.assertIn(("bcdboot.exe", "Z:\\Windows", "/s", "Y:", "/f", "UEFI", "/c"), flat_commands)
        apply_entry = next(entry for entry in self.runner.commands if entry[0] == apply_command)
        self.assertEqual(str(Path(temporary) / "scratch"), apply_entry[2]["TEMP"])
        self.assertEqual(str(Path(temporary) / "scratch"), apply_entry[2]["TMP"])
        self.assertEqual(
            "100% Deployment completed successfully using UEFI (GPT). "
            "Boot this disk using UEFI firmware with a GPT disk. For Hyper-V, use Generation 2.",
            progress_messages[-1],
        )

    def test_uefi_priority_selects_target_system_store_and_verifies_order(self) -> None:
        entries = ("Firmware Boot Manager\nidentifier {fwbootmgr}\ndisplayorder {bootmgr}\n"
                   "                 {12345678-1234-1234-1234-123456789abc}\n"
                   "Windows Boot Manager\nidentifier {bootmgr}\ndevice partition=Y:\n"
                   "path \\EFI\\Microsoft\\Boot\\bootmgfw.efi\n")
        self.runner.outputs = ["identifier {bootmgr}\n", "", "", "", entries]
        with mock.patch("winpe_deploy.services.Path.is_file", return_value=True):
            self.service._prefer_deployed_uefi_disk("Y:")
        self.assertEqual([
            ("bcdedit.exe", "/enum", "firmware"),
            ("bcdedit.exe", "/set", "{bootmgr}", "device", "partition=Y:"),
            ("bcdedit.exe", "/set", "{bootmgr}", "path", r"\EFI\Microsoft\Boot\bootmgfw.efi"),
            ("bcdedit.exe", "/set", "{fwbootmgr}", "displayorder", "{bootmgr}", "/addfirst"),
            ("bcdedit.exe", "/enum", "firmware"),
        ], [command[0] for command in self.runner.commands])

    def test_uefi_priority_refuses_missing_boot_file_without_modifying_firmware(self) -> None:
        with mock.patch("winpe_deploy.services.Path.is_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "boot file is missing"):
                self.service._prefer_deployed_uefi_disk("Y:")
        self.assertEqual([], self.runner.commands)

    def test_uefi_priority_refuses_missing_firmware_entry_without_changes(self) -> None:
        self.runner.outputs = ["identifier {fwbootmgr}\ndisplayorder {usb}\n"]
        with mock.patch("winpe_deploy.services.Path.is_file", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "automatic restart was stopped"):
                self.service._prefer_deployed_uefi_disk("Y:")
        self.assertEqual([("bcdedit.exe", "/enum", "firmware")], [entry[0] for entry in self.runner.commands])

    def test_uefi_priority_accepts_nt_device_path_for_target_esp(self) -> None:
        entries = ("identifier {fwbootmgr}\ndisplayorder {bootmgr}\n"
                   "identifier {bootmgr}\ndevice partition=\\Device\\HarddiskVolume4\n"
                   "path \\EFI\\Microsoft\\Boot\\bootmgfw.efi\n")
        self.runner.outputs = ["identifier {bootmgr}\n", "", "", "", entries]
        with mock.patch("winpe_deploy.services.Path.is_file", return_value=True), mock.patch.object(
            self.service, "_firmware_device_matches_volume", return_value=True
        ) as matches:
            self.service._prefer_deployed_uefi_disk("Y:")
        matches.assert_called_once_with(r"\Device\HarddiskVolume4", "Y:")

    def test_uefi_priority_rejects_nt_device_path_of_different_disk(self) -> None:
        entries = ("identifier {fwbootmgr}\ndisplayorder {bootmgr}\n"
                   "identifier {bootmgr}\ndevice partition=\\Device\\HarddiskVolume2\n"
                   "path \\EFI\\Microsoft\\Boot\\bootmgfw.efi\n")
        self.runner.outputs = ["identifier {bootmgr}\n", "", "", "", entries]
        with mock.patch("winpe_deploy.services.Path.is_file", return_value=True), mock.patch.object(
            self.service, "_firmware_device_matches_volume", return_value=False
        ):
            with self.assertRaisesRegex(RuntimeError, "automatic restart was stopped"):
                self.service._prefer_deployed_uefi_disk("Y:")

    def test_uefi_priority_compares_nt_device_mapping_without_guessing(self) -> None:
        kernel32 = mock.Mock()
        def query_device(letter, buffer, length):
            self.assertEqual("Y:", letter)
            self.assertGreater(length, 0)
            buffer.value = r"\Device\HarddiskVolume4"
            return len(buffer.value)
        kernel32.QueryDosDeviceW.side_effect = query_device
        with mock.patch("winpe_deploy.services.ctypes.WinDLL", return_value=kernel32):
            matches = self.service._firmware_device_matches_volume
            self.assertTrue(matches(r"\Device\HarddiskVolume4", "Y:"))
            self.assertFalse(matches(r"\Device\HarddiskVolume2", "Y:"))
            self.assertFalse(matches("Z:", "Y:"))

    def test_uefi_priority_refuses_unverified_order_and_tool_failure(self) -> None:
        for output in ("", "identifier {fwbootmgr}\ndisplayorder {other}\n"
                       "identifier {bootmgr}\ndevice partition=Y:\npath \\EFI\\Microsoft\\Boot\\bootmgfw.efi\n"):
            with self.subTest(output=output):
                self.runner = FakeRunner(["identifier {bootmgr}\n", "", "", "", output])
                self.service = DeploymentService(self.runner, logging.getLogger("test"))
                with mock.patch("winpe_deploy.services.Path.is_file", return_value=True):
                    with self.assertRaisesRegex(RuntimeError, "automatic restart was stopped"):
                        self.service._prefer_deployed_uefi_disk("Y:")
        self.runner.run = mock.Mock(side_effect=CommandExecutionError("NVRAM unavailable"))
        with mock.patch("winpe_deploy.services.Path.is_file", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "automatic restart was stopped"):
                self.service._prefer_deployed_uefi_disk("Y:")

    def test_select_free_deployment_letters_skips_occupied_and_x_ram_disk(self) -> None:
        # S:, W:, and X: are occupied. The dynamic UEFI layout must use V: and T: instead.
        occupied_mask = (1 << (ord("S") - ord("A"))) | (1 << (ord("W") - ord("A"))) | (1 << (ord("X") - ord("A")))
        kernel32 = mock.Mock()
        kernel32.GetLogicalDrives.return_value = occupied_mask
        with mock.patch("winpe_deploy.services.ctypes.windll.kernel32", kernel32):
            self.assertEqual(("V:", "T:"), self.service._select_free_deployment_drive_letters(FirmwareType.UEFI))

    def test_partition_script_uses_selected_temporary_drive_letters(self) -> None:
        script = self.service._partition_script(3, FirmwareType.UEFI, "Y:", "Z:")
        self.assertIn("assign letter=Y", script)
        self.assertIn("assign letter=Z", script)

    def test_deploy_progress_maps_dism_range_into_workflow(self) -> None:
        self.assertEqual("10.0% Applying image", self.service._map_deploy_dism_progress("0% Applying image"))
        self.assertEqual("52.5% Applying image", self.service._map_deploy_dism_progress("50% Applying image"))
        self.assertEqual("95.0% Applying image", self.service._map_deploy_dism_progress("100% Applying image"))

    def test_inject_files_copies_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "build.msi"
            source.write_text("package", encoding="utf-8")
            mount = root / "mount"
            self.service.inject_files("Z:\\base.wim", 1, str(mount), [str(source)], "Install\\Packages", lambda _: None)
            self.assertEqual("package", (mount / "Install" / "Packages" / "build.msi").read_text(encoding="utf-8"))
            commands = [item[0] for item in self.runner.commands]
            self.assertTrue(any("/Mount-Image" in command for command in commands))
            self.assertTrue(any("/Commit" in command for command in commands))

    def test_list_disks_prefers_diskpart_in_winpe(self) -> None:
        service = DeploymentService(FakeRunner(["  Disk 0    Online          476 GB      32 GB\n"]), logging.getLogger("test"))
        result = service.list_disks()
        self.assertEqual(1, len(result))
        self.assertEqual(0, result[0].number)
        self.assertEqual("Physical disk (DiskPart)", result[0].model)
        self.assertEqual(476 * 1024**3, result[0].size_bytes)
        self.assertEqual(32 * 1024**3, result[0].unallocated_bytes)

    def test_list_disks_uses_powershell_only_when_diskpart_returns_no_disks(self) -> None:
        disks = [{"Number": 0, "FriendlyName": "NVMe", "BusType": "NVMe", "SerialNumber": "SN-1", "Size": 512110190592, "VolumeSize": 500000000000, "VolumeFree": 200000000000, "Unallocated": 12110190592}]
        service = DeploymentService(FakeRunner(["", json.dumps(disks)]), logging.getLogger("test"))
        result = service.list_disks()
        self.assertEqual(1, len(result))
        self.assertEqual("NVMe", result[0].model)
        self.assertEqual("NVMe", result[0].bus_type)
        self.assertEqual("SN-1", result[0].serial_number)

    def test_disk_number_for_drive_uses_partition_metadata(self) -> None:
        service = DeploymentService(FakeRunner(["4\r\n"]), logging.getLogger("test"))
        self.assertEqual(4, service.disk_number_for_drive("E:"))

    def test_disk_number_for_drive_falls_back_to_diskpart_when_powershell_cannot_resolve_winpe_media(self) -> None:
        service = DeploymentService(
            FakeRunner(["not-a-disk-number\r\n", "  * Disk 2    Online          58 GB      0 B\r\n"]),
            logging.getLogger("test"),
        )
        self.assertEqual(2, service.disk_number_for_drive("D:"))
        commands = [entry[0] for entry in service._runner.commands]
        self.assertEqual("powershell.exe", commands[0][0])
        self.assertEqual("diskpart.exe", commands[1][0])

    def test_inject_drivers_mounts_adds_recursively_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            drivers = root / "drivers" / "storage"
            drivers.mkdir(parents=True)
            (drivers / "controller.inf").write_text("[Version]", encoding="utf-8")
            mount = root / "mount"
            self.service.inject_drivers("Z:\\base.wim", 1, str(mount), str(root / "drivers"), lambda _: None)
        commands = [entry[0] for entry in self.runner.commands]
        self.assertTrue(any("/Mount-Image" in command for command in commands))
        add_driver = next(command for command in commands if "/Add-Driver" in command)
        self.assertIn("/Recurse", add_driver)
        self.assertIn(f"/Driver:{root / 'drivers'}", add_driver)
        self.assertTrue(any("/Commit" in command for command in commands))

    def test_load_session_drivers_uses_drvload_for_each_inf_and_continues_after_failure(self) -> None:
        class FailingDriverRunner(FakeRunner):
            def run(self, command, *, secrets=(), environment=None):
                self.commands.append((tuple(command), tuple(secrets), dict(environment or {})))
                if str(command[1]).endswith("bad.inf"):
                    from winpe_deploy.command_runner import CommandExecutionError
                    raise CommandExecutionError("bad driver")
                return CommandResult(tuple(command), "", "", 0)

        with tempfile.TemporaryDirectory() as temporary:
            drivers = Path(temporary) / "drivers"
            drivers.mkdir()
            (drivers / "good.inf").write_text("[Version]", encoding="utf-8")
            (drivers / "bad.inf").write_text("[Version]", encoding="utf-8")
            runner = FailingDriverRunner()
            service = DeploymentService(runner, logging.getLogger("test"))
            loaded, failures = service.load_session_drivers(str(drivers), lambda _: None)

        self.assertEqual(1, loaded)
        self.assertEqual(1, len(failures))
        self.assertEqual(["drvload.exe", "drvload.exe"], [entry[0][0] for entry in runner.commands])

    def test_list_disks_returns_diskpart_data_without_powershell(self) -> None:
        class DiskPartRunner(FakeRunner):
            def run(self, command, *, secrets=()):
                return CommandResult(tuple(command), "  Disk 0    Online          476 GB      0 B\n", "", 0)

        service = DeploymentService(DiskPartRunner(), logging.getLogger("test"))
        result = service.list_disks()
        self.assertEqual(1, len(result))
        self.assertEqual(0, result[0].number)
        self.assertEqual(476 * 1024**3, result[0].size_bytes)
    def test_capture_uses_selected_compression(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reference.wim"
            self.service._get_windows_volume_info = lambda _: VolumeInfo("C:", "Images", "NTFS", 100 * 1024**3, 80 * 1024**3)
            scratch_directory = Path(temporary) / "scratch"
            with mock.patch("winpe_deploy.services.tempfile.mkdtemp", return_value=str(scratch_directory)):
                self.service.capture_image("D:", str(output), "Reference", "Test", CaptureCompression.MAXIMUM, lambda _: None)
        command = self.runner.commands[0][0]
        self.assertIn("/Compress:max", command)
        self.assertIn(f"/ScratchDir:{scratch_directory}", command)
        self.assertEqual(str(scratch_directory), self.runner.commands[0][2]["TEMP"])
        self.assertEqual(str(scratch_directory), self.runner.commands[0][2]["TMP"])

    def test_capture_rejects_fat32_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "reference.wim"
            self.service._get_windows_volume_info = lambda _: VolumeInfo("C:", "USB", "FAT32", 64 * 1024**3, 50 * 1024**3)
            with self.assertRaisesRegex(ValueError, "4 GiB FAT32"):
                self.service.capture_image("D:", str(output), "Reference", "Test", CaptureCompression.MAXIMUM, lambda _: None)

    def test_format_volume_uses_diskpart_with_selected_filesystem_and_label(self) -> None:
        self.service._run_diskpart = lambda script: self.runner.commands.append((("diskpart-script", script), ()))
        self.service.format_volume("E:", "exFAT", "Archive")
        script = self.runner.commands[0][0][1]
        self.assertIn("select volume=E", script)
        self.assertIn('format quick fs=EXFAT label="Archive"', script)

    def test_change_volume_letter_uses_diskpart(self) -> None:
        self.service._run_diskpart = lambda script: self.runner.commands.append((("diskpart-script", script), ()))
        with mock.patch("winpe_deploy.services.ctypes.windll.kernel32.GetLogicalDrives", return_value=1 << (ord("E") - ord("A"))):
            self.service.change_volume_letter("E:", "F:")
        script = self.runner.commands[0][0][1]
        self.assertIn("select volume=E", script)
        self.assertIn("remove letter=E", script)
        self.assertIn("assign letter=F", script)

    def test_change_volume_letter_rejects_an_occupied_letter(self) -> None:
        with mock.patch("winpe_deploy.services.ctypes.windll.kernel32.GetLogicalDrives", return_value=1 << (ord("F") - ord("A"))):
            with self.assertRaisesRegex(ValueError, "F: is already in use"):
                self.service.change_volume_letter("E:", "F:")

    def test_change_volume_label_uses_diskpart(self) -> None:
        self.service._run_diskpart = lambda script: self.runner.commands.append((("diskpart-script", script), ()))
        self.service.change_volume_label("E:", "Archive")
        self.assertIn('label="Archive"', self.runner.commands[0][0][1])

    def test_create_primary_partition_formats_and_assigns_letter(self) -> None:
        self.service._run_diskpart = lambda script: self.runner.commands.append((("diskpart-script", script), ()))
        self.service._is_drive_letter_in_use = lambda _: False
        self.service.create_primary_partition(2, 10240, "NTFS", "Data", "F:")
        script = self.runner.commands[0][0][1]
        self.assertIn("select disk 2", script)
        self.assertIn("create partition primary size=10240", script)
        self.assertIn('format quick fs=NTFS label="Data"', script)
        self.assertIn("assign letter=F", script)

    def test_delete_partition_uses_selected_disk_and_override(self) -> None:
        self.service._run_diskpart = lambda script: self.runner.commands.append((("diskpart-script", script), ()))
        self.service.delete_partition(3, 2)
        script = self.runner.commands[0][0][1]
        self.assertIn("select disk 3", script)
        self.assertIn("select partition 2", script)
        self.assertIn("delete partition override", script)

    def test_resize_volume_uses_diskpart_extend_and_shrink(self) -> None:
        self.service._run_diskpart = lambda script: self.runner.commands.append((("diskpart-script", script), ()))
        self.service.resize_volume("E:", extend=True, size_mib=2048)
        self.service.resize_volume("F:", extend=False, size_mib=512)
        self.assertIn("extend size=2048", self.runner.commands[0][0][1])
        self.assertIn("shrink desired=512", self.runner.commands[1][0][1])

    def test_clean_disk_uses_diskpart_clean(self) -> None:
        self.service._run_diskpart = lambda script: self.runner.commands.append((("diskpart-script", script), ()))
        self.service.clean_disk(4)
        self.assertEqual("select disk 4\r\nclean\r\n", self.runner.commands[0][0][1])

    def test_list_partitions_parses_localized_type_columns(self) -> None:
        self.service._run_diskpart_output = lambda _: "  Partition ###  Type              Size     Offset\n  -------------  ----------------  -------  -------\n  Partition 1    Primary            100 GB  1024 KB\n"
        partitions = self.service.list_partitions(0)
        self.assertEqual(1, len(partitions))
        self.assertEqual(1, partitions[0].number)
        self.assertEqual(100 * 1024**3, partitions[0].size_bytes)

    def test_disk_tools_reject_winpe_ram_disk(self) -> None:
        with self.assertRaisesRegex(ValueError, "X: RAM disk"):
            self.service.change_volume_label("X:", "Blocked")

    def test_disk_tools_allow_c_drive_when_winpe_x_drive_exists(self) -> None:
        self.service._run_diskpart = lambda script: self.runner.commands.append((("diskpart-script", script), ()))
        with mock.patch.object(DeploymentService, "_is_winpe_environment", return_value=True):
            self.service.change_volume_label("C:", "OfflineWindows")
        self.assertIn("select volume=C", self.runner.commands[0][0][1])

    def test_disk_tools_reject_c_drive_when_winpe_x_drive_is_absent(self) -> None:
        with mock.patch.object(DeploymentService, "_is_winpe_environment", return_value=False):
            with self.assertRaisesRegex(ValueError, "C: is protected"):
                self.service.format_volume("C:", "NTFS", "Blocked")


class NetworkServiceTests(unittest.TestCase):
    def test_startup_config_loads_optional_ethernet_then_share_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "startup-config.ini"
            path.write_text(
                "[ethernet]\n"
                "adapter = Ethernet\n"
                "address = 10.10.0.20\n"
                "mask = 255.255.255.0\n"
                "\n"
                "[share]\n"
                "drive_letter = z\n"
                "unc_path = \\\\fileserver\\images\n"
                "username = DOMAIN\\deploy\n"
                "password = plain-text-password\n",
                encoding="utf-8",
            )
            configuration = load_startup_config(path)
        assert configuration.ethernet is not None
        assert configuration.share is not None
        self.assertEqual("Ethernet", configuration.ethernet.adapter)
        self.assertEqual("", configuration.ethernet.mac)
        self.assertEqual("", configuration.ethernet.gateway)
        self.assertEqual("", configuration.ethernet.dns_servers)
        self.assertEqual("Z:", configuration.share.drive_letter)
        self.assertEqual("\\\\fileserver\\images", configuration.share.unc_path)

    def test_startup_config_accepts_mac_without_adapter_and_normalizes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "startup-config.ini"
            path.write_text("[ethernet]\nmac = aa:bb:cc:01:02:ff\naddress = 10.0.0.2\nmask = 255.255.255.0\n", encoding="utf-8")
            configuration = load_startup_config(path)
            assert configuration.ethernet is not None
            self.assertEqual("", configuration.ethernet.adapter)
            self.assertEqual("AA-BB-CC-01-02-FF", configuration.ethernet.mac)
            path.write_text("[ethernet]\nmac = AA-BB-CC-01-02-FF\naddress = 10.0.0.2\nmask = 255.255.255.0\n", encoding="utf-8")
            self.assertEqual("AA-BB-CC-01-02-FF", load_startup_config(path).ethernet.mac)

    def test_startup_config_rejects_missing_or_invalid_mac_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "startup-config.ini"
            for selection in ("", "mac = aa:bb:cc:dd:ee\n", "mac = aa:bb-cc:dd:ee:ff\n"):
                with self.subTest(selection=selection):
                    path.write_text(f"[ethernet]\n{selection}address = 10.0.0.2\nmask = 255.255.255.0\n", encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_startup_config(path)

    def test_startup_config_rejects_unknown_options_without_exposing_password(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "startup-config.ini"
            path.write_text("[share]\ndrive_letter = Z:\nunc_path = \\\\server\\share\nusername = tech\npassword = secret\nunsafe = yes\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"Unsupported \[share\] option") as error:
                load_startup_config(path)
        self.assertNotIn("secret", str(error.exception))

    def test_startup_config_loads_enabled_auto_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "startup-config.ini"
            path.write_text(
                "[auto_deploy]\n"
                "enabled = true\n"
                "wim_path = Z:\\Images\\base.wim\n"
                "image_index = 2\n"
                "firmware = UEFI (GPT)\n"
                "minimum_disk_size_gib = 128\n"
                "maximum_disk_size_gib = 1024\n"
                "expected_disk_serial = SSD-001\n",
                encoding="utf-8",
            )
            configuration = load_startup_config(path)
        assert configuration.auto_deploy is not None
        self.assertEqual("Z:\\Images\\base.wim", configuration.auto_deploy.wim_path)
        self.assertEqual(128, configuration.auto_deploy.minimum_disk_size_gib)
        self.assertEqual(1024, configuration.auto_deploy.maximum_disk_size_gib)

    def test_auto_deploy_maximum_size_is_optional_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "startup-config.ini"
            base = (
                "[auto_deploy]\nenabled = true\nwim_path = Z:\\Images\\base.wim\n"
                "image_index = 1\nfirmware = UEFI (GPT)\nminimum_disk_size_gib = 128\n"
            )
            for optional in ("", "maximum_disk_size_gib =\n"):
                path.write_text(base + optional, encoding="utf-8")
                configuration = load_startup_config(path)
                assert configuration.auto_deploy is not None
                self.assertIsNone(configuration.auto_deploy.maximum_disk_size_gib)
            for invalid in ("abc", "1.5", "0", "-1", "127"):
                with self.subTest(invalid=invalid):
                    path.write_text(base + f"maximum_disk_size_gib = {invalid}\n", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "maximum_disk_size_gib"):
                        load_startup_config(path)
            path.write_text(base + "maximum_disk_size_gib = 128\n", encoding="utf-8")
            configuration = load_startup_config(path)
            assert configuration.auto_deploy is not None
            self.assertEqual(128, configuration.auto_deploy.maximum_disk_size_gib)

    def test_auto_deploy_maximum_size_excludes_larger_disk_without_serial(self) -> None:
        configuration = AutoDeployStartupConfig("Z:\\base.wim", 1, FirmwareType.UEFI, 100, "", 1100)
        disks = [
            DiskInfo(1, "1 TB SSD", 1000 * 1000**3),
            DiskInfo(2, "4 TB SSD", 4000 * 1000**3),
        ]
        self.assertEqual(1, select_auto_deploy_target(disks, set(), configuration).number)
        with self.assertRaisesRegex(ValueError, "exactly one eligible"):
            select_auto_deploy_target(disks[1:], set(), configuration)
        with self.assertRaisesRegex(ValueError, "exactly one eligible"):
            select_auto_deploy_target(disks, set(), AutoDeployStartupConfig("Z:\\base.wim", 1, FirmwareType.UEFI, 100, "", 900))

    def test_auto_deploy_maximum_size_boundary_and_other_guards(self) -> None:
        configuration = AutoDeployStartupConfig("Z:\\base.wim", 1, FirmwareType.UEFI, 100, "SSD-001", 512)
        disk = DiskInfo(1, "SSD", 512 * 1024**3, "NVMe", "SSD-001")
        self.assertEqual(1, select_auto_deploy_target([disk], set(), configuration).number)
        for candidate, protected in (
            (DiskInfo(1, "SSD", 512 * 1024**3 + 1, "NVMe", "SSD-001"), set()),
            (disk, {1}),
            (DiskInfo(1, "SSD", 512 * 1024**3, "NVMe", "OTHER"), set()),
        ):
            with self.subTest(candidate=candidate, protected=protected):
                with self.assertRaisesRegex(ValueError, "exactly one eligible"):
                    select_auto_deploy_target([candidate], protected, configuration)

    def test_startup_config_rejects_obsolete_auto_deploy_mode_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "startup-config.ini"
            path.write_text(
                "[auto_deploy]\n"
                "enabled = true\n"
                "mode = automatic\n"
                "wim_path = Z:\\Images\\base.wim\n"
                "image_index = 1\n"
                "firmware = UEFI (GPT)\n"
                "minimum_disk_size_gib = 128\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "mode"):
                load_startup_config(path)
            path.write_text(
                "[auto_deploy]\n"
                "enabled = true\n"
                "confirmation = ERASE-ONLY-ELIGIBLE-DISK\n"
                "wim_path = Z:\\Images\\base.wim\n"
                "image_index = 1\n"
                "firmware = UEFI (GPT)\n"
                "minimum_disk_size_gib = 128\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "confirmation"):
                load_startup_config(path)

    def test_auto_deploy_selects_only_unprotected_matching_disk(self) -> None:
        configuration = AutoDeployStartupConfig("Z:\\base.wim", 1, FirmwareType.UEFI, 100, "SSD-001")
        disks = [
            DiskInfo(0, "WinPE USB", 64 * 1024**3, "USB", "USB-001"),
            DiskInfo(1, "Target NVMe", 512 * 1024**3, "NVMe", "SSD-001"),
        ]
        self.assertEqual(1, select_auto_deploy_target(disks, {0}, configuration).number)

    def test_auto_deploy_accepts_only_detected_disk_with_exact_serial(self) -> None:
        configuration = AutoDeployStartupConfig("Z:\\base.wim", 1, FirmwareType.UEFI, 100, "SSD-001")
        disks = [DiskInfo(1, "Target NVMe", 512 * 1024**3, "NVMe", "SSD-001")]
        self.assertEqual(1, select_auto_deploy_target(disks, set(), configuration).number)

    def test_auto_deploy_selects_exact_serial_among_multiple_unprotected_disks(self) -> None:
        configuration = AutoDeployStartupConfig("Z:\\base.wim", 1, FirmwareType.UEFI, 100, "SSD-001")
        disks = [
            DiskInfo(0, "WinPE USB", 256 * 1024**3, "USB", "USB-001"),
            DiskInfo(1, "Other NVMe", 512 * 1024**3, "NVMe", "SSD-002"),
            DiskInfo(2, "Target NVMe", 512 * 1024**3, "NVMe", "SSD-001"),
        ]
        self.assertEqual(2, select_auto_deploy_target(disks, {0}, configuration).number)
        self.assertEqual(2, select_auto_deploy_target(disks[1:], set(), configuration).number)

    def test_auto_deploy_without_serial_selects_only_remaining_unprotected_disk(self) -> None:
        configuration = AutoDeployStartupConfig("Z:\\base.wim", 1, FirmwareType.UEFI, 100, "")
        disks = [
            DiskInfo(0, "WinPE USB", 256 * 1024**3, "USB", "USB-001"),
            DiskInfo(1, "Target NVMe", 512 * 1024**3, "NVMe", "SSD-001"),
        ]
        self.assertEqual(1, select_auto_deploy_target(disks, {0}, configuration).number)
        with self.assertRaisesRegex(ValueError, "exactly one eligible"):
            select_auto_deploy_target(disks, set(), configuration)

    def test_auto_deploy_single_disk_serial_does_not_override_other_guards(self) -> None:
        configuration = AutoDeployStartupConfig("Z:\\base.wim", 1, FirmwareType.UEFI, 100, "SSD-001")
        disk = DiskInfo(1, "Target NVMe", 512 * 1024**3, "NVMe", "SSD-001")
        for disks, protected in (
            ([disk], {1}),
            ([DiskInfo(1, "Target NVMe", 99 * 1024**3, "NVMe", "SSD-001")], set()),
            ([DiskInfo(1, "Target NVMe", 512 * 1024**3, "NVMe", "ssd-001")], set()),
            ([DiskInfo(1, "Target NVMe", 512 * 1024**3, "NVMe", "")], set()),
        ):
            with self.subTest(disks=disks, protected=protected):
                with self.assertRaisesRegex(ValueError, "exactly one eligible"):
                    select_auto_deploy_target(disks, protected, configuration)

    def test_auto_deploy_duplicate_serial_is_ambiguous(self) -> None:
        configuration = AutoDeployStartupConfig("Z:\\base.wim", 1, FirmwareType.UEFI, 100, "SSD-001")
        disks = [
            DiskInfo(1, "NVMe 1", 512 * 1024**3, "NVMe", "SSD-001"),
            DiskInfo(2, "NVMe 2", 512 * 1024**3, "NVMe", "SSD-001"),
        ]
        with self.assertRaisesRegex(ValueError, "exactly one eligible"):
            select_auto_deploy_target(disks, set(), configuration)

    def test_auto_deploy_refuses_multiple_candidates(self) -> None:
        configuration = AutoDeployStartupConfig("Z:\\base.wim", 1, FirmwareType.UEFI, 100, "")
        disks = [DiskInfo(1, "NVMe 1", 512 * 1024**3, "NVMe"), DiskInfo(2, "NVMe 2", 512 * 1024**3, "NVMe")]
        with self.assertRaisesRegex(ValueError, "exactly one eligible"):
            select_auto_deploy_target(disks, set(), configuration)

    def test_connect_share_redacts_password(self) -> None:
        runner = FakeRunner()
        service = NetworkService(runner)
        service.connect_share("Z:", "\\\\server\\share", "DOMAIN\\tech", "s3cret")
        command, secrets, environment = runner.commands[0]
        self.assertEqual("net.exe", command[0])
        self.assertIn("s3cret", command)
        self.assertEqual(("s3cret",), secrets)
        self.assertEqual({}, environment)

    def test_configure_ipv4_adds_each_dns_server(self) -> None:
        runner = FakeRunner()
        service = NetworkService(runner)
        service.configure_ipv4("Ethernet", "10.0.0.10", "255.255.255.0", "10.0.0.1", "1.1.1.1, 8.8.8.8")
        self.assertEqual(3, len(runner.commands))
        self.assertIn("set", runner.commands[1][0])
        self.assertIn("add", runner.commands[2][0])

    def test_enable_dhcp_restores_ipv4_and_dns_for_selected_adapter(self) -> None:
        runner = FakeRunner()
        NetworkService(runner).enable_dhcp("Ethernet 2")
        self.assertEqual([
            ("netsh.exe", "interface", "ipv4", "set", "address", "name=Ethernet 2", "source=dhcp"),
            ("netsh.exe", "interface", "ipv4", "set", "dnsservers", "name=Ethernet 2", "source=dhcp"),
        ], [entry[0] for entry in runner.commands])

    def test_enable_dhcp_requires_adapter_before_running_commands(self) -> None:
        runner = FakeRunner()
        with self.assertRaisesRegex(ValueError, "Select a network adapter"):
            NetworkService(runner).enable_dhcp("  ")
        self.assertEqual([], runner.commands)

    def test_enable_dhcp_does_not_change_dns_if_address_command_fails(self) -> None:
        runner = FakeRunner()
        runner.run = mock.Mock(side_effect=CommandExecutionError("address configuration failed"))
        with self.assertRaisesRegex(CommandExecutionError, "address configuration failed"):
            NetworkService(runner).enable_dhcp("Ethernet")
        runner.run.assert_called_once()


class WinPEPowerServiceTests(unittest.TestCase):
    def test_restart_runs_wpeutil_reboot(self) -> None:
        runner = FakeRunner()
        WinPEPowerService(runner).restart()
        self.assertEqual(("wpeutil.exe", "Reboot"), runner.commands[0][0])

    def test_shutdown_runs_wpeutil_shutdown(self) -> None:
        runner = FakeRunner()
        WinPEPowerService(runner).shutdown()
        self.assertEqual(("wpeutil.exe", "Shutdown"), runner.commands[0][0])


class CommandRunnerTests(unittest.TestCase):
    def test_windows_child_processes_use_no_console_window_flag(self) -> None:
        with mock.patch("winpe_deploy.command_runner.os.name", "nt"):
            self.assertEqual({"creationflags": 0x08000000}, CommandRunner._hidden_window_options())