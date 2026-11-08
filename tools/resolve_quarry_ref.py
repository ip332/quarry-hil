#!/usr/bin/env python3
"""Resolve a Quarry branch/tag/SHA to an exact commit in the dedicated CI
checkout (never an interactive checkout) and print the 40-hex SHA.

The HIL workflows run this once per workflow run and pass the SHA to every
presence-layout job (--quarry-ref SHA --expect-quarry-sha SHA). Both
layouts are then built from the same commit even if the branch moves in
between.
"""

import argparse
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "infrastructure", "build"))
import quarry_source  # noqa: E402


class _StderrLog:
    def log(self, state, message):
        print("[%s] %s" % (state, message), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ref", default="main")
    parser.add_argument("--quarry-ci-checkout", default=quarry_source.DEFAULT_CI_CHECKOUT)
    args = parser.parse_args()
    try:
        print(quarry_source.resolve_ref(args.quarry_ci_checkout, args.ref, _StderrLog()))
    except quarry_source.SourceError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
