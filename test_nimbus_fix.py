#!/usr/bin/env python3
"""Unit tests for steelseries.nimbus-fix HID report parsing and event translation."""

from __future__ import annotations

import struct
import unittest

import nimbus_fix as nf


def make_report(
    *,
    up: int = 0,
    right: int = 0,
    down: int = 0,
    left: int = 0,
    a: int = 0,
    b: int = 0,
    x: int = 0,
    y: int = 0,
    l1: int = 0,
    r1: int = 0,
    l2: int = 0,
    r2: int = 0,
    menu: int = 0,
    lx: int = 0,
    ly: int = 0,
    rx: int = 0,
    ry: int = 0,
) -> bytes:
    return struct.pack(
        "<12BB4b",
        up,
        right,
        down,
        left,
        a,
        b,
        x,
        y,
        l1,
        r1,
        l2,
        r2,
        menu,
        lx,
        ly,
        rx,
        ry,
    )


class TestNimbusFix(unittest.TestCase):
    def test_rest_report_stays_centered_and_silent(self) -> None:
        parser = nf.NimbusReportParser()
        events = parser.parse_packet(make_report(), 1.0)
        self.assertEqual(events, [])
        self.assertEqual(parser.last_abs[nf.ABS_HAT0X], 0)
        self.assertEqual(parser.last_abs[nf.ABS_HAT0Y], 0)

    def test_malformed_packet_returns_none(self) -> None:
        parser = nf.NimbusReportParser()
        self.assertIsNone(parser.parse_packet(b"\x00" * 16, 1.0))
        self.assertIsNone(parser.parse_packet(b"\x01" + b"\x00" * 17, 1.0))
        self.assertEqual(parser.parse_packet(b"\x00" + make_report(), 1.0), [])

    def test_dpad_hysteresis_and_directions(self) -> None:
        parser = nf.NimbusReportParser()

        # Below press threshold (24) does not trigger
        self.assertEqual(parser.parse_packet(make_report(up=23), 1.0), [])

        # Up >= 24 triggers ABS_HAT0Y = -1
        ev = parser.parse_packet(make_report(up=24), 1.1)
        self.assertEqual(ev, [(nf.EV_ABS, nf.ABS_HAT0Y, -1), (nf.EV_SYN, nf.SYN_REPORT, 0)])

        # Stays pressed while >= 12 (hysteresis band)
        self.assertEqual(parser.parse_packet(make_report(up=12), 1.2), [])

        # Releases below 12
        ev = parser.parse_packet(make_report(up=11), 1.3)
        self.assertEqual(ev, [(nf.EV_ABS, nf.ABS_HAT0Y, 0), (nf.EV_SYN, nf.SYN_REPORT, 0)])

        # Diagonal Down-Right
        ev = parser.parse_packet(make_report(down=100, right=100), 1.4)
        self.assertEqual(
            ev,
            [
                (nf.EV_ABS, nf.ABS_HAT0X, 1),
                (nf.EV_ABS, nf.ABS_HAT0Y, 1),
                (nf.EV_SYN, nf.SYN_REPORT, 0),
            ],
        )

        # Left overrides to -1 when Right releases
        ev = parser.parse_packet(make_report(down=0, right=0, left=200), 1.5)
        self.assertEqual(
            ev,
            [
                (nf.EV_ABS, nf.ABS_HAT0X, -1),
                (nf.EV_ABS, nf.ABS_HAT0Y, 0),
                (nf.EV_SYN, nf.SYN_REPORT, 0),
            ],
        )

    def test_face_and_shoulder_buttons_xy_not_swapped(self) -> None:
        parser = nf.NimbusReportParser()

        # Press X (byte 6) -> BTN_NORTH (307 = BTN_X), Press Y (byte 7) -> BTN_WEST (308 = BTN_Y)
        ev = parser.parse_packet(make_report(x=20), 1.0)
        self.assertEqual(ev, [(nf.EV_KEY, 307, 1), (nf.EV_SYN, nf.SYN_REPORT, 0)])

        # Hysteresis keeps X pressed at 10
        self.assertEqual(parser.parse_packet(make_report(x=10), 1.1), [])

        # Release X (< 8) and press Y
        ev = parser.parse_packet(make_report(x=7, y=200), 1.2)
        self.assertEqual(
            ev,
            [
                (nf.EV_KEY, 307, 0),
                (nf.EV_KEY, 308, 1),
                (nf.EV_SYN, nf.SYN_REPORT, 0),
            ],
        )

        # Verify A, B, L1, R1 codes
        ev = parser.parse_packet(make_report(a=255, b=255, l1=255, r1=255, y=0), 1.3)
        self.assertEqual(
            ev,
            [
                (nf.EV_KEY, nf.BTN_SOUTH, 1),
                (nf.EV_KEY, nf.BTN_EAST, 1),
                (nf.EV_KEY, nf.BTN_WEST, 0),
                (nf.EV_KEY, nf.BTN_TL, 1),
                (nf.EV_KEY, nf.BTN_TR, 1),
                (nf.EV_SYN, nf.SYN_REPORT, 0),
            ],
        )

    def test_analog_triggers_and_sticks(self) -> None:
        parser = nf.NimbusReportParser()

        # Trigger noise below deadzone stays 0
        self.assertEqual(parser.parse_packet(make_report(l2=3, r2=2), 1.0), [])

        # Analog trigger values and stick inversion
        ev = parser.parse_packet(
            make_report(l2=128, r2=255, lx=127, ly=127, rx=-127, ry=-127),
            1.1,
        )
        self.assertEqual(
            ev,
            [
                (nf.EV_ABS, nf.ABS_X, 32767),
                (nf.EV_ABS, nf.ABS_Y, -32767),
                (nf.EV_ABS, nf.ABS_Z, 128),
                (nf.EV_ABS, nf.ABS_RX, -32767),
                (nf.EV_ABS, nf.ABS_RY, 32767),
                (nf.EV_ABS, nf.ABS_RZ, 255),
                (nf.EV_SYN, nf.SYN_REPORT, 0),
            ],
        )
        self.assertEqual(nf.scale_stick_axis(-128, invert=True), 32767)

    def test_menu_tap_pulse_and_hold_guide(self) -> None:
        parser = nf.NimbusReportParser()

        # Short tap (< 0.40s) emits BTN_START=1 on release and BTN_START=0 after 50ms
        self.assertEqual(parser.parse_packet(make_report(menu=1), 10.0), [])
        self.assertEqual(parser.tick(10.2), [])
        ev = parser.parse_packet(make_report(menu=0), 10.25)
        self.assertEqual(ev, [(nf.EV_KEY, nf.BTN_START, 1), (nf.EV_SYN, nf.SYN_REPORT, 0)])
        self.assertEqual(parser.tick(10.28), [])
        ev = parser.tick(10.30)
        self.assertEqual(ev, [(nf.EV_KEY, nf.BTN_START, 0), (nf.EV_SYN, nf.SYN_REPORT, 0)])

        # Long hold (>= 0.40s) emits BTN_MODE=1 on tick and BTN_MODE=0 on release
        self.assertEqual(parser.parse_packet(make_report(menu=1), 20.0), [])
        ev = parser.tick(20.41)
        self.assertEqual(ev, [(nf.EV_KEY, nf.BTN_MODE, 1), (nf.EV_SYN, nf.SYN_REPORT, 0)])
        ev = parser.parse_packet(make_report(menu=0), 20.6)
        self.assertEqual(ev, [(nf.EV_KEY, nf.BTN_MODE, 0), (nf.EV_SYN, nf.SYN_REPORT, 0)])

    def test_helpers_and_evdev_fallback_xy(self) -> None:
        self.assertEqual(nf.parse_hid_id("0005:00000111:00001420\n"), (0x0005, 0x0111, 0x1420))
        self.assertIsNone(nf.parse_hid_id("invalid"))
        self.assertEqual(nf.translate_evdev_event(nf.EV_KEY, 306, 1), (nf.EV_KEY, 307, 1))
        self.assertEqual(nf.translate_evdev_event(nf.EV_KEY, 307, 1), (nf.EV_KEY, 308, 1))


if __name__ == "__main__":
    unittest.main()
