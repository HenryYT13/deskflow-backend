"""
DeskFlow Pure-Python Standard MIDI File (SMF) Parser & Chiptune Library.
Extracts monophonic note streams [[frequency_hz, duration_ms], ...] suitable for
the ESP32 piezoelectric buzzer.
"""
from typing import List, Dict, Any, Optional, Tuple
import base64

def midi_note_to_freq(note: int) -> int:
    """Converts a MIDI note number (12-127) to frequency in Hz (A4 = note 69 = 440 Hz)."""
    if note < 12 or note > 127:
        return 0
    return int(round(440.0 * (2.0 ** ((note - 69) / 12.0))))

def _read_vlq(data: bytes, p: int) -> Tuple[int, int]:
    val = 0
    while p < len(data):
        b = data[p]
        p += 1
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return val, p

def _extract_midi_chunks(midi_bytes: bytes) -> Tuple[int, int, List[bytes]]:
    if len(midi_bytes) < 14 or midi_bytes[:4] != b"MThd":
        raise ValueError("Invalid MIDI file: Missing 'MThd' header.")
    p = 8
    fmt = int.from_bytes(midi_bytes[p:p+2], "big")
    p += 2
    p += 2  # num_tracks
    division = int.from_bytes(midi_bytes[p:p+2], "big") or 480
    p += 2
    tracks_data: List[bytes] = []
    while p + 8 <= len(midi_bytes):
        chunk_type = midi_bytes[p:p+4]
        p += 4
        chunk_len = int.from_bytes(midi_bytes[p:p+4], "big")
        p += 4
        if chunk_type == b"MTrk":
            tracks_data.append(midi_bytes[p:p+chunk_len])
        p += chunk_len
    if not tracks_data:
        raise ValueError("Invalid MIDI file: No tracks found.")
    return fmt, division, tracks_data


def inspect_midi_tracks(midi_bytes: bytes) -> Dict[str, Any]:
    """Lists every track inside a Standard MIDI File so user can pick the Lead Melody track."""
    fmt, division, tracks_data = _extract_midi_chunks(midi_bytes)
    tracks_info: List[Dict[str, Any]] = []
    best_idx = 0
    best_score = -1.0

    for idx, trk in enumerate(tracks_data):
        tp = 0
        status = 0
        tlen = len(trk)
        name = ""
        pitches: List[int] = []
        while tp < tlen:
            _, tp = _read_vlq(trk, tp)
            if tp >= tlen:
                break
            b = trk[tp]
            if b & 0x80:
                status = b
                tp += 1
            cmd = status & 0xF0
            if status == 0xFF:
                if tp >= tlen:
                    break
                meta_type = trk[tp]; tp += 1
                meta_len, tp = _read_vlq(trk, tp)
                if meta_type in (0x03, 0x04) and not name and tp + meta_len <= tlen:
                    name = trk[tp:tp+meta_len].decode("utf-8", errors="ignore").strip()
                tp += meta_len
            elif status in (0xF0, 0xF7):
                sys_len, tp = _read_vlq(trk, tp)
                tp += sys_len
            elif cmd in (0x90, 0x80):
                if tp + 2 > tlen:
                    break
                key = trk[tp]; tp += 1
                vel = trk[tp]; tp += 1
                if (status & 0x0F) != 9 and cmd == 0x90 and vel > 0:
                    pitches.append(key)
            elif cmd in (0xC0, 0xD0):
                tp += 1
            elif cmd in (0xA0, 0xB0, 0xE0):
                tp += 2

        avg_p = (sum(pitches) / len(pitches)) if pitches else 0.0
        # Prefer mid-high melodic tracks with plentiful notes
        if len(pitches) >= 8:
            score = avg_p + min(80, len(pitches) / 4.0)
            if score > best_score:
                best_score = score
                best_idx = idx
        tracks_info.append({
            "index": idx,
            "name": name or f"Track {idx + 1}",
            "note_count": len(pitches),
            "min_pitch": min(pitches) if pitches else 0,
            "max_pitch": max(pitches) if pitches else 0,
            "avg_pitch": round(avg_p, 1),
        })

    return {
        "format": fmt,
        "division": division,
        "track_count": len(tracks_info),
        "suggested_lead": best_idx,
        "tracks": tracks_info,
    }


def parse_midi_bytes(
    midi_bytes: bytes,
    max_notes: int = 512,
    auto_transpose: bool = True,
    filter_bass: bool = True,
    selected_tracks: Optional[List[int]] = None,
) -> List[List[int]]:
    """
    Parses SMF Format 0 or Format 1 binary bytes into monophonic [[freq_hz, duration_ms], ...].
    Multi-voice/polyphonic chords are automatically reduced to the highest melody pitch.
    Accompaniment basslines and out-of-range low frequencies are filtered and transposed
    to match the resonant frequency response of piezoelectric buzzers (400 Hz - 2500 Hz).
    """
    _fmt, division, tracks_data = _extract_midi_chunks(midi_bytes)
    allowed_tracks = set(selected_tracks) if selected_tracks else None

    events: List[Tuple[int, bool, int]] = []
    tempo_changes: List[Tuple[int, int]] = [(0, 500000)] # (tick, microseconds_per_quarter)

    for trk_idx, trk in enumerate(tracks_data):
        include_notes = (allowed_tracks is None) or (trk_idx in allowed_tracks)
        tp = 0
        tick = 0
        status = 0
        tlen = len(trk)
        while tp < tlen:
            dt, tp = _read_vlq(trk, tp)
            tick += dt
            if tp >= tlen:
                break
            b = trk[tp]
            if b & 0x80:
                status = b
                tp += 1
            cmd = status & 0xF0
            if status == 0xFF:
                if tp >= tlen: break
                meta_type = trk[tp]; tp += 1
                meta_len, tp = _read_vlq(trk, tp)
                if meta_type == 0x51 and meta_len == 3 and tp + 3 <= tlen:
                    tempo_changes.append((tick, int.from_bytes(trk[tp:tp+3], "big")))
                tp += meta_len
            elif status in (0xF0, 0xF7):
                sys_len, tp = _read_vlq(trk, tp)
                tp += sys_len
            elif cmd in (0x90, 0x80):
                if tp + 2 > tlen: break
                key = trk[tp]; tp += 1
                vel = trk[tp]; tp += 1
                channel = status & 0x0F
                if include_notes and channel != 9:  # Ignore MIDI channel 10 (drums/percussion)
                    events.append((tick, (cmd == 0x90 and vel > 0), key))
            elif cmd in (0xC0, 0xD0):
                tp += 1
            elif cmd in (0xA0, 0xB0, 0xE0):
                tp += 2

    if not events:
        raise ValueError("MIDI file contains no audible note events.")

    events.sort(key=lambda x: x[0])
    tempo_changes.sort(key=lambda x: x[0])

    # Filter out deep accompaniment bass notes when upper melody exists
    # (Prevents low bass accompaniment from overwriting melodic rests/silences)
    note_on_keys = sorted([e[2] for e in events if e[1]])
    if filter_bass and note_on_keys:
        q75 = note_on_keys[int(len(note_on_keys) * 0.75)]
        min_k = note_on_keys[0]
        if q75 >= 52 and min_k < 42:
            bass_floor = 48
            filtered = [e for e in events if e[2] >= bass_floor]
            if filtered:
                events = filtered

    def tick_to_ms(target_tick: int) -> int:
        cur_ms = 0.0
        cur_t = 0
        cur_tempo = 500000
        for chg_tick, chg_tempo in tempo_changes:
            if target_tick <= chg_tick:
                break
            cur_ms += ((chg_tick - cur_t) * cur_tempo) / (division * 1000.0)
            cur_t = chg_tick
            cur_tempo = chg_tempo
        cur_ms += ((target_tick - cur_t) * cur_tempo) / (division * 1000.0)
        return int(round(cur_ms))

    active_notes: List[int] = []
    raw_notes: List[Tuple[int, int]] = []
    last_ms = 0
    cur_pitch = 0

    for tick, is_on, key in events:
        now_ms = tick_to_ms(tick)
        dur = now_ms - last_ms
        if is_on:
            if key not in active_notes:
                active_notes.append(key)
        else:
            if key in active_notes:
                active_notes.remove(key)

        new_pitch = max(active_notes) if active_notes else 0
        if new_pitch != cur_pitch:
            if dur >= 20: # Debounce tiny transitions
                raw_notes.append((cur_pitch, dur))
                last_ms = now_ms
                if len(raw_notes) >= max_notes:
                    break
            cur_pitch = new_pitch

    # Auto-transposition to buzzer resonant sweet spot (~400 Hz - 2500 Hz, MIDI 58 - 88)
    shift = 0
    if auto_transpose:
        mel_keys = sorted([k for k, d in raw_notes if k > 0])
        if mel_keys:
            med_k = mel_keys[len(mel_keys) // 2]
            while med_k + shift < 58:
                shift += 12
            while med_k + shift > 88:
                shift -= 12

    notes_out: List[List[int]] = []
    for k, d in raw_notes:
        freq = midi_note_to_freq(k + shift) if k > 0 else 0
        notes_out.append([freq, d])

    # Trim excessive leading silence so playback starts promptly
    if notes_out and notes_out[0][0] == 0:
        if notes_out[0][1] > 200:
            notes_out[0][1] = 50
        elif notes_out[0][1] <= 20:
            notes_out.pop(0)

    # Trim trailing silence
    if notes_out and notes_out[-1][0] == 0:
        notes_out.pop()

    return notes_out


# Built-in classic 8-bit chiptune melodies for instant buzzer playback
PRESET_MELODIES: Dict[str, Dict[str, Any]] = {
    "mario": {
        "id": "mario",
        "title": "Super Mario Bros Theme",
        "artist": "Koji Kondo",
        "notes": [
            [659, 150], [0, 50], [659, 150], [0, 150], [659, 150], [0, 150],
            [523, 150], [0, 50], [659, 150], [0, 150], [784, 250], [0, 300],
            [392, 250], [0, 300],
            [523, 200], [0, 150], [392, 200], [0, 150], [330, 200], [0, 150],
            [440, 200], [0, 100], [494, 200], [0, 100], [466, 150], [0, 50],
            [440, 200], [0, 100], [392, 180], [659, 180], [784, 180], [880, 200],
            [698, 150], [784, 150], [659, 200], [0, 100], [523, 150], [587, 150], [494, 250]
        ]
    },
    "rickroll": {
        "id": "rickroll",
        "title": "Never Gonna Give You Up",
        "artist": "Rick Astley",
        "notes": [
            [370, 160], [415, 160], [494, 160], [415, 160],
            [622, 350], [0, 60], [622, 350], [0, 60], [554, 500], [0, 150],
            [370, 160], [415, 160], [494, 160], [415, 160],
            [554, 350], [0, 60], [554, 350], [0, 60], [494, 500], [0, 150],
            [370, 160], [415, 160], [494, 160], [415, 160],
            [494, 350], [554, 250], [440, 250], [415, 250], [370, 500], [0, 150],
            [370, 160], [554, 300], [494, 600]
        ]
    },
    "tetris": {
        "id": "tetris",
        "title": "Tetris Theme (Korobeiniki)",
        "artist": "Traditional Russian",
        "notes": [
            [659, 300], [494, 150], [523, 150], [587, 300], [523, 150], [494, 150],
            [440, 300], [440, 150], [523, 150], [659, 300], [587, 150], [523, 150],
            [494, 450], [523, 150], [587, 300], [659, 300],
            [523, 300], [440, 300], [440, 450], [0, 150],
            [587, 300], [698, 150], [880, 300], [784, 150], [698, 150],
            [659, 450], [523, 150], [659, 300], [587, 150], [523, 150],
            [494, 300], [494, 150], [523, 150], [587, 300], [659, 300],
            [523, 300], [440, 300], [440, 450]
        ]
    },
    "coffin": {
        "id": "coffin",
        "title": "Astronomia (Coffin Dance)",
        "artist": "Vicetone & Tony Igy",
        "notes": [
            [440, 150], [440, 150], [440, 150], [440, 150],
            [440, 150], [440, 150], [440, 150], [440, 150],
            [523, 150], [523, 150], [523, 150], [523, 150],
            [494, 150], [494, 150], [494, 150], [494, 150],
            [659, 200], [0, 50], [659, 200], [0, 50], [659, 200], [0, 50], [659, 200],
            [784, 200], [0, 50], [740, 200], [0, 50], [659, 200], [0, 50], [587, 200],
            [659, 200], [0, 50], [494, 200], [0, 50], [523, 200], [0, 50], [440, 400]
        ]
    },
    "starwars": {
        "id": "starwars",
        "title": "Star Wars - Imperial March",
        "artist": "John Williams",
        "notes": [
            [440, 400], [0, 100], [440, 400], [0, 100], [440, 400], [0, 100],
            [349, 300], [523, 150], [440, 400], [0, 100], [349, 300], [523, 150], [440, 700], [0, 200],
            [659, 400], [0, 100], [659, 400], [0, 100], [659, 400], [0, 100],
            [698, 300], [523, 150], [415, 400], [0, 100], [349, 300], [523, 150], [440, 700]
        ]
    },
    "nokia": {
        "id": "nokia",
        "title": "Nokia Tune (Gran Vals)",
        "artist": "Francisco Tarrega",
        "notes": [
            [659, 160], [587, 160], [370, 320], [415, 320],
            [554, 160], [494, 160], [294, 320], [330, 320],
            [494, 160], [440, 160], [277, 320], [330, 320],
            [440, 640]
        ]
    }
}

def get_preset_list() -> List[Dict[str, Any]]:
    """Returns metadata for all available preset chiptune melodies."""
    return [
        {
            "id": p["id"],
            "title": p["title"],
            "artist": p.get("artist", ""),
            "note_count": len(p["notes"]),
            "duration_ms": sum(n[1] for n in p["notes"])
        }
        for p in PRESET_MELODIES.values()
    ]

def get_preset(preset_id: str) -> Optional[Dict[str, Any]]:
    """Returns the preset melody dictionary or None."""
    return PRESET_MELODIES.get(preset_id.lower().strip())
