"""
Unit tests for HIL infrastructure: schema validation, JSON parsing, and state logic.

Run with: python3 -m pytest tests/ -v
"""

import json
import sys
import os

# Add infrastructure to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "infrastructure", "schema"))

from result_schema import ResultCode, validate_hil_result, parse_result_json


def minimal_valid_result():
    """Generate a minimal valid HIL result for testing."""
    return {
        "schema_version": 1,
        "result": "PASS",
        "run_dir": "/runs/20260812T231230Z",
        "quarry": {
            "branch": "main",
            "commit": "2767673447ac9752cad67579d83bebe14e0149c7",
            "dirty": False,
            "status_short": ""
        },
        "firmware": {
            "bin_name": "quarry_bench.bin",
            "sha256": "d2c055a762a3c64adcc560ba80f106b4846445a8072d24bd41dc486f3616fdbb",
            "bin_bytes": 5776,
            "text_bytes": 5776,
            "data_bytes": 0,
            "bss_bytes": 11072,
            "compiler": "arm-none-eabi-gcc 10.3.1",
            "flags": "-mcpu=cortex-m4 -mthumb"
        },
        "toolchain": {
            "compiler": "arm-none-eabi-gcc 10.3.1",
            "flags": "-mcpu=cortex-m4 -mthumb"
        },
        "target": {
            "board": "VisionCB-8M-STD",
            "soc": "NXP i.MX8M Mini",
            "cpu": "Cortex-M4F",
            "clock_hz": 200000000,
            "serial_device": "/dev/ttyACM1"
        }
    }


def test_schema_version_required():
    """Schema version is mandatory."""
    data = minimal_valid_result()
    del data["schema_version"]
    try:
        validate_hil_result(data)
        assert False, "should raise ValueError for missing schema_version"
    except ValueError as e:
        assert "schema_version" in str(e)


def test_schema_version_v1_only():
    """Unknown schema versions are rejected (1 and 2 are supported)."""
    data = minimal_valid_result()
    data["schema_version"] = 3
    try:
        validate_hil_result(data)
        assert False, "should raise ValueError for unknown schema_version"
    except ValueError as e:
        assert "unsupported schema_version" in str(e)


def test_result_field_valid_values():
    """Result field must be one of the defined ResultCode values."""
    for code in ResultCode:
        data = minimal_valid_result()
        data["result"] = code.value
        validate_hil_result(data)  # Should not raise
    
    data = minimal_valid_result()
    data["result"] = "UNKNOWN_STATUS"
    try:
        validate_hil_result(data)
        assert False, "should raise ValueError for invalid result"
    except ValueError as e:
        assert "invalid result" in str(e)


def test_commit_hash_validation():
    """Commit hash must be at least 7 chars (short SHA)."""
    data = minimal_valid_result()
    data["quarry"]["commit"] = "abc"  # Too short
    try:
        validate_hil_result(data)
        assert False, "should raise ValueError for short commit hash"
    except ValueError as e:
        assert "invalid commit hash" in str(e)
    
    # Valid short SHA
    data["quarry"]["commit"] = "2767673"
    validate_hil_result(data)  # Should not raise


def test_dirty_flag_type():
    """Quarry.dirty must be boolean."""
    data = minimal_valid_result()
    data["quarry"]["dirty"] = "true"  # String instead of bool
    try:
        validate_hil_result(data)
        assert False, "should raise ValueError for non-boolean dirty"
    except ValueError as e:
        assert "dirty must be boolean" in str(e)


def test_firmware_sha256_validation():
    """Firmware SHA256 must be exactly 64 hex chars."""
    data = minimal_valid_result()
    
    # Too short
    data["firmware"]["sha256"] = "abc123"
    try:
        validate_hil_result(data)
        assert False, "should raise ValueError for invalid sha256"
    except ValueError as e:
        assert "invalid firmware sha256" in str(e)
    
    # Valid SHA256
    data["firmware"]["sha256"] = "a" * 64
    validate_hil_result(data)  # Should not raise


def test_firmware_sizes_valid_integers():
    """Firmware size fields must be non-negative integers."""
    for size_field in ["bin_bytes", "text_bytes", "data_bytes", "bss_bytes"]:
        data = minimal_valid_result()
        data["firmware"][size_field] = -1
        try:
            validate_hil_result(data)
            assert False, f"should raise ValueError for negative {size_field}"
        except ValueError as e:
            assert size_field in str(e)
    
    # Zeros are valid
    data = minimal_valid_result()
    for size_field in ["bin_bytes", "text_bytes", "data_bytes", "bss_bytes"]:
        data["firmware"][size_field] = 0
    validate_hil_result(data)  # Should not raise


def test_target_clock_hz_positive():
    """Target clock_hz must be a positive integer."""
    data = minimal_valid_result()
    data["target"]["clock_hz"] = 0
    try:
        validate_hil_result(data)
        assert False, "should raise ValueError for zero clock_hz"
    except ValueError as e:
        assert "clock_hz" in str(e)
    
    data["target"]["clock_hz"] = -1
    try:
        validate_hil_result(data)
        assert False, "should raise ValueError for negative clock_hz"
    except ValueError as e:
        assert "clock_hz" in str(e)
    
    # Positive is valid
    data["target"]["clock_hz"] = 200000000
    validate_hil_result(data)  # Should not raise


def test_optional_benchmark_field():
    """Benchmark field is optional, but if present must be valid."""
    data = minimal_valid_result()
    if "benchmark" in data:
        del data["benchmark"]
    validate_hil_result(data)  # Should not raise
    
    # If benchmark is present, it must have required fields
    data["benchmark"] = {"platform": "VisionCB-8M-STD"}  # Incomplete
    try:
        validate_hil_result(data)
        assert False, "should raise ValueError for incomplete benchmark"
    except ValueError as e:
        assert "benchmark" in str(e)


def test_json_parsing():
    """Parse and validate JSON string."""
    data = minimal_valid_result()
    json_str = json.dumps(data)
    parsed = parse_result_json(json_str)
    assert parsed == data
    
    # Invalid JSON
    try:
        parse_result_json("{invalid json}")
        assert False, "should raise JSONDecodeError"
    except json.JSONDecodeError:
        pass
    
    # JSON array instead of object
    try:
        parse_result_json("[]")
        assert False, "should raise ValueError for JSON array"
    except ValueError as e:
        assert "must be a JSON object" in str(e)


def test_all_required_fields_present():
    """All required top-level fields must be present."""
    required = ["schema_version", "result", "run_dir", "quarry", "firmware", "toolchain", "target"]
    for field in required:
        data = minimal_valid_result()
        del data[field]
        try:
            validate_hil_result(data)
            assert False, f"should raise ValueError for missing {field}"
        except ValueError as e:
            assert f"missing required field: '{field}'" in str(e)


def test_quarry_all_required_fields():
    """All required fields in quarry metadata must be present."""
    required = ["branch", "commit", "dirty", "status_short"]
    for field in required:
        data = minimal_valid_result()
        del data["quarry"][field]
        try:
            validate_hil_result(data)
            assert False, f"should raise ValueError for missing quarry.{field}"
        except ValueError as e:
            assert f"missing required field in quarry: '{field}'" in str(e)


def test_target_all_required_fields():
    """All required fields in target metadata must be present."""
    required = ["board", "soc", "cpu", "clock_hz", "serial_device"]
    for field in required:
        data = minimal_valid_result()
        del data["target"][field]
        try:
            validate_hil_result(data)
            assert False, f"should raise ValueError for missing target.{field}"
        except ValueError as e:
            assert f"missing required field in target: '{field}'" in str(e)


def test_result_code_enum():
    """ResultCode enum has all expected values."""
    expected = {"PASS", "TEST_FAILURE", "INFRASTRUCTURE_ERROR", "RECOVERY_REQUIRED"}
    actual = {e.value for e in ResultCode}
    assert actual == expected, f"ResultCode mismatch: expected {expected}, got {actual}"


def v2_pass_result():
    data = minimal_valid_result()
    data["schema_version"] = 2
    data["presence_layout"] = "optimize-size"
    data["hil"] = {"commit": "a" * 40, "dirty": False}
    data["build"] = {
        "presence_layout": "optimize-size",
        "generation_options": ["--language", "c", "--c-optimize-size"],
        "input_digest": "b" * 64,
        "schema_compiler": "quarry-schema-compiler 0.1.266 (115e345)",
        "schema_sha256": "c" * 64,
        "generated_files": {"hil/telemetry.generated.h": "d" * 64},
        "footprint": {"elf": {"text": 1, "data": 0, "bss": 0}, "objects": {}},
    }
    stats = {"min": 1, "max": 1, "mean": 1.0, "sum": 500}
    data["benchmark"] = {
        "platform": "NUCLEO-F446RE", "cpu": "Cortex-M4F", "workload": "telemetry", "magic": "0x51424e31",
        "version": 2, "state": 2, "result_code": 1, "counter": 0, "dwt_supported": True,
        "timing_overhead_cycles": 1, "iterations": 500, "encoded_bytes": 2, "validation_failures": 0,
        "record_sizeof": 80, "presence_layout": 1, "encoded_payload_hex": "abcd",
        "encode_cycles": stats, "decode_cycles": stats, "roundtrip_cycles": stats,
        "stack_watermark_bytes": 100, "heap_bytes": 0,
    }
    return data


def test_v2_pass_result_valid():
    validate_hil_result(v2_pass_result())


def test_v2_requires_hil_provenance():
    data = v2_pass_result()
    del data["hil"]
    try:
        validate_hil_result(data)
        assert False, "v2 PASS without hil provenance must be rejected"
    except ValueError as e:
        assert "hil" in str(e)


def test_v2_generation_options_must_match_layout():
    data = v2_pass_result()
    data["build"]["generation_options"] = ["--language", "c"]
    try:
        validate_hil_result(data)
        assert False, "layout/options mismatch must be rejected"
    except ValueError as e:
        assert "generation_options" in str(e)


def test_v2_failure_needs_domain_only():
    data = {"schema_version": 2, "result": "INFRASTRUCTURE_ERROR", "run_dir": "/r", "presence_layout": "default",
            "failure_domain": "infrastructure", "error": "host compiler build failed"}
    validate_hil_result(data)
    del data["failure_domain"]
    try:
        validate_hil_result(data)
        assert False, "non-PASS v2 result without failure_domain must be rejected"
    except ValueError as e:
        assert "failure_domain" in str(e)


# For pytest discovery when run as: python3 -m pytest
if __name__ == "__main__":
    # Run manually for quick validation
    test_schema_version_required()
    test_schema_version_v1_only()
    test_result_field_valid_values()
    test_commit_hash_validation()
    test_dirty_flag_type()
    test_firmware_sha256_validation()
    test_firmware_sizes_valid_integers()
    test_target_clock_hz_positive()
    test_optional_benchmark_field()
    test_json_parsing()
    test_all_required_fields_present()
    test_quarry_all_required_fields()
    test_target_all_required_fields()
    test_result_code_enum()
    test_v2_pass_result_valid()
    test_v2_requires_hil_provenance()
    test_v2_generation_options_must_match_layout()
    test_v2_failure_needs_domain_only()
    print("✓ All tests passed")
