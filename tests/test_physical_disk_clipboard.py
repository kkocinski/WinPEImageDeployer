import unittest
from types import SimpleNamespace
from unittest import mock

from winpe_deploy.gui import WinPEImageDeployerApp
from winpe_deploy.models import DiskInfo


class PhysicalDiskClipboardTests(unittest.TestCase):
    def test_disk_refresh_keeps_serial_by_number_and_clears_stale_selection(self) -> None:
        app = SimpleNamespace(
            physical_disks={}, physical_disk_serials={},
            physical_disk_combo={}, physical_disk=mock.Mock(),
        )
        app.physical_disk.get.return_value = "old selection"
        disk = DiskInfo(2, "Source", 1024**3, serial_number="SN-123")
        WinPEImageDeployerApp._set_physical_disks(app, [disk])
        self.assertEqual({2: "SN-123"}, app.physical_disk_serials)
        self.assertEqual([disk.display_name()], app.physical_disk_combo["values"])
        app.physical_disk.set.assert_called_with(disk.display_name())
        WinPEImageDeployerApp._set_physical_disks(app, [])
        self.assertEqual({}, app.physical_disk_serials)
        app.physical_disk.set.assert_called_with("")

    def test_context_menu_copies_only_selected_disk_serial(self) -> None:
        menu = mock.Mock()
        app = SimpleNamespace(
            physical_disk_combo=object(), physical_disk_serials={1: "WRONG", 2: " SN-123 "},
            _selected_physical_disk_number=lambda: 2,
            _copy_physical_disk_serial=mock.Mock(),
        )
        event = SimpleNamespace(x_root=20, y_root=30)
        with mock.patch("winpe_deploy.gui.tk.Menu", return_value=menu):
            WinPEImageDeployerApp._show_physical_disk_menu(app, event)
        menu.add_command.assert_called_once()
        self.assertEqual("normal", str(menu.add_command.call_args.kwargs["state"]))
        menu.add_command.call_args.kwargs["command"]()
        app._copy_physical_disk_serial.assert_called_once_with("SN-123")
        menu.tk_popup.assert_called_once_with(20, 30)
        menu.grab_release.assert_called_once()

    def test_menu_disables_copy_when_serial_unavailable(self) -> None:
        menu = mock.Mock()
        app = SimpleNamespace(
            physical_disk_combo=object(), physical_disk_serials={2: ""},
            _selected_physical_disk_number=lambda: 2,
            _copy_physical_disk_serial=mock.Mock(),
        )
        with mock.patch("winpe_deploy.gui.tk.Menu", return_value=menu):
            WinPEImageDeployerApp._show_physical_disk_menu(app, SimpleNamespace(x_root=0, y_root=0))
        self.assertEqual("disabled", str(menu.add_command.call_args.kwargs["state"]))

    def test_copy_writes_exact_serial_only_and_ignores_empty(self) -> None:
        app = SimpleNamespace(clipboard_clear=mock.Mock(), clipboard_append=mock.Mock(), _status=mock.Mock())
        WinPEImageDeployerApp._copy_physical_disk_serial(app, "SN-123")
        app.clipboard_clear.assert_called_once_with()
        app.clipboard_append.assert_called_once_with("SN-123")
        WinPEImageDeployerApp._copy_physical_disk_serial(app, "")
        app.clipboard_append.assert_called_once_with("SN-123")


if __name__ == "__main__":
    unittest.main()