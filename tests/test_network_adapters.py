import ctypes
import unittest
from unittest import mock

from winpe_deploy.network_adapters import _AdapterAddresses, adapter_names_for_mac, normalize_mac


class NetworkAdapterDiscoveryTests(unittest.TestCase):
    def test_normalize_mac_accepts_colons_and_hyphens_only(self) -> None:
        self.assertEqual("AA-BB-CC-01-02-FF", normalize_mac(" aa:bb:cc:01:02:ff "))
        self.assertEqual("AA-BB-CC-01-02-FF", normalize_mac("aa-bb-cc-01-02-ff"))
        for value in ("", "AA:BB:CC:DD:EE", "AA:BB-CC:DD:EE:FF", "AA:BB:CC:DD:EE:GG"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_mac(value)

    def test_ip_helper_maps_mac_to_friendly_name(self) -> None:
        first = _AdapterAddresses()
        first.friendly_name = "Other adapter"
        first.physical_address_length = 6
        first.physical_address[:6] = (0, 1, 2, 3, 4, 5)
        second = _AdapterAddresses()
        second.friendly_name = "Local Area Connection 3"
        second.physical_address_length = 6
        second.physical_address[:6] = (0xAA, 0xBB, 0xCC, 1, 2, 0xFF)
        first.next = ctypes.pointer(second)

        def get_addresses(family, flags, reserved, buffer, size):
            self.assertEqual(0x0100, flags)
            ctypes.memmove(buffer, ctypes.addressof(first), ctypes.sizeof(first))
            return 0

        api = mock.Mock()
        api.GetAdaptersAddresses = mock.Mock(side_effect=get_addresses)
        with mock.patch("winpe_deploy.network_adapters.ctypes.WinDLL", return_value=api, create=True):
            self.assertEqual(["Local Area Connection 3"], adapter_names_for_mac("aa:bb:cc:01:02:ff"))

    def test_ip_helper_error_does_not_guess_adapter(self) -> None:
        api = mock.Mock()
        api.GetAdaptersAddresses = mock.Mock(return_value=5)
        with mock.patch("winpe_deploy.network_adapters.ctypes.WinDLL", return_value=api, create=True):
            with self.assertRaisesRegex(RuntimeError, "error 5"):
                adapter_names_for_mac("AA-BB-CC-01-02-FF")