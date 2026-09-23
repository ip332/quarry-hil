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

- **NUCLEO-F446RE's ST-LINK/V2.1** (port 3) — its own power *is* the board's power (no separate power rail), so `infrastructure/core/usb_hub_power.py` power-cycles this port at the start of every NUCLEO HIL run and as recovery.
- **VisionCB-8M-STD's SEGGER J-Link probe** (port 1) — a separate USB device from the board itself (the board's power comes from a relay instead, see `power_relay.py`), which has been observed to drop off the USB bus entirely between runs. `run_visioncb_hil.py`'s `discover_device()` power-cycles this port as recovery if the probe isn't found.

Both uses go through the same `infrastructure/core/usb_hub_power.py` module, parameterized by hub location + port rather than hardcoded to one board.

Verify it took effect:
```
uhubctl -l 3-11.2   # should list the hub's ports with no sudo needed
```
(`3-11.2` is this specific host's hub location. A different host would need `uhubctl` run once, with the relevant device connected, to find its own hub location — `lsusb -t` plus `udevadm info --query=path --name=/dev/bus/usb/<bus>/<dev>` shows the sysfs devpath, whose last `bus-port[.port...]` segment is uhubctl's `-l` location.)
