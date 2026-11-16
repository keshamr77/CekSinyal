"""Daftar interface jaringan laptop dan IP-nya (untuk mengisi `source_ip` USB tethering di config)."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

# Subnet bawaan USB tethering Android (AOSP). Android 11+ bisa memakai subnet 192.168.x.x lain.
ANDROID_USB_TETHER = ipaddress.ip_network("192.168.42.0/24")


@dataclass
class Interface:
    name: str
    ipv4: str
    netmask: str | None = None
    is_up: bool | None = None

    @property
    def hint(self) -> str:
        try:
            ip = ipaddress.ip_address(self.ipv4)
        except ValueError:
            return ""
        lowered = self.name.lower()
        if ip.is_loopback:
            return "loopback"
        if ip.is_link_local:
            return "tanpa DHCP (belum dapat IP)"
        if ip in ANDROID_USB_TETHER:
            return "kemungkinan USB tethering Android"
        if any(word in lowered for word in ("rndis", "usb", "tether")):
            return "kemungkinan USB tethering"
        if any(word in lowered for word in ("wi-fi", "wifi", "wlan", "wlp")):
            return "Wi-Fi"
        return ""


def list_interfaces() -> list[Interface]:
    """Semua alamat IPv4 di laptop. Butuh psutil (diimpor di sini supaya perintah lain tidak bergantung)."""
    import psutil

    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET:
                st = stats.get(name)
                out.append(Interface(name, addr.address, addr.netmask, st.isup if st else None))
    return sorted(out, key=lambda i: (i.name.lower(), i.ipv4))


def local_ipv4() -> set[str]:
    return {i.ipv4 for i in list_interfaces()}
