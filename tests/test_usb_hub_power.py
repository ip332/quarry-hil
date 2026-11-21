#!/usr/bin/env python3
"""usb_hub_power port discovery and the safety guard that runs before any
power cut. Uses a fake sysfs tree and recorded uhubctl output; never
touches real hardware."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infrastructure", "core"))
import usb_hub_power as uhp  # noqa: E402

JLINK = "000900003460"
STLINK = "0669FF565271525067071140"

# Real output from the HIL host (2026-11-21): J-Link on 3-7 port 2, USB3
# companion 4-6 reported first, an external disk on connector 3.
STATUS_NOW = """Current status for hub 4-6 [2109:0812 VIA Labs, Inc. USB3.0 Hub, USB 3.00, 4 ports, ppps]
  Port 2: 02a0 power 5gbps Rx.Detect
Current status for hub 3-7 [2109:2812 VIA Labs, Inc. USB2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 2: 0103 power enable connect [1366:0105 SEGGER J-Link 000900003460]
"""
# The old cabling, with the ST-LINK on port 3 of the same hub.
STATUS_STLINK = """Current status for hub 3-11.2 [2109:2812 VIA Labs, Inc. USB2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 3: 0103 power enable connect [0483:374b STMicroelectronics STM32 STLink 0669FF565271525067071140]
"""
STATUS_OTHER = """Current status for hub 3-7 [2109:2812 VIA Labs, Inc. USB2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0503 power highspeed enable connect [18d1:4ee7 Xiaomi MI 8 fb570c46]
"""
STATUS_COMPANION_BUSY = """Current status for hub 4-6 [2109:0812 VIA Labs, Inc. USB3.0 Hub, USB 3.00, 4 ports, ppps]
  Port 3: 0203 power 5gbps U0 enable connect [07ab:0621]
Current status for hub 3-7 [2109:2812 VIA Labs, Inc. USB2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 3: 0100 power
"""
STATUS_GANGED = """Current status for hub 3-11 [05e3:0610 Genesys Logic, Inc. Hub, USB 2.10, 4 ports, ganged]
  Port 1: 0103 power enable connect [1366:0105 SEGGER J-Link 000900003460]
"""
STATUS_EMPTY_PORT = """Current status for hub 3-7 [2109:2812 VIA Labs, Inc. USB2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 2: 0100 power
"""


def refused(*args):
    try:
        uhp.check_port_safe(*args)
    except uhp.UsbPortError as e:
        return str(e)
    return None


def test_guard():
    assert refused("3-7", "2", JLINK, STATUS_NOW) is None
    assert refused("3-7", "2", JLINK, STATUS_EMPTY_PORT) is None  # probe dropped off the bus
    msg = refused("3-11.2", "3", JLINK, STATUS_STLINK)
    assert msg and "NUCLEO-F446RE ST-LINK" in msg, msg
    assert "MI 8" in (refused("3-7", "1", JLINK, STATUS_OTHER) or "")
    assert "07ab:0621" in (refused("3-7", "3", JLINK, STATUS_COMPANION_BUSY) or "")
    assert "ppps" in (refused("3-11", "1", JLINK, STATUS_GANGED) or "")
    assert refused("9-9", "1", JLINK, "No compatible devices detected at location 9-9!")


def test_power_cycle_requires_serial_and_checks_first():
    calls = []
    real = uhp._run_uhubctl
    uhp._run_uhubctl = lambda args, timeout=10: calls.append(args) or STATUS_STLINK
    try:
        for kwargs in ({}, {"expected_serial": JLINK}):
            try:
                uhp.power_cycle("3-11.2", "3", **kwargs)
            except uhp.UsbPortError:
                pass
            else:
                raise AssertionError("power_cycle(%r) did not refuse" % kwargs)
        assert all("-a" not in a for a in calls), "a power action ran: %r" % calls
    finally:
        uhp._run_uhubctl = real


def make_sysfs(root, devices):
    for name, serial in devices.items():
        os.makedirs(os.path.join(root, name))
        if serial is not None:
            with open(os.path.join(root, name, "serial"), "w") as f:
                f.write(serial + "\n")
    os.makedirs(os.path.join(root, "3-7.2:1.0"))  # interface dirs are skipped


def test_discovery_and_cache():
    with tempfile.TemporaryDirectory() as tmp:
        sysfs, cache = os.path.join(tmp, "sys"), os.path.join(tmp, "cache")
        make_sysfs(sysfs, {"3-7": None, "3-7.2": JLINK, "3-11.1": STLINK, "3-10": "CJCP"})
        assert uhp.find_device_port(JLINK, sysfs) == ("3-7", "2")
        assert uhp.find_device_port(STLINK, sysfs) == ("3-11", "1")
        assert uhp.find_device_port("CJCP", sysfs) == ("3", "10")
        assert uhp.find_device_port("nope", sysfs) is None
        assert uhp.port_for_serial(JLINK, sysfs=sysfs, cache_dir=cache) == ("3-7", "2")

        os.rename(os.path.join(sysfs, "3-7.2"), os.path.join(tmp, "gone"))  # probe drops off the bus
        assert uhp.port_for_serial(JLINK, sysfs=sysfs, cache_dir=cache) == ("3-7", "2")
        try:
            uhp.port_for_serial(JLINK, sysfs=sysfs, cache_dir=os.path.join(tmp, "empty"))
        except uhp.UsbPortError:
            pass
        else:
            raise AssertionError("expected UsbPortError with no cached location")


if __name__ == "__main__":
    test_guard()
    test_power_cycle_requires_serial_and_checks_first()
    test_discovery_and_cache()
    print("✓ All USB hub power tests passed")
