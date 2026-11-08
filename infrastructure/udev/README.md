# udev rules

Host-level udev rules the HIL runners depend on. **Not applied automatically by checking out this repo** — udev only reads rules from `/etc/udev/rules.d/` (or `/usr/lib/udev/rules.d/`), so each file here has to be installed on any host that runs the HIL locally or hosts the self-hosted CI runner. These are checked in so that setup is reproducible and the reasoning behind each rule survives a fresh machine, rather than living only in shell history.

## Install

```
sudo cp infrastructure/udev/*.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## Rules

### `52-uhubctl-shared-hub.rules`

Grants the `plugdev` group control access to the VIA Labs VL812 USB2.0 hub (`idVendor=2109`, `idProduct=2812`) on this host. Without this, the hub's own USB device node (`/dev/bus/usb/<bus>/<dev>`) is root-only (`crw-rw-r-- root root`), and `uhubctl` — which talks to the hub directly via that node — fails even for a user in `dialout`/`plugdev` more generally.

Two independent things happen to sit behind different ports on this same physical hub, and both rely on this one rule:

- **NUCLEO-F446RE's ST-LINK/V2.1** (port 3) — its own power *is* the board's power (no separate power rail). **Do not power-cycle this port**: doing so leaves the STM32 unreachable over SWD until the USB cable is physically replugged (reproduced 2026-09-29), so the NUCLEO runner never touches it.
- **VisionCB-8M-STD's SEGGER J-Link probe** (port 1) — a separate USB device from the board itself (the board's power comes from a relay instead, see `power_relay.py`), which has been observed to drop off the USB bus entirely between runs. `run_visioncb_hil.py`'s `recover_probe()` power-cycles this port as recovery if the probe isn't found, or if the board stays silent (a wedged probe keeps its serial port but delivers no bytes).

Both uses go through the same `infrastructure/core/usb_hub_power.py` module, parameterized by hub location + port rather than hardcoded to one board.

Verify it took effect:
```
uhubctl -l 3-11.2   # should list the hub's ports with no sudo needed
```
(`3-11.2` is this specific host's hub location. A different host would need `uhubctl` run once, with the relevant device connected, to find its own hub location — `lsusb -t` plus `udevadm info --query=path --name=/dev/bus/usb/<bus>/<dev>` shows the sysfs devpath, whose last `bus-port[.port...]` segment is uhubctl's `-l` location.)

### `53-udisks-ignore-nucleo.rules` (NUCLEO-F446RE)

Stops `udisks2`/`gvfs` from auto-mounting the ST-LINK/V2.1's own mass-storage LUN (labeled `NOD_F446RE`). That is its drag-and-drop flashing volume, which this HIL flow doesn't use: it flashes over SWD via `st-flash`. The volume reappears every time the ST-LINK re-enumerates (cable replug, host reboot). Without this rule, that re-triggers auto-mount, which shows a mount-authentication popup in a desktop session and, on the headless self-hosted CI host, is just friction with no one to answer it.

The rule matches `ATTRS{serial}` (a plain sysfs attribute, readable immediately), not `ENV{ID_SERIAL_SHORT}`. The latter looks more obviously "correct", but it is only populated later, by `/usr/lib/udev/rules.d/60-persistent-storage.rules`'s `IMPORT{builtin}="usb_id"`. udev evaluates rule files in filename sort order, and this file is numbered `53` (see "Numbering" below), so a rule keyed on `ID_SERIAL_SHORT` would run before that property exists and silently never match. This was diagnosed by comparing `udevadm info --attribute-walk`, which showed `ATTRS{serial}` on the ST-LINK's own USB device node one level above the block device, with `udevadm info --query=property`, which only showed `ID_SERIAL_SHORT` well after the fact.

Verify it took effect (after a replug, so a fresh device add event re-evaluates the rule):
```
udevadm info --query=property --name=/dev/sdX | grep IGNORE   # should show UDISKS_IGNORE=1
udisksctl info -b /dev/sdX | grep HintIgnore                  # should show true
```

### VisionCB-8M-STD's relay — no custom rule needed

The `usbrelay` CLI's own Debian package ships `/usr/lib/udev/rules.d/92-usbrelay.rules`, which already grants non-root HID access to the dcttech USBRelay4 board `infrastructure/core/power_relay.py` talks to. Nothing extra to install here beyond `sudo apt-get install -y usbrelay`.

## Numbering

udev evaluates filenames in lexical sort order, and that order matters here (see the `53-udisks-ignore-nucleo.rules` note above). Custom rules that only read early-available data (plain `ATTR{}`/`ATTRS{}` sysfs attributes) aren't order-sensitive. Any rule that reads an `ENV{}` property set by another rule (like `ID_SERIAL_SHORT`) must sort after the file that sets it. `52`/`53` just continue this host's existing local numbering gap between the system defaults (`50-*`, `60-*`) and vendor-shipped rules (`70-*` snap rules, `80-udisks2.rules`, `92-usbrelay.rules`, `99-*`). Another host doesn't have to use exactly these numbers, only keep any `ENV{}`-dependent rule after its source.
