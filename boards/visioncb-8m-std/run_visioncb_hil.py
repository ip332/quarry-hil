#!/usr/bin/env python3
"""VisionCB Cortex-M4F local HIL runner (Phase 8).

One-command execution of the proven Phase 6/7 manual procedure:

    build -> transfer -> U-Boot -> bootaux -> resume Linux ->
    storage safety -> collect DTCM result -> validate -> JSON

Everything board-specific (serial identity, U-Boot commands, SD/eMMC
device paths, the DTCM physical alias) lives in this file and its
sibling modules under visioncb-m4/hil_runner/ -- Quarry itself is never
made aware of any of this.

Exit codes (stable, documented contract for future CI use):
    0  PASS
    1  TEST_FAILURE          -- board reached a result, but it was a fail
    2  INFRASTRUCTURE_ERROR  -- build/device/transfer/storage-safety problem
    3  RECOVERY_REQUIRED     -- board unresponsive or in an unexpected state;
                                needs a physical power-cycle before retrying
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serial_link import SerialLink, SerialTimeout, resolve_segger_jlink_device  # noqa: E402

SEGGER_SERIAL_NUMBER = "000900003460"
BAUD = 115200

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
# STATE: quarry provenance + build
# ---------------------------------------------------------------------------


def capture_quarry_provenance(quarry_dir, log):
    log.log("BUILD", "capturing Quarry git provenance from %s" % quarry_dir)
    rc, branch, _ = run_host(["git", "branch", "--show-current"], cwd=quarry_dir)
    rc2, commit, _ = run_host(["git", "rev-parse", "HEAD"], cwd=quarry_dir)
    rc3, status, _ = run_host(["git", "status", "--porcelain"], cwd=quarry_dir)
    if rc != 0 or rc2 != 0:
        raise InfrastructureError("failed to read Quarry git state in %s" % quarry_dir)
    dirty = len(status.strip()) > 0
    info = {
        "branch": branch.strip(),
        "commit": commit.strip(),
        "dirty": dirty,
        "status_short": status.strip(),
    }
    log.log(
        "BUILD",
        "Quarry branch=%s commit=%s dirty=%s" % (info["branch"], info["commit"], dirty),
    )
    if dirty:
        log.log("BUILD", "WARNING: Quarry working tree is dirty -- provenance recorded, proceeding")
    return info


def build_firmware(harness_dir, log):
    log.log("BUILD", "running %s/build.sh" % harness_dir)
    rc, out, err = run_host(["./build.sh"], cwd=harness_dir, timeout=180)
    log.log("BUILD", "build.sh exit=%d" % rc)
    if rc != 0:
        raise InfrastructureError("firmware build failed:\nstdout:\n%s\nstderr:\n%s" % (out, err))

    bin_name = None
    for name in os.listdir(harness_dir):
        if name.endswith(".bin"):
            bin_name = name
    if bin_name is None:
        raise InfrastructureError("build.sh succeeded but no .bin artifact found in %s" % harness_dir)
    bin_path = os.path.join(harness_dir, bin_name)
    elf_path = bin_path[:-4] + ".elf"

    sha256 = sha256_file(bin_path)
    bin_size = os.path.getsize(bin_path)

    rc, size_out, _ = run_host(["arm-none-eabi-size", elf_path])
    text = data = bss = None
    if rc == 0:
        lines = size_out.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            text, data, bss = int(parts[0]), int(parts[1]), int(parts[2])

    rc, gcc_version, _ = run_host(["arm-none-eabi-gcc", "--version"])
    compiler_version = gcc_version.splitlines()[0] if rc == 0 else "unknown"

    info = {
        "bin_path": bin_path,
        "bin_name": bin_name,
        "sha256": sha256,
        "bin_bytes": bin_size,
        "text_bytes": text,
        "data_bytes": data,
        "bss_bytes": bss,
        "compiler": compiler_version,
        "flags": "-mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard "
        "-ffreestanding -fno-builtin -nostdlib -nostartfiles -Os -std=c11",
    }
    log.log(
        "BUILD",
        "firmware=%s sha256=%s text=%s data=%s bss=%s bin=%d bytes"
        % (bin_name, sha256[:16], text, data, bss, bin_size),
    )
    return info


# ---------------------------------------------------------------------------
# STATE: device discovery
# ---------------------------------------------------------------------------


def discover_device(log):
    log.log("DISCOVER", "resolving SEGGER J-Link %s via /dev/serial/by-id" % SEGGER_SERIAL_NUMBER)
    try:
        device = resolve_segger_jlink_device(SEGGER_SERIAL_NUMBER)
    except RuntimeError as e:
        raise InfrastructureError(str(e))
    log.log("DISCOVER", "resolved device: %s" % device)
    return device


# ---------------------------------------------------------------------------
# STATE: acquire console / classify current board state
# ---------------------------------------------------------------------------

UBOOT_PATTERN = re.compile(r"u-boot=>\s*$", re.MULTILINE)
LOGIN_PATTERN = re.compile(r"login:\s*$", re.MULTILINE)
# Root shell prompt for an *already* logged-in session (e.g. left over from a
# prior run in this same process, or a prior manual session) -- distinct from
# a fresh "login:" banner, which requires sending credentials.
SHELL_PROMPT_PATTERN = re.compile(r"root@[\w-]+:\S*#\s*$", re.MULTILINE)


def classify_console(link, log, timeout=20):
    """Send one Enter and classify what's on the other end.

    Three real possibilities were observed in practice, not two: a fresh
    U-Boot prompt, a fresh Linux "login:" banner (needs credentials), and
    -- missed by an earlier version of this function, which mis-reported
    a real, valid shell prompt as UNRESPONSIVE -- an *already logged in*
    root shell left over from a previous session. The banner text that
    would normally confirm which OS image this is isn't necessarily still
    in the buffer for that third case, so it's verified directly with a
    safe, read-only command instead of trusted blindly. Never sends
    credentials against an unrecognized system in either case."""
    log.log("ACQUIRE_CONSOLE", "classifying current board state (timeout=%ds)" % timeout)
    link.clear_buffer()
    link.send_raw(b"\r\n")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = link.snapshot_text()
        if UBOOT_PATTERN.search(text):
            log.log("ACQUIRE_CONSOLE", "state=UBOOT")
            return "UBOOT", text
        if LOGIN_PATTERN.search(text):
            if EXPECTED_OS_MARKER in text:
                log.log("ACQUIRE_CONSOLE", "state=LOGIN_PROMPT_EXPECTED (banner contains %r)" % EXPECTED_OS_MARKER)
                return "LOGIN_PROMPT_EXPECTED", text
            log.log(
                "ACQUIRE_CONSOLE",
                "state=LOGIN_PROMPT_UNEXPECTED -- login banner present but does "
                "not contain %r; NOT attempting login" % EXPECTED_OS_MARKER,
            )
            return "LOGIN_PROMPT_UNEXPECTED", text
        if SHELL_PROMPT_PATTERN.search(text):
            log.log("ACQUIRE_CONSOLE", "shell prompt already present -- verifying OS identity before trusting it")
            link.clear_buffer()
            link.send_line("cat /etc/issue")
            time.sleep(1.0)
            verify_text = link.snapshot_text()
            if EXPECTED_OS_MARKER in verify_text:
                log.log("ACQUIRE_CONSOLE", "state=SHELL_LOGGED_IN_EXPECTED (verified %r)" % EXPECTED_OS_MARKER)
                return "SHELL_LOGGED_IN_EXPECTED", verify_text
            log.log(
                "ACQUIRE_CONSOLE",
                "state=SHELL_LOGGED_IN_UNEXPECTED -- shell prompt present but "
                "/etc/issue does not contain %r: %r" % (EXPECTED_OS_MARKER, verify_text),
            )
            return "SHELL_LOGGED_IN_UNEXPECTED", verify_text
        time.sleep(0.2)
    log.log("ACQUIRE_CONSOLE", "state=UNRESPONSIVE (no recognizable prompt within timeout)")
    return "UNRESPONSIVE", link.snapshot_text()


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
        raise TestFailure(
            "bootaux stack/pc mismatch: got stack=%s pc=%s, expected stack=%s pc=%s "
            "(wrong firmware loaded, or build/link inconsistency)"
            % (got_stack, got_pc, expected_stack_hex, expected_pc_hex)
        )
    log.log("START_M4", "confirmed: stack=%s pc=%s matches built ELF exactly" % (got_stack, got_pc))


def uboot_resume_linux(link, log, timeout=40):
    def send(cmd, settle=2.5):
        link.send_line(cmd)
        time.sleep(settle)

    log.log("BOOT_LINUX", "resuming SD Linux boot (fatload Image/dtb, booti)")
    send("fatload mmc 0:1 ${loadaddr} Image")
    send("fatload mmc 0:1 ${fdt_addr} visionsom-8mm-cb-std.dtb")
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


def validate_result(result, log):
    log.log("VALIDATE", "checking magic/version/state/result_code/validation_failures")
    checks = []

    ok_magic = result.get("magic") == "0x51424e31"
    checks.append(("magic", ok_magic, result.get("magic")))

    ok_version = result.get("version") == 1
    checks.append(("version", ok_version, result.get("version")))

    ok_state = result.get("state") == 2
    checks.append(("state (TEST_COMPLETE)", ok_state, result.get("state")))

    ok_result_code = result.get("result_code") == 1
    checks.append(("result_code (PASS)", ok_result_code, result.get("result_code")))

    ok_failures = result.get("validation_failures") == 0
    checks.append(("validation_failures", ok_failures, result.get("validation_failures")))

    counter = result.get("counter", 0)
    ok_heartbeat = counter is not None and counter > 0
    checks.append(("heartbeat counter > 0", ok_heartbeat, counter))

    for name, ok, value in checks:
        log.log("VALIDATE", "%-28s %-4s (value=%s)" % (name, "OK" if ok else "FAIL", value))

    if not all(ok for _, ok, _ in checks):
        failed = [name for name, ok, _ in checks if not ok]
        raise TestFailure("validation failed: %s" % ", ".join(failed))

    log.log("VALIDATE", "all checks passed")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="VisionCB Cortex-M4F local HIL runner")
    parser.add_argument("--quarry-dir", default="/home/igor/work/quarry")
    parser.add_argument("--harness-dir", default="/home/igor/work/visioncb-m4/quarry_bench")
    parser.add_argument("--runs-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs"))
    parser.add_argument("--skip-build", action="store_true", help="reuse existing firmware artifact")
    args = parser.parse_args()

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(args.runs_dir, timestamp)
    os.makedirs(run_dir, exist_ok=True)
    log = Logger(run_dir)
    console_log_path = os.path.join(run_dir, "console.log")

    result_doc = {
        "schema_version": 1,
        "result": "UNKNOWN",
        "run_dir": run_dir,
    }

    link = None
    try:
        quarry_info = capture_quarry_provenance(args.quarry_dir, log)
        result_doc["quarry"] = quarry_info

        if args.skip_build:
            log.log("BUILD", "skip-build requested, reusing existing artifact")
            fw_info = build_firmware(args.harness_dir, log)  # still recomputes checksum/sizes
        else:
            fw_info = build_firmware(args.harness_dir, log)
        result_doc["firmware"] = {
            k: v for k, v in fw_info.items() if k not in ("bin_path",)
        }
        result_doc["toolchain"] = {"compiler": fw_info["compiler"], "flags": fw_info["flags"]}

        rc, nm_out, _ = run_host(["arm-none-eabi-nm", fw_info["bin_path"][:-4] + ".elf"])
        stack_top = pc_entry = None
        for line in nm_out.splitlines():
            if line.endswith(" B __StackTop") or line.endswith("__StackTop"):
                stack_top = "0x" + line.split()[0]
        rc, readelf_out, _ = run_host(["arm-none-eabi-readelf", "-h", fw_info["bin_path"][:-4] + ".elf"])
        for line in readelf_out.splitlines():
            if "Entry point address" in line:
                pc_entry = line.split(":")[1].strip()
        if stack_top is None or pc_entry is None:
            raise InfrastructureError("could not determine expected __StackTop/entry point from build artifacts")
        log.log("BUILD", "expected bootaux banner: stack=%s pc=%s" % (stack_top, pc_entry))

        device = discover_device(log)
        result_doc["target"] = {
            "board": "VisionCB-8M-STD",
            "soc": "NXP i.MX8M Mini",
            "cpu": "Cortex-M4F",
            "clock_hz": 200000000,
            "serial_device": device,
        }

        link = SerialLink(device, baud=BAUD, log_file=console_log_path)

        state, _text = classify_console(link, log)
        if state == "UNRESPONSIVE":
            raise RecoveryRequired("board did not respond to console input -- physical power-cycle required")
        if state in ("LOGIN_PROMPT_UNEXPECTED", "SHELL_LOGGED_IN_UNEXPECTED"):
            raise RecoveryRequired(
                "board is running an unexpected OS image (missing %r marker) -- "
                "not proceeding against an unrecognized system; physical "
                "power-cycle and manual investigation required" % EXPECTED_OS_MARKER
            )
        if state == "UBOOT":
            # Need Linux to transfer firmware (base64 over a shell) -- resume
            # the existing SD image first, without touching the M4 yet.
            log.log("ACQUIRE_CONSOLE", "at U-Boot prompt with no firmware transferred yet -- resuming Linux first")
            uboot_resume_linux(link, log)
            login(link, log)
        elif state == "LOGIN_PROMPT_EXPECTED":
            log.log("ACQUIRE_CONSOLE", "fresh login banner confirmed, logging in")
            login(link, log)
        elif state == "SHELL_LOGGED_IN_EXPECTED":
            log.log("ACQUIRE_CONSOLE", "already logged in with verified OS identity, skipping login")
        else:
            raise InfrastructureError("unhandled console state: %r" % (state,))

        verify_sd_root_and_unmount_emmc(link, log)

        remote_bin_path = transfer_firmware(link, log, fw_info["bin_path"], fw_info["sha256"])
        remote_bin_name = os.path.basename(remote_bin_path)

        reader_source = os.path.join(args.harness_dir, "read_bench_result.c")
        # Reader must be transferred/compiled while Linux is up, before reboot.
        remote_reader_bin = ensure_reader_on_target(link, log, reader_source)

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

        board_result = collect_result(link, log, remote_reader_bin)
        result_doc["benchmark"] = board_result

        validate_result(board_result, log)

        result_doc["result"] = "PASS"
        log.log("PASS", "HIL run PASSED")
        exit_code = 0

    except RunnerError as e:
        result_doc["result"] = type(e).__name__.upper()
        result_doc["error"] = str(e)
        log.log(type(e).__name__.upper(), str(e))
        exit_code = e.exit_code
    except Exception as e:  # noqa: BLE001 -- top-level safety net, must not hang/crash silently
        result_doc["result"] = "INFRASTRUCTURE_ERROR"
        result_doc["error"] = "unexpected exception: %r" % (e,)
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


if __name__ == "__main__":
    sys.exit(main())
