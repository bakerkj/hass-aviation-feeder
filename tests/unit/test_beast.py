# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.

"""Unit tests for beast.py — the Beast frame parser and the DF tally.

The escaping rules are the whole difficulty here: a first attempt that ignored
them produced a roughly uniform spread across DF0..DF31, which is physically
impossible for real Mode S traffic. These tests pin the framing so that failure
mode cannot come back."""

import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "aviation_feeder")
)

from aviation_feeder_mqtt import beast

ESC = 0x1A


def frame(
    ftype: int, payload: bytes, ts: bytes = b"\x00" * 6, sig: int = 0x40
) -> bytes:
    """Build a Beast frame, escaping 0x1a in the body the way readsb does."""
    body = ts + bytes([sig]) + payload
    return bytes([ESC, ftype]) + body.replace(b"\x1a", b"\x1a\x1a")


def mode_s_long(df: int) -> bytes:
    """A 14-byte Mode-S long payload whose first byte encodes `df`."""
    return bytes([df << 3]) + b"\x00" * 13


class ParseFrames(unittest.TestCase):
    def test_single_long_frame(self):
        frames, consumed = beast.parse_frames(frame(0x33, mode_s_long(17)))
        self.assertEqual(len(frames), 1)
        ftype, payload = frames[0]
        self.assertEqual(ftype, 0x33)
        self.assertEqual(payload[0] >> 3, 17)
        self.assertGreater(consumed, 0)

    def test_back_to_back_frames(self):
        buf = frame(0x33, mode_s_long(17)) + frame(0x32, bytes([11 << 3]) + b"\x00" * 6)
        frames, _ = beast.parse_frames(buf)
        self.assertEqual([f[0] for f in frames], [0x33, 0x32])
        self.assertEqual(frames[0][1][0] >> 3, 17)
        self.assertEqual(frames[1][1][0] >> 3, 11)

    def test_escaped_0x1a_in_payload_is_unescaped(self):
        # THE regression test. A payload byte of 0x1a is sent doubled; a parser
        # that does not collapse it shifts every subsequent offset and loses sync.
        payload = bytes([17 << 3]) + b"\x1a" * 5 + b"\x00" * 8
        buf = frame(0x33, payload) + frame(0x33, mode_s_long(11))
        frames, _ = beast.parse_frames(buf)
        self.assertEqual(len(frames), 2, "escaped 0x1a broke framing")
        self.assertEqual(frames[0][1], payload, "payload not unescaped correctly")
        self.assertEqual(frames[1][1][0] >> 3, 11, "lost sync after escaped frame")

    def test_escaped_0x1a_in_timestamp(self):
        buf = frame(0x33, mode_s_long(17), ts=b"\x1a\x1a\x00\x00\x00\x00")
        buf += frame(0x33, mode_s_long(4))
        frames, _ = beast.parse_frames(buf)
        self.assertEqual([f[1][0] >> 3 for f in frames], [17, 4])

    def test_partial_trailing_frame_is_left_for_next_read(self):
        whole = frame(0x33, mode_s_long(17))
        buf = whole + frame(0x33, mode_s_long(11))[:5]  # cut mid-frame
        frames, consumed = beast.parse_frames(buf)
        self.assertEqual(len(frames), 1)
        # the leftover must be preserved so the next chunk completes it
        self.assertEqual(buf[consumed:], frame(0x33, mode_s_long(11))[:5])

    def test_split_frame_reassembles_across_reads(self):
        whole = frame(0x33, mode_s_long(17))
        first, second = whole[:6], whole[6:]
        frames, consumed = beast.parse_frames(first)
        self.assertEqual(frames, [])
        frames, _ = beast.parse_frames(first[consumed:] + second)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][1][0] >> 3, 17)

    def test_unknown_frame_type_is_skipped(self):
        # 0x34 config messages and any resync garbage must not derail parsing.
        buf = bytes([ESC, 0x34, 0x01]) + frame(0x33, mode_s_long(17))
        frames, _ = beast.parse_frames(buf)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][1][0] >> 3, 17)

    def test_leading_garbage_is_skipped(self):
        frames, _ = beast.parse_frames(b"\x00\xff\x99" + frame(0x33, mode_s_long(17)))
        self.assertEqual(len(frames), 1)

    def test_empty_input(self):
        self.assertEqual(beast.parse_frames(b""), ([], 0))


class CountDfs(unittest.TestCase):
    def test_counts_by_df_and_modeac(self):
        frames = [
            (0x33, mode_s_long(17)),
            (0x33, mode_s_long(17)),
            (0x32, bytes([11 << 3]) + b"\x00" * 6),
            (0x31, b"\x00\x00"),
        ]
        got: dict = {}
        beast.count_dfs(frames, got)
        self.assertEqual(got[17], 2)
        self.assertEqual(got[11], 1)
        self.assertEqual(got[beast.MODEAC_KEY], 1)

    def test_accumulates_into_existing_tally(self):
        got = {17: 5}
        beast.count_dfs([(0x33, mode_s_long(17))], got)
        self.assertEqual(got[17], 6)


class Snapshot(unittest.TestCase):
    def test_first_call_returns_nothing(self):
        # No baseline interval yet -> publishing a rate would be fabrication.
        c = beast.BeastDfCounter()
        c._counts = {17: 100}
        self.assertEqual(c.snapshot(now=1000.0), {})

    def test_rates_and_reset(self):
        c = beast.BeastDfCounter()
        c.snapshot(now=1000.0)  # baseline
        c._counts = {17: 300, 11: 60}
        out = c.snapshot(now=1060.0)  # 60s later
        self.assertAlmostEqual(out[17], 5.0)
        self.assertAlmostEqual(out[11], 1.0)
        # tally reset, so the next window starts clean
        self.assertEqual(c.snapshot(now=1120.0), {})

    def test_zero_elapsed_returns_nothing(self):
        c = beast.BeastDfCounter()
        c.snapshot(now=1000.0)
        c._counts = {17: 10}
        self.assertEqual(c.snapshot(now=1000.0), {})

    def test_not_started_is_not_connected(self):
        self.assertFalse(beast.BeastDfCounter().connected)


if __name__ == "__main__":
    unittest.main()
