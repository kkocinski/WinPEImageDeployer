import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from winpe_deploy.models import DiskInfo, WimImageInfo
from winpe_deploy.media_content import MEDIA_CONTENT_DIR
from winpe_deploy.startup_preflight import run_startup_preflight
from winpe_deploy.startup_config import EthernetStartupConfig, StartupConfig
from winpe_deploy.startup_preflight import _apply_ethernet_configuration


class FakeDeploymentService:
    def __init__(self) -> None:
        self.directories: list[str] = []

    def load_session_drivers(self, directory: str, progress):
        self.directories.append(directory)
        progress("Loaded test.inf into the current WinPE session.")
        return 1, []


class AutoDeployFakeDeploymentService(FakeDeploymentService):
    def __init__(self, protected_disk_number: int) -> None:
        super().__init__()
        self.protected_disk_number = protected_disk_number
        self.deployed: list[tuple[str, int, int, object]] = []

    def inspect_wim(self, path: str) -> list[WimImageInfo]:
        return [WimImageInfo(1, "Windows 11", "")]

    def disk_number_for_drive(self, drive: str) -> int:
        return self.protected_disk_number

    def list_disks(self) -> list[DiskInfo]:
        return [
            DiskInfo(self.protected_disk_number, "WinPE USB", 28 * 1024**3),
            DiskInfo(0, "Internal SSD", 476 * 1024**3),
        ]

    def deploy_image(self, path: str, index: int, disk_number: int, firmware, progress) -> None:
        self.deployed.append((path, index, disk_number, firmware))
        progress("100% Test deployment complete.")


class FakePowerService:
    def __init__(self) -> None:
        self.restart_calls = 0

    def restart(self) -> None:
        self.restart_calls += 1


class FakeNetworkService:
    def __init__(self) -> None:
        self.configured: list[tuple[str, str, str, str, str]] = []
        self.share_calls: list[tuple[str, str, str, str]] = []
        self.unc_calls: list[tuple[str, str, str]] = []

    def list_adapters(self) -> list[str]:
        return ["Ethernet"]

    def configure_ipv4(self, adapter: str, address: str, mask: str, gateway: str, dns_servers: str) -> None:
        self.configured.append((adapter, address, mask, gateway, dns_servers))

    def connect_share(self, *args) -> None:
        self.share_calls.append(args)

    def connect_unc_share(self, *args) -> None:
        self.unc_calls.append(args)


class RetryNetworkService(FakeNetworkService):
    def __init__(self) -> None:
        super().__init__()
        self.remaining_failures = 2

    def connect_share(self, *args) -> None:
        self.share_calls.append(args)
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise RuntimeError("network is still initializing")


class StartupPreflightTests(unittest.TestCase):
    def test_unc_auto_deploy_authenticates_without_mapping_and_retries_before_disk_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / MEDIA_CONTENT_DIR
            content.mkdir()
            (content / "startup-config.ini").write_text(
                "[auto_deploy]\nenabled = true\nwim_path = \\\\server\\images\\base.wim\n"
                "username = DOMAIN\\deploy\npassword = secret\nimage_index = 1\n"
                "firmware = UEFI (GPT)\nminimum_disk_size_gib = 100\n", encoding="utf-8",
            )
            deployment = AutoDeployFakeDeploymentService(protected_disk_number=1)
            network = FakeNetworkService()
            network.connect_unc_share = mock.Mock(side_effect=[RuntimeError("not ready"), None])
            power = FakePowerService()
            waits = []
            with mock.patch("winpe_deploy.startup_preflight._validate_existing_wim_path", side_effect=lambda path: path), \
                 mock.patch("winpe_deploy.startup_preflight._drive_type", return_value=3):
                completed = run_startup_preflight(
                    roots=[root], deployment=deployment, network=network, power=power,
                    sleeper=waits.append, logger=logging.getLogger("test_unc_deploy"),
                )
        self.assertTrue(completed)
        self.assertEqual([5], waits)
        self.assertEqual(2, network.connect_unc_share.call_count)
        network.connect_unc_share.assert_called_with("\\\\server\\images", "DOMAIN\\deploy", "secret")
        self.assertEqual([], network.share_calls)
        self.assertEqual("\\\\server\\images\\base.wim", deployment.deployed[0][0])
        self.assertEqual(0, deployment.deployed[0][2])
        self.assertEqual(1, power.restart_calls)

    def test_unc_auto_deploy_fails_closed_and_redacts_password_on_connection_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / MEDIA_CONTENT_DIR
            content.mkdir()
            (content / "startup-config.ini").write_text(
                "[auto_deploy]\nenabled = true\nwim_path = \\\\server\\images\\base.wim\n"
                "username = tech\npassword = secret\nimage_index = 1\n"
                "firmware = UEFI (GPT)\nminimum_disk_size_gib = 100\n", encoding="utf-8",
            )
            deployment = AutoDeployFakeDeploymentService(protected_disk_number=1)
            network = FakeNetworkService()
            network.connect_unc_share = mock.Mock(side_effect=RuntimeError("bad secret"))
            waits = []
            with self.assertLogs("test_unc_failure", level="WARNING") as captured:
                completed = run_startup_preflight(
                    roots=[root], deployment=deployment, network=network,
                    sleeper=waits.append, logger=logging.getLogger("test_unc_failure"),
                )
        self.assertFalse(completed)
        self.assertEqual(6, network.connect_unc_share.call_count)
        self.assertEqual([5] * 5, waits)
        self.assertEqual([], deployment.deployed)
        self.assertNotIn("secret", "\n".join(captured.output))

    def test_unc_auto_deploy_does_not_erase_disk_if_wim_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / MEDIA_CONTENT_DIR
            content.mkdir()
            (content / "startup-config.ini").write_text(
                "[auto_deploy]\nenabled = true\nwim_path = \\\\server\\images\\missing.wim\n"
                "username = tech\npassword = secret\nimage_index = 1\n"
                "firmware = UEFI (GPT)\nminimum_disk_size_gib = 100\n", encoding="utf-8",
            )
            deployment = AutoDeployFakeDeploymentService(protected_disk_number=1)
            network = FakeNetworkService()
            with mock.patch("winpe_deploy.startup_preflight._validate_existing_wim_path", side_effect=ValueError("WIM inaccessible")):
                completed = run_startup_preflight(
                    roots=[root], deployment=deployment, network=network,
                    sleeper=lambda _: None, logger=logging.getLogger("test_unc_missing_wim"),
                )
        self.assertFalse(completed)
        self.assertEqual(1, len(network.unc_calls))
        self.assertEqual([], deployment.deployed)

    def test_unc_auto_deploy_waits_after_static_ipv4_before_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / MEDIA_CONTENT_DIR
            content.mkdir()
            (content / "startup-config.ini").write_text(
                "[ethernet]\nadapter = Ethernet\naddress = 10.0.0.2\nmask = 255.255.255.0\n"
                "[auto_deploy]\nenabled = true\nwim_path = \\\\server\\images\\base.wim\n"
                "username = tech\npassword = secret\nimage_index = 1\n"
                "firmware = UEFI (GPT)\nminimum_disk_size_gib = 100\n", encoding="utf-8",
            )
            events = []
            network = FakeNetworkService()
            network.configure_ipv4 = lambda *args: events.append("ipv4")
            network.connect_unc_share = lambda *args: events.append("unc")
            deployment = AutoDeployFakeDeploymentService(protected_disk_number=1)
            with mock.patch("winpe_deploy.startup_preflight._validate_existing_wim_path", side_effect=ValueError("not accessible")):
                completed = run_startup_preflight(
                    roots=[root], deployment=deployment, network=network,
                    sleeper=lambda seconds: events.append(f"wait:{seconds}"),
                    logger=logging.getLogger("test_unc_ipv4_wait"),
                )
        self.assertFalse(completed)
        self.assertEqual(["ipv4", "wait:10", "unc"], events)
        self.assertEqual([], deployment.deployed)

    def test_mac_selects_named_adapter_before_configuring_ipv4(self) -> None:
        network = FakeNetworkService()
        network.list_adapters = lambda: ["Ethernet", "Local Area Connection 3"]
        configuration = StartupConfig(ethernet=EthernetStartupConfig("", "10.0.0.2", "255.255.255.0", "", "", "AA-BB-CC-01-02-FF"))
        with mock.patch("winpe_deploy.startup_preflight.adapter_names_for_mac", return_value=["Local Area Connection 3"]) as discover:
            _apply_ethernet_configuration(Path("startup-config.ini"), configuration, network, logging.getLogger("test_mac"))
        discover.assert_called_once_with("AA-BB-CC-01-02-FF")
        self.assertEqual([("Local Area Connection 3", "10.0.0.2", "255.255.255.0", "", "")], network.configured)

    def test_mac_selection_fails_closed_if_missing_ambiguous_or_conflicting(self) -> None:
        for matches, name in (([], ""), (["Ethernet", "Ethernet 2"], ""), (["Ethernet"], "Other")):
            with self.subTest(matches=matches, name=name):
                network = FakeNetworkService()
                network.list_adapters = lambda: ["Ethernet", "Ethernet 2", "Other"]
                configuration = StartupConfig(ethernet=EthernetStartupConfig(name, "10.0.0.2", "255.255.255.0", "", "", "AA-BB-CC-01-02-FF"))
                with mock.patch("winpe_deploy.startup_preflight.adapter_names_for_mac", return_value=matches):
                    _apply_ethernet_configuration(Path("startup-config.ini"), configuration, network, logging.getLogger("test_mac"))
                self.assertEqual([], network.configured)

    def test_mac_selection_ignores_non_configurable_filter_interfaces(self) -> None:
        network = FakeNetworkService()
        configuration = StartupConfig(ethernet=EthernetStartupConfig("", "10.0.0.2", "255.255.255.0", "", "", "AA-BB-CC-01-02-FF"))
        with mock.patch("winpe_deploy.startup_preflight.adapter_names_for_mac", return_value=["Ethernet", "Ethernet-WFP filter"]):
            _apply_ethernet_configuration(Path("startup-config.ini"), configuration, network, logging.getLogger("test_mac"))
        self.assertEqual("Ethernet", network.configured[0][0])

    def test_media_folder_name_is_shared_by_preflight_and_gui(self) -> None:
        from winpe_deploy.gui import WinPEImageDeployerApp
        from unittest import mock

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / MEDIA_CONTENT_DIR
            drivers = content / "Drivers"
            drivers.mkdir(parents=True)
            (drivers / "test.inf").write_text("[Version]", encoding="utf-8")
            config = content / "startup-config.ini"
            config.write_text("[auto_deploy]\nenabled = false\n", encoding="utf-8")
            with mock.patch.object(WinPEImageDeployerApp, "_directory_roots", return_value=[root]):
                self.assertEqual([drivers], WinPEImageDeployerApp._auto_driver_directories())
                self.assertEqual([config], WinPEImageDeployerApp._startup_configuration_files())

    def test_preflight_scans_roots_loads_drivers_configures_ethernet_and_maps_share(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / "winpe-media-content"
            drivers = content / "Drivers" / "network"
            drivers.mkdir(parents=True)
            (drivers / "test.inf").write_text("[Version]", encoding="utf-8")
            (content / "startup-config.ini").write_text(
                "[ethernet]\n"
                "adapter = Ethernet\n"
                "address = 192.168.1.241\n"
                "mask = 255.255.255.0\n"
                "gateway = 192.168.1.1\n"
                "dns = 1.1.1.1\n\n"
                "[share]\n"
                "drive_letter = Z:\n"
                "unc_path = \\\\server\\deployment\n"
                "username = tech\n"
                "password = secret\n",
                encoding="utf-8",
            )
            deployment = FakeDeploymentService()
            network = FakeNetworkService()
            logger = logging.getLogger("test_startup_preflight")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())

            waits: list[int] = []
            run_startup_preflight(
                roots=[root, root / "missing"],
                logger=logger,
                deployment=deployment,
                network=network,
                sleeper=waits.append,
            )

        self.assertEqual([str(content / "Drivers")], deployment.directories)
        self.assertEqual([("Ethernet", "192.168.1.241", "255.255.255.0", "192.168.1.1", "1.1.1.1")], network.configured)
        self.assertEqual([("Z:", "\\\\server\\deployment", "tech", "secret")], network.share_calls)
        self.assertEqual([10, 10], waits)

    def test_source_disk_wim_requires_unique_serial_and_exactly_one_file(self) -> None:
        from winpe_deploy.startup_config import AutoDeployStartupConfig
        from winpe_deploy.models import FirmwareType
        from winpe_deploy.startup_preflight import _resolve_source_wim

        with tempfile.TemporaryDirectory():
            configuration = AutoDeployStartupConfig("Images\\base.wim", 1, FirmwareType.UEFI, 100, "", source_disk_serial="SOURCE")
            deployment = mock.Mock()
            deployment.disk_number_for_drive.return_value = 2
            disk = DiskInfo(2, "Source", 64 * 1024**3, serial_number="SOURCE", partition_letters=((2, "R:"),))
            available = {r"R:\Images\base.wim"}
            def accessible(path: Path) -> bool:
                return str(path) in available

            with mock.patch("winpe_deploy.startup_preflight.Path.is_file", autospec=True, side_effect=accessible):
                path, chosen = _resolve_source_wim(configuration, [disk], deployment)
                self.assertEqual(r"R:\Images\base.wim", path)
                self.assertEqual(disk, chosen)
                for disks in ([], [disk, disk]):
                    with self.assertRaisesRegex(ValueError, "exactly one physical disk"):
                        _resolve_source_wim(configuration, disks, deployment)
                two = DiskInfo(2, "Source", 64 * 1024**3, serial_number="SOURCE", partition_letters=((1, "R:"), (2, "S:")))
                # An unrelated WIM on S: does not affect the exact-path lookup.
                self.assertEqual(r"R:\Images\base.wim", _resolve_source_wim(configuration, [two], deployment)[0])
                available.add(r"S:\Images\base.wim")
                with self.assertRaisesRegex(ValueError, "exactly one accessible WIM"):
                    _resolve_source_wim(configuration, [two], deployment)
                available.remove(r"S:\Images\base.wim")
                deployment.disk_number_for_drive.return_value = 3
                with self.assertRaisesRegex(ValueError, "does not belong"):
                    _resolve_source_wim(configuration, [disk], deployment)
            with self.assertRaisesRegex(ValueError, "exactly one accessible WIM"):
                _resolve_source_wim(configuration, [disk], deployment)

    def test_source_disk_is_never_selected_for_clean(self) -> None:
        from winpe_deploy.startup_config import AutoDeployStartupConfig
        from winpe_deploy.models import FirmwareType
        from winpe_deploy.startup_preflight import _run_automatic_deployment

        config = AutoDeployStartupConfig("Images\\base.wim", 1, FirmwareType.UEFI, 100, "", source_disk_serial="SOURCE")
        source = DiskInfo(2, "Image disk", 500 * 1024**3, serial_number="SOURCE", partition_letters=((2, "R:"),))
        target = DiskInfo(0, "Target", 500 * 1024**3)
        deployment = mock.Mock()
        deployment.list_disks_for_auto_deploy.return_value = [source, target]
        deployment.disk_number_for_drive.return_value = 2
        deployment.inspect_wim.return_value = [WimImageInfo(1, "Windows", "")]
        power = FakePowerService()
        logger = logging.getLogger("test_source_disk_protection")
        with mock.patch("winpe_deploy.startup_preflight.Path.is_file", return_value=True), mock.patch(
            "winpe_deploy.startup_preflight._auto_deploy_protected_disk_numbers", return_value={3}
        ):
            self.assertTrue(_run_automatic_deployment(Path("C:\\startup-config.ini"), config, deployment, power, logger, FakeNetworkService(), lambda _: None))
            self.assertEqual(0, deployment.deploy_image.call_args.args[2])
            self.assertEqual(1, power.restart_calls)
            deployment.deploy_image.reset_mock()
            deployment.list_disks_for_auto_deploy.return_value = [source]
            self.assertFalse(_run_automatic_deployment(Path("C:\\startup-config.ini"), config, deployment, power, logger, FakeNetworkService(), lambda _: None))
            deployment.deploy_image.assert_not_called()

    def test_preflight_deploys_before_gui_when_one_eligible_disk_remains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / "winpe-media-content"
            content.mkdir()
            wim_path = root / "reference.wim"
            wim_path.write_bytes(b"test")
            (content / "startup-config.ini").write_text(
                "[auto_deploy]\n"
                "enabled = true\n"
                f"wim_path = {wim_path}\n"
                "image_index = 1\n"
                "firmware = UEFI (GPT)\n"
                "minimum_disk_size_gib = 100\n"
                "maximum_disk_size_gib = 1100\n"
                "expected_disk_serial =\n",
                encoding="utf-8",
            )
            deployment = AutoDeployFakeDeploymentService(protected_disk_number=1)
            original_list_disks = deployment.list_disks
            deployment.list_disks = lambda: original_list_disks() + [DiskInfo(2, "4 TB data disk", 4000 * 1000**3)]
            power = FakePowerService()
            logger = logging.getLogger("test_startup_preflight_auto_deploy")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())

            completed = run_startup_preflight(
                roots=[root],
                logger=logger,
                deployment=deployment,
                network=FakeNetworkService(),
                power=power,
                sleeper=lambda _: None,
            )

        self.assertTrue(completed)
        self.assertEqual(1, len(deployment.deployed))
        self.assertEqual(str(wim_path), deployment.deployed[0][0])
        self.assertEqual(1, deployment.deployed[0][1])
        self.assertEqual(0, deployment.deployed[0][2])
        self.assertEqual(1, power.restart_calls)

    def test_preflight_maximum_size_blocks_deployment_before_disk_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / "winpe-media-content"
            content.mkdir()
            wim_path = root / "reference.wim"
            wim_path.write_bytes(b"test")
            (content / "startup-config.ini").write_text(
                "[auto_deploy]\nenabled = true\n"
                f"wim_path = {wim_path}\n"
                "image_index = 1\nfirmware = UEFI (GPT)\n"
                "minimum_disk_size_gib = 100\nmaximum_disk_size_gib = 400\n",
                encoding="utf-8",
            )
            deployment = AutoDeployFakeDeploymentService(protected_disk_number=1)
            power = FakePowerService()
            completed = run_startup_preflight(
                roots=[root], logger=logging.getLogger("test_preflight_maximum_size"),
                deployment=deployment, network=FakeNetworkService(), power=power,
                sleeper=lambda _: None,
            )
        self.assertFalse(completed)
        self.assertEqual([], deployment.deployed)
        self.assertEqual(0, power.restart_calls)

    def test_preflight_does_not_reboot_when_firmware_priority_cannot_be_set(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / "winpe-media-content"
            content.mkdir()
            wim_path = root / "reference.wim"
            wim_path.write_bytes(b"test")
            (content / "startup-config.ini").write_text(
                "[auto_deploy]\n"
                "enabled = true\n"
                f"wim_path = {wim_path}\n"
                "image_index = 1\n"
                "firmware = UEFI (GPT)\n"
                "minimum_disk_size_gib = 100\n"
                "expected_disk_serial =\n", encoding="utf-8",
            )
            deployment = AutoDeployFakeDeploymentService(protected_disk_number=1)
            deployment.deploy_image = mock.Mock(side_effect=RuntimeError("UEFI priority unavailable"))
            power = FakePowerService()
            logger = logging.getLogger("test_startup_preflight_no_reboot")
            logger.addHandler(logging.NullHandler())
            completed = run_startup_preflight(
                roots=[root], logger=logger, deployment=deployment,
                network=FakeNetworkService(), power=power, sleeper=lambda _: None,
            )
        self.assertFalse(completed)
        self.assertEqual(0, power.restart_calls)

    def test_preflight_skips_ethernet_when_multiple_active_configs_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            roots = [parent / "first", parent / "second"]
            for root in roots:
                content = root / "winpe-media-content"
                content.mkdir(parents=True)
                (content / "startup-config.ini").write_text(
                    "[ethernet]\nadapter = Ethernet\naddress = 10.0.0.10\nmask = 255.255.255.0\n",
                    encoding="utf-8",
                )
            network = FakeNetworkService()
            logger = logging.getLogger("test_startup_preflight_multiple")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())

            run_startup_preflight(roots=roots, logger=logger, deployment=FakeDeploymentService(), network=network, sleeper=lambda _: None)

        self.assertEqual([], network.configured)

    def test_preflight_retries_smb_mapping_after_network_initialization_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / "winpe-media-content"
            content.mkdir()
            (content / "startup-config.ini").write_text(
                "[share]\n"
                "drive_letter = Z:\n"
                "unc_path = \\\\server\\deployment\n"
                "username = tech\n"
                "password = secret\n",
                encoding="utf-8",
            )
            network = RetryNetworkService()
            logger = logging.getLogger("test_startup_preflight_retry")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            waits: list[float] = []

            run_startup_preflight(
                roots=[root],
                logger=logger,
                deployment=FakeDeploymentService(),
                network=network,
                sleeper=waits.append,
            )

        self.assertEqual(3, len(network.share_calls))
        self.assertEqual([5, 5], waits)

    def test_smb_wait_follows_successful_ipv4_before_first_mapping_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / "winpe-media-content"
            content.mkdir()
            (content / "startup-config.ini").write_text(
                "[ethernet]\nadapter = Ethernet\naddress = 10.0.0.2\nmask = 255.255.255.0\n"
                "[share]\ndrive_letter = Z:\nunc_path = \\\\server\\share\nusername = tech\npassword = secret\n",
                encoding="utf-8",
            )
            events: list[str] = []
            network = FakeNetworkService()
            network.configure_ipv4 = lambda *args: events.append("ipv4")
            network.connect_share = lambda *args: events.append("share")
            logger = logging.getLogger("test_smb_wait_order")
            logger.addHandler(logging.NullHandler())
            run_startup_preflight(
                roots=[root], logger=logger, deployment=FakeDeploymentService(), network=network,
                sleeper=lambda seconds: events.append(f"wait:{seconds}"),
            )
        self.assertEqual(["ipv4", "wait:10", "share"], events)

    def test_smb_does_not_wait_after_failed_ipv4(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / "winpe-media-content"
            content.mkdir()
            (content / "startup-config.ini").write_text(
                "[ethernet]\nadapter = Ethernet\naddress = 10.0.0.2\nmask = 255.255.255.0\n"
                "[share]\ndrive_letter = Z:\nunc_path = \\\\server\\share\nusername = tech\npassword = secret\n",
                encoding="utf-8",
            )
            network = FakeNetworkService()
            network.configure_ipv4 = mock.Mock(side_effect=RuntimeError("netsh failed"))
            logger = logging.getLogger("test_smb_no_wait_failed_ipv4")
            logger.addHandler(logging.NullHandler())
            waits: list[float] = []
            run_startup_preflight(
                roots=[root], logger=logger, deployment=FakeDeploymentService(), network=network,
                sleeper=waits.append,
            )
        self.assertEqual([], waits)
        self.assertEqual(1, len(network.share_calls))