import unittest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
try:
    from midi_parser import (
        midi_note_to_freq,
        parse_midi_bytes,
        get_preset_list,
        get_preset,
        PRESET_MELODIES
    )
except ImportError:
    from src.midi_parser import (
        midi_note_to_freq,
        parse_midi_bytes,
        get_preset_list,
        get_preset,
        PRESET_MELODIES
    )

class TestMidiPlayer(unittest.TestCase):
    def test_midi_note_to_freq(self):
        # A4 = 69 -> 440 Hz
        self.assertEqual(midi_note_to_freq(69), 440)
        # Middle C (C4 = 60) -> 262 Hz
        self.assertEqual(midi_note_to_freq(60), 262)
        # Out of bounds
        self.assertEqual(midi_note_to_freq(5), 0)
        self.assertEqual(midi_note_to_freq(130), 0)

    def test_presets_exist(self):
        presets = get_preset_list()
        self.assertTrue(len(presets) >= 6)
        ids = [p["id"] for p in presets]
        self.assertIn("mario", ids)
        self.assertIn("rickroll", ids)
        self.assertIn("tetris", ids)
        self.assertIn("coffin", ids)

        mario = get_preset("mario")
        self.assertIsNotNone(mario)
        self.assertTrue(len(mario["notes"]) > 10)
        for note in mario["notes"]:
            self.assertEqual(len(note), 2)
            self.assertIsInstance(note[0], int)
            self.assertIsInstance(note[1], int)

    def test_parse_sample_smf(self):
        sample_midi = bytes([
            0x4D, 0x54, 0x68, 0x64, 0x00, 0x00, 0x00, 0x06, 0x00, 0x00, 0x00, 0x01,
            0x01, 0xE0, 0x4D, 0x54, 0x72, 0x6B, 0x00, 0x00, 0x00, 0x41, 0x00, 0xFF,
            0x51, 0x03, 0x07, 0xA1, 0x20, 0x00, 0x90, 0x3C, 0x5A, 0x81, 0x52, 0x80,
            0x3C, 0x00, 0x1E, 0x90, 0x40, 0x5A, 0x81, 0x52, 0x80, 0x40, 0x00, 0x1E,
            0x90, 0x43, 0x5A, 0x81, 0x52, 0x80, 0x43, 0x00, 0x1E, 0x90, 0x48, 0x5A,
            0x83, 0x42, 0x80, 0x48, 0x00, 0x1E, 0x90, 0x43, 0x5A, 0x81, 0x52, 0x80,
            0x43, 0x00, 0x1E, 0x90, 0x48, 0x5A, 0x85, 0x32, 0x80, 0x48, 0x00, 0x00,
            0xFF, 0x2F, 0x00
        ])
        notes = parse_midi_bytes(sample_midi)
        self.assertEqual(len(notes), 11)
        # First note should be Middle C (~262 Hz)
        self.assertEqual(notes[0][0], 262)
        # Note duration should be positive
        self.assertTrue(notes[0][1] > 0)

    def test_invalid_midi_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_midi_bytes(b"not a valid midi")

if __name__ == "__main__":
    unittest.main()
