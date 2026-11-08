"""Firmware build specs for every board, keyed by board directory name.

Used by the board runners and by tools/build_firmware.py. `sources` lists
every file a build may read, as repo-relative paths. They are copied into
the variant's build directory, so nothing undeclared can be #included.
"""

BOARDS = {}

BOARDS["nucleo-f446re"] = {
    "board": "nucleo-f446re",
    "schema_rel": "boards/common/telemetry.brd",
    "device_id": "nucleo-hil-01",
    "sources": [
        "boards/nucleo-f446re/firmware/startup.c",
        "boards/common/libc_shim.c",
        "boards/nucleo-f446re/firmware/main.c",
        "boards/nucleo-f446re/firmware/uart.h",
        "boards/common/dwt.h",
        "boards/common/bench_result.h",
        "boards/common/telemetry_workload.h",
    ],
    "linker_script": "boards/nucleo-f446re/firmware/link.ld",
    "output_name": "nucleo_bench",
    "defines": [],
}

BOARDS["visioncb-8m-std"] = {
    "board": "visioncb-8m-std",
    "schema_rel": "boards/common/telemetry.brd",
    "device_id": "m4-hil-01",
    "sources": [
        "boards/visioncb-8m-std/firmware/startup.c",
        "boards/common/libc_shim.c",
        "boards/visioncb-8m-std/firmware/main.c",
        "boards/common/dwt.h",
        "boards/common/bench_result.h",
        "boards/common/telemetry_workload.h",
    ],
    "linker_script": "boards/visioncb-8m-std/firmware/link.ld",
    "output_name": "quarry_bench",
    "defines": [],
}

