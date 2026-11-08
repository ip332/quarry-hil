#!/usr/bin/env python3
"""Build one board firmware variant without touching any hardware.

Runs exactly the build stage the board runners run: source selection,
provenance capture, host compiler, binding generation, firmware build and
the host layout-equivalence gate. Writes build/<board>/<layout>/ and its
build_manifest.json. Useful as a pre-flight check and for inspecting
footprints.

    python3 tools/build_firmware.py --board nucleo-f446re --presence-layout optimize-size
    python3 tools/build_firmware.py --board all --presence-layout all --quarry-ref main
"""

import argparse
import copy
import datetime
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "infrastructure", "build"))
import board_specs  # noqa: E402
import firmware_build  # noqa: E402
import quarry_source  # noqa: E402
import run_setup  # noqa: E402


class _Log:
    def log(self, state, message):
        print("[%s] %-10s %s" % (datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                                 state, message))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    run_setup.add_source_args(parser)
    parser.add_argument("--board", default="all", choices=sorted(board_specs.BOARDS) + ["all"])
    # Override the runner-facing single choice with one that also accepts "all".
    for action in parser._actions:
        if action.dest == "presence_layout":
            action.choices = list(firmware_build.PRESENCE_LAYOUTS) + ["all"]
    args = parser.parse_args()

    boards = sorted(board_specs.BOARDS) if args.board == "all" else [args.board]
    layouts = list(firmware_build.PRESENCE_LAYOUTS) if args.presence_layout == "all" else [args.presence_layout]
    log = _Log()
    first = True
    for board in boards:
        for layout in layouts:
            a = copy.copy(args)
            a.presence_layout = layout
            if not first and not a.quarry_dir:
                # Resolve the ref once; later builds use the same checkout as it now is.
                a.quarry_dir, a.quarry_ref = a.quarry_ci_checkout, None
            try:
                _, manifest = run_setup.prepare_firmware(a, board_specs.BOARDS[board], log)
            except (quarry_source.SourceError, firmware_build.BuildError) as e:
                print("INFRASTRUCTURE_ERROR: %s" % e, file=sys.stderr)
                return 2
            except firmware_build.CodecCheckError as e:
                print("TEST_FAILURE (codec): %s" % e, file=sys.stderr)
                return 1
            first = False
            fw = manifest["firmware"]
            print("%s/%s: %s sha256=%s text=%d data=%d bss=%d manifest=%s" % (
                board, layout, fw["bin_name"], fw["sha256"], fw["text_bytes"], fw["data_bytes"],
                fw["bss_bytes"], manifest["manifest_path"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
