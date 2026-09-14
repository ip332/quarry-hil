# udev rules

Host-level udev rules the HIL runners depend on. **Not applied automatically by checking out this repo** — udev only reads rules from `/etc/udev/rules.d/` (or `/usr/lib/udev/rules.d/`), so each file here has to be installed on any host that runs the HIL locally or hosts the self-hosted CI runner. These are checked in so that setup is reproducible and the reasoning behind each rule survives a fresh machine, rather than living only in shell history.

## Install

```
sudo cp infrastructure/udev/*.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## Rules

### `52-uhubctl-nucleo.rules` (NUCLEO-F446RE)

Grants the `plugdev` group control access to the VIA Labs VL812 USB2.0 hub (`idVendor=2109`, `idProduct=2812`) that the NUCLEO-F446RE's ST-LINK/V2.1 sits behind. Without this, the hub's own USB device node (`/dev/bus/usb/<bus>/<dev>`) is root-only (`crw-rw-r-- root root`), and `uhubctl` — which talks to the hub directly via that node, not through the ST-LINK's own device file — fails even for a user in `dialout`/`plugdev` more generally. Used by `infrastructure/core/usb_hub_power.py`.

Verify it took effect:
```
uhubctl -l 3-11.2 -p 3   # should list the port with no sudo needed
```
(`3-11.2` / port `3` is this specific host's hub topology — see `usb_hub_power.py`'s `HUB_LOCATION`/`HUB_PORT`. A different host would need `uhubctl` run once to find its own hub location for the ST-LINK.)

### `53-udisks-ignore-nucleo.rules` (NUCLEO-F446RE)

Stops `udisks2`/`gvfs` from auto-mounting the ST-LINK/V2.1's own mass-storage LUN (labeled `NOD_F446RE`, its drag-and-drop flashing volume — unused by this HIL flow, which flashes over SWD via `st-flash` instead). Without this, every USB power-cycle (`usb_hub_power.power_cycle()`) makes that volume reappear and re-triggers auto-mount, which on a desktop session surfaces as a mount-authentication popup, and on the headless self-hosted CI host (no desktop session to grant it) is just friction with no one to answer it.

Matched via `ATTRS{serial}` (a plain sysfs attribute, readable immediately) rather than `ENV{ID_SERIAL_SHORT}`. The latter looks more obviously "correct" but is only populated later, by `/usr/lib/udev/rules.d/60-persistent-storage.rules`'s `IMPORT{builtin}="usb_id"` — since udev rule files are evaluated in filename sort order and this file is deliberately numbered `53` (see "Numbering" below), a rule keyed on `ID_SERIAL_SHORT` here would evaluate before that property exists and would silently never match. This was diagnosed by comparing `udevadm info --attribute-walk` (which showed `ATTRS{serial}` available on the ST-LINK's own USB device node, one level up from the block device) against `udevadm info --query=property` (which only showed `ID_SERIAL_SHORT` well after the fact).

Verify it took effect (after a power-cycle, so a fresh device add event actually re-evaluates the rule):
```
udevadm info --query=property --name=/dev/sdX | grep IGNORE   # should show UDISKS_IGNORE=1
udisksctl info -b /dev/sdX | grep HintIgnore                  # should show true
```

### VisionCB-8M-STD's relay — no custom rule needed

The `usbrelay` CLI's own Debian package ships `/usr/lib/udev/rules.d/92-usbrelay.rules`, which already grants non-root HID access to the dcttech USBRelay4 board `infrastructure/core/power_relay.py` talks to. Nothing extra to install here beyond `sudo apt-get install -y usbrelay`.

## Numbering

Filenames are evaluated by udev in lexical sort order, and that order matters here (see the `53-udisks-ignore-nucleo.rules` note above): custom rules that only read early-available data (plain `ATTR{}`/`ATTRS{}` sysfs attributes) aren't order-sensitive, but any rule that reads an `ENV{}` property populated by another rule (like `ID_SERIAL_SHORT`) must sort after whichever file sets it. `52`/`53` here just continue this host's existing local numbering gap between the system defaults (`50-*`, `60-*`) and vendor-shipped rules (`70-*` snap rules, `80-udisks2.rules`, `92-usbrelay.rules`, `99-*`); there's no requirement to use exactly these numbers on another host, only to keep any `ENV{}`-dependent rule after its source.
