"""
HIL result JSON schema — shared contract across all boards.

Version 1: original layout (historical runs).
Version 2: adds quarry-hil provenance (`hil`), build provenance (`build`),
`presence_layout`, and `failure_domain` on non-PASS results. A non-PASS
version-2 result may stop before firmware/target information exists, so
only the envelope is required then.

This schema defines the structure that every board-specific HIL runner must emit.
All runners produce this JSON, regardless of architecture or toolchain.
"""

import json
from enum import Enum
from typing import Any, Dict, Optional


class ResultCode(str, Enum):
    """HIL runner terminal state codes."""
    PASS = "PASS"
    TEST_FAILURE = "TEST_FAILURE"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


SUPPORTED_SCHEMA_VERSIONS = (1, 2)
PRESENCE_LAYOUTS = ("default", "optimize-size")
FAILURE_DOMAINS = ("codec", "binding", "hardware", "infrastructure")


def validate_hil_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate a HIL result JSON object against schema version 1 or 2.
    
    Raises ValueError if validation fails.
    Returns the validated data dict.
    """
    if "schema_version" not in data:
        raise ValueError("missing required field: 'schema_version'")
    version = data.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"unsupported schema_version: {version} (expected one of {SUPPORTED_SCHEMA_VERSIONS})"
        )
    if version == 2:
        _validate_v2_envelope(data)
        if data.get("result") != ResultCode.PASS.value:
            return data

    # Required top-level fields
    required_fields = ["schema_version", "result", "run_dir", "quarry", "firmware", "toolchain", "target"]
    if version == 2:
        required_fields += ["hil", "build", "benchmark"]
    for field in required_fields:
        if field not in data:
            raise ValueError(f"missing required field: {field!r}")
    
    # Result field
    result = data.get("result")
    valid_results = {e.value for e in ResultCode}
    if result not in valid_results:
        raise ValueError(
            f"invalid result: {result!r} (must be one of {valid_results})"
        )
    
    # Quarry metadata
    quarry = data.get("quarry", {})
    quarry_required = ["branch", "commit", "dirty", "status_short"]
    for field in quarry_required:
        if field not in quarry:
            raise ValueError(f"missing required field in quarry: {field!r}")
    if not isinstance(quarry.get("commit"), str) or len(quarry["commit"]) < 7:
        raise ValueError(f"invalid commit hash: {quarry.get('commit')!r}")
    if not isinstance(quarry.get("dirty"), bool):
        raise ValueError(f"quarry.dirty must be boolean, got {type(quarry.get('dirty'))}")
    
    # Firmware metadata
    firmware = data.get("firmware", {})
    firmware_required = ["bin_name", "sha256", "bin_bytes", "text_bytes", "data_bytes", "bss_bytes", "compiler", "flags"]
    for field in firmware_required:
        if field not in firmware:
            raise ValueError(f"missing required field in firmware: {field!r}")
    if not isinstance(firmware.get("sha256"), str) or len(firmware["sha256"]) != 64:
        raise ValueError(f"invalid firmware sha256: {firmware.get('sha256')!r}")
    for size_field in ["bin_bytes", "text_bytes", "data_bytes", "bss_bytes"]:
        if not isinstance(firmware.get(size_field), int) or firmware[size_field] < 0:
            raise ValueError(f"invalid firmware.{size_field}: {firmware.get(size_field)!r}")
    
    # Toolchain metadata
    toolchain = data.get("toolchain", {})
    toolchain_required = ["compiler", "flags"]
    for field in toolchain_required:
        if field not in toolchain:
            raise ValueError(f"missing required field in toolchain: {field!r}")
    
    # Target metadata
    target = data.get("target", {})
    target_required = ["board", "soc", "cpu", "clock_hz", "serial_device"]
    for field in target_required:
        if field not in target:
            raise ValueError(f"missing required field in target: {field!r}")
    if not isinstance(target.get("clock_hz"), int) or target["clock_hz"] <= 0:
        raise ValueError(f"invalid target.clock_hz: {target.get('clock_hz')!r}")
    
    # Benchmark (optional but if present, must be valid)
    benchmark = data.get("benchmark")
    if benchmark:
        benchmark_required = ["platform", "cpu", "workload", "magic", "version", "state",
                             "result_code", "counter", "dwt_supported", "timing_overhead_cycles",
                             "iterations", "encoded_bytes", "validation_failures"]
        if version == 2:
            benchmark_required += ["record_sizeof", "presence_layout", "encoded_payload_hex",
                                   "encode_cycles", "decode_cycles", "roundtrip_cycles",
                                   "stack_watermark_bytes", "heap_bytes"]
        for field in benchmark_required:
            if field not in benchmark:
                raise ValueError(f"missing required field in benchmark: {field!r}")
        if not isinstance(benchmark.get("validation_failures"), int):
            raise ValueError(f"benchmark.validation_failures must be int, got {type(benchmark.get('validation_failures'))}")

    if version == 2:
        _validate_v2_provenance(data)
    
    return data


def _validate_v2_envelope(data: Dict[str, Any]) -> None:
    for field in ("result", "run_dir", "presence_layout"):
        if field not in data:
            raise ValueError(f"missing required field: {field!r}")
    if data["result"] not in {e.value for e in ResultCode}:
        raise ValueError(f"invalid result: {data['result']!r}")
    if data["presence_layout"] not in PRESENCE_LAYOUTS:
        raise ValueError(f"invalid presence_layout: {data['presence_layout']!r}")
    if data["result"] != ResultCode.PASS.value and data.get("failure_domain") not in FAILURE_DOMAINS:
        raise ValueError(f"non-PASS result needs failure_domain in {FAILURE_DOMAINS}, got {data.get('failure_domain')!r}")


def _validate_v2_provenance(data: Dict[str, Any]) -> None:
    hil = data["hil"]
    for field in ("commit", "dirty"):
        if field not in hil:
            raise ValueError(f"missing required field in hil: {field!r}")
    if not isinstance(hil["commit"], str) or len(hil["commit"]) != 40:
        raise ValueError(f"invalid hil.commit: {hil['commit']!r}")
    if len(data["quarry"]["commit"]) != 40:
        raise ValueError(f"version 2 requires the full 40-hex quarry.commit, got {data['quarry']['commit']!r}")
    build = data["build"]
    for field in ("presence_layout", "generation_options", "input_digest", "schema_compiler",
                  "schema_sha256", "generated_files", "footprint"):
        if field not in build:
            raise ValueError(f"missing required field in build: {field!r}")
    if build["presence_layout"] != data["presence_layout"]:
        raise ValueError("build.presence_layout does not match presence_layout")
    if ("--c-optimize-size" in build["generation_options"]) != (data["presence_layout"] == "optimize-size"):
        raise ValueError("build.generation_options do not match presence_layout")


def parse_result_json(json_str: str) -> Dict[str, Any]:
    """
    Parse and validate a HIL result JSON string.
    
    Raises json.JSONDecodeError or ValueError on invalid input.
    """
    data = json.loads(json_str)
    if not isinstance(data, dict):
        raise ValueError(f"HIL result must be a JSON object, got {type(data).__name__}")
    return validate_hil_result(data)
