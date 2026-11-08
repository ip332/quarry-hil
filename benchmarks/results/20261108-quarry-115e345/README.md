# Quarry `115e345` on VisionCB-8M-STD and NUCLEO-F446RE, both presence layouts

- Quarry: `115e345223d31e3ea94b842c2173f740223fd4c0` ("Generate C header bindings backed by a shared BRF v2 codec (#77)"), resolved from `main` on 2026-11-08 in the dedicated CI checkout. Tree clean.
- quarry-hil: `67438ba98dddc046b3e5acfbde52e67166c64d68`. Tree clean; the only untracked file was a stale `boards/visioncb-8m-std/.visioncb-8m-std.lock` that no code reads and no build includes.
- Schema compiler: `quarry-schema-compiler 0.1.266 (115e345)`, built from that tree. Generated C API version 5.
- Target compiler: `arm-none-eabi-gcc 10.3.1 20210621`, `-mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard -ffreestanding -fno-builtin -nostdlib -nostartfiles -Wall -Wextra -Os -g -std=c11`. Zero warnings.

Commands, run from the quarry-hil repo root in this order:

```
python3 boards/nucleo-f446re/run_nucleo_hil.py   --quarry-ref main --presence-layout default
python3 boards/nucleo-f446re/run_nucleo_hil.py   --quarry-ref main --presence-layout optimize-size
python3 boards/visioncb-8m-std/run_visioncb_hil.py --quarry-ref main --presence-layout default
python3 boards/visioncb-8m-std/run_visioncb_hil.py --quarry-ref main --presence-layout optimize-size
python3 tools/compare_results.py benchmarks/results/20261108-quarry-115e345/*/* --markdown .../comparison.md
```

All four runs: `PASS`, exit 0. There were no hardware or infrastructure failures, and no recovery path was taken: each VisionCB run caught U-Boot on the first relay power cycle, and NUCLEO results came over UART, not the SWD fallback. VisionCB logged the known relay EEPROM serial warning (`9oOMZ` instead of `QAAMZ`, see the top-level README). It is unrelated to these results.

The table is in [comparison.md](comparison.md). Each run directory holds `result.json`, `runner.log`, `console.log`, `build_manifest.json` (every input hash, flags, per-object footprint) and `build.log`.

## Reading the numbers

- **Compare layouts within a board, not boards with each other.** VisionCB encodes `device_id = "m4-hil-01"` (84-byte payload). NUCLEO encodes `"nucleo-hil-01"` (88 bytes). VisionCB runs from TCM at 200 MHz, NUCLEO from flash at 16 MHz (HSI, zero wait states).
- Cycle counts are means over 500 iterations per loop. Every loop on every run had min == max. The DWT read-pair overhead (9 cycles) is included, not subtracted.
- "Encode" times exactly `_encode()`, "Decode" exactly `_decode()` of the same known-good payload, and "Round-trip" one encode followed by one decode. Validation runs outside the timed span.
- `sizeof(record)` is what the board reported for `hil_telemetry_SensorSample_t`: 88 bytes default, 72 packed. The host-native x86-64 values in `build_manifest.json` (96 / 80) differ because of ABI layout and are not the target figures.
- Wire equivalence: on each board both layouts produced byte-identical payloads (NUCLEO fnv1a `a1bf5120`, VisionCB `305fca0b`), and each payload also equals the host-native encoding of the same input under both layouts.
- Heap: 0, because no allocator is linked; the build fails if a heap symbol is present. Stack watermark comes from stack painting and undercounts by at most the 64-byte guard.
- Do not compare these with the earlier nightly results in `boards/*/runs/` (before 2026-11-08 16:00 UTC). Those compiled a stale, checked-in snapshot of the API-version-2 bindings against whatever `/home/igor/work/quarry` had checked out. They do not measure any specific Quarry revision.
