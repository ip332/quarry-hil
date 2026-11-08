"""Tests for the shared result checks and the NUCLEO wire format.

Run with: python3 -m pytest tests/ -v  (or python3 tests/test_result_checks.py)
"""

import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "infrastructure", "build"))
sys.path.insert(0, os.path.join(ROOT, "infrastructure", "core"))

import result_checks  # noqa: E402


def _load_nucleo_runner():
    path = os.path.join(ROOT, "boards", "nucleo-f446re", "run_nucleo_hil.py")
    spec = importlib.util.spec_from_file_location("run_nucleo_hil", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PAYLOAD = bytes(range(1, 89))


def _manifest(layout="default", payload=PAYLOAD):
    return {
        "presence_layout": layout,
        "quarry": {"commit": "115e345223d31e3ea94b842c2173f740223fd4c0"},
        "input_digest": "f0d75093" + "0" * 56,
        "host_layout_gate": {layout: {"payload_hex": payload.hex()}},
    }


def _benchmark(layout_code=0, payload=PAYLOAD):
    return {
        "magic": "0x51424e31", "version": 2, "state": 2, "result_code": 1, "validation_failures": 0,
        "iterations": 500, "record_sizeof": 96, "counter": 0, "presence_layout": layout_code,
        "quarry_commit_prefix": "115e3452", "build_input_digest": "f0d75093",
        "encoded_bytes": len(payload), "encoded_payload_captured_bytes": len(payload),
        "encoded_payload_hex": payload.hex(), "encoded_payload_fnv1a": "%08x" % result_checks.fnv1a32(payload),
    }


def test_fnv1a_reference_values():
    assert result_checks.fnv1a32(b"") == 0x811C9DC5
    assert result_checks.fnv1a32(b"a") == 0xE40C292C


def test_matching_result_passes():
    ok, failed, _ = result_checks.summarize(result_checks.check_benchmark(_benchmark(), _manifest()))
    assert ok, failed


def test_wrong_layout_is_binding_failure():
    ok, failed, domain = result_checks.summarize(result_checks.check_benchmark(_benchmark(layout_code=1), _manifest()))
    assert not ok and domain == "binding" and "presence_layout matches build" in failed


def test_stale_digest_is_binding_failure():
    b = _benchmark()
    b["build_input_digest"] = "deadbeef"
    ok, _, domain = result_checks.summarize(result_checks.check_benchmark(b, _manifest()))
    assert not ok and domain == "binding"


def test_payload_divergence_is_codec_failure():
    other = PAYLOAD[:-1] + b"\x00"
    ok, failed, domain = result_checks.summarize(result_checks.check_benchmark(_benchmark(payload=other), _manifest()))
    assert not ok and domain == "codec" and failed == ["payload equals host-native encoding"]


def test_validation_failures_are_codec_failure():
    b = _benchmark()
    b["validation_failures"] = 3
    ok, _, domain = result_checks.summarize(result_checks.check_benchmark(b, _manifest()))
    assert not ok and domain == "codec"


def test_nucleo_wire_parse_roundtrip():
    runner = _load_nucleo_runner()
    values = [0x51424E31, 2, 2, 0, 1, 1, 1, 0, 0, len(PAYLOAD), 1, 1, 9, 500, 0,
              100, 120, 55000, 200, 220, 105000, 300, 330, 160000, 700, 4096, 0,
              96, 0, 0x115E3452, 0xF0D75093, result_checks.fnv1a32(PAYLOAD), len(PAYLOAD),
              PAYLOAD + b"\x00" * (256 - len(PAYLOAD))]
    raw = struct.pack(runner.RESULT_SCALAR_FORMAT, *values)

    class _Log:
        def log(self, *a):
            pass

    parsed = runner.parse_result(raw, _Log())
    doc = runner.build_benchmark_doc(parsed)
    ok, failed, _ = result_checks.summarize(result_checks.check_benchmark(doc, _manifest()))
    assert ok, failed
    assert doc["encode_cycles"]["mean"] == 110.0


def test_nucleo_wire_format_matches_c_header():
    """offsetof(encode_cycles_samples) and sizeof(struct) from the real
    header must equal the runner's RESULT_WIRE_SIZE / RESULT_STRUCT_SIZE."""
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        print("skipping: no host C compiler")
        return
    runner = _load_nucleo_runner()
    src = ('#include <stddef.h>\n#include <stdio.h>\n#include "bench_result.h"\n'
           'int main(void){printf("%zu %zu\\n", offsetof(struct quarry_bench_result, encode_cycles_samples),'
           ' sizeof(struct quarry_bench_result)); return 0;}\n')
    with tempfile.TemporaryDirectory() as tmp:
        c_path, exe = os.path.join(tmp, "layout.c"), os.path.join(tmp, "layout")
        with open(c_path, "w") as f:
            f.write(src)
        subprocess.run([cc, "-std=c11", "-I", os.path.join(ROOT, "boards", "common"), "-o", exe, c_path], check=True)
        prefix, total = map(int, subprocess.run([exe], capture_output=True, text=True, check=True).stdout.split())
    assert prefix == runner.RESULT_WIRE_SIZE, (prefix, runner.RESULT_WIRE_SIZE)
    assert total == runner.RESULT_STRUCT_SIZE, (total, runner.RESULT_STRUCT_SIZE)


def test_visioncb_reader_layout_check_compiles():
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        print("skipping: no host C compiler")
        return
    subprocess.run([cc, "-std=c11", "-fsyntax-only", "-DQUARRY_BENCH_LAYOUT_CHECK",
                    "-I", os.path.join(ROOT, "boards", "common"),
                    os.path.join(ROOT, "boards", "visioncb-8m-std", "firmware", "read_bench_result.c")], check=True)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("✓ All result-check tests passed")
