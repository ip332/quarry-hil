#!/usr/bin/env python3
"""Compare HIL result.json files across presence layouts.

    python3 tools/compare_results.py RUN_DIR_OR_RESULT_JSON... [--markdown OUT.md]

Results are grouped by (board, Quarry commit, workload payload). Layouts
are compared only within a group: the boards run different device_id
strings (and different clocks), so their numbers are never presented as
the same workload. For each group it checks:

  * every run is PASS with zero validation failures;
  * every run was built from the same Quarry commit, from clean trees;
  * all layouts produced byte-identical wire output for the same input.

Exit status is 0 only if every check holds.
"""

import argparse
import json
import os
import sys


def load(paths):
    docs = []
    for p in paths:
        path = os.path.join(p, "result.json") if os.path.isdir(p) else p
        with open(path) as f:
            d = json.load(f)
        d["_path"] = path
        docs.append(d)
    return docs


def mean(stats):
    return stats["mean"] if stats else None


def fmt_cycles(stats, clock_hz):
    if not stats:
        return "n/a"
    us = stats["mean"] / clock_hz * 1e6
    spread = "" if stats["min"] == stats["max"] else " (%d-%d)" % (stats["min"], stats["max"])
    return "%.0f%s / %.1f us" % (stats["mean"], spread, us)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results", nargs="+")
    ap.add_argument("--markdown", help="also write the report to this file")
    args = ap.parse_args()

    docs = load(args.results)
    lines = []
    problems = []

    infra = [d for d in docs if d.get("result") != "PASS"]
    for d in infra:
        problems.append("%s: %s [%s] %s" % (d["_path"], d.get("result"), d.get("failure_domain"),
                                            (d.get("error") or "").splitlines()[0][:200]))

    groups = {}
    for d in docs:
        if d.get("result") != "PASS":
            continue
        key = (d["target"]["board"], d["quarry"]["commit"])
        groups.setdefault(key, []).append(d)

    for (board, commit), runs in sorted(groups.items()):
        runs.sort(key=lambda d: d["presence_layout"])
        clock = runs[0]["target"]["clock_hz"]
        lines.append("### %s @ %d MHz -- Quarry `%s`" % (board, clock // 1000000, commit[:12]))
        lines.append("")
        lines.append("Cycles are mean over %s iterations (min-max shown only if they differ), DWT CYCCNT, "
                     "timing overhead (%s cycles) not subtracted." % (
                         runs[0]["benchmark"]["iterations"], runs[0]["benchmark"]["timing_overhead_cycles"]))
        lines.append("")
        lines.append("| Layout | Encode | Decode | Round-trip | Payload B | sizeof(record) | .text | .data | .bss "
                     "| typed_record.o .text | Stack watermark | Heap | Validation failures |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for d in runs:
            b, fw = d["benchmark"], d["firmware"]
            codec = d["build"]["footprint"]["objects"].get("typed_record", {}).get("text")
            lines.append("| %s | %s | %s | %s | %d | %d | %d | %d | %d | %s | %d / %d | %d | %d |" % (
                d["presence_layout"], fmt_cycles(b["encode_cycles"], clock), fmt_cycles(b["decode_cycles"], clock),
                fmt_cycles(b["roundtrip_cycles"], clock), b["encoded_bytes"], b["record_sizeof"],
                fw["text_bytes"], fw["data_bytes"], fw["bss_bytes"], codec,
                b["stack_watermark_bytes"], b["stack_region_bytes"], b["heap_bytes"], b["validation_failures"]))
        lines.append("")

        payloads = {d["presence_layout"]: d["benchmark"]["encoded_payload_hex"] for d in runs}
        same = len(set(payloads.values())) == 1
        lines.append("Wire output identical across layouts: **%s** (%d bytes, fnv1a %s)" % (
            "yes" if same else "NO", runs[0]["benchmark"]["encoded_bytes"],
            ", ".join(sorted({d["benchmark"]["encoded_payload_fnv1a"] for d in runs}))))
        if not same:
            problems.append("%s: layouts produced different wire bytes: %s" % (board, payloads))
        if len(payloads) < 2:
            problems.append("%s: only layout(s) %s present; nothing to compare" % (board, list(payloads)))
        for d in runs:
            if d["benchmark"]["validation_failures"] != 0:
                problems.append("%s/%s: %d validation failures" % (board, d["presence_layout"],
                                                                   d["benchmark"]["validation_failures"]))
            if d["quarry"]["dirty"] or d["hil"]["dirty"]:
                problems.append("%s/%s: built from a dirty tree" % (board, d["presence_layout"]))
        if len(runs) == 2:
            a, c = runs
            def delta(key):
                x, y = mean(a["benchmark"][key]), mean(c["benchmark"][key])
                return "%+.0f cycles (%+.1f%%)" % (y - x, (y - x) / x * 100.0)
            lines.append("%s vs %s: encode %s, decode %s, round-trip %s, sizeof %+d bytes, .text %+d bytes." % (
                c["presence_layout"], a["presence_layout"], delta("encode_cycles"), delta("decode_cycles"),
                delta("roundtrip_cycles"), c["benchmark"]["record_sizeof"] - a["benchmark"]["record_sizeof"],
                c["firmware"]["text_bytes"] - a["firmware"]["text_bytes"]))
        lines.append("")
        lines.append("Provenance: quarry-hil `%s`, compiler `%s`, schema sha256 `%s`, toolchain `%s`." % (
            runs[0]["hil"]["commit"][:12], runs[0]["build"]["schema_compiler"],
            runs[0]["build"]["schema_sha256"][:16], runs[0]["firmware"]["compiler"]))
        for d in runs:
            lines.append("- %s: firmware sha256 `%s`, input digest `%s`, run `%s`" % (
                d["presence_layout"], d["firmware"]["sha256"], d["build"]["input_digest"][:16],
                os.path.basename(os.path.dirname(d["_path"]))))
        lines.append("")

    if len(groups) > 1:
        lines.append("Boards are listed separately on purpose: they run different device_id strings "
                     "(different payload sizes) at different clocks, so their numbers are not the same workload.")
        lines.append("")
    if problems:
        lines.append("### Problems")
        lines += ["- " + p for p in problems]
    else:
        lines.append("All checks passed: every run PASS, zero validation failures, clean trees, identical wire "
                     "output across layouts per board.")

    report = "\n".join(lines) + "\n"
    print(report)
    if args.markdown:
        with open(args.markdown, "w") as f:
            f.write(report)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
