"""Discover Windows network interface names and physical addresses without PowerShell."""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes


class _AdapterAddresses(ctypes.Structure):
    pass


_AdapterAddresses._fields_ = [
    ("length", wintypes.ULONG),
    ("if_index", wintypes.ULONG),
    ("next", ctypes.POINTER(_AdapterAddresses)),
    ("adapter_name", ctypes.c_char_p),
    ("first_unicast", ctypes.c_void_p),
    ("first_anycast", ctypes.c_void_p),
    ("first_multicast", ctypes.c_void_p),
    ("first_dns", ctypes.c_void_p),
    ("dns_suffix", ctypes.c_void_p),
    ("description", ctypes.c_void_p),
    ("friendly_name", ctypes.c_wchar_p),
    ("physical_address", ctypes.c_ubyte * 8),
    ("physical_address_length", wintypes.ULONG),
]


def normalize_mac(value: str) -> str:
    """Require exactly six hexadecimal octets, accepting colon or hyphen notation."""
    if not re.fullmatch(r"[0-9a-fA-F]{2}([:-])[0-9a-fA-F]{2}(?:\1[0-9a-fA-F]{2}){4}", value.strip()):
        raise ValueError("[ethernet] mac must contain six hexadecimal octets separated by colons or hyphens.")
    return value.strip().replace(":", "-").upper()


def adapter_names_for_mac(mac: str) -> list[str]:
    """Read interface aliases directly from IP Helper; never infer them from list order."""
    expected = normalize_mac(mac)
    try:
        api = ctypes.WinDLL("iphlpapi.dll")
    except (AttributeError, OSError) as error:
        raise RuntimeError("Windows IP Helper API is unavailable for MAC discovery.") from error
    get_addresses = api.GetAdaptersAddresses
    get_addresses.argtypes = [wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]
    get_addresses.restype = wintypes.ULONG
    size = wintypes.ULONG(15000)
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = get_addresses(0, 0x0100, None, buffer, ctypes.byref(size))
        if result == 111:  # ERROR_BUFFER_OVERFLOW: adapters changed or initial buffer was too small.
            continue
        if result != 0:
            raise RuntimeError(f"Windows IP Helper adapter discovery failed (error {result}).")
        matches: list[str] = []
        current = ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses))
        while current:
            adapter = current.contents
            if adapter.physical_address_length == 6:
                actual = "-".join(f"{byte:02X}" for byte in adapter.physical_address[:6])
                if actual == expected and adapter.friendly_name:
                    matches.append(adapter.friendly_name)
            current = adapter.next
        return matches
    raise RuntimeError("Windows IP Helper adapter list changed repeatedly during discovery.")