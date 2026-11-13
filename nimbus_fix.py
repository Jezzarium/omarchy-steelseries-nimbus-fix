#!/usr/bin/env python3
"""Fix SteelSeries Nimbus (0111:1420) Linux input via hidraw + uinput.

The SteelSeries Nimbus Apple MFi HID report descriptor exposes 4x 8-bit
analog D-pad usages (0x90..0x93), 8x 8-bit analog buttons (A/B/X/Y/L1/R1/L2/R2),
a 1-bit Menu button (AC Home), and 4x signed 8-bit stick axes with inverted
Y-axes and right stick on Z/Rz.

Linux's hid-input driver corrupts the D-pad (locking ABS_HAT0Y=-1 at rest and
dropping Right/Down/Left) and collapses analog L2/R2 triggers to digital keys.
This daemon reads raw 17-byte HID reports directly from /dev/hidraw* (falling
back to evdev only if hidraw permission is denied) and exposes a standard
Microsoft X-Box 360 pad via /dev/uinput.
"""

from __future__ import annotations

import array
import errno
import fcntl
import glob
import os
import select
import signal
import struct
import sys
import time

# Target SteelSeries Nimbus Bluetooth HID device
NIMBUS_BUSTYPE = 0x0005  # BUS_BLUETOOTH
NIMBUS_VENDOR = 0x0111
NIMBUS_PRODUCT = 0x1420

# Emulate standard Microsoft X-Box 360 pad so SDL2 / Steam / Proton / Wine
# apply native Xbox 360 mappings with analog triggers and full axis range.
VIRTUAL_NAME = b"Microsoft X-Box 360 pad"
VIRTUAL_BUSTYPE = 0x0003  # BUS_USB
VIRTUAL_VENDOR = 0x045E
VIRTUAL_PRODUCT = 0x028E
VIRTUAL_VERSION = 0x0114

# Linux input / uinput constants (from <linux/input-event-codes.h>)
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

SYN_REPORT = 0x00

ABS_X = 0x00
ABS_Y = 0x01
ABS_Z = 0x02
ABS_RX = 0x03
ABS_RY = 0x04
ABS_RZ = 0x05
ABS_HAT0X = 0x10
ABS_HAT0Y = 0x11

KEY_HOMEPAGE = 172
BTN_SOUTH = 304   # BTN_A (bottom)
BTN_EAST = 305    # BTN_B (right)
BTN_NORTH = 307   # BTN_X in input-event-codes.h (SDL x:b2 on Xbox 360)
BTN_WEST = 308    # BTN_Y in input-event-codes.h (SDL y:b3 on Xbox 360)
BTN_TL = 310      # LB / L1
BTN_TR = 311      # RB / R1
BTN_SELECT = 314  # Back
BTN_START = 315   # Start
BTN_MODE = 316    # Guide
BTN_THUMBL = 317  # L3
BTN_THUMBR = 318  # R3

# ioctl numbers (x86_64 / asm-generic/ioctl.h)
EVIOCGID = 0x80084502
EVIOCGRAB = 0x40044590
EVIOCGNAME_256 = 0x81004506
HIDIOCGRAWINFO = 0x80084803  # _IOR('H', 0x03, struct hidraw_devinfo)

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503   # _IOW('U', 3, struct uinput_setup) [92 bytes]
UI_ABS_SETUP = 0x401C5504   # _IOW('U', 4, struct uinput_abs_setup) [28 bytes]
UI_SET_EVBIT = 0x40045564   # _IOW('U', 100, int)
UI_SET_KEYBIT = 0x40045565  # _IOW('U', 101, int)
UI_SET_ABSBIT = 0x40045567  # _IOW('U', 103, int)

INPUT_EVENT_STRUCT = struct.Struct("llHHi")  # time_sec, time_usec, type, code, value
HIDRAW_DEVINFO_STRUCT = struct.Struct("Ihh")  # bustype, vendor, product
NIMBUS_REPORT_STRUCT = struct.Struct("<12BB4b")  # 17-byte raw Nimbus input report

# Thresholds, hysteresis, and Menu button timing
DPAD_PRESS_THRESHOLD = 24
DPAD_RELEASE_THRESHOLD = 12
BUTTON_PRESS_THRESHOLD = 16
BUTTON_RELEASE_THRESHOLD = 8
TRIGGER_DEADZONE = 4

MENU_HOLD_SECONDS = 0.40
MENU_TAP_PULSE_SECONDS = 0.05
EACCES_LOG_COOLDOWN_SECONDS = 30.0

INSTALL_UDEV_HINT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install-udev.sh")

# Raw HID report byte indices 4..9 -> standard Xbox 360 EV_KEY codes
HIDRAW_BUTTON_CODES = (
    BTN_SOUTH,  # Byte 4: A
    BTN_EAST,   # Byte 5: B
    BTN_NORTH,  # Byte 6: X (BTN_X = 307)
    BTN_WEST,   # Byte 7: Y (BTN_Y = 308)
    BTN_TL,     # Byte 8: L1
    BTN_TR,     # Byte 9: R1
)

# Fallback evdev key map (used only if hidraw cannot be opened due to EACCES)
EVDEV_KEY_MAP: dict[int, int] = {
    304: BTN_SOUTH,  # A
    305: BTN_EAST,   # B
    306: BTN_NORTH,  # X (BTN_C on raw hid-input -> BTN_X = 307)
    307: BTN_WEST,   # Y (BTN_NORTH on raw hid-input -> BTN_Y = 308)
    308: BTN_TL,     # L1
    309: BTN_TR,     # R1
}

EVDEV_TRIGGER_KEY_MAP: dict[int, int] = {
    310: ABS_Z,   # L2 -> Left Trigger
    311: ABS_RZ,  # R2 -> Right Trigger
}

EVDEV_ABS_MAP: dict[int, tuple[int, bool, bool]] = {
    ABS_X: (ABS_X, False, True),
    ABS_Y: (ABS_Y, True, True),
    ABS_Z: (ABS_RX, False, True),
    ABS_RZ: (ABS_RY, True, True),
    ABS_HAT0X: (ABS_HAT0X, False, False),
    ABS_HAT0Y: (ABS_HAT0Y, False, False),
}

# Standard Xbox 360 key set (exact order matches kernel xpad driver)
VIRTUAL_KEYS = (
    BTN_SOUTH,
    BTN_EAST,
    BTN_WEST,
    BTN_NORTH,
    BTN_TL,
    BTN_TR,
    BTN_SELECT,
    BTN_START,
    BTN_MODE,
    BTN_THUMBL,
    BTN_THUMBR,
)

# (code, value, min, max, fuzz, flat, resolution)
VIRTUAL_ABS_AXES = (
    (ABS_X, 0, -32768, 32767, 16, 128, 0),
    (ABS_Y, 0, -32768, 32767, 16, 128, 0),
    (ABS_Z, 0, 0, 255, 0, 0, 0),
    (ABS_RX, 0, -32768, 32767, 16, 128, 0),
    (ABS_RY, 0, -32768, 32767, 16, 128, 0),
    (ABS_RZ, 0, 0, 255, 0, 0, 0),
    (ABS_HAT0X, 0, -1, 1, 0, 0, 0),
    (ABS_HAT0Y, 0, -1, 1, 0, 0, 0),
)


def apply_hysteresis(raw_val: int, was_pressed: bool, press_thresh: int, release_thresh: int) -> bool:
    """Apply Schmitt-trigger hysteresis to an 8-bit pressure-sensitive input."""
    if was_pressed:
        return raw_val >= release_thresh
    return raw_val >= press_thresh


def scale_stick_axis(raw_val: int, invert: bool) -> int:
    """Scale Nimbus -127..127 stick axis to Xbox 360 -32768..32767."""
    val = -raw_val if invert else raw_val
    scaled = int(round(val * (32767.0 / 127.0)))
    return max(-32768, min(32767, scaled))


def scale_trigger(raw_val: int) -> int:
    """Apply small deadzone to 0..255 analog trigger pressure."""
    if raw_val < TRIGGER_DEADZONE:
        return 0
    return min(255, raw_val)


class MenuButtonTracker:
    """Turns Nimbus Menu press into a 50ms BTN_START pulse (tap) or BTN_MODE (hold)."""

    def __init__(self) -> None:
        self.down = False
        self.pressed_at: float | None = None
        self.sent_guide = False
        self.start_release_at: float | None = None

    def update(self, pressed: bool, now: float) -> list[tuple[int, int, int]]:
        """Process a Menu button state sample and return EV_KEY events to emit."""
        events: list[tuple[int, int, int]] = []
        if pressed and not self.down:
            if self.start_release_at is not None:
                # Release pending tap pulse before starting a new press cycle.
                events.append((EV_KEY, BTN_START, 0))
                self.start_release_at = None
            self.down = True
            self.pressed_at = now
            self.sent_guide = False
        elif not pressed and self.down:
            self.down = False
            if self.sent_guide:
                events.append((EV_KEY, BTN_MODE, 0))
            elif self.pressed_at is not None:
                events.append((EV_KEY, BTN_START, 1))
                self.start_release_at = now + MENU_TAP_PULSE_SECONDS
            self.pressed_at = None
            self.sent_guide = False
        return events

    def tick(self, now: float) -> list[tuple[int, int, int]]:
        """Check hold/release timers and return EV_KEY events to emit."""
        events: list[tuple[int, int, int]] = []
        if self.down and not self.sent_guide and self.pressed_at is not None:
            if now - self.pressed_at >= MENU_HOLD_SECONDS:
                self.sent_guide = True
                events.append((EV_KEY, BTN_MODE, 1))
        if self.start_release_at is not None and now >= self.start_release_at:
            self.start_release_at = None
            events.append((EV_KEY, BTN_START, 0))
        return events

    def next_deadline(self) -> float | None:
        """Return the earliest monotonic timestamp when tick() needs to run."""
        deadlines: list[float] = []
        if self.down and not self.sent_guide and self.pressed_at is not None:
            deadlines.append(self.pressed_at + MENU_HOLD_SECONDS)
        if self.start_release_at is not None:
            deadlines.append(self.start_release_at)
        return min(deadlines) if deadlines else None


class NimbusReportParser:
    """Stateful parser for 17-byte SteelSeries Nimbus raw HID input reports."""

    def __init__(self) -> None:
        self.dpad_state = [False, False, False, False]  # Up, Right, Down, Left
        self.button_state = [False] * len(HIDRAW_BUTTON_CODES)
        self.last_abs: dict[int, int] = {code: 0 for code, *_ in VIRTUAL_ABS_AXES}
        self.last_keys: dict[int, int] = {code: 0 for code in HIDRAW_BUTTON_CODES}
        self.menu = MenuButtonTracker()

    def parse_packet(self, data: bytes, now: float) -> list[tuple[int, int, int]] | None:
        """Parse one raw HID packet and return changed Xbox 360 events + SYN_REPORT.

        Returns None if the packet length is unrecognized.
        """
        if len(data) == NIMBUS_REPORT_STRUCT.size:
            offset = 0
        elif len(data) == NIMBUS_REPORT_STRUCT.size + 1 and data[0] == 0:
            offset = 1
        else:
            return None

        fields = NIMBUS_REPORT_STRUCT.unpack_from(data, offset)
        dpad_raw = fields[0:4]       # Up, Right, Down, Left (0..255)
        buttons_raw = fields[4:10]   # A, B, X, Y, L1, R1 (0..255)
        l2_raw, r2_raw = fields[10], fields[11]
        menu_byte = fields[12]
        lx, ly, rx, ry = fields[13], fields[14], fields[15], fields[16]

        events: list[tuple[int, int, int]] = []

        for idx, raw_val in enumerate(dpad_raw):
            self.dpad_state[idx] = apply_hysteresis(
                raw_val, self.dpad_state[idx], DPAD_PRESS_THRESHOLD, DPAD_RELEASE_THRESHOLD
            )
        hat_x = int(self.dpad_state[1]) - int(self.dpad_state[3])  # Right - Left
        hat_y = int(self.dpad_state[2]) - int(self.dpad_state[0])  # Down - Up

        abs_values = (
            (ABS_X, scale_stick_axis(lx, invert=False)),
            (ABS_Y, scale_stick_axis(ly, invert=True)),
            (ABS_Z, scale_trigger(l2_raw)),
            (ABS_RX, scale_stick_axis(rx, invert=False)),
            (ABS_RY, scale_stick_axis(ry, invert=True)),
            (ABS_RZ, scale_trigger(r2_raw)),
            (ABS_HAT0X, hat_x),
            (ABS_HAT0Y, hat_y),
        )
        for code, value in abs_values:
            if self.last_abs[code] != value:
                self.last_abs[code] = value
                events.append((EV_ABS, code, value))

        for idx, (code, raw_val) in enumerate(zip(HIDRAW_BUTTON_CODES, buttons_raw)):
            pressed = apply_hysteresis(
                raw_val, self.button_state[idx], BUTTON_PRESS_THRESHOLD, BUTTON_RELEASE_THRESHOLD
            )
            self.button_state[idx] = pressed
            val = 1 if pressed else 0
            if self.last_keys[code] != val:
                self.last_keys[code] = val
                events.append((EV_KEY, code, val))

        events.extend(self.menu.update(bool(menu_byte & 0x01), now))

        if events:
            events.append((EV_SYN, SYN_REPORT, 0))
        return events

    def tick(self, now: float) -> list[tuple[int, int, int]]:
        """Advance Menu button hold/release timers."""
        events = self.menu.tick(now)
        if events:
            events.append((EV_SYN, SYN_REPORT, 0))
        return events


def translate_evdev_event(ev_type: int, code: int, value: int) -> tuple[int, int, int] | None:
    """Translate a stateless raw evdev Nimbus input_event into an Xbox 360 event."""
    if ev_type == EV_SYN:
        return (EV_SYN, code, value)
    if ev_type == EV_KEY:
        if code in EVDEV_TRIGGER_KEY_MAP:
            return (EV_ABS, EVDEV_TRIGGER_KEY_MAP[code], 255 if value else 0)
        mapped = EVDEV_KEY_MAP.get(code)
        if mapped is None:
            return None
        return (EV_KEY, mapped, 1 if value else 0)
    if ev_type == EV_ABS:
        spec = EVDEV_ABS_MAP.get(code)
        if spec is None:
            return None
        new_code, invert, scale_s16 = spec
        if scale_s16:
            value = scale_stick_axis(value, invert)
        return (EV_ABS, new_code, value)
    return None


def parse_hid_id(hid_id: str) -> tuple[int, int, int] | None:
    """Parse sysfs HID_ID ('BBBB:VVVVVVVV:PPPPPPPP') into (bustype, vendor, product)."""
    parts = hid_id.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0], 16), int(parts[1], 16), int(parts[2], 16))
    except ValueError:
        return None


def find_nimbus_hidraw_devices(skip_paths: set[str] | None = None) -> list[str]:
    """Return sorted /dev/hidraw* paths matching a SteelSeries Nimbus."""
    matches: list[str] = []
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        dev_name = os.path.basename(sys_path)
        dev_path = f"/dev/{dev_name}"
        if skip_paths and dev_path in skip_paths:
            continue
        uevent_path = os.path.join(sys_path, "device", "uevent")
        try:
            with open(uevent_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("HID_ID="):
                        parsed = parse_hid_id(line.split("=", 1)[1])
                        if parsed == (NIMBUS_BUSTYPE, NIMBUS_VENDOR, NIMBUS_PRODUCT):
                            matches.append(dev_path)
                        break
        except OSError:
            continue
    return matches


def is_nimbus_event_device(path: str) -> bool:
    """Return True if path is a raw Bluetooth SteelSeries Nimbus event device."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        id_buf = array.array("H", [0, 0, 0, 0])
        fcntl.ioctl(fd, EVIOCGID, id_buf)
        bustype, vendor, product, _version = id_buf
        if bustype != NIMBUS_BUSTYPE or vendor != NIMBUS_VENDOR or product != NIMBUS_PRODUCT:
            return False
        name_buf = bytearray(256)
        n = fcntl.ioctl(fd, EVIOCGNAME_256, name_buf)
        name = name_buf[:n].rstrip(b"\x00")
        return name != VIRTUAL_NAME
    except OSError:
        return False
    finally:
        os.close(fd)


def find_nimbus_evdev_devices(skip_paths: set[str] | None = None) -> list[str]:
    """Return sorted /dev/input/event* paths matching a raw SteelSeries Nimbus."""
    matches: list[str] = []
    for path in sorted(glob.glob("/dev/input/event*")):
        if skip_paths and path in skip_paths:
            continue
        if is_nimbus_event_device(path):
            matches.append(path)
    return matches


def create_virtual_gamepad() -> int:
    """Create and register a virtual Xbox 360 uinput device; return its fd."""
    ufd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(ufd, UI_SET_EVBIT, EV_KEY)
        for key in VIRTUAL_KEYS:
            fcntl.ioctl(ufd, UI_SET_KEYBIT, key)

        fcntl.ioctl(ufd, UI_SET_EVBIT, EV_ABS)
        for code, val, minimum, maximum, fuzz, flat, res in VIRTUAL_ABS_AXES:
            fcntl.ioctl(ufd, UI_SET_ABSBIT, code)
            abs_setup = struct.pack("H6i", code, val, minimum, maximum, fuzz, flat, res)
            fcntl.ioctl(ufd, UI_ABS_SETUP, abs_setup)

        uinput_setup = struct.pack(
            "4H80sI",
            VIRTUAL_BUSTYPE,
            VIRTUAL_VENDOR,
            VIRTUAL_PRODUCT,
            VIRTUAL_VERSION,
            VIRTUAL_NAME,
            0,
        )
        fcntl.ioctl(ufd, UI_DEV_SETUP, uinput_setup)
        fcntl.ioctl(ufd, UI_DEV_CREATE)
        return ufd
    except Exception:
        os.close(ufd)
        raise


def destroy_virtual_gamepad(ufd: int) -> None:
    """Destroy and close the virtual uinput device."""
    try:
        fcntl.ioctl(ufd, UI_DEV_DESTROY)
    except OSError:
        pass
    try:
        os.close(ufd)
    except OSError:
        pass


class _BaseBridge:
    """Shared virtual Xbox 360 uinput emission and lifecycle management."""

    is_fallback = False

    def __init__(self, path: str, src_fd: int) -> None:
        self.path = path
        self.src_fd = src_fd
        self.uinput_fd = create_virtual_gamepad()

    def _emit(self, events: list[tuple[int, int, int]]) -> bool:
        if not events:
            return True
        out = bytearray()
        for ev_type, code, value in events:
            out += INPUT_EVENT_STRUCT.pack(0, 0, ev_type, code, value)
        try:
            os.write(self.uinput_fd, out)
            return True
        except OSError:
            return False

    def next_deadline(self) -> float | None:
        return None

    def tick(self, now: float) -> bool:
        return True

    def process_available(self) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        try:
            os.close(self.src_fd)
        except OSError:
            pass
        destroy_virtual_gamepad(self.uinput_fd)
        print(f"nimbus-fix: detached from {self.path}", flush=True)


class NimbusHidrawBridge(_BaseBridge):
    """Reads raw 17-byte HID reports from /dev/hidraw* and emits Xbox 360 events."""

    is_fallback = False

    def __init__(self, path: str) -> None:
        src_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            info_buf = bytearray(HIDRAW_DEVINFO_STRUCT.size)
            fcntl.ioctl(src_fd, HIDIOCGRAWINFO, info_buf)
            bustype, vendor, product = HIDRAW_DEVINFO_STRUCT.unpack(info_buf)
            if (
                bustype != NIMBUS_BUSTYPE
                or (vendor & 0xFFFF) != NIMBUS_VENDOR
                or (product & 0xFFFF) != NIMBUS_PRODUCT
            ):
                raise OSError(errno.ENODEV, f"{path} is not SteelSeries Nimbus")
            super().__init__(path, src_fd)
        except Exception:
            os.close(src_fd)
            raise
        self.parser = NimbusReportParser()
        self._warned_unexpected_len = False
        print(f"nimbus-fix: attached hidraw {path} -> {VIRTUAL_NAME.decode()}", flush=True)

    def next_deadline(self) -> float | None:
        return self.parser.menu.next_deadline()

    def tick(self, now: float) -> bool:
        return self._emit(self.parser.tick(now))

    def process_available(self) -> bool:
        now = time.monotonic()
        while True:
            try:
                data = os.read(self.src_fd, 64)
            except BlockingIOError:
                return True
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return True
                return False

            if not data:
                return False

            events = self.parser.parse_packet(data, now)
            if events is None:
                if not self._warned_unexpected_len:
                    self._warned_unexpected_len = True
                    print(
                        f"nimbus-fix: ignoring unexpected {len(data)}-byte report from {self.path}",
                        file=sys.stderr,
                        flush=True,
                    )
                continue
            if not self._emit(events):
                return False


class NimbusEvdevBridge(_BaseBridge):
    """Fallback bridge grabbing /dev/input/event* when hidraw is inaccessible."""

    is_fallback = True

    def __init__(self, path: str) -> None:
        src_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(src_fd, EVIOCGRAB, 1)
            super().__init__(path, src_fd)
        except Exception:
            os.close(src_fd)
            raise
        self.menu = MenuButtonTracker()
        print(f"nimbus-fix: attached evdev fallback {path} -> {VIRTUAL_NAME.decode()}", flush=True)

    def next_deadline(self) -> float | None:
        return self.menu.next_deadline()

    def tick(self, now: float) -> bool:
        events = self.menu.tick(now)
        if events:
            events.append((EV_SYN, SYN_REPORT, 0))
            return self._emit(events)
        return True

    def process_available(self) -> bool:
        while True:
            try:
                data = os.read(self.src_fd, INPUT_EVENT_STRUCT.size * 64)
            except BlockingIOError:
                return True
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return True
                return False

            if not data:
                return False

            now = time.monotonic()
            batch: list[tuple[int, int, int]] = []
            for _sec, _usec, ev_type, code, value in INPUT_EVENT_STRUCT.iter_unpack(data):
                if ev_type == EV_KEY and code == KEY_HOMEPAGE:
                    menu_events = self.menu.update(bool(value), now)
                    if menu_events:
                        batch.extend(menu_events)
                        batch.append((EV_SYN, SYN_REPORT, 0))
                    continue
                translated = translate_evdev_event(ev_type, code, value)
                if translated is not None:
                    batch.append(translated)

            if not self._emit(batch):
                return False

    def close(self) -> None:
        try:
            fcntl.ioctl(self.src_fd, EVIOCGRAB, 0)
        except OSError:
            pass
        super().close()


def run_daemon() -> int:
    """Monitor for SteelSeries Nimbus devices and bridge them until signaled."""
    running = True

    def handle_signal(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    bridges: dict[str, _BaseBridge] = {}
    last_scan = 0.0
    last_eacces_log = 0.0

    try:
        while running:
            now = time.monotonic()
            scan_interval = 2.0 if bridges else 1.0
            if now - last_scan >= scan_interval:
                last_scan = now
                hidraw_denied = False
                for path in find_nimbus_hidraw_devices(set(bridges.keys())):
                    try:
                        bridge = NimbusHidrawBridge(path)
                    except OSError as exc:
                        if exc.errno in (errno.EACCES, errno.EPERM):
                            hidraw_denied = True
                            if now - last_eacces_log >= EACCES_LOG_COOLDOWN_SECONDS:
                                last_eacces_log = now
                                print(
                                    f"nimbus-fix: permission denied opening {path} (0111:1420); "
                                    f"run '{INSTALL_UDEV_HINT}' and reconnect the controller",
                                    file=sys.stderr,
                                    flush=True,
                                )
                        else:
                            print(
                                f"nimbus-fix: failed to attach {path}: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                    else:
                        # Drop any evdev fallback bridges once hidraw succeeds.
                        for old_path in [p for p, b in bridges.items() if b.is_fallback]:
                            bridges.pop(old_path).close()
                        bridges[path] = bridge

                has_hidraw = any(not b.is_fallback for b in bridges.values())
                if hidraw_denied and not has_hidraw:
                    for path in find_nimbus_evdev_devices(set(bridges.keys())):
                        try:
                            bridges[path] = NimbusEvdevBridge(path)
                        except OSError as exc:
                            print(
                                f"nimbus-fix: failed to attach evdev fallback {path}: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )

            if not bridges:
                time.sleep(0.5)
                continue

            fds = {b.src_fd: path for path, b in bridges.items()}
            deadlines = [d for b in bridges.values() if (d := b.next_deadline()) is not None]
            if deadlines:
                timeout = max(0.005, min(1.0, min(deadlines) - time.monotonic()))
            else:
                timeout = 1.0

            try:
                readable, _, exceptional = select.select(list(fds.keys()), [], list(fds.keys()), timeout)
            except InterruptedError:
                continue
            except OSError:
                readable, exceptional = [], list(fds.keys())

            dead: list[str] = []
            for fd in set(readable) | set(exceptional):
                path = fds[fd]
                if fd in exceptional or not bridges[path].process_available():
                    dead.append(path)

            now = time.monotonic()
            for path, bridge in bridges.items():
                if path not in dead and not bridge.tick(now):
                    dead.append(path)

            for path in dead:
                bridges.pop(path).close()
    finally:
        for bridge in bridges.values():
            bridge.close()
        bridges.clear()

    return 0


if __name__ == "__main__":
    sys.exit(run_daemon())
