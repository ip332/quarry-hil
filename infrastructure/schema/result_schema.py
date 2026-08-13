"""
HIL result JSON schema (version 1) — shared contract across all boards.

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


def validate_hil_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate a HIL result JSON object against schema version 1.
    
    Raises ValueError if validation fails.
    Returns the validated data dict.
    """
    # Required top-level fields
    required_fields = ["schema_version", "result", "run_dir", "quarry", "firmware", "toolchain", "target"]
    for field in required_fields:
        if field not in data:
            raise ValueError(f"missing required field: {field!r}")
    
    # Schema version
    if data.get("schema_version") != 1:
        raise ValueError(
            f"unsupported schema_version: {data.get('schema_version')} (expected 1)"
        )
    
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
        for field in benchmark_required:
            if field not in benchmark:
                raise ValueError(f"missing required field in benchmark: {field!r}")
        if not isinstance(benchmark.get("validation_failures"), int):
            raise ValueError(f"benchmark.validation_failures must be int, got {type(benchmark.get('validation_failures'))}")
    
    return data


def parse_result_json(json_str: str) -> Dict[str, Any]:
    """
    Parse and validate a HIL result JSON string.
    
    Raises json.JSONDecodeError or ValueError on invalid input.
    """
    data = json.loads(json_str)
    if not isinstance(data, dict):
        raise ValueError(f"HIL result must be a JSON object, got {type(data).__name__}")
    return validate_hil_result(data)
