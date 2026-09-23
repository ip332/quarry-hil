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

Parameterized by hub location + port (uhubctl's own addressing scheme,
e.g. "3-11.2" / "1") rather than hardcoded to one board, since more than
one board/probe on this host's topology needs this: NUCLEO-F446RE's
on-board ST-LINK *is* the board's power (see run_nucleo_hil.py), and
VisionCB-8M-STD's SEGGER J-Link probe -- a separate USB device from the
board itself, which gets its own power from a relay (power_relay.py) --
has been observed to drop off the bus and require exactly this kind of
power cycle to recover (the fix a human was doing by hand: unplug/replug
the hub). Both happen to sit behind the same physical hub on this host,
just different ports.

Callers must not assume a device's /dev/ttyACM* (or similar) index
survives a power_cycle() -- per serial_link.py's own rationale, that
index is not stable across a reconnect even though a /dev/serial/by-id
path is. Re-resolve the device after every power_cycle() call; this
module deliberately doesn't do that resolution itself, to stay agnostic
of what's actually plugged into the port.
"""

import subprocess
import time

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


def _set_port(hub_location, hub_port, on, log=None):
    action = "on" if on else "off"
    _run_uhubctl(["-l", hub_location, "-p", hub_port, "-a", action])
    if log is not None:
        log.log(
            "USB_POWER",
            "hub %s port %s -> %s" % (hub_location, hub_port, action.upper()),
        )


def port_on(hub_location, hub_port, log=None):
    _set_port(hub_location, hub_port, True, log)


def port_off(hub_location, hub_port, log=None):
    _set_port(hub_location, hub_port, False, log)


def power_cycle(hub_location, hub_port, log=None):
    """Cut and reapply power to one USB hub port. Unconditional (does not
    check prior state first), same rationale as power_relay.power_cycle:
    identical behavior whether called at the start of a run or as
    recovery from a missing/unresponsive device.

    Does not itself wait for a device to reappear -- see module
    docstring; the caller re-resolves whatever's on the port afterward.
    """
    if log is not None:
        log.log("USB_POWER", "power-cycling USB hub port %s-%s" % (hub_location, hub_port))
    port_off(hub_location, hub_port, log)
    time.sleep(POWER_OFF_SETTLE_SECONDS)
    port_on(hub_location, hub_port, log)
    time.sleep(POWER_ON_SETTLE_SECONDS)
