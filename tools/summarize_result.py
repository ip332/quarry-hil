#!/usr/bin/env python3
"""Print a Markdown summary of one HIL result.json (used for the GitHub
step summary). Tolerates partial results from failed runs."""

import json
import sys


def main(path):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        print("### HIL result unavailable\n`%s`: %s" % (path, e))
        return 0
    target = (d.get("target") or {}).get("board", "?")
    print("### %s HIL result (%s layout): %s" % (target, d.get("presence_layout"), d.get("result")))
    if d.get("failure_domain"):
        print("Failure domain: **%s**" % d["failure_domain"])
    q, h, b = d.get("quarry") or {}, d.get("hil") or {}, d.get("build") or {}
    if q:
        print("Quarry: `%s` (requested `%s`, dirty=%s)" % (q.get("commit"), q.get("requested_ref"), q.get("dirty")))
    if h:
        print("quarry-hil: `%s` (dirty=%s)" % (h.get("commit"), h.get("dirty")))
    if b:
        print("Generation: `%s` | compiler: `%s` | input digest: `%s`"
              % (" ".join(b.get("generation_options", [])), b.get("schema_compiler"), b.get("input_digest", "")[:16]))
    fw = d.get("firmware") or {}
    if fw:
        print("Firmware: `%s` sha256=`%s` text=%s data=%s bss=%s"
              % (fw.get("bin_name"), fw.get("sha256"), fw.get("text_bytes"), fw.get("data_bytes"), fw.get("bss_bytes")))
    bm = d.get("benchmark")
    if bm and "encode_cycles" in bm:
        print("Encode: %.0f | Decode: %.0f | Round-trip: %.0f cycles (mean, %s iterations) | Payload: %s bytes | "
              "sizeof(record): %s | Stack watermark: %s bytes | Heap: %s | Validation failures: %s"
              % (bm["encode_cycles"]["mean"], bm["decode_cycles"]["mean"], bm["roundtrip_cycles"]["mean"],
                 bm.get("iterations"), bm.get("encoded_bytes"), bm.get("record_sizeof"),
                 bm.get("stack_watermark_bytes"), bm.get("heap_bytes"), bm.get("validation_failures")))
    if "error" in d:
        print("Error: %s" % d["error"].splitlines()[0][:500])
    if d.get("result_transport") == "swd":
        print("**Warning:** UART was silent; result read over SWD instead (ST-LINK VCP bridge stuck -- "
              "replug the board's USB cable).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
