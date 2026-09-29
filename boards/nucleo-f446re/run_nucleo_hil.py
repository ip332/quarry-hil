#!/usr/bin/env python3
"""NUCLEO-F446RE local HIL runner.

One-command execution of: build -> flash (SWD via ST-LINK/V2.1) -> reset ->
capture the firmware's self-reported result over its UART (ST-LINK VCP) ->
validate -> JSON.

Unlike VisionCB-8M-STD (a Linux-hosted M4 auxiliary core reached over
U-Boot/eMMC/SD boot plumbing), NUCLEO-F446RE is a standalone chip: it has
no OS of its own and no separate "reader" process. The firmware itself
(this board's own firmware/ subdirectory) runs the same Quarry encode/decode/round-trip
workload as VisionCB's harness, then streams its result struct off-device
as raw hex over UART once, framed with begin/end markers -- this script
decodes that hex, computes the same summary statistics the VisionCB
reader would, and applies the same PASS/FAIL contract.

Everything board-specific (ST-LINK identity, flash address, UART framing,
result struct layout) lives in this file -- Quarry itself is never made
aware of any of this. See infrastructure/schema/result_schema.py for the
JSON contract every board's runner (this one included) must emit.

Exit codes (stable, documented contract, shared across all quarry-hil
board runners):
    0  PASS
    1  TEST_FAILURE          -- board reached a result, but it was a fail
    2  INFRASTRUCTURE_ERROR  -- build/device/flash/transfer problem
    3  RECOVERY_REQUIRED     -- board unresponsive after flash+reset;
                                needs a physical check before retrying
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys

_hil_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_hil_root, "infrastructure", "core"))
from serial_link import SerialLink, SerialTimeout, resolve_stm32_stlink_device  # noqa: E402

STLINK_SERIAL_NUMBER = "0669FF565271525067071140"
BAUD = 115200

FLASH_LOAD_ADDR = "0x08000000"
SYSCLK_HZ = 16000000  # HSI, untouched post-reset default -- see nucleo_bench/uart.h

MARKER_BEGIN = "===NUCLEO_HIL_RESULT_BEGIN==="
MARKER_END = "===NUCLEO_HIL_RESULT_END==="
RESULT_PATTERN = re.compile(
    re.escape(MARKER_BEGIN) + r"\s*([0-9a-fA-F]+)\s*" + re.escape(MARKER_END)
)

# Layout of `struct quarry_bench_result` (visioncb-m4/nucleo_bench/bench_result.h),
# little-endian, verified empirically against both a native and an
# arm-none-eabi-gcc compile (sizeof == 6128, identical field offsets --
# AAPCS and the x86-64 SysV ABI agree on natural alignment for these
# fixed-width integer types). 4-byte pads appear wherever a uint64_t
# field needs 8-byte alignment; see the sibling struct definition for the
# authoritative field list.
QUARRY_BENCH_MAX_SAMPLES = 500
# Scalar prefix (magic .. heap_bytes) as one struct format, then the three
# 500-entry sample arrays unpacked separately -- a single flat format+zip
# across scalar field *names* and per-sample array *values* doesn't work
# since each array field expands to 500 positional values, not one.
RESULT_SCALAR_FORMAT = "<17I4xQ2IQ2IQ3I"
RESULT_SCALAR_FIELDS = [
    "magic", "version", "state", "counter",
    "encode_stage", "decode_stage", "validate_stage",
    "quarry_encode_status", "quarry_decode_status", "encoded_bytes", "result_code",
    "dwt_supported", "timing_overhead_cycles",
    "iteration_count", "validation_failures",
    "encode_cycles_min", "encode_cycles_max", "encode_cycles_sum",
    "decode_cycles_min", "decode_cycles_max", "decode_cycles_sum",
    "roundtrip_cycles_min", "roundtrip_cycles_max", "roundtrip_cycles_sum",
    "stack_watermark_bytes", "stack_region_bytes", "heap_bytes",
]
assert struct.calcsize(RESULT_SCALAR_FORMAT) == 124
# The firmware transmits only this 124-byte scalar prefix (magic ..
# heap_bytes), not the three 500-entry raw per-iteration sample arrays
# that follow it in the on-device struct (see nucleo_bench/main.c's
# report_result() for why: a real on-hardware run streaming the full
# ~6 KiB struct over this polled, flow-control-free UART was observed to
# occasionally truncate mid-transfer, and the raw samples aren't needed --
# every observed run has shown zero variance across all 500 iterations of
# every loop, matching the theoretical expectation for TCM/flash execution
# with no cache, no branch predictor, and no interrupts enabled).
RESULT_WIRE_SIZE = 124

QUARRY_BENCH_STATE_TEST_COMPLETE = 2
QUARRY_BENCH_RESULT_PASS = 1


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
    result = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True)
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
    log.log("BUILD", "Quarry branch=%s commit=%s dirty=%s" % (info["branch"], info["commit"], dirty))
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


def discover_console_device(log):
    log.log("DISCOVER", "resolving ST-LINK/V2.1 VCP %s via /dev/serial/by-id" % STLINK_SERIAL_NUMBER)
    try:
        device = resolve_stm32_stlink_device(STLINK_SERIAL_NUMBER)
    except RuntimeError as e:
        raise InfrastructureError(str(e))
    log.log("DISCOVER", "resolved device: %s" % device)
    return device


def require_st_flash(log):
    path = shutil.which("st-flash")
    if path is None:
        raise InfrastructureError(
            "st-flash not found on PATH -- install with 'sudo apt-get install -y stlink-tools'"
        )
    log.log("DISCOVER", "using st-flash: %s" % path)
    return path


# ---------------------------------------------------------------------------
# STATE: flash + reset
# ---------------------------------------------------------------------------


def flash_firmware(bin_path, log, timeout=60):
    log.log("FLASH", "st-flash write %s %s (serial=%s)" % (bin_path, FLASH_LOAD_ADDR, STLINK_SERIAL_NUMBER))
    rc, out, err = run_host(
        ["st-flash", "--serial", STLINK_SERIAL_NUMBER, "write", bin_path, FLASH_LOAD_ADDR],
        timeout=timeout,
    )
    log.log("FLASH", "st-flash write exit=%d" % rc)
    if rc != 0:
        raise InfrastructureError("st-flash write failed:\nstdout:\n%s\nstderr:\n%s" % (out, err))
    if "flash written and verified" not in (out + err).lower():
        raise InfrastructureError(
            "st-flash write did not report a verified write:\nstdout:\n%s\nstderr:\n%s" % (out, err)
        )
    log.log("FLASH", "write verified")


def reset_target(log, timeout=15):
    log.log("RESET", "st-flash reset (serial=%s)" % STLINK_SERIAL_NUMBER)
    rc, out, err = run_host(["st-flash", "--serial", STLINK_SERIAL_NUMBER, "reset"], timeout=timeout)
    log.log("RESET", "st-flash reset exit=%d" % rc)
    if rc != 0:
        raise InfrastructureError("st-flash reset failed:\nstdout:\n%s\nstderr:\n%s" % (out, err))


# ---------------------------------------------------------------------------
# STATE: result collection
# ---------------------------------------------------------------------------


def collect_result(link, log, timeout=15):
    log.log("COLLECT_RESULT", "waiting for firmware to stream its result over UART")
    try:
        _match, text = link.wait_for(RESULT_PATTERN, timeout=timeout)
    except SerialTimeout as e:
        raise RecoveryRequired(
            "firmware did not report a result within %ds after reset -- board may be "
            "hung or not running the expected firmware; physical check required: %s" % (timeout, e)
        )

    m = RESULT_PATTERN.search(text)
    hex_blob = m.group(1)
    if len(hex_blob) != RESULT_WIRE_SIZE * 2:
        raise InfrastructureError(
            "result hex blob is %d chars, expected %d (wire prefix is %d bytes): possible "
            "UART corruption/truncation or firmware/runner struct-layout mismatch"
            % (len(hex_blob), RESULT_WIRE_SIZE * 2, RESULT_WIRE_SIZE)
        )

    raw = bytes.fromhex(hex_blob)
    scalar_values = struct.unpack(RESULT_SCALAR_FORMAT, raw)
    result = dict(zip(RESULT_SCALAR_FIELDS, scalar_values))
    log.log(
        "COLLECT_RESULT",
        "parsed result: result_code=%s validation_failures=%s"
        % (result.get("result_code"), result.get("validation_failures")),
    )
    return result


def stats_for(count, minimum, maximum, total):
    # No median: only the on-device aggregates (min/max/sum) are available
    # here, not the raw per-iteration samples -- see RESULT_WIRE_SIZE's
    # comment for why those aren't transmitted. mean is exact (derived
    # from sum/count, both on-device aggregates); median would require the
    # actual sorted samples and isn't approximated.
    mean = round(total / float(count), 2) if count else 0.0
    return {"min": minimum, "max": maximum, "mean": mean, "sum": total}


def build_benchmark_doc(raw_result):
    n = raw_result["iteration_count"]
    if n > QUARRY_BENCH_MAX_SAMPLES:
        n = QUARRY_BENCH_MAX_SAMPLES
    return {
        "platform": "NUCLEO-F446RE",
        "cpu": "Cortex-M4F",
        "workload": "telemetry",
        "magic": "0x%08x" % raw_result["magic"],
        "version": raw_result["version"],
        "state": raw_result["state"],
        "result_code": raw_result["result_code"],
        "counter": raw_result["counter"],
        "dwt_supported": bool(raw_result["dwt_supported"]),
        "timing_overhead_cycles": raw_result["timing_overhead_cycles"],
        "iterations": n,
        "encoded_bytes": raw_result["encoded_bytes"],
        "validation_failures": raw_result["validation_failures"],
        "encode_cycles": stats_for(
            n, raw_result["encode_cycles_min"], raw_result["encode_cycles_max"], raw_result["encode_cycles_sum"],
        ),
        "decode_cycles": stats_for(
            n, raw_result["decode_cycles_min"], raw_result["decode_cycles_max"], raw_result["decode_cycles_sum"],
        ),
        "roundtrip_cycles": stats_for(
            n, raw_result["roundtrip_cycles_min"], raw_result["roundtrip_cycles_max"], raw_result["roundtrip_cycles_sum"],
        ),
        "stack_watermark_bytes": raw_result["stack_watermark_bytes"],
        "stack_region_bytes": raw_result["stack_region_bytes"],
        "heap_bytes": raw_result["heap_bytes"],
    }


# ---------------------------------------------------------------------------
# STATE: validate
# ---------------------------------------------------------------------------


def validate_result(benchmark, log):
    log.log("VALIDATE", "checking magic/version/state/result_code/validation_failures")
    # No "heartbeat counter > 0" check here, unlike VisionCB: this firmware
    # streams its result immediately after run_benchmark() completes, before
    # its post-report counter-increment loop runs even once, so counter is
    # always 0 by construction -- asserting >0 on it would always fail, and
    # asserting >=0 on an unsigned field would never mean anything.
    checks = [
        ("magic", benchmark.get("magic") == "0x51424e31", benchmark.get("magic")),
        ("version", benchmark.get("version") == 1, benchmark.get("version")),
        ("state (TEST_COMPLETE)", benchmark.get("state") == QUARRY_BENCH_STATE_TEST_COMPLETE, benchmark.get("state")),
        ("result_code (PASS)", benchmark.get("result_code") == QUARRY_BENCH_RESULT_PASS, benchmark.get("result_code")),
        ("validation_failures", benchmark.get("validation_failures") == 0, benchmark.get("validation_failures")),
    ]

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
    parser = argparse.ArgumentParser(description="NUCLEO-F446RE local HIL runner")
    parser.add_argument("--quarry-dir", default="/home/igor/work/quarry")
    parser.add_argument(
        "--harness-dir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmware"),
    )
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

        fw_info = build_firmware(args.harness_dir, log)
        result_doc["firmware"] = {k: v for k, v in fw_info.items() if k not in ("bin_path",)}
        result_doc["toolchain"] = {"compiler": fw_info["compiler"], "flags": fw_info["flags"]}

        require_st_flash(log)
        console_device = discover_console_device(log)
        result_doc["target"] = {
            "board": "NUCLEO-F446RE",
            "soc": "STMicroelectronics STM32F446RE",
            "cpu": "Cortex-M4F",
            "clock_hz": SYSCLK_HZ,
            "serial_device": console_device,
        }

        link = SerialLink(console_device, baud=BAUD, log_file=console_log_path)
        link.clear_buffer()

        flash_firmware(fw_info["bin_path"], log)
        reset_target(log)

        raw_result = collect_result(link, log)
        benchmark = build_benchmark_doc(raw_result)
        result_doc["benchmark"] = benchmark

        validate_result(benchmark, log)

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
