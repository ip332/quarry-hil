"""Validation of a board-reported benchmark against the build that was
supposed to produce it. Shared by every board runner.

Each check has a failure domain, so a non-PASS result says which kind of
problem it was:

  "binding"  -- the board ran firmware other than the build just made
               (wrong layout, commit or input digest, or old struct
               version). The result is not trustworthy, so the run is an
               INFRASTRUCTURE_ERROR, not a codec verdict.
  "codec"    -- the right firmware ran and Quarry misbehaved: a validation
               failure, a non-PASS result code, or wire bytes that differ
               from the host-native encoding of the same input. These are
               TEST_FAILURE.
"""

QUARRY_BENCH_MAGIC = "0x51424e31"
QUARRY_BENCH_VERSION = 2
QUARRY_BENCH_STATE_TEST_COMPLETE = 2
QUARRY_BENCH_RESULT_PASS = 1
LAYOUT_CODES = {"default": 0, "optimize-size": 1}


def fnv1a32(data):
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def check_benchmark(benchmark, manifest, require_heartbeat=False):
    """Returns a list of (name, ok, observed, domain)."""
    layout = manifest["presence_layout"]
    commit = manifest["quarry"]["commit"]
    digest = manifest["input_digest"]
    host = manifest["host_layout_gate"][layout]
    b = benchmark
    checks = [
        ("magic", b.get("magic") == QUARRY_BENCH_MAGIC, b.get("magic"), "binding"),
        ("result struct version", b.get("version") == QUARRY_BENCH_VERSION, b.get("version"), "binding"),
        ("presence_layout matches build", b.get("presence_layout") == LAYOUT_CODES[layout],
         b.get("presence_layout"), "binding"),
        ("quarry_commit_prefix matches build", b.get("quarry_commit_prefix") == commit[:8],
         b.get("quarry_commit_prefix"), "binding"),
        ("build_input_digest matches build", b.get("build_input_digest") == digest[:8],
         b.get("build_input_digest"), "binding"),
        ("state (TEST_COMPLETE)", b.get("state") == QUARRY_BENCH_STATE_TEST_COMPLETE, b.get("state"), "codec"),
        ("result_code (PASS)", b.get("result_code") == QUARRY_BENCH_RESULT_PASS, b.get("result_code"), "codec"),
        ("validation_failures", b.get("validation_failures") == 0, b.get("validation_failures"), "codec"),
        ("iterations", b.get("iterations") == 500, b.get("iterations"), "codec"),
        ("record_sizeof > 0", isinstance(b.get("record_sizeof"), int) and b["record_sizeof"] > 0,
         b.get("record_sizeof"), "codec"),
    ]
    if require_heartbeat:
        checks.append(("heartbeat counter > 0", (b.get("counter") or 0) > 0, b.get("counter"), "codec"))

    payload_hex = b.get("encoded_payload_hex") or ""
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError:
        payload = b""
    complete = b.get("encoded_payload_captured_bytes") == b.get("encoded_bytes") == len(payload)
    checks.append(("payload fully captured", complete,
                   "%s/%s" % (b.get("encoded_payload_captured_bytes"), b.get("encoded_bytes")), "codec"))
    checks.append(("payload fnv1a matches capture", complete and "%08x" % fnv1a32(payload) == b.get("encoded_payload_fnv1a"),
                   b.get("encoded_payload_fnv1a"), "codec"))
    checks.append(("payload equals host-native encoding", payload_hex == host["payload_hex"],
                   "%d bytes" % len(payload), "codec"))
    return checks


def summarize(checks):
    """Returns (ok, failed_names, failure_domain); a binding failure wins
    over a codec failure, because a mismatched build makes codec checks
    meaningless."""
    failed = [(n, d) for n, ok, _, d in checks if not ok]
    if not failed:
        return True, [], None
    domains = {d for _, d in failed}
    domain = "binding" if "binding" in domains else "codec"
    return False, [n for n, _ in failed], domain
