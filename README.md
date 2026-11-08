Quarry HIL provides hardware-in-the-loop validation and benchmarking infrastructure for the Quarry schema compiler and serialization framework on real embedded targets.

It builds Quarry-generated code for target MCUs, deploys and executes test firmware on physical hardware, collects correctness and performance results, and produces reproducible machine-readable benchmark artifacts.

Initial hardware support targeted the VisionCB-8M-STD / NXP i.MX8M Mini Cortex-M4F; NUCLEO-F446RE / STM32F446RE was added second, with the infrastructure designed to support further embedded targets over time.

## VisionCB-8M-STD hardware

- **Board**: VisionCB-8M-STD (SomLabs VisionSOM-8MM on a `STD` carrier board)
- **SoC**: NXP i.MX8M Mini, Cortex-M4F auxiliary core at 200 MHz (application cores are A53, not used by this HIL flow)
- **Host connection**: a single SEGGER J-Link probe (serial `000900003460`) provides the UART console over USB-CDC. The probe stays USB-enumerated even when the target board's own power is cycled independently — do not use USB re-enumeration as a signal that the board rebooted. The probe's own USB connection (separate from board power) has been observed to drop off the host's USB bus entirely between runs — if `discover_device()` doesn't find it, it automatically power-cycles the probe's specific USB hub port (`infrastructure/core/usb_hub_power.py`, hub `3-11.2` port `1` on this host — the same physical hub NUCLEO's ST-LINK sits behind, at a different port) and retries, the same recovery a human was doing by hand (unplug/replug the hub). Only after that recovery attempt fails does it surface as `RECOVERY_REQUIRED`.
- **Power control**: board power is switched through channel 1 of a `usbrelay`-controlled USB relay (board serial `QAAMZ`; `infrastructure/core/power_relay.py`). The relay keeps that serial in EEPROM and has been seen to corrupt it on a spontaneous re-enumeration (`QAAMZ` became `9oOMZ` on 2026-10-05), so the runner uses whichever single relay board is present and logs a warning if its serial doesn't match. It is independent of the J-Link probe's own USB connection. `run_visioncb_hil.py` power-cycles the board via this relay at the start of every run (see "Boot recovery" below) and again, automatically, if the hardware sequence hits `RECOVERY_REQUIRED` mid-run. Requires the `usbrelay` CLI on the runner host (`sudo apt-get install -y usbrelay`), plus HID device permissions for the user running the HIL host (already satisfied on `visioncb-hil-host`, same user/host as the rest of this flow). Probe recovery (see "Host connection" above) separately requires the `uhubctl` CLI (`sudo apt-get install -y uhubctl`) and a udev rule granting hub access — see `infrastructure/udev/README.md`.
- **Storage**: the board has both a removable SD card and onboard eMMC. **The SD card holds the only image this HIL flow is validated against** (`kirkstone`-based Yocto Linux). The eMMC holds the board's original, untouched **factory image** (`hardknott`-based Yocto Linux) — a different, older OS release, kept purely as shipped. The two are not interchangeable and the runner actively refuses to proceed if it detects it isn't talking to the SD/kirkstone image.
- **Other USB devices commonly present on the host and never touched by this flow**: an OpenMV camera, a Prolific USB-serial adapter. Device discovery always resolves through `/dev/serial/by-id/usb-SEGGER_J-Link_000900003460-if00` — never a raw `/dev/ttyACM*` index, which is not stable across boots.

## Local HIL command

```
python3 boards/visioncb-8m-std/run_visioncb_hil.py --quarry-ref main --presence-layout default
python3 boards/visioncb-8m-std/run_visioncb_hil.py --quarry-ref main --presence-layout optimize-size
```

With no source flag the runner benchmarks Quarry `main`, resolved to an exact SHA in the dedicated CI-only clone (see "Benchmark builds and provenance" below). `--presence-layout` defaults to `default`. Every run rebuilds the firmware for the selected revision and layout; there is no reuse of earlier firmware. Exit codes are a stable, documented contract:

| Exit | Result | Meaning |
|---|---|---|
| 0 | `PASS` | board reached a result, correctness validated, result bound to the build just made |
| 1 | `TEST_FAILURE` | Quarry codec failure: validation failures, a failed encode/decode/`_encoded_size()`, or wire bytes that differ between layouts or between the board and a host-native encode |
| 2 | `INFRASTRUCTURE_ERROR` | source selection/build/device/transfer/storage-safety problem, or the board did not run the firmware just built — never a codec verdict |
| 3 | `RECOVERY_REQUIRED` | board still unresponsive after an automatic relay power-cycle retry, or the J-Link probe still not found after a USB hub port power-cycle — needs physical/hardware investigation |

Every non-PASS `result.json` also carries `failure_domain`: `codec`, `binding` (wrong/stale firmware on the board), `hardware`, or `infrastructure`.

## Boot recovery: the eMMC/autoboot race (important, read before debugging a `RECOVERY_REQUIRED`)

The board's **factory U-Boot `bootcmd` autoboots the eMMC (`hardknott`) image by default**. Reaching the SD/kirkstone image has never been the automatic path — it requires **interrupting U-Boot's autoboot countdown** (`Hit any key to stop autoboot: 2 1 0`, roughly a 3-second window) before it falls through to eMMC, then driving the proven SD-boot sequence (`mmc dev 0` / `fatload` / `booti`), which `run_visioncb_hil.py` already automates once it reaches an interactive `u-boot=>` prompt.

**Power control is now automated** (`infrastructure/core/power_relay.py`, relay channel `QAAMZ_1`). Every run starts by power-cycling the board via the relay and immediately racing the same autoboot-countdown window described above with repeated keypresses (`SerialLink.catch_uboot_prompt`) — the exact procedure that used to require a human watching the console. This removes the old ambiguity about what state the board was already in (fresh/mid-session/hung/unresponsive): every run, and every recovery attempt, now starts from the same known state.

Within a single run, the *second* boot (after firmware has been transferred, to load and start the M4 workload) still uses a **software `reboot`** issued from the live Linux shell rather than another power-cycle — no need to re-win the eMMC race there, since U-Boot is being re-entered from a session the runner is already driving, not raced from cold.

**Automatic recovery**: if `run_hardware_sequence()` hits `RecoveryRequired` at any step (autoboot window missed twice in a row, Linux not reached after the mid-run reboot, etc.), `run_visioncb_hil.py` retries the *entire* hardware sequence exactly once — whose own first step is itself a fresh power-cycle. Only a board still unresponsive after that clean power transition surfaces as `RECOVERY_REQUIRED`, at which point it genuinely warrants physical/hardware investigation rather than another automatic retry (`RECOVERY_REQUIRED` remains a firm stop, not an unbounded retry loop).

## Safety constraints (all enforced by `run_visioncb_hil.py`, not just documented)

- **SD-only deployment**: every run asserts `findmnt /` reports `/dev/mmcblk0p2` before proceeding, and re-asserts it after every reboot.
- **eMMC is never written**: any `mmcblk2*` partitions the stock image auto-mounts are unmounted (read-only from this flow's perspective); no `mmcblk2` write ever occurs.
- **No persistent U-Boot changes**: no `saveenv` anywhere in the code; boot commands are issued interactively each run, never persisted.
- **No SWD/JTAG/RTT**: all communication is over the SEGGER J-Link's UART passthrough only.
- **No credentials sent to an unconfirmed system**: the runner always drives its own boot from a freshly power-cycled `u-boot=>` prompt to the SD/kirkstone image via explicit U-Boot commands, and `uboot_resume_linux()` verifies the login banner contains the expected OS marker before `login()` ever sends credentials.
- **No indefinite retries against a hung board**: the hardware sequence gets exactly one power-cycle retry (see "Boot recovery" above); a board still unresponsive after that returns `RECOVERY_REQUIRED` as a firm stop, not an unbounded retry loop.

## NUCLEO-F446RE hardware

- **Board**: NUCLEO-F446RE (STM32F446RE, Cortex-M4F, on-board ST-LINK/V2.1)
- **Host connection**: the on-board ST-LINK/V2.1 (serial `0669FF565271525067071140`) provides both the SWD programming/reset interface (used by `st-flash`) and a UART console over USB-CDC (the board's Virtual COM Port), all through one USB cable. That cable is also the board's only power, and it must not be power-cycled from software: cutting the ST-LINK's hub port (e.g. with `uhubctl`) leaves the STM32 unreachable over SWD (`st-info --probe` shows chipid `0x0000`) until the cable is physically replugged — the same thing a host reboot or mains outage does. If a run fails with `Can not connect to target`, replug the Nucleo's USB cable. After a host reboot the ST-LINK's VCP bridge has also been seen to go silent (firmware runs and transmits, 0 bytes reach the host) while SWD keeps working; the runner then reads the result from target RAM over SWD instead, records `"result_transport": "swd"` in `result.json`, and the workflow raises a warning annotation. Replug the cable to restore the UART path. Device discovery always resolves through `/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_0669FF565271525067071140-if02` — never a raw `/dev/ttyACM*` index, which is not stable across boots.
- **Unlike VisionCB-8M-STD**, this is a standalone chip with no OS, no U-Boot, no eMMC/SD, and no second CPU to hand off to: the firmware (`boards/nucleo-f446re/firmware/` plus `boards/common/`) boots directly from flash, runs the same Quarry encode/decode/round-trip workload as the VisionCB harness, and streams its result off-device as raw hex over its UART (USART2, the board's default ST-LINK VCP route) once, framed with begin/end markers. `run_nucleo_hil.py` decodes that hex and computes the same summary statistics VisionCB's on-target reader would. Only the aggregate stats (min/max/mean/sum) are transmitted, not raw per-iteration samples — see `firmware/main.c`'s `report_result()` for why (a full-struct transfer was observed to occasionally truncate on this flow-control-free UART).
- **Clock**: deliberately left at the post-reset default (HSI, 16 MHz, no PLL) — see `firmware/uart.h` for the rationale. DWT cycle counts are reported at this clock, not the chip's 180 MHz ceiling.
- **Flashing**: via `st-flash` (package `stlink-tools`) over SWD — `sudo apt-get install -y stlink-tools` if not already present. No OpenOCD dependency.

### Local HIL command (NUCLEO-F446RE)

```
python3 boards/nucleo-f446re/run_nucleo_hil.py --quarry-ref main --presence-layout default
python3 boards/nucleo-f446re/run_nucleo_hil.py --quarry-ref main --presence-layout optimize-size
```

Same source selection, layout option, rebuild-every-run behavior and exit-code contract as VisionCB (0 `PASS`, 1 `TEST_FAILURE`, 2 `INFRASTRUCTURE_ERROR`, 3 `RECOVERY_REQUIRED` — the last meaning the firmware didn't report a result after flash+reset within timeout, warranting a physical check of the USB connection/board rather than an automatic retry).

**CI**: `.github/workflows/nucleo-hil.yml` runs this on the same self-hosted host as VisionCB (via the runner's existing generic `cortex-m4` label, not a new board-specific one), on `workflow_dispatch` and a nightly schedule offset 20 minutes after VisionCB's slot. Same never-triggers-on-`pull_request` policy as VisionCB HIL, for the same reason (see that section above).

## Benchmark builds and provenance

Both boards run the same telemetry schema and workload code (`boards/common/telemetry.brd`, `boards/common/telemetry_workload.h`): an untimed single-shot pass (encode, `_encoded_size()` cross-check, decode, validate), then three timed loops of 500 iterations each: encode-only, decode-only of the same known-good payload, and encode+decode round-trip. Validation always runs outside the timed span. The firmware sources for both boards are versioned in this repository: `boards/common/` (schema, workload, result struct, DWT, libc shim), `boards/nucleo-f446re/firmware/` and `boards/visioncb-8m-std/firmware/` (startup, linker script, `main.c`, and VisionCB's on-target `read_bench_result.c`). VisionCB's harness used to be built from the untracked `/home/igor/work/visioncb-m4/quarry_bench`; that directory is no longer used.

**The workloads are not identical across boards.** VisionCB encodes `device_id = "m4-hil-01"` (84-byte payload), NUCLEO encodes `"nucleo-hil-01"` (88 bytes). The boards also run at different clocks (200 MHz vs 16 MHz HSI). Compare layouts within a board, not boards against each other.

### Source selection

- `--quarry-ref REF` (default `main`): fetch and check out REF detached in the dedicated CI-only clone `~/.cache/visioncb-hil/quarry-ci-checkout` (override with `--quarry-ci-checkout` or `QUARRY_HIL_CI_CHECKOUT`). The tool refuses any clone that is attached to a branch or has local modifications, so a developer's interactive checkout (for example `/home/igor/work/quarry`) is never switched or overwritten.
- `--quarry-dir PATH`: build an existing tree exactly as it is. No fetch or checkout is run.
- `--expect-quarry-sha SHA`: fail unless the selected sources are exactly SHA. The workflows use this to pin every layout job to the single SHA they resolved up front.
- A dirty Quarry or quarry-hil tree is refused unless `--allow-dirty` is given, and the result then records `dirty: true` plus a digest of the diff. Untracked files in quarry-hil are listed but do not count as dirty, because builds only read declared sources (see below).

### What every run builds (`infrastructure/build/firmware_build.py`)

1. **Host schema compiler** from the selected tree, into `~/.cache/quarry-hil/host-build/<sha>`. It is reused only if a stamp records the same commit, tree and binary hash and `quarry-schema-compiler --version` names that commit.
2. **Bindings** for `boards/common/telemetry.brd`, freshly generated with `--language c` or `--language c --c-optimize-size`. No generated code is checked in. The header's `QUARRY_C_GENERATED_CODE_API_VERSION` guard must match the `version.h` produced by configuring the same tree, and its presence layout must match the requested one.
3. **Firmware** in `build/<board>/<layout>/`, which is deleted first. The declared harness sources (`infrastructure/build/board_specs.py`) are copied there, and nothing else is on the include path besides the generated bindings, the tree's `include/` and its `version.h`. The tree's shared `runtime_c/typed_record.c` is compiled and linked. The build fails if the image links any heap symbol.
4. **Host layout-equivalence gate**: the shared workload is compiled natively against both layouts. Encoded bytes must be identical, and decode must validate in both. A mismatch is reported as a codec failure, before any hardware is touched.
5. **Manifest** `build/<board>/<layout>/build_manifest.json`: hashes of every input (harness sources, schema, generated files, Quarry runtime sources and headers, `version.h`, compiler binary), tool versions, full compiler flags, generation options, per-object and total footprint, firmware/ELF hashes, compile warnings, and the host gate's payloads. Its input digest is compiled into the firmware.

The firmware reports its result struct version (2), `sizeof(hil_telemetry_SensorSample_t)`, the presence layout, the first 8 hex digits of the Quarry commit and of the input digest, and the encoded payload bytes plus FNV-1a. The runner discards any result whose layout, commit or digest does not match the build it just made (`binding` failure). It then requires the payload to equal the host-native encoding of the same input (`codec` failure otherwise). VisionCB additionally keeps its on-device sha256 transfer check and the `bootaux` stack/pc check.

`result.json` (schema version 2, `infrastructure/schema/result_schema.py`) records `quarry` (resolved SHA, requested ref, dirty state), `hil` (quarry-hil SHA, dirty state, untracked files), `build` (generation options, compiler version and hash, schema and input hashes, flags, footprint), `firmware` (hash and sizes) and `benchmark`. Each run directory also gets a copy of `build_manifest.json` and `build.log`.

Generated C API used by the harness (Quarry ≥ `115e345`): headers include `quarry/runtime_c/typed_record.h` and wrap the shared codec in inline functions. Strings, bytes and arrays have no `has_*` member; an empty value encodes as absent. Scalars and enums keep explicit presence, as `bool has_<field>` members by default or, with `--c-optimize-size`, as a `quarry_presence_bits[]` bitmap accessed through `<record>_has_<field>()` / `<record>_set_<field>_present()`. `_encoded_size()` returns `{status, size}`. `telemetry_workload.h` maps presence to the right form per layout, so building a layout against the other layout's bindings fails to compile.

### Tools

```
python3 tools/build_firmware.py --board all --presence-layout all --quarry-ref main   # build + host gate, no hardware
python3 tools/resolve_quarry_ref.py --ref main                                         # print resolved SHA (CI checkout only)
python3 tools/compare_results.py RUN_DIR...                                            # per-board layout comparison table
```

### Host build dependencies

Building Quarry's host compiler needs CMake, protobuf (`protoc` matching the headers), absl and libyaml development files, plus `arm-none-eabi-gcc` with newlib headers. The HIL host has the protobuf toolchain in `/usr/local` but no `libyaml-dev`/`libabsl-dev` system packages. Their unmodified Ubuntu `.deb`s are extracted, without root, into `~/.cache/quarry-hil/hostdeps/prefix` (`apt-get download libyaml-dev libabsl-dev libabsl20210324` then `dpkg -x`), and the build passes that prefix to CMake automatically when it exists (override with `QUARRY_HIL_HOSTDEPS_PREFIX`).

## GitHub Actions: self-hosted VisionCB HIL

Physical HIL runs on a dedicated, **repository-scoped** self-hosted GitHub Actions runner attached to this host.

- **Runner name**: `visioncb-hil-host`
- **Labels**: `self-hosted, linux, x64, visioncb, cortex-m4`
- **Scope**: registered against `ip332/quarry-hil` only — not org-wide, not shared with any other repository
- **Service**: user-level systemd unit `~/.config/systemd/user/quarry-hil-runner.service`, running as the `igor` user (already has the necessary `dialout` group / serial device access — no new privileged account was created)
- **Reboot survivability**: `loginctl enable-linger igor` is set, so the user's systemd instance (and the runner) starts at boot without requiring an active login session

**Verifying the runner service after a host reboot**:
```
systemctl --user status quarry-hil-runner.service
loginctl show-user igor -p Linger        # should show Linger=yes
gh api repos/ip332/quarry-hil/actions/runners --jq '.runners[]'   # should show status: online
```

### Workflows

- **`.github/workflows/ci.yml`** — hardware-independent, runs on GitHub-hosted runners for every PR/push to main: syntax checks, schema tests, result-check and wire-layout tests (including the VisionCB reader's struct-layout assertion). Its informational `firmware-build` job builds every board/layout against current Quarry main, including the host layout-equivalence gate. It is deliberately not part of `ci-complete`, so an upstream Quarry API change cannot block unrelated PRs.
- **`.github/workflows/visioncb-runner-smoketest.yml`** — harmless, `workflow_dispatch`-only. Confirms GitHub can reach this host, select it by label, check out the repo, and see expected tooling. Never opens a serial device.
- **`.github/workflows/visioncb-hil.yml`** — the real physical HIL workflow for VisionCB-8M-STD. **Never triggers on `pull_request`**: only `workflow_dispatch` (inputs `quarry_ref`, `presence_layout` = `both`/`default`/`optimize-size`) and a nightly `schedule` (`17 3 * * *` **UTC**, both layouts). A `resolve` job resolves the ref once in the dedicated CI-only clone. One hardware job per layout then runs sequentially (`max-parallel: 1`), each with `--quarry-ref <sha> --expect-quarry-sha <sha>`, so both layouts are guaranteed to be the same Quarry commit. Hardware access is serialized by a `concurrency` group (a queued run waits rather than cancelling an in-progress hardware session), and each job has a 30-minute timeout. All hardware logic stays in Python. `result.json` / `runner.log` / `console.log` / `build_manifest.json` / `build.log` are uploaded per layout on every run (pass or fail), with 90-day retention.
- **`.github/workflows/nucleo-hil.yml`** — the same for NUCLEO-F446RE, on the same self-hosted host: never on `pull_request`, `workflow_dispatch` with the same inputs, plus a nightly `schedule` (`37 3 * * *` UTC, 20 minutes after VisionCB's slot). It is `concurrency`-serialized in its own group (`nucleo-hil-hardware`), uses the same artifact structure, and targets the runner via its generic `cortex-m4` label. It shares VisionCB's dedicated CI-only clone. That is safe even if jobs of the two workflows interleave, because every hardware job checks out and verifies its own SHA.

**Manual dispatch** (also the release/RC validation mechanism):
```
gh workflow run visioncb-hil.yml -f quarry_ref=main                                  # both layouts (nightly-equivalent)
gh workflow run visioncb-hil.yml -f quarry_ref=v1.2.3-rc.1 -f presence_layout=both    # release/RC validation
gh workflow run nucleo-hil.yml -f quarry_ref=main -f presence_layout=optimize-size    # one layout, NUCLEO-F446RE
```
The `quarry_ref` accepts a branch name, tag, or commit SHA. The exact resolved commit is recorded in the run's step summary and in `result.json`'s `quarry.commit` field, next to the quarry-hil commit (`hil.commit`) that built it. A result is never published as "tested latest Quarry" without a SHA.

**Nightly**: both workflows run automatically against current Quarry `main`, both presence layouts, every day (VisionCB at 03:17 UTC, NUCLEO-F446RE at 03:37 UTC). A nightly failure is visible as a normal failed GitHub Actions run; nothing modifies Quarry automatically based on the result.

**Release validation**: there is currently **no automatic Quarry→quarry-hil cross-repo trigger** — that would require a new PAT/secret stored in the Quarry repository that does not yet exist, and none was fabricated. Today, release validation means a human manually dispatches `visioncb-hil.yml` and/or `nucleo-hil.yml` with `quarry_ref` set to the release/RC tag after cutting it in Quarry. These workflows **run against** a given ref; they do not currently **block** any Quarry release — those are two different things, and only the former is actually implemented.

**Current status honestly stated**: normal scheduled HIL execution (nightly, manual dispatch, power-cycle at the start of every run, software `reboot` for the in-run M4-load boot) is automated end-to-end for VisionCB, including recovery from a cold power-on race or an unresponsive board via the relay (`infrastructure/core/power_relay.py`, channel `QAAMZ_1`) — no human needs to be physically at the hardware for that case anymore. A board still unresponsive after the one automatic power-cycle retry (a genuine hardware fault, not just a missed timing window) still surfaces as `RECOVERY_REQUIRED` and needs manual investigation; that residual case is not, and cannot be, eliminated by power control alone.
