Quarry HIL provides hardware-in-the-loop validation and benchmarking infrastructure for the Quarry schema compiler and serialization framework on real embedded targets.

It builds Quarry-generated code for target MCUs, deploys and executes test firmware on physical hardware, collects correctness and performance results, and produces reproducible machine-readable benchmark artifacts.

Initial hardware support targeted the VisionCB-8M-STD / NXP i.MX8M Mini Cortex-M4F; NUCLEO-F446RE / STM32F446RE was added second, with the infrastructure designed to support further embedded targets over time.

## VisionCB-8M-STD hardware

- **Board**: VisionCB-8M-STD (SomLabs VisionSOM-8MM on a `STD` carrier board)
- **SoC**: NXP i.MX8M Mini, Cortex-M4F auxiliary core at 200 MHz (application cores are A53, not used by this HIL flow)
- **Host connection**: a single SEGGER J-Link probe (serial `000900003460`) provides the UART console over USB-CDC. The probe stays USB-enumerated even when the target board's own power is cycled independently — do not use USB re-enumeration as a signal that the board rebooted.
- **Power control**: board power is switched through channel `QAAMZ_1` of a `usbrelay`-controlled USB relay (`infrastructure/core/power_relay.py`), independent of the J-Link probe's own USB connection. `run_visioncb_hil.py` power-cycles the board via this relay at the start of every run (see "Boot recovery" below) and again, automatically, if the hardware sequence hits `RECOVERY_REQUIRED` mid-run. Requires the `usbrelay` CLI on the runner host (`sudo apt-get install -y usbrelay`), plus HID device permissions for the user running the HIL host (already satisfied on `visioncb-hil-host`, same user/host as the rest of this flow).
- **Storage**: the board has both a removable SD card and onboard eMMC. **The SD card holds the only image this HIL flow is validated against** (`kirkstone`-based Yocto Linux). The eMMC holds the board's original, untouched **factory image** (`hardknott`-based Yocto Linux) — a different, older OS release, kept purely as shipped. The two are not interchangeable and the runner actively refuses to proceed if it detects it isn't talking to the SD/kirkstone image.
- **Other USB devices commonly present on the host and never touched by this flow**: an OpenMV camera, a Prolific USB-serial adapter. Device discovery always resolves through `/dev/serial/by-id/usb-SEGGER_J-Link_000900003460-if00` — never a raw `/dev/ttyACM*` index, which is not stable across boots.

## Local HIL command

```
python3 boards/visioncb-8m-std/run_visioncb_hil.py \
    --quarry-dir /home/igor/work/quarry \
    --harness-dir /home/igor/work/visioncb-m4/quarry_bench
```

Both flags default to those paths already, so a bare invocation from the repo root is normally sufficient. Exit codes are a stable, documented contract:

| Exit | Result | Meaning |
|---|---|---|
| 0 | `PASS` | board reached a result, correctness validated |
| 1 | `TEST_FAILURE` | board reached a result, but it failed validation |
| 2 | `INFRASTRUCTURE_ERROR` | build/device/transfer/storage-safety problem |
| 3 | `RECOVERY_REQUIRED` | board still unresponsive after an automatic relay power-cycle retry — needs physical/hardware investigation |

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
- **Host connection**: the on-board ST-LINK/V2.1 (serial `0669FF565271525067071140`) provides both the SWD programming/reset interface (used by `st-flash`) and a UART console over USB-CDC (the board's Virtual COM Port), all through one USB cable. Device discovery always resolves through `/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_0669FF565271525067071140-if02` — never a raw `/dev/ttyACM*` index, which is not stable across boots.
- **Unlike VisionCB-8M-STD**, this is a standalone chip with no OS, no U-Boot, no eMMC/SD, and no second CPU to hand off to: the firmware (`boards/nucleo-f446re/firmware/`, checked into this repo — unlike VisionCB's harness, which lives externally) boots directly from flash, runs the same Quarry encode/decode/round-trip workload as the VisionCB harness, and streams its result off-device as raw hex over its UART (USART2, the board's default ST-LINK VCP route) once, framed with begin/end markers. `run_nucleo_hil.py` decodes that hex and computes the same summary statistics VisionCB's on-target reader would. Only the aggregate stats (min/max/mean/sum) are transmitted, not raw per-iteration samples — see `firmware/main.c`'s `report_result()` for why (a full-struct transfer was observed to occasionally truncate on this flow-control-free UART).
- **Clock**: deliberately left at the post-reset default (HSI, 16 MHz, no PLL) — see `firmware/uart.h` for the rationale. DWT cycle counts are reported at this clock, not the chip's 180 MHz ceiling.
- **Flashing**: via `st-flash` (package `stlink-tools`) over SWD — `sudo apt-get install -y stlink-tools` if not already present. No OpenOCD dependency.

### Local HIL command (NUCLEO-F446RE)

```
python3 boards/nucleo-f446re/run_nucleo_hil.py --quarry-dir /home/igor/work/quarry
```

`--quarry-dir` defaults to that path already, so a bare invocation from the repo root is normally sufficient. `--harness-dir` defaults to this board's own `firmware/` subdirectory. Same exit-code contract as VisionCB (0 `PASS`, 1 `TEST_FAILURE`, 2 `INFRASTRUCTURE_ERROR`, 3 `RECOVERY_REQUIRED` — the last meaning the firmware didn't report a result after flash+reset within timeout, warranting a physical check of the USB connection/board rather than an automatic retry).

**CI**: `.github/workflows/nucleo-hil.yml` runs this on the same self-hosted host as VisionCB (via the runner's existing generic `cortex-m4` label, not a new board-specific one), on `workflow_dispatch` and a nightly schedule offset 20 minutes after VisionCB's slot. Same never-triggers-on-`pull_request` policy as VisionCB HIL, for the same reason (see that section above).

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

- **`.github/workflows/ci.yml`** — unchanged, hardware-independent, runs on GitHub-hosted runners for every PR/push to main.
- **`.github/workflows/visioncb-runner-smoketest.yml`** — harmless, `workflow_dispatch`-only. Confirms GitHub can reach this host, select it by label, check out the repo, and see expected tooling. Never opens a serial device.
- **`.github/workflows/visioncb-hil.yml`** — the real physical HIL workflow for VisionCB-8M-STD. **Never triggers on `pull_request`** — only `workflow_dispatch` and a nightly `schedule` (`17 3 * * *` **UTC**, not local time). Serializes hardware access via a `concurrency` group (a queued run waits rather than cancelling an in-progress hardware session). Bounded to a 20-minute job timeout. Resolves the requested Quarry ref in a **dedicated CI-only clone** (`/home/igor/.cache/visioncb-hil/quarry-ci-checkout` on the runner host) — never the developer's own interactive checkout — so a nightly run can never switch out anyone's working branch. Invokes `run_visioncb_hil.py` unmodified (all hardware logic stays in Python, not YAML). Uploads `result.json` / `runner.log` / `console.log` as artifacts on every run (pass or fail), 90-day retention.
- **`.github/workflows/nucleo-hil.yml`** — the same, for NUCLEO-F446RE, on the same self-hosted host. Same never-`pull_request`, `workflow_dispatch` + nightly-`schedule` (`37 3 * * *` UTC, offset 20 minutes after VisionCB's slot), `concurrency`-serialized (own group, `nucleo-hil-hardware`), artifact-upload structure. Targets the runner via its existing generic `cortex-m4` label rather than a new board-specific one. Shares VisionCB's dedicated CI-only Quarry checkout (safe: GitHub only ever runs one job at a time on a given self-hosted runner instance, so the two workflows can never touch it concurrently).

**Manual dispatch** (also the release/RC validation mechanism):
```
gh workflow run visioncb-hil.yml -f quarry_ref=main            # ad-hoc / nightly-equivalent
gh workflow run visioncb-hil.yml -f quarry_ref=v1.2.3-rc.1      # release/RC validation
gh workflow run nucleo-hil.yml -f quarry_ref=main               # same, for NUCLEO-F446RE
```
The `quarry_ref` accepts a branch name, tag, or commit SHA. The exact resolved commit is recorded in the run's step summary and in `result.json`'s `quarry.commit` field — a result is never published as "tested latest Quarry" without a SHA.

**Nightly**: both workflows run automatically against current Quarry `main` every day (VisionCB at 03:17 UTC, NUCLEO-F446RE at 03:37 UTC). A nightly failure is visible as a normal failed GitHub Actions run; nothing modifies Quarry automatically based on the result.

**Release validation**: there is currently **no automatic Quarry→quarry-hil cross-repo trigger** — that would require a new PAT/secret stored in the Quarry repository that does not yet exist, and none was fabricated. Today, release validation means a human manually dispatches `visioncb-hil.yml` and/or `nucleo-hil.yml` with `quarry_ref` set to the release/RC tag after cutting it in Quarry. These workflows **run against** a given ref; they do not currently **block** any Quarry release — those are two different things, and only the former is actually implemented.

**Current status honestly stated**: normal scheduled HIL execution (nightly, manual dispatch, power-cycle at the start of every run, software `reboot` for the in-run M4-load boot) is automated end-to-end for VisionCB, including recovery from a cold power-on race or an unresponsive board via the relay (`infrastructure/core/power_relay.py`, channel `QAAMZ_1`) — no human needs to be physically at the hardware for that case anymore. A board still unresponsive after the one automatic power-cycle retry (a genuine hardware fault, not just a missed timing window) still surfaces as `RECOVERY_REQUIRED` and needs manual investigation; that residual case is not, and cannot be, eliminated by power control alone.
