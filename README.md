# SteelSeries Nimbus Fix (`io.github.jezzarium.steelseries-nimbus-fix`)

Headless Omarchy shell service plugin that fixes Linux input handling for the **SteelSeries Nimbus** Bluetooth controller (`0111:1420`) by reading raw HID reports via `/dev/hidraw*` and exposing a standard **Microsoft X-Box 360 pad** (`045e:028e`) via `/dev/uinput`.

## Root causes & what it fixes

The SteelSeries Nimbus uses an Apple MFi HID report descriptor (`0005:0111:1420`) with 4x 8-bit pressure-sensitive D-pad usages (`0x90..0x93`), 8x 8-bit analog buttons (`A/B/X/Y/L1/R1/L2/R2`), and 4x signed 8-bit stick axes. Linux's `hid-input` driver:

- Breaks the D-pad (`ABS_HAT0Y` locks to `-1` at rest; Right/Down/Left are dropped or corrupted)
- Collapses analog `L2`/`R2` triggers (`0..255`) into digital keys
- Exposes inverted analog stick Y-axes and puts the right stick on `ABS_Z`/`ABS_RZ`
- Maps buttons sequentially starting at `BTN_A` (including `BTN_C`/`BTN_Z`)

`nimbus_fix.py` bypasses `hid-input` by reading the 17-byte raw HID reports directly from `/dev/hidraw*` and emulating a standard Xbox 360 controller:

- **Working 8-way D-pad** (`ABS_HAT0X` / `ABS_HAT0Y`, `-1..1`) with Schmitt-trigger hysteresis
- **True analog triggers** (`ABS_Z` / `ABS_RZ`, `0..255`) for `L2`/`R2`
- **Correct Xbox 360 buttons** (`BTN_A`, `BTN_B`, `BTN_X`, `BTN_Y`, `BTN_TL`, `BTN_TR`) with hysteresis
- **Corrected analog sticks** (`-32768..32767`) on `ABS_X`/`ABS_Y` and `ABS_RX`/`ABS_RY`
- **Center Menu button**: short tap emits a 50ms `BTN_START` pulse; hold (`>= 0.4s`) emits `BTN_MODE` (Steam Guide)

## Required: Install the udev rule (`71-steelseries-nimbus-fix.rules`)

The udev rule runs at priority `71-` (before systemd's `73-seat-late.rules`) so that:

1. `/dev/hidraw*` for `0111:1420` is accessible (`uaccess`, `GROUP="input"`, `MODE="0660"`) to `nimbus_fix.py`.
2. Raw `/dev/input/event*` and `/dev/input/js*` nodes for `0111:1420` are completely hidden (`MODE:="0000"`, `TAG-="uaccess"`, `ID_INPUT*=""`) from Steam, SDL2, Wine/Proton, and libinput so only the virtual Xbox 360 pad is visible in Player 1 (Slot 0).

Install (or upgrade from the legacy `99-` rule) once:

```bash
~/.config/omarchy/plugins/io.github.jezzarium.steelseries-nimbus-fix/install-udev.sh
```

Then reconnect your Nimbus controller (or restart Steam).

## Proton / Wine & Bluetooth notes

- **Proton / Wine `hidraw`:** Proton disables raw `hidraw` gamepad enumeration in `winebus` by default (`DisableHidraw`). If you enable `PROTON_ENABLE_HIDRAW` and notice a duplicate raw Nimbus device in-game, set `PROTON_DISABLE_HIDRAW=0x0111/0x1420` or `SDL_GAMECONTROLLER_IGNORE_DEVICES=0x0111/0x1420`.
- **Standalone Wine (without Steam/Proton):** Upstream Wine enables `hidraw` in `winebus.sys` by default, which can expose the raw Nimbus alongside the virtual Xbox 360 pad. To force SDL/uinput only in a Wine prefix, run:
  ```bash
  wine reg add "HKEY_LOCAL_MACHINE\System\CurrentControlSet\Services\winebus" /v "DisableHidraw" /t REG_DWORD /d 1 /f
  wine reg add "HKEY_LOCAL_MACHINE\System\CurrentControlSet\Services\winebus" /v "Enable SDL" /t REG_DWORD /d 1 /f
  ```
- **Bluetooth latency:** Keep Bluetooth discovery (`bluetoothctl scan on` / Bluetooth pairing panels) turned off during gameplay so the adapter is not continuously scanning while polling HID reports.

## Requirements

- Read/write access to `/dev/uinput` (standard on Omarchy for users in `input` / active seat) and the included udev rule for `/dev/hidraw*`.
- Python 3 (standard library only).

## Install & Validate

```bash
omarchy plugin add https://github.com/Jezzarium/omarchy-steelseries-nimbus-fix.git --enable
~/.config/omarchy/plugins/io.github.jezzarium.steelseries-nimbus-fix/install-udev.sh
```

Or validate a local checkout:

```bash
omarchy plugin validate ~/.config/omarchy/plugins/io.github.jezzarium.steelseries-nimbus-fix
```

## Remove

```bash
omarchy plugin remove io.github.jezzarium.steelseries-nimbus-fix
sudo rm -f /etc/udev/rules.d/71-steelseries-nimbus-fix.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input --subsystem-match=hidraw
```

## Credits

Built with Gemini 4 Hydrogen.
