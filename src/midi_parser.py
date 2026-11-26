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
    },
    "megalovania": {"id": "megalovania", "title": "Megalovania", "artist": "Toby Fox (Undertale)", "notes": [[294, 120], [294, 120], [587, 240], [440, 360], [415, 240], [392, 240], [349, 240], [294, 120], [349, 120], [392, 120], [262, 120], [262, 120], [587, 240], [440, 360], [415, 240], [392, 240], [349, 240], [294, 120], [349, 120], [392, 120]]},
    "zelda": {"id": "zelda", "title": "Legend of Zelda Main Theme", "artist": "Koji Kondo", "notes": [[440, 400], [440, 150], [440, 150], [440, 150], [440, 150], [392, 150], [440, 450], [440, 150], [440, 150], [440, 150], [440, 150], [392, 150], [440, 400], [330, 150], [330, 150], [440, 300], [330, 150], [392, 150], [440, 300], [523, 300], [587, 300], [659, 600]]},
    "zelda_lullaby": {"id": "zelda_lullaby", "title": "Zelda's Lullaby", "artist": "Koji Kondo", "notes": [[494, 600], [587, 300], [440, 600], [392, 150], [440, 150], [494, 600], [587, 300], [440, 800]]},
    "harry_potter": {"id": "harry_potter", "title": "Hedwig's Theme", "artist": "John Williams", "notes": [[494, 240], [659, 360], [784, 140], [740, 240], [659, 480], [988, 240], [880, 640], [740, 640], [659, 360], [784, 140], [740, 240], [622, 480], [698, 240], [494, 640]]},
    "pirates": {"id": "pirates", "title": "He's a Pirate", "artist": "Klaus Badelt", "notes": [[330, 150], [392, 150], [440, 300], [440, 150], [440, 150], [494, 150], [523, 300], [523, 150], [523, 150], [587, 150], [494, 300], [494, 150], [440, 150], [392, 150], [440, 450]]},
    "fur_elise": {"id": "fur_elise", "title": "Für Elise", "artist": "Ludwig van Beethoven", "notes": [[659, 180], [622, 180], [659, 180], [622, 180], [659, 180], [494, 180], [587, 180], [523, 180], [440, 400], [262, 180], [330, 180], [440, 180], [494, 400], [330, 180], [415, 180], [494, 180], [523, 450]]},
    "ode_to_joy": {"id": "ode_to_joy", "title": "Ode to Joy", "artist": "Ludwig van Beethoven", "notes": [[659, 250], [659, 250], [698, 250], [784, 250], [784, 250], [698, 250], [659, 250], [587, 250], [523, 250], [523, 250], [587, 250], [659, 250], [659, 380], [587, 140], [587, 500]]},
    "turkish_march": {"id": "turkish_march", "title": "Turkish March (Alla Turca)", "artist": "W.A. Mozart", "notes": [[494, 130], [440, 130], [415, 130], [440, 130], [523, 420], [587, 130], [523, 130], [494, 130], [523, 130], [659, 420], [698, 130], [659, 130], [622, 130], [659, 130], [988, 130], [880, 130], [831, 130], [880, 130], [988, 130], [880, 130], [831, 130], [880, 130], [1047, 500]]},
    "canon_in_d": {"id": "canon_in_d", "title": "Canon in D", "artist": "Johann Pachelbel", "notes": [[740, 420], [659, 420], [587, 420], [554, 420], [494, 420], [440, 420], [494, 420], [554, 420], [587, 220], [740, 220], [659, 220], [880, 220], [587, 220], [740, 220], [554, 220], [659, 220]]},
    "moonlight": {"id": "moonlight", "title": "Moonlight Sonata (Intro)", "artist": "Beethoven", "notes": [[415, 220], [554, 220], [659, 220], [415, 220], [554, 220], [659, 220], [415, 220], [554, 220], [659, 220], [415, 220], [554, 220], [659, 220], [440, 220], [554, 220], [659, 220], [440, 220], [587, 220], [698, 220]]},
    "happy_birthday": {"id": "happy_birthday", "title": "Happy Birthday", "artist": "Traditional", "notes": [[392, 220], [392, 150], [440, 350], [392, 350], [523, 350], [494, 650], [392, 220], [392, 150], [440, 350], [392, 350], [587, 350], [523, 650]]},
    "jingle_bells": {"id": "jingle_bells", "title": "Jingle Bells", "artist": "James Pierpont", "notes": [[659, 220], [659, 220], [659, 440], [659, 220], [659, 220], [659, 440], [659, 220], [784, 220], [523, 280], [587, 160], [659, 650]]},
    "we_wish_xmas": {"id": "we_wish_xmas", "title": "We Wish You a Merry Xmas", "artist": "Traditional", "notes": [[523, 250], [698, 250], [698, 150], [784, 150], [698, 150], [622, 150], [587, 250], [587, 250], [587, 250], [784, 250], [784, 150], [880, 150], [784, 150], [698, 150], [659, 250], [523, 250], [523, 250], [880, 250], [880, 150], [988, 150], [880, 150], [784, 150], [698, 300], [587, 300]]},
    "mission_impossible": {"id": "mission_impossible", "title": "Mission Impossible", "artist": "Lalo Schifrin", "notes": [[392, 300], [0, 80], [392, 300], [0, 80], [466, 200], [523, 200], [392, 300], [0, 80], [392, 300], [0, 80], [349, 200], [370, 200], [392, 300], [0, 80], [392, 300]]},
    "pink_panther": {"id": "pink_panther", "title": "The Pink Panther", "artist": "Henry Mancini", "notes": [[311, 180], [330, 340], [0, 120], [370, 180], [392, 340], [0, 120], [311, 180], [330, 200], [370, 180], [392, 200], [523, 180], [494, 200], [330, 180], [392, 200], [494, 180], [466, 520]]},
    "take_on_me": {"id": "take_on_me", "title": "Take On Me", "artist": "a-ha", "notes": [[740, 160], [740, 160], [587, 160], [494, 160], [0, 160], [494, 160], [0, 160], [659, 160], [0, 160], [659, 160], [0, 160], [659, 160], [831, 160], [831, 160], [880, 160], [988, 160], [880, 160], [880, 160], [880, 160], [659, 160], [587, 160], [740, 160]]},
    "bad_guy": {"id": "bad_guy", "title": "bad guy", "artist": "Billie Eilish", "notes": [[392, 180], [392, 180], [466, 180], [392, 180], [587, 180], [392, 180], [523, 180], [466, 180], [392, 180], [392, 180], [466, 180], [392, 180], [587, 180], [392, 180], [523, 180], [466, 180]]},
    "shape_of_you": {"id": "shape_of_you", "title": "Shape of You", "artist": "Ed Sheeran", "notes": [[554, 180], [659, 180], [554, 240], [554, 180], [659, 180], [554, 240], [554, 180], [659, 180], [554, 240], [494, 180], [554, 180], [440, 240]]},
    "believer": {"id": "believer", "title": "Believer", "artist": "Imagine Dragons", "notes": [[466, 200], [466, 200], [523, 200], [587, 250], [466, 200], [466, 200], [523, 200], [587, 250], [698, 220], [587, 220], [523, 220], [466, 360]]},
    "despacito": {"id": "despacito", "title": "Despacito", "artist": "Luis Fonsi", "notes": [[587, 450], [554, 220], [494, 220], [554, 220], [587, 220], [659, 220], [587, 220], [554, 220], [494, 420], [440, 220], [392, 420], [587, 220], [659, 220], [740, 220], [587, 320], [554, 420]]},
    "blinding_lights": {"id": "blinding_lights", "title": "Blinding Lights", "artist": "The Weeknd", "notes": [[698, 320], [698, 160], [622, 160], [698, 220], [784, 220], [523, 320], [622, 320], [698, 320], [698, 160], [622, 160], [698, 220], [784, 220], [523, 450]]},
    "giorno": {"id": "giorno", "title": "Il Vento D'Oro (Giorno)", "artist": "Yugo Kanno", "notes": [[740, 220], [587, 140], [0, 80], [587, 140], [659, 140], [698, 220], [659, 140], [587, 140], [554, 220], [587, 140], [659, 140], [740, 220], [988, 320], [880, 320]]},
    "evangelion": {"id": "evangelion", "title": "Cruel Angel's Thesis", "artist": "Yoko Takahashi", "notes": [[523, 300], [622, 300], [698, 220], [622, 220], [698, 300], [698, 160], [698, 160], [932, 160], [784, 160], [698, 160], [622, 160], [698, 450]]},
    "unravel": {"id": "unravel", "title": "Unravel (Tokyo Ghoul)", "artist": "TK", "notes": [[932, 240], [1047, 240], [932, 240], [880, 240], [784, 360], [932, 240], [1047, 240], [932, 240], [880, 240], [784, 360], [698, 240], [698, 240], [659, 240], [587, 480]]},
    "gurenge": {"id": "gurenge", "title": "Gurenge (Demon Slayer)", "artist": "LiSA", "notes": [[659, 220], [740, 220], [784, 320], [740, 220], [659, 220], [494, 380], [659, 220], [740, 220], [784, 220], [988, 320], [880, 220], [784, 220], [740, 450]]},
    "idol": {"id": "idol", "title": "Idol (Oshi no Ko)", "artist": "YOASOBI", "notes": [[784, 150], [784, 150], [880, 150], [784, 150], [698, 150], [659, 150], [587, 300], [784, 150], [784, 150], [880, 150], [1047, 300], [988, 220], [880, 350]]},
    "gravity_falls": {"id": "gravity_falls", "title": "Gravity Falls Theme", "artist": "Brad Breeck", "notes": [[587, 240], [659, 140], [698, 380], [880, 240], [784, 140], [880, 140], [523, 380], [587, 240], [659, 140], [698, 240], [659, 240], [784, 240], [880, 240], [784, 240], [698, 450]]},
    "among_us": {"id": "among_us", "title": "Among Us Trap Remix", "artist": "Leonz", "notes": [[523, 180], [622, 180], [698, 180], [740, 180], [698, 180], [622, 180], [523, 360], [0, 120], [466, 120], [587, 120], [523, 400]]},
    "subway_surfers": {"id": "subway_surfers", "title": "Subway Surfers Theme", "artist": "SYBO", "notes": [[523, 160], [0, 80], [622, 160], [659, 160], [698, 220], [784, 220], [523, 160], [622, 160], [698, 320], [880, 180], [784, 180], [698, 320]]},
    "pacman": {"id": "pacman", "title": "Pac-Man Intro", "artist": "Toshio Kai", "notes": [[494, 110], [988, 110], [740, 110], [622, 110], [988, 110], [740, 110], [622, 220], [523, 110], [1047, 110], [784, 110], [659, 110], [1047, 110], [784, 110], [659, 220], [494, 110], [988, 110], [740, 110], [622, 110], [988, 110], [740, 110], [622, 220]]},
    "kirby": {"id": "kirby", "title": "Kirby Gourmet Race", "artist": "Jun Ishikawa", "notes": [[784, 160], [698, 160], [659, 160], [587, 160], [523, 240], [587, 160], [659, 160], [523, 240], [440, 160], [523, 160], [587, 160], [494, 160], [523, 380]]},
    "sonic": {"id": "sonic", "title": "Sonic Green Hill Zone", "artist": "Masato Nakamura", "notes": [[784, 200], [659, 200], [784, 200], [659, 200], [784, 200], [659, 320], [880, 200], [784, 200], [659, 200], [587, 450]]},
    "doom": {"id": "doom", "title": "DOOM E1M1", "artist": "Bobby Prince", "notes": [[330, 130], [330, 130], [659, 130], [330, 130], [330, 130], [587, 130], [330, 130], [330, 130], [523, 130], [330, 130], [330, 130], [466, 130], [330, 130], [330, 130], [494, 130], [523, 260]]},
    "halo": {"id": "halo", "title": "Halo Theme (Chant)", "artist": "Martin O'Donnell", "notes": [[330, 500], [370, 500], [440, 600], [392, 400], [330, 500], [294, 650], [330, 800]]},
    "mortal_kombat": {"id": "mortal_kombat", "title": "Techno Syndrome (Mortal Kombat)", "artist": "The Immortals", "notes": [[440, 140], [440, 140], [523, 140], [440, 140], [587, 140], [440, 140], [659, 140], [587, 140], [523, 140], [523, 140], [659, 140], [523, 140], [784, 140], [523, 140], [659, 140], [523, 140]]},
    "minecraft": {"id": "minecraft", "title": "Sweden (Minecraft)", "artist": "C418", "notes": [[392, 480], [440, 480], [494, 600], [659, 480], [587, 480], [494, 480], [440, 720]]},
    "wii_shop": {"id": "wii_shop", "title": "Wii Shop Channel", "artist": "Kazumi Totaka", "notes": [[740, 220], [0, 80], [880, 220], [1109, 220], [0, 80], [880, 320], [0, 120], [740, 160], [587, 160], [587, 160], [587, 260]]},
    "mii_channel": {"id": "mii_channel", "title": "Mii Channel Theme", "artist": "Kazumi Totaka", "notes": [[740, 180], [0, 60], [880, 180], [1109, 180], [0, 60], [880, 220], [740, 180], [587, 160], [587, 160], [587, 240], [494, 180], [587, 180], [740, 180], [659, 360]]},
    "angry_birds": {"id": "angry_birds", "title": "Angry Birds Theme", "artist": "Ari Pulkkinen", "notes": [[659, 160], [698, 160], [784, 300], [659, 160], [784, 160], [880, 320], [784, 160], [698, 160], [659, 160], [587, 160], [523, 380]]},
    "bad_piggies": {"id": "bad_piggies", "title": "Bad Piggies Theme", "artist": "Ilmari Hakkola", "notes": [[659, 150], [784, 150], [988, 150], [880, 150], [784, 150], [659, 150], [587, 240], [659, 150], [784, 150], [659, 320]]},
    "pvz": {"id": "pvz", "title": "Loonboon (Plants vs Zombies)", "artist": "Laura Shigihara", "notes": [[440, 160], [523, 160], [659, 160], [587, 160], [523, 160], [494, 160], [440, 160], [392, 160], [440, 160], [523, 160], [698, 220], [659, 320]]},
    "pokemon": {"id": "pokemon", "title": "Pokémon Red/Blue Theme", "artist": "Junichi Masuda", "notes": [[523, 220], [523, 140], [523, 140], [523, 220], [659, 220], [784, 420], [659, 220], [784, 220], [880, 480], [784, 320]]},
    "lavender": {"id": "lavender", "title": "Lavender Town", "artist": "Junichi Masuda", "notes": [[523, 380], [784, 380], [988, 380], [740, 380], [523, 380], [784, 380], [988, 380], [740, 380]]},
    "tetris_b": {"id": "tetris_b", "title": "Tetris Type B", "artist": "Hirokazu Tanaka", "notes": [[659, 220], [587, 220], [523, 220], [494, 220], [523, 220], [659, 220], [784, 360], [659, 220], [587, 360]]},
    "chopin_nocturne": {"id": "chopin_nocturne", "title": "Nocturne Op.9 No.2", "artist": "Frédéric Chopin", "notes": [[466, 320], [784, 640], [698, 240], [784, 240], [698, 240], [622, 480], [587, 240], [622, 480], [523, 640]]},
    "spring_vivaldi": {"id": "spring_vivaldi", "title": "The Four Seasons - Spring", "artist": "Antonio Vivaldi", "notes": [[659, 240], [831, 180], [831, 180], [831, 180], [740, 120], [659, 120], [988, 420], [831, 180], [831, 180], [831, 180], [740, 120], [659, 120], [988, 420]]},
    "carmen": {"id": "carmen", "title": "Habanera (Carmen)", "artist": "Georges Bizet", "notes": [[587, 260], [554, 160], [523, 160], [494, 160], [466, 260], [440, 160], [415, 160], [392, 160], [349, 260], [330, 320]]},
    "avengers": {"id": "avengers", "title": "The Avengers Theme", "artist": "Alan Silvestri", "notes": [[587, 320], [587, 160], [587, 160], [880, 420], [784, 220], [698, 220], [659, 320], [587, 500]]},
    "jurassic": {"id": "jurassic", "title": "Jurassic Park Theme", "artist": "John Williams", "notes": [[466, 220], [440, 220], [466, 550], [0, 120], [466, 220], [440, 220], [466, 550], [0, 120], [466, 220], [440, 220], [466, 280], [523, 380], [698, 600]]},
    "got": {"id": "got", "title": "Game of Thrones", "artist": "Ramin Djawadi", "notes": [[784, 420], [523, 420], [622, 180], [698, 180], [784, 420], [523, 420], [622, 180], [698, 180], [587, 650]]},
    "simpsons": {"id": "simpsons", "title": "The Simpsons Theme", "artist": "Danny Elfman", "notes": [[523, 300], [659, 220], [740, 220], [880, 180], [784, 300], [659, 220], [523, 220], [440, 180], [370, 180], [370, 180], [370, 180], [392, 480]]},
    "rick_morty": {"id": "rick_morty", "title": "Moonmen / Rick & Morty", "artist": "Ryan Elder", "notes": [[587, 200], [740, 200], [880, 200], [988, 280], [880, 200], [740, 200], [659, 400]]},
    "xp_startup": {"id": "xp_startup", "title": "Windows XP Startup", "artist": "Brian Eno", "notes": [[622, 220], [466, 160], [311, 320], [466, 260], [698, 520]]},
    "ngot_cho_toi_di_theo": {
        "id": "ngot_cho_toi_di_theo",
        "title": "Cho Tôi Đi Theo",
        "artist": "Ngọt",
        "notes": [
            # Full melodic arrangement from Flat.io score (m4-12, m29-36, m57-60, m69-76) at 106 BPM
            [440, 142], [524, 142], [588, 283], [698, 283], [784, 283], [698, 283], [880, 850], [0, 280],
            [784, 283], [1046, 283], [880, 283], [0, 35], [880, 850], [0, 280],
            [784, 283], [1046, 283], [784, 283], [0, 35], [784, 850], [0, 280],
            [698, 283], [588, 283], [524, 283], [698, 283], [588, 142], [524, 142], [588, 283], [524, 142],
            [440, 142], [524, 283], [698, 283], [784, 283], [698, 283], [880, 850], [0, 280],
            [784, 283], [1046, 283], [880, 283], [0, 35], [880, 850], [0, 280],
            [784, 283], [1046, 283], [784, 283], [0, 35], [784, 850], [0, 280],
            [698, 283], [588, 283], [524, 283], [698, 425], [588, 425], [440, 566], [350, 71], [392, 283], [350, 566],
            [880, 425], [784, 425], [698, 849], [524, 142], [588, 142], [698, 283], [660, 425], [588, 425], [524, 849],
            [392, 142], [440, 142], [524, 283], [440, 425], [392, 425], [350, 566], [0, 35], [350, 142], [392, 142], [350, 283], [294, 283], [262, 425], [0, 35], [262, 425], [278, 425],
            [524, 142], [588, 142], [698, 142], [784, 142], [0, 35], [784, 142], [698, 142], [784, 142], [880, 283], [1046, 142], [784, 283], [698, 849], [0, 142],
            [524, 142], [588, 142], [698, 283], [660, 425], [588, 425], [524, 849], [392, 142], [440, 142], [524, 283], [440, 425], [392, 425], [350, 425], [0, 280], [350, 142], [392, 142], [350, 283], [0, 35], [350, 850],
            [588, 283], [698, 142], [880, 283], [698, 142], [588, 142], [698, 142], [880, 142], [1046, 283], [880, 283], [698, 283], [588, 283],
            [524, 283], [660, 142], [880, 283], [660, 142], [524, 142], [660, 142], [0, 35], [660, 283], [588, 283], [524, 283], [588, 283],
            [466, 283], [588, 142], [784, 283], [588, 142], [466, 142], [588, 142], [784, 142], [880, 283], [784, 283], [588, 283], [466, 283],
            [622, 283], [466, 142], [784, 283], [466, 142], [622, 283], [784, 566], [0, 35], [784, 566],
            [880, 850], [0, 280], [784, 283], [1046, 283], [880, 283], [0, 35], [880, 850], [0, 280],
            [784, 283], [1046, 283], [784, 283], [0, 35], [784, 850], [0, 280],
            [698, 283], [588, 283], [524, 283], [698, 283], [588, 142], [524, 142], [588, 283], [524, 142], [440, 142], [524, 283], [698, 283], [784, 283], [698, 283],
            [880, 850], [0, 280], [784, 283], [1046, 283], [880, 283], [0, 35], [880, 850], [0, 280],
            [784, 283], [1046, 283], [784, 283], [0, 35], [784, 850], [0, 280],
            [698, 283], [588, 283], [524, 283], [698, 425], [0, 35], [698, 850]
        ]
    },
    "ngot_lan_cuoi": {
        "id": "ngot_lan_cuoi",
        "title": "Lần Cuối",
        "artist": "Ngọt",
        "notes": [
            # Tone C major - Nhin quanh lan cuoi, rung thay la ngam ngui
            [659, 280], [784, 280], [880, 320], [1047, 520], [0, 120],
            [659, 260], [784, 260], [880, 320], [784, 520], [0, 140],
            [698, 260], [784, 260], [698, 260], [659, 440], [0, 100],
            [587, 260], [698, 260], [784, 600], [0, 220],
            # Chorus: Vay la lan cuoi di ben nhau, cay dang nhung khong dau
            [523, 240], [0, 35], [523, 240], [0, 35], [523, 240], [587, 240], [659, 440], [0, 80],
            [659, 240], [587, 240], [523, 240], [440, 520], [0, 140],
            # Neu ai cung mang toi thi nguoi mong doi gi noi toi
            [440, 240], [523, 240], [587, 240], [698, 440], [0, 80],
            [698, 240], [659, 240], [587, 240], [523, 520], [0, 140],
            # Y nhu mot giac mo troi
            [523, 240], [587, 240], [523, 680]
        ]
    },
    "ngot_em_dao_nay": {
        "id": "ngot_em_dao_nay",
        "title": "Em Dạo Này",
        "artist": "Ngọt",
        "notes": [
            [587, 220], [659, 220], [784, 260], [659, 220], [587, 280], [523, 360], [0, 100],
            [440, 220], [523, 220], [587, 260], [659, 320], [587, 420], [0, 120],
            [784, 240], [659, 220], [587, 240], [523, 240], [440, 480]
        ]
    },
    "ngot_de_quen": {
        "id": "ngot_de_quen",
        "title": "Để Quên",
        "artist": "Ngọt",
        "notes": [
            [523, 240], [587, 240], [659, 360], [587, 220], [523, 360], [0, 100],
            [440, 240], [523, 240], [659, 320], [784, 440], [0, 120],
            [659, 240], [587, 240], [523, 240], [494, 240], [440, 520]
        ]
    },
    "ngot_chuyen_kenh": {
        "id": "ngot_chuyen_kenh",
        "title": "Chuyển Kênh",
        "artist": "Ngọt",
        "notes": [
            [659, 180], [659, 180], [784, 220], [659, 180], [587, 260], [523, 280], [0, 80],
            [587, 180], [659, 180], [784, 240], [880, 300], [784, 360], [0, 100],
            [659, 200], [587, 200], [523, 420]
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
