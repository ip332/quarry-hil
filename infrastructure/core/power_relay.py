"""USB relay board power control (VisionCB-8M-STD only).

Wraps the `usbrelay` CLI (package `usbrelay`, dcttech USBRelay4-family HID
relay boards) to give the HIL runner actual control over target board
power -- something Phase 8 explicitly did not have (see quarry-hil's
README, "Known limitation": a relay was ordered but never integrated).
This module is that integration.

The relay channel that switches VisionCB's board power is identified by
`RELAY_ID` ("<board serial>_<channel>", the same "KEY=VAL" form the
`usbrelay` CLI itself uses both to set and to report state) -- not a
generic parameter, since only one relay channel is wired to VisionCB
today.
"""

import re
import subprocess
import time

RELAY_ID = "QAAMZ_1"

_STATE_LINE = re.compile(r"^%s=([01])\s*$" % re.escape(RELAY_ID), re.MULTILINE)

# Empirically, a target board needs its rails to fully discharge before a
# reapplied power-on is a real, clean cold boot rather than a fast
# glitch a switching regulator rides through unnoticed.
POWER_OFF_SETTLE_SECONDS = 3.0
# Give the SoC's own power-on reset and boot ROM time to release control to
# U-Boot before anything (like catching its prompt) depends on it running.
POWER_ON_SETTLE_SECONDS = 1.0


class RelayError(Exception):
    pass


def _run_usbrelay(args, timeout=10):
    try:
        result = subprocess.run(
            ["usbrelay"] + args, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise RelayError(
            "usbrelay not found on PATH -- install the 'usbrelay' package"
        )
    except subprocess.TimeoutExpired:
        raise RelayError("usbrelay %s timed out after %ds" % (" ".join(args), timeout))
    if result.returncode != 0:
        raise RelayError(
            "usbrelay %s failed (exit %d):\nstdout:\n%s\nstderr:\n%s"
            % (" ".join(args), result.returncode, result.stdout, result.stderr)
        )
    return result.stdout


def _query_state():
    out = _run_usbrelay([])
    m = _STATE_LINE.search(out)
    if not m:
        raise RelayError(
            "could not find relay %r in usbrelay query output: %r" % (RELAY_ID, out)
        )
    return m.group(1) == "1"


def _set_state(on, log=None):
    desired = "1" if on else "0"
    _run_usbrelay(["%s=%s" % (RELAY_ID, desired)])
    actual = _query_state()
    if actual != on:
        raise RelayError(
            "usbrelay reported relay %r as %s after requesting %s"
            % (RELAY_ID, "ON" if actual else "OFF", "ON" if on else "OFF")
        )
    if log is not None:
        log.log("POWER", "relay %s -> %s (confirmed)" % (RELAY_ID, "ON" if on else "OFF"))


def relay_on(log=None):
    _set_state(True, log)


def relay_off(log=None):
    _set_state(False, log)


def power_cycle(log=None):
    """Cut and reapply board power via the relay, with settle times either
    side. Unconditional (does not check prior state first) so it behaves
    the same whether called at the start of a run or as mid-run recovery
    from an unresponsive board -- both cases want a real, clean power
    transition, not a no-op if the relay happened to already read ON."""
    if log is not None:
        log.log("POWER", "power-cycling board via relay %s" % RELAY_ID)
    relay_off(log)
    time.sleep(POWER_OFF_SETTLE_SECONDS)
    relay_on(log)
    time.sleep(POWER_ON_SETTLE_SECONDS)
