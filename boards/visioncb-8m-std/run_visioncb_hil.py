#!/usr/bin/env python3
"""VisionCB Cortex-M4F local HIL runner (Phase 8).

One-command execution of the proven Phase 6/7 manual procedure, now with
relay-controlled board power (see power_relay.py):

    power-cycle -> catch U-Boot -> resume Linux -> transfer -> bootaux ->
    resume Linux -> storage safety -> collect DTCM result -> validate -> JSON

Everything board-specific (serial identity, U-Boot commands, SD/eMMC
device paths, the DTCM physical alias) lives in this file, and the M4
harness itself in this board's firmware/ directory (plus boards/common/)
-- Quarry itself is never made aware of any of this. The firmware is
rebuilt from the selected Quarry revision on every run; see
infrastructure/build/firmware_build.py.

Exit codes (stable, documented contract for future CI use):
    0  PASS
    1  TEST_FAILURE          -- board reached a result, but it was a fail
    2  INFRASTRUCTURE_ERROR  -- build/device/transfer/storage-safety problem
    3  RECOVERY_REQUIRED     -- board or J-Link probe still unresponsive/absent
                                after a power-cycle retry; needs physical
                                /hardware investigation
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time

# Import serial_link from infrastructure/core (two levels up from this file)
_hil_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_hil_root, "infrastructure", "core"))
sys.path.insert(0, os.path.join(_hil_root, "infrastructure", "build"))
from serial_link import SerialLink, SerialTimeout, resolve_segger_jlink_device  # noqa: E402
import board_specs  # noqa: E402
import firmware_build  # noqa: E402
import power_relay  # noqa: E402
import quarry_source  # noqa: E402
import result_checks  # noqa: E402
import run_setup  # noqa: E402
import usb_hub_power  # noqa: E402

FIRMWARE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmware")
READER_SOURCE = os.path.join(FIRMWARE_DIR, "read_bench_result.c")

FIRMWARE_SPEC = board_specs.BOARDS["visioncb-8m-std"]

SEGGER_SERIAL_NUMBER = "000900003460"
BAUD = 115200

# The J-Link probe's own USB connection -- separate from board power
# (power_relay.py) -- has been observed to drop off the bus entirely and
# need a real power cycle (what a human was doing by hand: unplug/replug
# the hub) to come back. Same physical hub NUCLEO's ST-LINK sits behind,
# different port -- see infrastructure/core/usb_hub_power.py.
JLINK_HUB_LOCATION = "3-11.2"
JLINK_HUB_PORT = "1"
JLINK_REENUMERATE_TIMEOUT_SECONDS = 15
JLINK_REENUMERATE_POLL_SECONDS = 0.5

SD_ROOT_DEVICE = "/dev/mmcblk0p2"
SD_BOOT_MMC_SPEC = "mmc 0:1"  # U-Boot's own device:partition syntax
EMMC_DEVICE_PREFIX = "mmcblk2"

M4_TCM_LOAD_ADDR = "0x7e0000"
DTCM_PHYS_ADDR = "0x00800000"

EXPECTED_OS_MARKER = "kirkstone"  # substring of the expected boot banner

TARGET_ROOT_DIR = "/root/hil_runner"  # persistent (ext4) location on-target,
# survives reboot unlike /tmp (tmpfs) -- avoids re-transferring the reader
# every run when it hasn't changed.


class RunnerError(Exception):
    exit_code = 2


class TestFailure(RunnerError):
    exit_code = 1


class InfrastructureError(RunnerError):
    exit_code = 2


class RecoveryRequired(RunnerError):
    exit_code = 3


class BindingMismatch(InfrastructureError):
    """The board is not running the firmware this run built."""


RESULT_CODES = {1: "TEST_FAILURE", 2: "INFRASTRUCTURE_ERROR", 3: "RECOVERY_REQUIRED"}


class Logger:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.path = os.path.join(run_dir, "runner.log")
        self._fh = open(self.path, "a")

    def log(self, state, message):
        line = "[%s] %-20s %s" % (
            datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            state,
            message,
        )
        print(line)
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self):
        self._fh.close()


def run_host(cmd, cwd=None, timeout=120):
    result = subprocess.run(
        cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True
    )
    return result.returncode, result.stdout, result.stderr


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# STATE: build checks
# ---------------------------------------------------------------------------


def check_reader_layout(log):
    """The on-target reader carries its own copy of the result struct;
    refuse to run if it has drifted from boards/common/bench_result.h."""
    rc, out, err = run_host([
        "cc", "-std=c11", "-fsyntax-only", "-DQUARRY_BENCH_LAYOUT_CHECK",
        "-I", os.path.join(_hil_root, "boards", "common"), READER_SOURCE,
    ])
    if rc != 0:
        raise InfrastructureError("read_bench_result.c layout check failed:\n%s%s" % (out, err))
    log.log("BUILD", "reader struct layout matches bench_result.h")


# ---------------------------------------------------------------------------
# STATE: device discovery
# ---------------------------------------------------------------------------


def discover_device(log, power_cycle_attempts=2):
    """Resolve the SEGGER J-Link probe via /dev/serial/by-id.

    Tries a plain resolve first -- the probe isn't power-cycled on every
    run, so most runs should find it immediately with no extra delay. Only on failure does this fall back
    to power-cycling the probe's own USB hub port and retrying, the same
    recovery a human was doing by hand (unplug/replug the hub)."""
    log.log("DISCOVER", "resolving SEGGER J-Link %s via /dev/serial/by-id" % SEGGER_SERIAL_NUMBER)
    try:
        device = resolve_segger_jlink_device(SEGGER_SERIAL_NUMBER)
        log.log("DISCOVER", "resolved device: %s" % device)
        return device
    except RuntimeError as e:
        last_error = e
    return recover_probe(log, "probe not found (%s)" % last_error, power_cycle_attempts)


def recover_probe(log, reason, power_cycle_attempts=2):
    """Power-cycle the J-Link's USB hub port and re-resolve it.

    Covers both ways the probe has been seen to fail: dropping off the
    bus entirely (discover_device's fallback), and staying enumerated
    -- /dev/serial/by-id still resolves -- while its firmware is wedged
    and the VCOM delivers zero bytes (seen after a mains power outage;
    J-Link Commander couldn't connect to it either). The relay only
    power-cycles the board, never the probe, so the second case needs
    this even though discovery succeeded."""
    last_error = None
    for attempt in range(1, power_cycle_attempts + 1):
        log.log(
            "DISCOVER",
            "%s -- power-cycle attempt %d/%d: cycling USB hub port %s-%s"
            % (reason, attempt, power_cycle_attempts, JLINK_HUB_LOCATION, JLINK_HUB_PORT),
        )
        usb_hub_power.power_cycle(JLINK_HUB_LOCATION, JLINK_HUB_PORT, log)
        deadline = time.monotonic() + JLINK_REENUMERATE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                device = resolve_segger_jlink_device(SEGGER_SERIAL_NUMBER)
                log.log("DISCOVER", "resolved device: %s (power-cycle attempt %d)" % (device, attempt))
                return device
            except RuntimeError as e:
                last_error = e
                time.sleep(JLINK_REENUMERATE_POLL_SECONDS)
        log.log(
            "DISCOVER",
            "attempt %d/%d: probe still not found after power-cycle: %s" % (attempt, power_cycle_attempts, last_error),
        )

    raise RecoveryRequired(
        "SEGGER J-Link probe not found after %d USB hub port power-cycle attempt(s) (%s): %s"
        % (power_cycle_attempts, reason, last_error)
    )


# ---------------------------------------------------------------------------
# STATE: acquire console / classify current board state
# ---------------------------------------------------------------------------

LOGIN_PATTERN = re.compile(r"login:\s*$", re.MULTILINE)
FATLOAD_DONE_PATTERN = re.compile(r"\d+ bytes read in \d+ ms")

UBOOT_CATCH_TIMEOUT_SECONDS = 15


def acquire_console_via_power_cycle(link, log, attempts=2):
    """Power-cycle the board via the relay and catch its U-Boot prompt
    before autoboot falls through to the eMMC/factory image.

    This replaces the old classify-whatever-state-it's-currently-in entry
    point: without power control, the runner had no way to know whether
    the board was fresh, mid-session, or hung, so it had to distinguish
    U-Boot/login/already-logged-in-shell/unresponsive by inspection. With
    the relay, every run (and every recovery attempt) starts from the
    same known state -- board powered off, then on -- so it only has to
    win the same autoboot-countdown race the README's manual recovery
    procedure describes, now automated with repeated keypresses via
    SerialLink.catch_uboot_prompt instead of a human watching the console.

    Retries the full power-cycle (not just the keypress race) on a missed
    window, since the only way autoboot could have won is a bad power
    transition or console timing, both of which a fresh cycle can fix.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        log.log(
            "ACQUIRE_CONSOLE",
            "power-cycle attempt %d/%d: cycling board power via relay" % (attempt, attempts),
        )
        power_relay.power_cycle(log)
        link.clear_buffer()
        try:
            text = link.catch_uboot_prompt(timeout=UBOOT_CATCH_TIMEOUT_SECONDS)
            log.log("ACQUIRE_CONSOLE", "state=UBOOT (caught after power-cycle attempt %d)" % attempt)
            return text
        except SerialTimeout as e:
            last_error = e
            log.log(
                "ACQUIRE_CONSOLE",
                "attempt %d/%d: did not catch U-Boot prompt within %ds after power-on "
                "(likely autobooted past the window): %s"
                % (attempt, attempts, UBOOT_CATCH_TIMEOUT_SECONDS, e),
            )
    raise RecoveryRequired(
        "did not catch a U-Boot prompt after %d power-cycle attempt(s): %s" % (attempts, last_error)
    )


# ---------------------------------------------------------------------------
# STATE: storage safety
# ---------------------------------------------------------------------------


def verify_sd_root_and_unmount_emmc(link, log, timeout=15):
    log.log("VERIFY_SD_ROOT", "checking findmnt /")
    link.clear_buffer()
    link.send_line("findmnt /")
    time.sleep(1.5)
    text = link.snapshot_text()
    if SD_ROOT_DEVICE not in text:
        raise InfrastructureError(
            "storage safety violation: expected root on %s, findmnt output: %r"
            % (SD_ROOT_DEVICE, text)
        )
    log.log("VERIFY_SD_ROOT", "confirmed root on %s" % SD_ROOT_DEVICE)

    log.log("UNMOUNT_EMMC", "checking for auto-mounted %s* partitions" % EMMC_DEVICE_PREFIX)
    link.clear_buffer()
    link.send_line("mount | grep %s" % EMMC_DEVICE_PREFIX)
    time.sleep(1.5)
    text = link.snapshot_text()
    mounted = [ln for ln in text.splitlines() if EMMC_DEVICE_PREFIX in ln and " on " in ln]
    if mounted:
        log.log("UNMOUNT_EMMC", "found mounted eMMC partitions, unmounting: %s" % mounted)
        link.clear_buffer()
        link.clear_buffer()
        link.send_line(
            "umount /run/media/root-%sp2 /run/media/boot-%sp1"
            % (EMMC_DEVICE_PREFIX, EMMC_DEVICE_PREFIX)
        )
        time.sleep(1.5)
        log.log("UNMOUNT_EMMC", "umount output: %r" % link.snapshot_text())

    link.clear_buffer()
    link.send_line("mount | grep %s" % EMMC_DEVICE_PREFIX)
    time.sleep(1.5)
    text = link.snapshot_text()
    still_mounted = [ln for ln in text.splitlines() if EMMC_DEVICE_PREFIX in ln and " on " in ln]
    if still_mounted:
        raise InfrastructureError(
            "storage safety violation: eMMC still mounted after unmount attempt: %s"
            % still_mounted
        )
    log.log("UNMOUNT_EMMC", "confirmed no %s* partitions mounted" % EMMC_DEVICE_PREFIX)


# ---------------------------------------------------------------------------
# STATE: firmware transfer + verify
# ---------------------------------------------------------------------------


def transfer_firmware(link, log, bin_path, expected_sha256, timeout=60):
    remote_name = os.path.basename(bin_path)
    remote_path = "/run/media/boot-mmcblk0p1/%s" % remote_name

    log.log("TRANSFER", "checking whether %s already present with matching checksum" % remote_path)
    link.clear_buffer()
    link.send_line("sha256sum %s 2>/dev/null" % remote_path)
    time.sleep(1.5)
    text = link.snapshot_text()
    if expected_sha256 in text:
        log.log("TRANSFER", "byte-identical firmware already present, skipping re-transfer")
        return remote_path

    log.log("TRANSFER", "transferring %s (%d bytes) via base64 over console" % (bin_path, os.path.getsize(bin_path)))
    with open(bin_path, "rb") as f:
        import base64

        b64 = base64.b64encode(f.read()).decode("ascii")
    lines = [b64[i : i + 76] for i in range(0, len(b64), 76)]

    link.clear_buffer()
    link.send_line("base64 -d > %s << 'B64EOF'" % remote_path)
    time.sleep(0.2)
    for line in lines:
        link.send_line(line)
        time.sleep(0.03)
    link.send_line("B64EOF")
    time.sleep(1.0)

    link.clear_buffer()
    link.send_line("sha256sum %s" % remote_path)
    try:
        link.wait_for(re.compile(re.escape(expected_sha256)), timeout=timeout)
    except SerialTimeout as e:
        raise InfrastructureError("firmware checksum verification failed after transfer: %s" % e)

    log.log("TRANSFER", "checksum verified on-device: %s" % expected_sha256[:16])
    return remote_path


# ---------------------------------------------------------------------------
# STATE: U-Boot sequence (load M4, start it, resume Linux)
# ---------------------------------------------------------------------------


def uboot_load_and_start_m4(link, log, remote_bin_name, expected_stack_hex, expected_pc_hex, timeout=20):
    def send(cmd, settle=2.0):
        link.send_line(cmd)
        time.sleep(settle)

    log.log("LOAD_M4", "mmc dev 0 / mmc rescan")
    send("mmc dev 0")
    send("mmc rescan")

    log.log("LOAD_M4", "fatload %s" % remote_bin_name)
    link.clear_buffer()
    send("fatload %s ${loadaddr} %s" % (SD_BOOT_MMC_SPEC, remote_bin_name))
    text = link.snapshot_text()
    if "bytes read" not in text:
        raise InfrastructureError("fatload did not report bytes read: %r" % text)

    send("cp.b ${loadaddr} %s ${filesize}" % M4_TCM_LOAD_ADDR)

    log.log("START_M4", "bootaux %s" % M4_TCM_LOAD_ADDR)
    link.clear_buffer()
    send("bootaux %s" % M4_TCM_LOAD_ADDR, settle=2.5)
    text = link.snapshot_text()
    m = re.search(r"stack = (0x[0-9A-Fa-f]+), pc = (0x[0-9A-Fa-f]+)", text)
    if not m:
        raise InfrastructureError("bootaux did not print expected stack/pc banner: %r" % text)
    got_stack, got_pc = m.group(1).lower(), m.group(2).lower()
    if got_stack != expected_stack_hex.lower() or got_pc != expected_pc_hex.lower():
        # Not a codec verdict: the board is about to run something other
        # than the firmware just built, so this is a binding failure.
        raise BindingMismatch(
            "bootaux stack/pc mismatch: got stack=%s pc=%s, expected stack=%s pc=%s "
            "(wrong firmware loaded, or build/link inconsistency)"
            % (got_stack, got_pc, expected_stack_hex, expected_pc_hex)
        )
    log.log("START_M4", "confirmed: stack=%s pc=%s matches built ELF exactly" % (got_stack, got_pc))


def uboot_resume_linux(link, log, timeout=40):
    def send(cmd, settle=2.5):
        link.send_line(cmd)
        time.sleep(settle)

    # Wait for each load to finish rather than a fixed settle: the ~27MB
    # Image read has been seen to take 2645ms, longer than send()'s 2.5s,
    # and U-Boot drops characters typed while it's busy -- the dtb fatload
    # got mangled, booti then failed with "Could not find a valid device tree".
    def fatload(addr, name, load_timeout=20):
        link.clear_buffer()
        link.send_line("fatload mmc 0:1 %s %s" % (addr, name))
        try:
            link.wait_for(FATLOAD_DONE_PATTERN, timeout=load_timeout)
        except SerialTimeout as e:
            raise RecoveryRequired("fatload %s did not complete within %ds: %s" % (name, load_timeout, e))
        time.sleep(0.5)  # let U-Boot print its prompt before the next command

    log.log("BOOT_LINUX", "resuming SD Linux boot (fatload Image/dtb, booti)")
    fatload("${loadaddr}", "Image")
    fatload("${fdt_addr}", "visionsom-8mm-cb-std.dtb")
    send("setenv bootargs console=${console} root=/dev/mmcblk0p2 rootwait rw")
    link.clear_buffer()
    link.send_line("booti ${loadaddr} - ${fdt_addr}")

    log.log("WAIT_FOR_LINUX", "waiting up to %ds for login prompt" % timeout)
    try:
        _match, text = link.wait_for(LOGIN_PATTERN, timeout=timeout)
    except SerialTimeout as e:
        if "Synchronous Abort" in link.snapshot_text():
            log.log("WAIT_FOR_LINUX", "A53 Synchronous Abort detected during resume -- see log for full console output")
        raise RecoveryRequired("Linux did not reach login prompt within %ds: %s" % (timeout, e))

    if "Synchronous Abort" in text:
        log.log("WAIT_FOR_LINUX", "WARNING: 'Synchronous Abort' text present in boot log even though login was reached -- recording, not treating as fatal since login succeeded")

    if EXPECTED_OS_MARKER not in text:
        raise InfrastructureError(
            "reached a login prompt but banner does not contain %r -- unexpected OS image booted"
            % EXPECTED_OS_MARKER
        )
    log.log("WAIT_FOR_LINUX", "login prompt reached, banner confirms %r" % EXPECTED_OS_MARKER)


def login(link, log, timeout=15):
    log.log("LOGIN", "logging in as root")
    link.clear_buffer()
    link.send_line("root")
    time.sleep(1.0)
    link.send_line("")
    try:
        link.wait_for(re.compile(r"#\s*$", re.MULTILINE), timeout=timeout)
    except SerialTimeout as e:
        raise InfrastructureError("login did not reach a shell prompt: %s" % e)
    log.log("LOGIN", "shell prompt acquired")


# ---------------------------------------------------------------------------
# STATE: result collection
# ---------------------------------------------------------------------------


def ensure_reader_on_target(link, log, reader_source_path, timeout=30):
    local_sha = sha256_file(reader_source_path)
    remote_marker = "%s/read_bench_result.sha256" % TARGET_ROOT_DIR
    remote_src = "%s/read_bench_result.c" % TARGET_ROOT_DIR
    remote_bin = "%s/read_bench_result" % TARGET_ROOT_DIR

    link.clear_buffer()
    link.send_line("mkdir -p %s && cat %s 2>/dev/null" % (TARGET_ROOT_DIR, remote_marker))
    time.sleep(1.0)
    text = link.snapshot_text()
    if local_sha in text:
        log.log("COLLECT_RESULT", "reader already present on-target with matching checksum, reusing")
        return remote_bin

    log.log("COLLECT_RESULT", "transferring reader source (%s)" % local_sha[:16])
    with open(reader_source_path, "r") as f:
        source = f.read()

    link.clear_buffer()
    link.send_line("cat > %s << 'C_EOF'" % remote_src)
    time.sleep(0.2)
    for line in source.splitlines():
        link.send_line(line)
        time.sleep(0.03)
    link.send_line("C_EOF")
    time.sleep(1.0)

    log.log("COLLECT_RESULT", "compiling reader on-target")
    link.clear_buffer()
    link.send_line(
        "cc -Wall -Wextra -O2 -o %s %s && echo READER_COMPILE_OK" % (remote_bin, remote_src)
    )
    try:
        link.wait_for(re.compile("READER_COMPILE_OK"), timeout=timeout)
    except SerialTimeout as e:
        raise InfrastructureError("reader compile failed on-target: %s" % e)

    link.send_line("echo %s > %s" % (local_sha, remote_marker))
    time.sleep(0.5)
    log.log("COLLECT_RESULT", "reader compiled and marker written")
    return remote_bin


def collect_result(link, log, remote_bin, timeout=15):
    log.log("COLLECT_RESULT", "running on-target reader")
    link.clear_buffer()
    marker_begin = "===HIL_JSON_BEGIN==="
    marker_end = "===HIL_JSON_END==="
    # Sent as three separate short lines rather than one chained `a && b && c`
    # command: a long single line hit a transient serial corruption once
    # (a stray byte landed mid-command), matching similar short glitches
    # seen occasionally in manual sessions too. Shorter individual lines,
    # each with time to be echoed/processed before the next is sent, is
    # the same pattern already proven reliable for every other multi-step
    # interaction in this runner.
    link.send_line("echo %s" % marker_begin)
    time.sleep(0.3)
    link.send_line(remote_bin)
    time.sleep(0.3)
    link.send_line("echo %s" % marker_end)
    try:
        link.wait_for(re.compile(re.escape(marker_end)), timeout=timeout)
    except SerialTimeout as e:
        raise InfrastructureError("timed out waiting for reader output: %s" % e)

    text = link.snapshot_text()
    start = text.find(marker_begin)
    end = text.find(marker_end)
    if start == -1 or end == -1 or end <= start:
        raise InfrastructureError("could not locate reader JSON markers in console output: %r" % text)
    blob = text[start + len(marker_begin) : end]

    json_start = blob.find("{")
    json_end = blob.rfind("}")
    if json_start == -1 or json_end == -1:
        raise InfrastructureError("no JSON object found in reader output: %r" % blob)
    json_text = blob[json_start : json_end + 1]
    try:
        result = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise InfrastructureError("reader output was not valid JSON: %s\nraw: %r" % (e, json_text))

    log.log("COLLECT_RESULT", "parsed result: result_code=%s validation_failures=%s" % (
        result.get("result_code"), result.get("validation_failures")))
    return result


# ---------------------------------------------------------------------------
# STATE: validate
# ---------------------------------------------------------------------------


def validate_result(result, manifest, log):
    log.log("VALIDATE", "checking result against the build that produced the firmware")
    checks = result_checks.check_benchmark(result, manifest, require_heartbeat=True)
    for name, ok, value, domain in checks:
        log.log("VALIDATE", "%-38s %-4s (value=%s)" % (name, "OK" if ok else "FAIL", value))
    ok, failed, domain = result_checks.summarize(checks)
    if not ok:
        if domain == "binding":
            raise BindingMismatch(
                "board did not run the firmware just built (%s) -- result discarded" % ", ".join(failed)
            )
        raise TestFailure("validation failed: %s" % ", ".join(failed))
    log.log("VALIDATE", "all checks passed")


def run_hardware_sequence(link, log, fw_info, stack_top, pc_entry, args):
    """One full attempt at the board sequence: power-cycle, resume Linux,
    transfer firmware, run the M4 workload, collect the result.

    Raises RecoveryRequired if the board never becomes responsive at any
    step; the caller decides whether to retry (see main()'s single
    power-cycle retry below)."""
    acquire_console_via_power_cycle(link, log)
    log.log("ACQUIRE_CONSOLE", "at U-Boot prompt with no firmware transferred yet -- resuming Linux first")
    uboot_resume_linux(link, log)
    login(link, log)

    verify_sd_root_and_unmount_emmc(link, log)

    remote_bin_path = transfer_firmware(link, log, fw_info["bin_path"], fw_info["sha256"])
    remote_bin_name = os.path.basename(remote_bin_path)

    # Reader must be transferred/compiled while Linux is up, before reboot.
    remote_reader_bin = ensure_reader_on_target(link, log, READER_SOURCE)

    log.log("ENTER_UBOOT", "issuing reboot from live shell")
    link.clear_buffer()
    link.send_line("reboot")
    try:
        link.catch_uboot_prompt(timeout=30)
    except SerialTimeout as e:
        raise RecoveryRequired("could not catch U-Boot prompt after reboot: %s" % e)
    log.log("ENTER_UBOOT", "U-Boot prompt caught")

    uboot_load_and_start_m4(link, log, remote_bin_name, stack_top, pc_entry)
    uboot_resume_linux(link, log)
    login(link, log)
    verify_sd_root_and_unmount_emmc(link, log)

    return collect_result(link, log, remote_reader_bin)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="VisionCB Cortex-M4F local HIL runner")
    run_setup.add_source_args(parser)
    parser.add_argument("--runs-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs"))
    args = parser.parse_args()

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(args.runs_dir, "%s-%s" % (timestamp, args.presence_layout))
    os.makedirs(run_dir, exist_ok=True)
    log = Logger(run_dir)
    console_log_path = os.path.join(run_dir, "console.log")

    result_doc = {
        "schema_version": 2,
        "result": "UNKNOWN",
        "run_dir": run_dir,
        "presence_layout": args.presence_layout,
    }

    link = None
    stage = "build"
    try:
        try:
            provenance, manifest = run_setup.prepare_firmware(args, FIRMWARE_SPEC, log)
        except (quarry_source.SourceError, firmware_build.BuildError) as e:
            raise InfrastructureError(str(e))
        except firmware_build.CodecCheckError as e:
            raise TestFailure(str(e))
        finally:
            run_setup.copy_build_evidence(args, FIRMWARE_SPEC, run_dir)
        result_doc.update(provenance)
        fw = manifest["firmware"]
        result_doc["firmware"] = fw
        result_doc["toolchain"] = {"compiler": fw["compiler"], "flags": fw["flags"]}
        fw_info = {"bin_path": manifest["firmware_paths"]["bin"], "sha256": fw["sha256"]}
        check_reader_layout(log)

        stack_top, pc_entry = fw["stack_top"], fw["entry_point"]
        if stack_top is None or pc_entry is None:
            raise InfrastructureError("could not determine expected __StackTop/entry point from build artifacts")
        log.log("BUILD", "expected bootaux banner: stack=%s pc=%s" % (stack_top, pc_entry))

        stage = "hardware"
        device = discover_device(log)
        result_doc["target"] = {
            "board": "VisionCB-8M-STD",
            "soc": "NXP i.MX8M Mini",
            "cpu": "Cortex-M4F",
            "clock_hz": 200000000,
            "serial_device": device,
        }

        link = SerialLink(device, baud=BAUD, log_file=console_log_path)

        try:
            board_result = run_hardware_sequence(link, log, fw_info, stack_top, pc_entry, args)
        except RecoveryRequired as e:
            # The board was unresponsive somewhere in the sequence -- the
            # one case this relay integration exists for. Retry the whole
            # sequence exactly once (its own first step is itself a fresh
            # power-cycle) rather than failing straight to RECOVERY_REQUIRED;
            # only a board that's still unresponsive after a clean power
            # transition really needs the physical check that error implies.
            # The probe is power-cycled too: an enumerated-but-wedged J-Link
            # looks exactly like a silent board from here (see recover_probe).
            log.log(
                "RECOVERY",
                "hardware sequence was unresponsive (%s) -- retrying once via probe + board power-cycle" % e,
            )
            link.close()
            link = None
            device = recover_probe(log, "retrying after unresponsive hardware sequence")
            result_doc["target"]["serial_device"] = device
            link = SerialLink(device, baud=BAUD, log_file=console_log_path)
            board_result = run_hardware_sequence(link, log, fw_info, stack_top, pc_entry, args)

        result_doc["benchmark"] = board_result

        stage = "validate"
        validate_result(board_result, manifest, log)

        result_doc["result"] = "PASS"
        log.log("PASS", "HIL run PASSED")
        exit_code = 0

    except RunnerError as e:
        # Schema result codes (infrastructure/schema/result_schema.py), keyed
        # by the exit-code contract -- not the exception class name, which
        # used to leak out as e.g. "INFRASTRUCTUREERROR".
        result_doc["result"] = RESULT_CODES[e.exit_code]
        result_doc["error"] = str(e)
        result_doc["failure_domain"] = failure_domain(e, stage)
        log.log(result_doc["result"], "[%s] %s" % (result_doc["failure_domain"], e))
        exit_code = e.exit_code
    except Exception as e:  # noqa: BLE001 -- top-level safety net, must not hang/crash silently
        result_doc["result"] = "INFRASTRUCTURE_ERROR"
        result_doc["error"] = "unexpected exception: %r" % (e,)
        result_doc["failure_domain"] = "infrastructure"
        log.log("INFRASTRUCTURE_ERROR", "unexpected exception: %r" % (e,))
        exit_code = 2
    finally:
        if link is not None:
            link.close()
        result_path = os.path.join(run_dir, "result.json")
        with open(result_path, "w") as f:
            json.dump(result_doc, f, indent=2, default=str)
        log.log("DONE", "result written to %s, exit_code=%d" % (result_path, exit_code))
        log.close()

    print(json.dumps(result_doc, indent=2, default=str))
    return exit_code


def failure_domain(error, stage):
    """codec: Quarry itself misbehaved. binding: the board did not run the
    firmware just built. hardware: device/probe/boot/transport problem.
    infrastructure: source selection or build problem."""
    if isinstance(error, BindingMismatch):
        return "binding"
    if isinstance(error, TestFailure):
        return "codec"
    if isinstance(error, RecoveryRequired):
        return "hardware"
    if stage == "build":
        return "infrastructure"
    return "hardware"


if __name__ == "__main__":
    sys.exit(main())
