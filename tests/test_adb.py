from pathlib import Path

import pytest

from ceksinyal.collector.adb import Adb, AdbNotFoundError, parse_devices_output

DEVICES_OUTPUT = """\
* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached
R58M12345AB            device usb:1-1 product:a51nsxx model:SM_A515F device:a51 transport_id:1
9A1B2C3D               unauthorized usb:1-2 transport_id:2
192.168.1.20:5555      offline transport_id:3
ZY22ABCDEF             no permissions (user in plugdev group; are your udev rules wrong?); see [http://developer.android.com/tools/device.html]

"""


def test_parse_devices_output():
    devices = parse_devices_output(DEVICES_OUTPUT)
    assert [(d.serial, d.state) for d in devices] == [
        ("R58M12345AB", "device"),
        ("9A1B2C3D", "unauthorized"),
        ("192.168.1.20:5555", "offline"),
        ("ZY22ABCDEF", "no permissions"),
    ]
    assert devices[0].model == "SM_A515F" and devices[0].usable
    assert not devices[1].usable and devices[3].props == {}


def test_parse_devices_empty():
    assert parse_devices_output("List of devices attached\n\n") == []


def test_explicit_wrong_adb_path_is_not_replaced():
    with pytest.raises(AdbNotFoundError, match="adb_path"):
        Adb("adb-yang-tidak-ada-xyz").devices()


def test_missing_adb_raises_not_found(monkeypatch):
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", "/nonexistent-ceksinyal")
    monkeypatch.setenv("HOME", "/nonexistent-ceksinyal")
    monkeypatch.setenv("USERPROFILE", "/nonexistent-ceksinyal")
    monkeypatch.setattr("ceksinyal.collector.adb.shutil.which", lambda _: None)
    monkeypatch.setattr("ceksinyal.collector.adb.Path.home", lambda: Path("/nonexistent-ceksinyal"))
    with pytest.raises(AdbNotFoundError, match="platform-tools"):
        Adb("adb").devices()
