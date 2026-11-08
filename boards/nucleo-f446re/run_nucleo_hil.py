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
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

_hil_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_hil_root, "infrastructure", "core"))
sys.path.insert(0, os.path.join(_hil_root, "infrastructure", "build"))
from serial_link import SerialLink, SerialTimeout, resolve_stm32_stlink_device  # noqa: E402
import board_specs  # noqa: E402
import firmware_build  # noqa: E402
import quarry_source  # noqa: E402
import result_checks  # noqa: E402
import run_setup  # noqa: E402

STLINK_SERIAL_NUMBER = "0669FF565271525067071140"
BAUD = 115200

FLASH_LOAD_ADDR = "0x08000000"
SYSCLK_HZ = 16000000  # HSI, untouched post-reset default -- see firmware/uart.h

MARKER_BEGIN = "===NUCLEO_HIL_RESULT_BEGIN==="
MARKER_END = "===NUCLEO_HIL_RESULT_END==="
RESULT_PATTERN = re.compile(
    re.escape(MARKER_BEGIN) + r"\s*([0-9a-fA-F]+)\s*" + re.escape(MARKER_END)
)

# Layout of `struct quarry_bench_result` (boards/common/bench_result.h,
# version 2), little-endian. AAPCS and the x86-64 SysV ABI agree on natural
# alignment for these fixed-width types; 4-byte pads appear where a
# uint64_t needs 8-byte alignment. The build cross-checks the total struct
# size against the linked g_result symbol (see check_result_symbol()).
QUARRY_BENCH_MAX_SAMPLES = 500
PAYLOAD_CAPTURE_BYTES = 256
# Version-1 scalar prefix (magic .. heap_bytes), then the version-2 block
# (record_sizeof .. encoded_payload). The three 500-entry sample arrays
# follow and are not transmitted.
RESULT_SCALAR_FORMAT = "<17I4xQ2IQ2IQ3I6I%ds" % PAYLOAD_CAPTURE_BYTES
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
    "record_sizeof", "presence_layout", "quarry_commit_prefix", "build_input_digest",
    "encoded_payload_fnv1a", "encoded_payload_captured_bytes", "encoded_payload",
]
# The firmware transmits only this prefix (offsetof(encode_cycles_samples)),
# not the three 500-entry raw per-iteration sample arrays that follow it in
# the on-device struct. See firmware/main.c's report_result() for why: a
# real on-hardware run streaming the full ~6 KiB struct over this polled,
# flow-control-free UART was observed to occasionally truncate mid-transfer.
# The raw samples are not needed either: every observed run has shown zero
# variance across all 500 iterations of every loop.
RESULT_WIRE_SIZE = struct.calcsize(RESULT_SCALAR_FORMAT)
assert RESULT_WIRE_SIZE == 404
# Full on-device struct: wire prefix + 3 sample arrays, rounded up to the
# struct's 8-byte alignment.
RESULT_STRUCT_SIZE = (RESULT_WIRE_SIZE + 3 * 4 * QUARRY_BENCH_MAX_SAMPLES + 7) // 8 * 8

FIRMWARE_SPEC = board_specs.BOARDS["nucleo-f446re"]

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
    result = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# STATE: build checks
# ---------------------------------------------------------------------------


def check_result_symbol(elf_path, log):
    """The runner's view of the struct must match what was linked."""
    rc, out, err = run_host(["arm-none-eabi-nm", "-S", elf_path])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[3] == "g_result":
            size = int(parts[1], 16)
            if size != RESULT_STRUCT_SIZE:
                raise InfrastructureError(
                    "g_result is %d bytes in the ELF but the runner expects %d: bench_result.h and "
                    "RESULT_SCALAR_FORMAT have drifted" % (size, RESULT_STRUCT_SIZE)
                )
            log.log("BUILD", "g_result layout check OK (%d bytes, %d-byte wire prefix)" % (size, RESULT_WIRE_SIZE))
            return
    raise InfrastructureError("g_result not found in %s: %s" % (elf_path, err))


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


def collect_result(link, log, elf_path, timeout=15):
    """Returns (result, transport): transport is "uart" normally, "swd" when
    the UART stayed completely silent and the result was read from RAM."""
    log.log("COLLECT_RESULT", "waiting for firmware to stream its result over UART")
    try:
        _match, text = link.wait_for(RESULT_PATTERN, timeout=timeout)
    except SerialTimeout as e:
        if link.snapshot_text():
            raise RecoveryRequired(
                "firmware did not report a result within %ds after reset -- board may be "
                "hung or not running the expected firmware; physical check required: %s" % (timeout, e)
            )
        # Zero bytes at all: seen after every host reboot (2026-09-29,
        # 2026-10-01) -- the firmware ran and transmitted (USART2 TXE/TC
        # set, g_result complete in RAM), but the ST-LINK's VCP bridge
        # forwarded nothing until the USB cable was physically replugged.
        # SWD still works then, so read the same bytes from RAM instead.
        log.log(
            "COLLECT_RESULT",
            "WARNING: UART silent for %ds (ST-LINK VCP bridge stuck? replug the board's USB cable "
            "to restore it) -- falling back to reading g_result over SWD" % timeout,
        )
        return read_result_over_swd(elf_path, log), "swd"

    m = RESULT_PATTERN.search(text)
    hex_blob = m.group(1)
    if len(hex_blob) != RESULT_WIRE_SIZE * 2:
        raise InfrastructureError(
            "result hex blob is %d chars, expected %d (wire prefix is %d bytes): possible "
            "UART corruption/truncation or firmware/runner struct-layout mismatch"
            % (len(hex_blob), RESULT_WIRE_SIZE * 2, RESULT_WIRE_SIZE)
        )

    return parse_result(bytes.fromhex(hex_blob), log), "uart"


def parse_result(raw, log):
    scalar_values = struct.unpack(RESULT_SCALAR_FORMAT, raw)
    result = dict(zip(RESULT_SCALAR_FIELDS, scalar_values))
    captured = min(result["encoded_payload_captured_bytes"], PAYLOAD_CAPTURE_BYTES)
    result["encoded_payload_hex"] = result.pop("encoded_payload")[:captured].hex()
    for key in ("quarry_commit_prefix", "build_input_digest", "encoded_payload_fnv1a"):
        result[key] = "%08x" % result[key]
    log.log(
        "COLLECT_RESULT",
        "parsed result: result_code=%s validation_failures=%s"
        % (result.get("result_code"), result.get("validation_failures")),
    )
    return result


def read_result_over_swd(elf_path, log, timeout=15, poll_seconds=1.0):
    """Read the same RESULT_WIRE_SIZE-byte prefix of g_result the firmware
    streams over UART (report_result() hex-dumps exactly that), straight
    from target RAM via st-flash. Only `counter` differs: it keeps ticking
    after the report, and nothing validates it."""
    rc, nm_out, err = run_host(["arm-none-eabi-nm", elf_path])
    addr = None
    for line in nm_out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "g_result":
            addr = "0x" + parts[0]
    if rc != 0 or addr is None:
        raise InfrastructureError("could not find g_result in %s: rc=%d %s" % (elf_path, rc, err))

    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "g_result.bin")
        while True:
            rc, out, err = run_host(
                ["st-flash", "--serial", STLINK_SERIAL_NUMBER, "read", out_path, addr, str(RESULT_WIRE_SIZE)],
                timeout=15,
            )
            if rc != 0:
                raise InfrastructureError("st-flash read of g_result failed:\nstdout:\n%s\nstderr:\n%s" % (out, err))
            with open(out_path, "rb") as f:
                raw = f.read()
            if len(raw) != RESULT_WIRE_SIZE:
                raise InfrastructureError("st-flash read returned %d bytes, expected %d" % (len(raw), RESULT_WIRE_SIZE))
            state = struct.unpack_from("<I", raw, 8)[0]
            if state == QUARRY_BENCH_STATE_TEST_COMPLETE:
                log.log("COLLECT_RESULT", "read g_result at %s over SWD (state=TEST_COMPLETE)" % addr)
                return parse_result(raw, log)
            if time.monotonic() >= deadline:
                raise RecoveryRequired(
                    "UART silent and g_result (read over SWD) still not TEST_COMPLETE after %ds "
                    "(state=%d) -- board may be hung; physical check required" % (timeout, state)
                )
            time.sleep(poll_seconds)


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
        "record_sizeof": raw_result["record_sizeof"],
        "presence_layout": raw_result["presence_layout"],
        "quarry_commit_prefix": raw_result["quarry_commit_prefix"],
        "build_input_digest": raw_result["build_input_digest"],
        "encoded_payload_fnv1a": raw_result["encoded_payload_fnv1a"],
        "encoded_payload_captured_bytes": raw_result["encoded_payload_captured_bytes"],
        "encoded_payload_hex": raw_result["encoded_payload_hex"],
    }


# ---------------------------------------------------------------------------
# STATE: validate
# ---------------------------------------------------------------------------


def validate_result(benchmark, manifest, log):
    log.log("VALIDATE", "checking result against the build that produced the firmware")
    # No "heartbeat counter > 0" check here, unlike VisionCB: this firmware
    # streams its result immediately after run_benchmark() completes, before
    # its post-report counter-increment loop runs even once, so counter is
    # always 0 by construction.
    checks = result_checks.check_benchmark(benchmark, manifest, require_heartbeat=False)
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


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="NUCLEO-F446RE local HIL runner")
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
        bin_path = manifest["firmware_paths"]["bin"]
        elf_path = manifest["firmware_paths"]["elf"]
        result_doc["firmware"] = fw
        result_doc["toolchain"] = {"compiler": fw["compiler"], "flags": fw["flags"]}
        check_result_symbol(elf_path, log)

        stage = "hardware"
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

        flash_firmware(bin_path, log)
        reset_target(log)

        raw_result, result_doc["result_transport"] = collect_result(link, log, elf_path)
        benchmark = build_benchmark_doc(raw_result)
        result_doc["benchmark"] = benchmark

        stage = "validate"
        validate_result(benchmark, manifest, log)

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
    firmware just built. hardware: device/probe/transport problem.
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
