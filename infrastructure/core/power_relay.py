"""USB relay board power control (VisionCB-8M-STD only).

Wraps the `usbrelay` CLI (package `usbrelay`, dcttech USBRelay4-family HID
relay boards) to give the HIL runner actual control over target board
power -- something Phase 8 explicitly did not have (see quarry-hil's
README, "Known limitation": a relay was ordered but never integrated).
This module is that integration.

The relay channel that switches VisionCB's board power is identified as
"<board serial>_<channel>" (the same "KEY=VAL" form the `usbrelay` CLI
itself uses both to set and to report state) -- not a generic parameter,
since only one relay channel is wired to VisionCB today.

The board serial is NOT trusted to stay `RELAY_SERIAL`: these boards keep
it in EEPROM, and on 2026-10-05 the relay spontaneously re-enumerated with
it corrupted ("QAAMZ" -> "9oOMZ"), failing every run since. So the serial
is resolved from usbrelay's own query on every call: `RELAY_SERIAL` if
present, otherwise the one and only relay board on the host (logged as a
warning). More than one board with none matching is an error -- guessing
there could switch the wrong thing.
"""

import re
import subprocess
import time

RELAY_SERIAL = "QAAMZ"
RELAY_CHANNEL = 1

_STATE_LINE = re.compile(r"^(.+)_([0-9]+)=([01])\s*$", re.MULTILINE)

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


def _query():
    """Return ({relay_id: is_on}, query output) for every channel of every
    relay board usbrelay can see."""
    out = _run_usbrelay([])
    states = {"%s_%s" % (m.group(1), m.group(2)): m.group(3) == "1" for m in _STATE_LINE.finditer(out)}
    return states, out


def _resolve_relay_id(log=None):
    states, out = _query()
    serials = sorted({rid.rsplit("_", 1)[0] for rid in states})
    if RELAY_SERIAL in serials:
        serial = RELAY_SERIAL
    elif len(serials) == 1:
        serial = serials[0]
        if log is not None:
            log.log(
                "POWER",
                "WARNING: relay board serial is %r, expected %r (EEPROM serial corrupted?) "
                "-- using it since it is the only relay board present" % (serial, RELAY_SERIAL),
            )
    else:
        raise RelayError(
            "relay board %r not found and %d other board(s) present (%r), refusing to guess; "
            "usbrelay query output: %r" % (RELAY_SERIAL, len(serials), serials, out)
        )
    relay_id = "%s_%d" % (serial, RELAY_CHANNEL)
    if relay_id not in states:
        raise RelayError("relay %r not found in usbrelay query output: %r" % (relay_id, out))
    return relay_id


def _set_state(relay_id, on, log=None):
    desired = "1" if on else "0"
    _run_usbrelay(["%s=%s" % (relay_id, desired)])
    states, out = _query()
    if relay_id not in states:
        raise RelayError("relay %r disappeared from usbrelay query output: %r" % (relay_id, out))
    actual = states[relay_id]
    if actual != on:
        raise RelayError(
            "usbrelay reported relay %r as %s after requesting %s"
            % (relay_id, "ON" if actual else "OFF", "ON" if on else "OFF")
        )
    if log is not None:
        log.log("POWER", "relay %s -> %s (confirmed)" % (relay_id, "ON" if on else "OFF"))


def relay_on(log=None):
    _set_state(_resolve_relay_id(log), True, log)


def relay_off(log=None):
    _set_state(_resolve_relay_id(log), False, log)


def power_cycle(log=None):
    """Cut and reapply board power via the relay, with settle times either
    side. Unconditional (does not check prior state first) so it behaves
    the same whether called at the start of a run or as mid-run recovery
    from an unresponsive board -- both cases want a real, clean power
    transition, not a no-op if the relay happened to already read ON."""
    relay_id = _resolve_relay_id(log)
    if log is not None:
        log.log("POWER", "power-cycling board via relay %s" % relay_id)
    _set_state(relay_id, False, log)
    time.sleep(POWER_OFF_SETTLE_SECONDS)
    _set_state(relay_id, True, log)
    time.sleep(POWER_ON_SETTLE_SECONDS)
