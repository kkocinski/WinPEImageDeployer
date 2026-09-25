import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from winpe_deploy.gui import WinPEImageDeployerApp
from winpe_deploy.operation_locks import OperationLocks, path_disk_number
from winpe_deploy.services import DeploymentService


class OperationLockTests(unittest.TestCase):
    def test_format_disk_a_allows_editor_disk_b_but_blocks_disk_a(self) -> None:
        locks = OperationLocks()
        token = locks.acquire("Formatting volume", {1})
        with locks.editor_access(Path("B:/example.txt"), lambda _: 2):
            pass
        for operation in ("open", "save"):
            with self.subTest(operation=operation), self.assertRaisesRegex(RuntimeError, "Formatting volume"):
                with locks.editor_access(Path("A:/example.txt"), lambda _: 1):
                    pass
        with self.assertRaises(RuntimeError):
            locks.acquire("Relabel", {1})
        second = locks.acquire("Relabel", {2})
        locks.release(second)
        locks.release(token)
        self.assertFalse(locks.active)

    def test_global_deploy_and_capture_exclude_all_disk_operations(self) -> None:
        locks = OperationLocks()
        for name in ("Deploying image", "Capturing image"):
            token = locks.acquire(name)
            with self.assertRaisesRegex(RuntimeError, name):
                locks.acquire("Formatting volume", {3})
            with self.assertRaisesRegex(RuntimeError, name):
                with locks.editor_access(Path("D:/file.txt"), lambda _: 3):
                    pass
            locks.release(token)

    def test_unknown_path_and_unknown_volume_fail_closed(self) -> None:
        locks = OperationLocks()
        token = locks.acquire("Formatting volume", {1})
        with self.assertRaisesRegex(RuntimeError, "Cannot safely identify"):
            with locks.editor_access(Path("Z:/unknown"), lambda _: None):
                pass
        with self.assertRaises(RuntimeError):
            locks.acquire("Unknown disk", set(), unknown=True)
        locks.release(token)
        unknown = locks.acquire("Unknown disk", set(), unknown=True)
        with self.assertRaises(RuntimeError):
            locks.acquire("Other disk", {4})
        locks.release(unknown)

    def test_editor_holds_guard_for_entire_write(self) -> None:
        locks = OperationLocks()
        with locks.editor_access(Path("B:/file"), lambda _: 2):
            with self.assertRaises(RuntimeError):
                locks.acquire("Format", {2})
        token = locks.acquire("Format", {2})
        locks.release(token)

    def test_resolution_rejects_unmapped_and_remote_volumes(self) -> None:
        kernel = mock.Mock()
        def volume_name(_path, buffer, _length):
            buffer.value = "C:\\"
            return True
        kernel.GetVolumePathNameW.side_effect = volume_name
        kernel.GetDriveTypeW.return_value = 4
        resolve = mock.Mock(return_value=1)
        with mock.patch("winpe_deploy.operation_locks.ctypes.windll", create=True) as windll:
            windll.kernel32 = kernel
            self.assertIsNone(path_disk_number(Path("C:/file"), resolve))
            kernel.GetDriveTypeW.return_value = 3
            self.assertEqual(1, path_disk_number(Path("C:/file"), resolve))
            resolve.assert_called_once_with("C:")
            self.assertIsNone(path_disk_number(Path("relative.txt"), resolve))

    def test_volume_spanning_two_disks_is_not_assigned_to_one_lock(self) -> None:
        service = DeploymentService.__new__(DeploymentService)
        service._run_diskpart_output = mock.Mock(return_value="  * Disk 1   Online  20 GB\n    Disk 2   Online  20 GB\n")
        with self.assertRaisesRegex(RuntimeError, "exactly one physical disk"):
            service.single_disk_number_for_drive("E:")
        service._run_diskpart_output.return_value = "  * Disk 2   Online  20 GB\n"
        self.assertEqual(2, service.single_disk_number_for_drive("E:"))

    def test_editor_gui_blocks_same_disk_before_io_and_checks_save_as_destination(self) -> None:
        locks = OperationLocks()
        app = SimpleNamespace(
            _operation_locks=locks, _disk_for_path=lambda p: 1 if "A:" in str(p) else 2,
            _read_text_file=mock.Mock(return_value="original"), _write_text_file=mock.Mock(),
            text_editor=mock.Mock(), text_editor_path=mock.Mock(), _log=mock.Mock(),
        )
        app.text_editor_path.get.return_value = "B:\\original.txt"
        app.text_editor.get.return_value = "modified"
        token = locks.acquire("Formatting volume", {1})
        with mock.patch("winpe_deploy.gui.messagebox") as messages, mock.patch(
            "winpe_deploy.gui.filedialog.asksaveasfilename", return_value="A:\\new.txt"
        ):
            WinPEImageDeployerApp._load_text_file(app, Path("A:/original.txt"))
            WinPEImageDeployerApp._save_text_file(app, save_as=True)
            messages.showerror.assert_called()
        app._read_text_file.assert_not_called()
        app._write_text_file.assert_not_called()
        locks.release(token)


if __name__ == "__main__":
    unittest.main()