"""USB hub per-port power control, for boards/probes with no power rail
independent of their own USB connection.

Wraps the `uhubctl` CLI (package `uhubctl`) to cut and reapply real VBUS
power to a specific hub port -- confirmed a genuine power cycle, not
just a logical disconnect, on the hubs this host uses (they report
"ppps", per-port power switching support). Requires the hub's own USB
device node to be readable/writable by this user -- not true by default
(root-only), so a udev rule granting the `plugdev` group access to the
relevant hub(s) was added on the HIL host; see
infrastructure/udev/README.md.

Ports are never hardcoded: port_for_serial() finds the hub location and
port (uhubctl's addressing, e.g. "3-7" / "2") of a device from its USB
serial number in sysfs, so moving a cable to another host port cannot
leave recovery pointing at the wrong place. The last location seen for
each serial is cached, so a probe that has dropped off the bus can still
be power-cycled back. Its user is VisionCB-8M-STD's SEGGER J-Link probe --
a separate USB device from the board itself, which gets its own power
from a relay (power_relay.py) -- which has been observed to drop off the
bus, or stay enumerated but wedged, and require exactly this kind of
power cycle to recover (the fix a human was doing by hand: unplug/replug
the hub).

Before any power cut, check_port_safe() asks uhubctl what is connected to
that port on every hub it would switch (a USB3 hub's USB2 and USB3 halves
share each physical connector) and refuses unless the port is empty or
holds only the expected device, and unless the hub switches ports
individually ("ppps"). In particular it refuses any port carrying a
PROTECTED_SERIALS device:

NUCLEO-F446RE's ST-LINK must never be power-cycled: cutting its port's
power leaves the STM32 unreachable over SWD (core_id 0xffffffff, even
under reset) while the ST-LINK itself re-enumerates fine -- reproduced
2026-09-29; only a physical USB replug recovers it.

Callers must not assume a device's /dev/ttyACM* (or similar) index
survives a power_cycle() -- per serial_link.py's own rationale, that
index is not stable across a reconnect even though a /dev/serial/by-id
path is. Re-resolve the device after every power_cycle() call; this
module deliberately doesn't do that resolution itself, to stay agnostic
of what's actually plugged into the port.
"""

import json
import os
import re
import subprocess
import time

SYSFS_USB_DEVICES = "/sys/bus/usb/devices"
PORT_CACHE_DIR = os.environ.get("QUARRY_HIL_PORT_CACHE", os.path.expanduser("~/.cache/quarry-hil/usb-ports"))

# Devices whose port power must never be cut, whatever port they are on.
PROTECTED_SERIALS = {
    "0669FF565271525067071140": "NUCLEO-F446RE ST-LINK/V2.1",
}

# Mirrors power_relay.py's settle times: let the port fully discharge
# before reapplying power so it's a real cold boot/reconnect, then give
# the hub a moment before a caller starts polling for re-enumeration.
POWER_OFF_SETTLE_SECONDS = 3.0
POWER_ON_SETTLE_SECONDS = 1.0


class UsbPortError(Exception):
    pass


def _run_uhubctl(args, timeout=10):
    try:
        result = subprocess.run(
            ["uhubctl"] + args, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise UsbPortError(
            "uhubctl not found on PATH -- install the 'uhubctl' package"
        )
    except subprocess.TimeoutExpired:
        raise UsbPortError("uhubctl %s timed out after %ds" % (" ".join(args), timeout))
    if result.returncode != 0:
        raise UsbPortError(
            "uhubctl %s failed (exit %d):\nstdout:\n%s\nstderr:\n%s"
            % (" ".join(args), result.returncode, result.stdout, result.stderr)
        )
    return result.stdout


def _device_name_to_location(name):
    """sysfs device name -> (hub_location, port): "3-7.2" -> ("3-7", "2"),
    "3-10" (on a root port) -> ("3", "10")."""
    if "." in name:
        hub, port = name.rsplit(".", 1)
    else:
        hub, port = name.split("-", 1)
    return hub, port


def find_device_port(serial, sysfs=SYSFS_USB_DEVICES):
    """Return (hub_location, port) of the USB device with this serial, or
    None if it is not on the bus. Raises if more than one matches."""
    matches = []
    for name in os.listdir(sysfs):
        if ":" in name or not re.match(r"^\d+-[\d.]+$", name):
            continue  # interfaces and root hubs
        try:
            with open(os.path.join(sysfs, name, "serial")) as f:
                if f.read().strip() == serial:
                    matches.append(name)
        except OSError:
            continue
    if len(matches) > 1:
        raise UsbPortError("serial %s matches %d USB devices: %s" % (serial, len(matches), sorted(matches)))
    return _device_name_to_location(matches[0]) if matches else None


def _cache_path(serial, cache_dir):
    return os.path.join(cache_dir, "%s.json" % serial)


def _save_location(serial, location, cache_dir):
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(_cache_path(serial, cache_dir), "w") as f:
            json.dump({"hub": location[0], "port": location[1], "seen": time.time()}, f)
    except OSError:
        pass  # the cache only helps when the device is absent later


def _load_location(serial, cache_dir):
    try:
        with open(_cache_path(serial, cache_dir)) as f:
            d = json.load(f)
        return d["hub"], d["port"]
    except (OSError, ValueError, KeyError):
        return None


def remember_port(serial, sysfs=SYSFS_USB_DEVICES, cache_dir=PORT_CACHE_DIR):
    """Record where `serial` is right now (if present). Cheap; call it
    whenever the device is known good so a later recovery can find its
    port even after the device has dropped off the bus."""
    try:
        location = find_device_port(serial, sysfs)
    except UsbPortError:
        return None
    if location:
        _save_location(serial, location, cache_dir)
    return location


_HUB_HEADER_RE = re.compile(r"^Current status for hub (\S+) \[(.*)\]")
_PORT_RE = re.compile(r"^\s*Port (\d+): \S+.*?(?:\[([0-9a-f]{4}:[0-9a-f]{4})([^\]]*)\])?\s*$")


def parse_uhubctl_status(text):
    """uhubctl status output -> {hub: {"ppps": bool, "ports": {port: device-or-None}}},
    where a device is "vid:pid description serial" as uhubctl prints it."""
    hubs, current = {}, None
    for line in text.splitlines():
        m = _HUB_HEADER_RE.match(line)
        if m:
            current = {"ppps": "ppps" in m.group(2).split(", ")[-1], "ports": {}}
            hubs[m.group(1)] = current
            continue
        m = _PORT_RE.match(line)
        if m and current is not None:
            current["ports"][m.group(1)] = ("%s%s" % (m.group(2), m.group(3))).strip() if m.group(2) else None
    return hubs


def check_port_safe(hub_location, hub_port, expected_serial, status_text=None):
    """Raise UsbPortError unless cutting this port can only affect the
    expected device. `status_text` is uhubctl's status for the port (taken
    from uhubctl when not given)."""
    if status_text is None:
        status_text = _run_uhubctl(["-l", hub_location, "-p", hub_port])
    hubs = parse_uhubctl_status(status_text)
    if not hubs:
        raise UsbPortError("uhubctl reported no hub at %s:\n%s" % (hub_location, status_text))
    for hub, info in hubs.items():
        if not info["ppps"]:
            raise UsbPortError("hub %s has no per-port power switching (ppps); cutting port %s would "
                               "cut every port on it" % (hub, hub_port))
        device = info["ports"].get(hub_port)
        if device is None:
            continue
        for serial, what in PROTECTED_SERIALS.items():
            if serial in device:
                raise UsbPortError("refusing to power-cycle hub %s port %s: it carries the %s (%s), whose "
                                   "port power must never be cut" % (hub, hub_port, what, device))
        if not device.split()[-1:] == [expected_serial]:
            raise UsbPortError("refusing to power-cycle hub %s port %s: expected only device %s there, "
                               "found %r" % (hub, hub_port, expected_serial, device))


def port_for_serial(serial, log=None, sysfs=SYSFS_USB_DEVICES, cache_dir=PORT_CACHE_DIR):
    """(hub_location, port) to power-cycle the device with this serial:
    where it is now, else where it was last seen. Raises UsbPortError if
    neither is known."""
    location = find_device_port(serial, sysfs)
    if location:
        _save_location(serial, location, cache_dir)
        source = "found on the bus"
    else:
        location = _load_location(serial, cache_dir)
        if location is None:
            raise UsbPortError("device %s is not on the USB bus and no previous location is recorded "
                               "(%s); cannot tell which hub port to power-cycle" % (serial, cache_dir))
        source = "not on the bus; using its last recorded location"
    if log is not None:
        log.log("USB_POWER", "device %s: hub %s port %s (%s)" % (serial, location[0], location[1], source))
    return location


def _set_port(hub_location, hub_port, on, log=None):
    action = "on" if on else "off"
    _run_uhubctl(["-l", hub_location, "-p", hub_port, "-a", action])
    if log is not None:
        log.log(
            "USB_POWER",
            "hub %s port %s -> %s" % (hub_location, hub_port, action.upper()),
        )


def _port_on(hub_location, hub_port, log=None):
    _set_port(hub_location, hub_port, True, log)


def _port_off(hub_location, hub_port, log=None):
    _set_port(hub_location, hub_port, False, log)


def power_cycle(hub_location, hub_port, log=None, expected_serial=None):
    """Cut and reapply power to one USB hub port. Unconditional (does not
    check prior state first), same rationale as power_relay.power_cycle:
    identical behavior whether called at the start of a run or as
    recovery from a missing/unresponsive device.

    Does not itself wait for a device to reappear -- see module
    docstring; the caller re-resolves whatever's on the port afterward.

    Refuses (UsbPortError) unless check_port_safe() passes for
    `expected_serial`; there is no way to call it unguarded.
    """
    if not expected_serial:
        raise UsbPortError("power_cycle needs expected_serial so the port can be checked first")
    check_port_safe(hub_location, hub_port, expected_serial)
    if log is not None:
        log.log("USB_POWER", "power-cycling USB hub port %s-%s" % (hub_location, hub_port))
    _port_off(hub_location, hub_port, log)
    time.sleep(POWER_OFF_SETTLE_SECONDS)
    _port_on(hub_location, hub_port, log)
    time.sleep(POWER_ON_SETTLE_SECONDS)
