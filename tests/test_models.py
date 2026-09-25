import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from winpe_deploy.models import CaptureCompression, DiskInfo, VolumeInfo, WimImageInfo, normalize_drive_letter, validate_image_index, validate_relative_image_destination, validate_volume_label, validate_wim_path
from winpe_deploy.gui import WinPEImageDeployerApp
from winpe_deploy.startup_preflight import _require_wim_image_index, _validate_existing_wim_path


class ModelValidationTests(unittest.TestCase):
    def test_normalize_drive_letter_accepts_short_and_colon_forms(self) -> None:
        self.assertEqual("D:", normalize_drive_letter("d"))
        self.assertEqual("E:", normalize_drive_letter(" e:\\ "))

    def test_normalize_drive_letter_rejects_paths(self) -> None:
        with self.assertRaises(ValueError):
            normalize_drive_letter("D:\\Windows")

    def test_wim_path_requires_wim_extension(self) -> None:
        self.assertEqual("Z:\\images\\base.wim", validate_wim_path("Z:\\images\\base.wim"))
        with self.assertRaises(ValueError):
            validate_wim_path("Z:\\images\\base.esd")

    def test_wim_path_normalizes_forward_slashes_for_dism(self) -> None:
        self.assertEqual("D:\\Images\\reference.wim", validate_wim_path(" D:/Images/reference.wim "))

    def test_image_index_must_be_positive(self) -> None:
        self.assertEqual(2, validate_image_index("2"))
        self.assertEqual(2, validate_image_index("2 — Windows 11 Pro"))
        for invalid in ("0", "-1", "one"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_image_index(invalid)

    def test_image_destination_is_relative(self) -> None:
        self.assertEqual("Install\\Packages", validate_relative_image_destination("C:\\Install\\Packages"))
        self.assertEqual(
            "ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\Startup",
            validate_relative_image_destination("ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\Startup"),
        )
        self.assertEqual("Users\\Public\\Desktop", validate_relative_image_destination("Users\\Public\\Desktop"))
        with self.assertRaises(ValueError):
            validate_relative_image_destination("..\\escape")

    def test_volume_label_rejects_invalid_characters(self) -> None:
        self.assertEqual("Deployment", validate_volume_label("Deployment"))
        with self.assertRaises(ValueError):
            validate_volume_label("Bad:Label")

    def test_capture_compression_maps_to_dism_values(self) -> None:
        self.assertEqual("max", CaptureCompression.MAXIMUM.dism_value)
        self.assertEqual("fast", CaptureCompression.FAST.dism_value)
        self.assertEqual("none", CaptureCompression.NONE.dism_value)

    def test_volume_display_includes_capacity_and_free_space(self) -> None:
        volume = VolumeInfo("E:", "Images", "NTFS", 100 * 1024**3, 70 * 1024**3)
        self.assertIn("E:", volume.display_name())
        self.assertIn("70.0 GiB free", volume.display_name())

    def test_disk_display_includes_physical_identity_and_usage(self) -> None:
        disk = DiskInfo(2, "Samsung SSD", 100 * 1024**3, "NVMe", "ABC123", 80 * 1024**3, 20 * 1024**3, 10 * 1024**3)
        display = disk.display_name()
        self.assertIn("Samsung SSD", display)
        self.assertIn("NVMe", display)
        self.assertIn("S/N: ABC123", display)
        self.assertIn("60.0 GiB used", display)
        self.assertIn("20.0 GiB free", display)
        self.assertIn("10.0 GiB", display)

    def test_directory_picker_lists_only_sorted_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Zulu").mkdir()
            (root / "alpha").mkdir()
            (root / "file.txt").write_text("not a folder", encoding="utf-8")
            directories = WinPEImageDeployerApp._list_subdirectories(root)
        self.assertEqual(["alpha", "Zulu"], [entry.name for entry in directories])

    def test_initial_window_size_uses_large_screen_aware_fallback(self) -> None:
        self.assertEqual((984, 688), WinPEImageDeployerApp._initial_window_size(1024, 768))
        self.assertEqual((1440, 960), WinPEImageDeployerApp._initial_window_size(1920, 1080))
        self.assertEqual((800, 560), WinPEImageDeployerApp._initial_window_size(640, 480))

    def test_text_editor_reads_utf8_bom_and_writes_windows_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "startup-config.ini"
            path.write_text("\ufeff[share]\nunc_path = \\\\server\\images\n", encoding="utf-8")
            self.assertEqual("[share]\nunc_path = \\\\server\\images\n", WinPEImageDeployerApp._read_text_file(path))
            WinPEImageDeployerApp._write_text_file(path, "[auto_deploy]\nenabled = false")
            self.assertEqual(b"[auto_deploy]\r\nenabled = false", path.read_bytes())

    def test_auto_deploy_requires_an_existing_wim_file_and_configured_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reference.wim"
            path.write_bytes(b"test")
            self.assertEqual(str(path), _validate_existing_wim_path(str(path)))
            with self.assertRaisesRegex(ValueError, "does not exist"):
                _validate_existing_wim_path(str(path.with_name("missing.wim")))
        images = [WimImageInfo(1, "Windows 11 Pro", ""), WimImageInfo(3, "Windows 11 Enterprise", "")]
        _require_wim_image_index(images, 3)
        with self.assertRaisesRegex(ValueError, "Available indexes: 1, 3"):
            _require_wim_image_index(images, 2)

    def test_disk_display_parser_accepts_only_physical_disk_rows(self) -> None:
        self.assertEqual(12, WinPEImageDeployerApp._disk_number_from_display("Disk 12: Target NVMe | NVMe | 512.0 GiB"))
        self.assertIsNone(WinPEImageDeployerApp._disk_number_from_display("No physical disks found"))

    def test_remaining_time_estimate_uses_observed_progress_rate(self) -> None:
        self.assertEqual(240, WinPEImageDeployerApp._estimate_remaining_seconds(10.0, 10.0, 40.0, 20.0))
        self.assertIsNone(WinPEImageDeployerApp._estimate_remaining_seconds(10.0, 10.0, 40.0, 10.0))
        self.assertEqual("~4 min 30 s", WinPEImageDeployerApp._format_remaining_time(270))

    def test_directory_roots_use_windows_drive_mask(self) -> None:
        with mock.patch("winpe_deploy.gui.ctypes.windll", create=True) as windll:
            windll.kernel32.GetLogicalDrives.return_value = (1 << 2) | (1 << 4)
            self.assertEqual([Path("C:\\"), Path("E:\\")], WinPEImageDeployerApp._directory_roots())

    def test_startup_config_button_opens_only_one_active_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            roots = [Path(temporary) / "first", Path(temporary) / "second"]
            configs = []
            for root in roots:
                content = root / "winpe-media-content"
                content.mkdir(parents=True)
                configs.append(content / "startup-config.ini")
            app = SimpleNamespace(
                _load_text_file=mock.Mock(),
                _startup_configuration_files=WinPEImageDeployerApp._startup_configuration_files,
            )
            with mock.patch.object(WinPEImageDeployerApp, "_directory_roots", return_value=roots), mock.patch("winpe_deploy.gui.messagebox") as messages:
                WinPEImageDeployerApp._open_startup_configuration_in_editor(app)
                messages.showinfo.assert_called_once()
                configs[0].write_text("[auto_deploy]\nenabled = false\n", encoding="utf-8")
                WinPEImageDeployerApp._open_startup_configuration_in_editor(app)
                app._load_text_file.assert_called_once_with(configs[0])
                configs[1].write_text("[auto_deploy]\nenabled = false\n", encoding="utf-8")
                WinPEImageDeployerApp._open_startup_configuration_in_editor(app)
                messages.showerror.assert_called_once()
                app._load_text_file.assert_called_once()

    def test_manual_deploy_restarts_only_when_selected_and_successful(self) -> None:
        for restart, deployment_fails in ((False, False), (True, False), (True, True)):
            with self.subTest(restart=restart, deployment_fails=deployment_fails):
                deployment = mock.Mock()
                if deployment_fails:
                    deployment.deploy_image.side_effect = RuntimeError("deployment failed")
                power = mock.Mock()
                operations = []
                app = SimpleNamespace(
                    disk_list=mock.Mock(), disk_confirmation=mock.Mock(),
                    deploy_wim=mock.Mock(), deploy_index=mock.Mock(), firmware=mock.Mock(),
                    restart_after_deploy=mock.Mock(), deploy_progress_value=mock.Mock(),
                    deploy_progress_text=mock.Mock(), _deployment=deployment, _power=power,
                    _make_progress_reporter=mock.Mock(return_value=lambda _: None),
                    _reset_progress_estimate=mock.Mock(), _log=mock.Mock(),
                    _run_background=lambda name, action: operations.append(action),
                )
                app.disk_list.curselection.return_value = (0,)
                app.disk_list.get.return_value = "Disk 2: Target NVMe"
                app.disk_confirmation.get.return_value = "2"
                app.deploy_wim.get.return_value = "Z:\\Images\\target.wim"
                app.deploy_index.get.return_value = "1"
                app.firmware.get.return_value = "UEFI (GPT)"
                app.restart_after_deploy.get.return_value = restart
                with mock.patch("winpe_deploy.gui.messagebox.askyesno", return_value=True):
                    WinPEImageDeployerApp._deploy_image(app)
                self.assertEqual(1, len(operations))
                app.restart_after_deploy.get.assert_called_once()
                if deployment_fails:
                    with self.assertRaisesRegex(RuntimeError, "deployment failed"):
                        operations[0]()
                else:
                    operations[0]()
                deployment.deploy_image.assert_called_once()
                self.assertEqual(int(restart and not deployment_fails), power.restart.call_count)