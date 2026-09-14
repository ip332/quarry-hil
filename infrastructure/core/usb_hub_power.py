"""USB hub per-port power control (NUCLEO-F446RE only).

Unlike VisionCB-8M-STD, NUCLEO-F446RE has no separate probe/target power
rail: the on-board ST-LINK/V2.1 and the STM32F446RE it flashes and reads
are both powered from the single USB connection, off one hub port. Where
VisionCB gets an external relay (see power_relay.py) because its board
has its own power input independent of its debug probe's USB link,
NUCLEO's board power and USB link are the same thing -- so cutting that
USB port's power (confirmed to be a genuine VBUS switch, not just a
logical disconnect -- the upstream VIA Labs VL812 hub the ST-LINK sits
behind reports "ppps", per-port power switching support) is a real
power-on reset of the whole board, achieved through the USB hub itself
instead of extra relay hardware.

Wraps the `uhubctl` CLI (package `uhubctl`). Requires the hub's own USB
device node to be readable/writable by this user -- not true by default
(root-only), so a udev rule granting the `plugdev` group access to this
specific hub (idVendor=2109, idProduct=2812) was added on the HIL host;
see quarry-hil's README.

Because the ST-LINK itself disappears and reappears, callers must not
assume its /dev/ttyACM* index survives a power_cycle() -- per
serial_link.py's own rationale, that index is not stable across a
reconnect even though the /dev/serial/by-id path is. Re-resolve the
device (e.g. via serial_link.resolve_stm32_stlink_device) after every
power_cycle() call; this module deliberately doesn't do that resolution
itself, to keep it symmetric with power_relay.py (which also knows
nothing about serial devices -- that stays the runner's job).
"""

import subprocess
import time

HUB_LOCATION = "3-11.2"
HUB_PORT = "3"

# Mirrors power_relay.py's settle times: let the port fully discharge
# before reapplying power so it's a real cold boot, then give the hub a
# moment before a caller starts polling for re-enumeration.
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


def _set_port(on, log=None):
    action = "on" if on else "off"
    _run_uhubctl(["-l", HUB_LOCATION, "-p", HUB_PORT, "-a", action])
    if log is not None:
        log.log(
            "USB_POWER",
            "hub %s port %s -> %s" % (HUB_LOCATION, HUB_PORT, action.upper()),
        )


def port_on(log=None):
    _set_port(True, log)


def port_off(log=None):
    _set_port(False, log)


def power_cycle(log=None):
    """Cut and reapply power to the ST-LINK's USB hub port. Unconditional
    (does not check prior state first), same rationale as
    power_relay.power_cycle: identical behavior whether called at the
    start of a run or as mid-run recovery from an unresponsive board.

    Does not itself wait for the device to reappear -- see module
    docstring; the caller re-resolves the device path afterward."""
    if log is not None:
        log.log("USB_POWER", "power-cycling ST-LINK via USB hub port %s-%s" % (HUB_LOCATION, HUB_PORT))
    port_off(log)
    time.sleep(POWER_OFF_SETTLE_SECONDS)
    port_on(log)
    time.sleep(POWER_ON_SETTLE_SECONDS)
