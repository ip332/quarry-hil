Quarry HIL provides hardware-in-the-loop validation and benchmarking infrastructure for the Quarry schema compiler and serialization framework on real embedded targets.

It builds Quarry-generated code for target MCUs, deploys and executes test firmware on physical hardware, collects correctness and performance results, and produces reproducible machine-readable benchmark artifacts.

Initial hardware support targets the VisionCB-8M-STD / NXP i.MX8M Mini Cortex-M4F, with the infrastructure designed to support additional embedded targets over time.

## VisionCB-8M-STD hardware

- **Board**: VisionCB-8M-STD (SomLabs VisionSOM-8MM on a `STD` carrier board)
- **SoC**: NXP i.MX8M Mini, Cortex-M4F auxiliary core at 200 MHz (application cores are A53, not used by this HIL flow)
- **Host connection**: a single SEGGER J-Link probe (serial `000900003460`) provides the UART console over USB-CDC. The probe stays USB-enumerated even when the target board's own power is cycled independently — do not use USB re-enumeration as a signal that the board rebooted.
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
| 3 | `RECOVERY_REQUIRED` | board unresponsive, or running an unrecognized OS image — needs a physical power-cycle before retrying |

## Boot recovery: the eMMC/autoboot race (important, read before debugging a `RECOVERY_REQUIRED`)

The board's **factory U-Boot `bootcmd` autoboots the eMMC (`hardknott`) image by default**. Reaching the SD/kirkstone image has never been the automatic path — it requires **interrupting U-Boot's autoboot countdown** (`Hit any key to stop autoboot: 2 1 0`, roughly a 3-second window) before it falls through to eMMC, then driving the proven SD-boot sequence (`mmc dev 0` / `fatload` / `booti`), which `run_visioncb_hil.py` already automates once it reaches an interactive `u-boot=>` prompt.

If the board is *already sitting mid-session* (a live Linux shell, reachable at either OS), the runner instead uses a **software `reboot`** from that shell to cycle back through U-Boot — this is fast, reliable, and requires no physical action. This is the path used for essentially all normal, healthy HIL runs (including every run driven by the GitHub Actions workflow below): the board only needs a *physical* power-cycle recovery when it's cold or hung, not between consecutive healthy runs.

**On a cold power-on** (host reboot, board power loss, or a genuinely hung board that needed a physical power-cycle), there is currently a real timing race: the runner's console classifier sends a single keypress on connect, and if the board has already sailed past the autoboot countdown and landed on the eMMC/`hardknott` login prompt by the time the runner attaches, it correctly refuses to guess at OS identity or send credentials to an unconfirmed system, and returns `RECOVERY_REQUIRED` with the message `physical power-cycle and manual investigation required`.

**Recovery procedure** (current, manual):
1. Physically power-cycle the board.
2. Immediately watch the console and send repeated harmless `\r\n` keypresses (e.g. every ~300ms) for up to ~20-30s to reliably land inside the autoboot countdown window, or simply re-run `run_visioncb_hil.py` promptly after the power-cycle and be ready to repeat if it lands on the eMMC prompt again.
3. Once `run_visioncb_hil.py` reports `state=UBOOT` (or completes a full `PASS`), the board is healthy and stays healthy across further runs via the software-`reboot` path — no further physical action is needed until the next cold/hung state.

**Known limitation, explicitly not yet resolved**: there is no automatic/remote power control for the board today. A USB-controlled 12V relay has been ordered to eventually make this recovery step scriptable and reliable without a human present at the physical hardware, but it is not yet delivered, installed, or integrated into any workflow. Until then, cold/hung-board recovery is a manual, human-in-the-loop procedure — this is a real, current limitation, not a hypothetical one. Integrating the relay is planned as a separate follow-up phase once the exact hardware, its USB protocol, and its default power-on state have been physically verified; no speculative relay code exists in this repository.

## Safety constraints (all enforced by `run_visioncb_hil.py`, not just documented)

- **SD-only deployment**: every run asserts `findmnt /` reports `/dev/mmcblk0p2` before proceeding, and re-asserts it after every reboot.
- **eMMC is never written**: any `mmcblk2*` partitions the stock image auto-mounts are unmounted (read-only from this flow's perspective); no `mmcblk2` write ever occurs.
- **No persistent U-Boot changes**: no `saveenv` anywhere in the code; boot commands are issued interactively each run, never persisted.
- **No SWD/JTAG/RTT**: all communication is over the SEGGER J-Link's UART passthrough only.
- **No credentials sent to an unconfirmed system**: the console classifier verifies OS identity (banner text, or a safe read-only `cat /etc/issue`) before ever attempting login — see the eMMC/autoboot section above.
- **No indefinite retries against a hung board**: `RECOVERY_REQUIRED` is a firm stop, not a retry loop.

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
- **`.github/workflows/visioncb-hil.yml`** — the real physical HIL workflow. **Never triggers on `pull_request`** — only `workflow_dispatch` and a nightly `schedule` (`17 3 * * *` **UTC**, not local time). Serializes hardware access via a `concurrency` group (a queued run waits rather than cancelling an in-progress hardware session). Bounded to a 20-minute job timeout. Resolves the requested Quarry ref in a **dedicated CI-only clone** (`/home/igor/.cache/visioncb-hil/quarry-ci-checkout` on the runner host) — never the developer's own interactive checkout — so a nightly run can never switch out anyone's working branch. Invokes `run_visioncb_hil.py` unmodified (all hardware logic stays in Python, not YAML). Uploads `result.json` / `runner.log` / `console.log` as artifacts on every run (pass or fail), 90-day retention.

**Manual dispatch** (also the release/RC validation mechanism):
```
gh workflow run visioncb-hil.yml -f quarry_ref=main            # ad-hoc / nightly-equivalent
gh workflow run visioncb-hil.yml -f quarry_ref=v1.2.3-rc.1      # release/RC validation
```
The `quarry_ref` accepts a branch name, tag, or commit SHA. The exact resolved commit is recorded in the run's step summary and in `result.json`'s `quarry.commit` field — a result is never published as "tested latest Quarry" without a SHA.

**Nightly**: runs automatically against current Quarry `main` every day at 03:17 UTC. A nightly failure is visible as a normal failed GitHub Actions run; nothing modifies Quarry automatically based on the result.

**Release validation**: there is currently **no automatic Quarry→quarry-hil cross-repo trigger** — that would require a new PAT/secret stored in the Quarry repository that does not yet exist, and none was fabricated. Today, release validation means a human manually dispatches `visioncb-hil.yml` with `quarry_ref` set to the release/RC tag after cutting it in Quarry. This workflow **runs against** a given ref; it does not currently **block** any Quarry release — those are two different things, and only the former is actually implemented.

**Current status honestly stated**: normal scheduled HIL execution (nightly, manual dispatch, software `reboot` between healthy runs) is automated end-to-end. Recovery from a cold power-on race or a genuinely hung board still requires a human physically at the hardware — this is **not** a fully unattended system yet, and won't be until the ordered USB relay is delivered, installed, and integrated in a follow-up phase.
