"""Cheap local signals Claude uses alongside the transcript:
an audio energy map and a contact sheet of frames (who is on screen, watermarks).
These replace uploading video and audio chunks."""
import json
from pathlib import Path

import numpy as np

from . import util


def energy_map(media_path, out_dir, window=10.0):
    sr = 8000
    _, raw, _ = util.run_bytes(["ffmpeg", "-v", "error", "-i", str(media_path), "-vn", "-ac", "1",
                                "-ar", str(sr), "-f", "s16le", "-"])
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    _write_peaks(audio, sr, out_dir)
    n = int(sr * window)
    windows = []
    for i in range(0, len(audio), n):
        chunk = audio[i:i + n]
        if len(chunk) < sr:
            break
        rms = float(np.sqrt(np.mean(chunk ** 2)) + 1e-9)
        windows.append({"t": round(i / sr, 1), "db": round(20 * np.log10(rms), 1)})
    if not windows:
        return {"window": window, "median": 0, "spikes": [], "windows": []}
    dbs = np.array([w["db"] for w in windows])
    med = float(np.median(dbs))
    spikes = sorted([w for w in windows if w["db"] >= med + 3],
                    key=lambda w: -w["db"])[:12]
    spikes.sort(key=lambda w: w["t"])
    out = {"window": window, "median": round(med, 1), "spikes": spikes, "windows": windows}
    (Path(out_dir) / "energy.json").write_text(json.dumps(out), encoding="utf-8")
    return out


PEAK_RATE = 20  # waveform samples per second, for the Review timeline


def _write_peaks(audio, sr, out_dir):
    hop = sr // PEAK_RATE
    n = len(audio) // hop
    if n == 0:
        peaks = []
    else:
        frames = np.abs(audio[:n * hop]).reshape(n, hop).max(axis=1)
        top = float(np.percentile(frames, 99.5)) or 1.0
        peaks = np.clip(frames / top * 100, 0, 100).astype(int).tolist()
    (Path(out_dir) / "peaks.json").write_text(json.dumps({"rate": PEAK_RATE, "peaks": peaks}))


def load_peaks(media_path, out_dir):
    """Waveform for the timeline; computed on first use for interviews prepared before this existed."""
    p = Path(out_dir) / "peaks.json"
    if not p.exists():
        sr = 8000
        _, raw, _ = util.run_bytes(["ffmpeg", "-v", "error", "-i", str(media_path), "-vn", "-ac", "1",
                                    "-ar", str(sr), "-f", "s16le", "-"])
        _write_peaks(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, sr, out_dir)
    return json.loads(p.read_text())


def energy_summary(e):
    if not e or not e.get("spikes"):
        return "No standout loud moments (flat or normalized audio; rely on the transcript)."
    parts = [f"{util.ts(s['t'])} ({s['db'] - e['median']:+.0f} dB)" for s in e["spikes"]]
    return (f"Median loudness {e['median']} dBFS over {e['window']:.0f}s windows. "
            f"Loudest windows: " + ", ".join(parts))


def contact_sheet(media_path, out_dir, cols=6, rows=4, tile_w=320):
    """Grid of evenly spaced frames, each stamped with its timestamp."""
    import cv2

    cap = cv2.VideoCapture(str(media_path))
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = frames / fps if frames else util.probe(media_path)["duration"]
    w = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 16
    h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 9
    tile_h = int(tile_w * h / w)
    n = cols * rows
    times = [duration * (i + 0.5) / n for i in range(n)]
    sheet = np.zeros((tile_h * rows, tile_w * cols, 3), dtype=np.uint8)
    for i, t in enumerate(times):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        tile = cv2.resize(frame, (tile_w, tile_h))
        label = util.ts(t)
        cv2.rectangle(tile, (0, 0), (70, 22), (0, 0, 0), -1)
        cv2.putText(tile, label, (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
                    cv2.LINE_AA)
        r, c = divmod(i, cols)
        sheet[r * tile_h:(r + 1) * tile_h, c * tile_w:(c + 1) * tile_w] = tile
    cap.release()
    path = Path(out_dir) / "contact_sheet.jpg"
    cv2.imwrite(str(path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return path
