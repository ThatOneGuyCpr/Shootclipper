"""Where to point the crop: face tracking, and on shots with several people, whoever is talking.

One person on screen: follow the face, hold still while they talk, glide when they move,
snap on camera cuts.

Several people on screen (a two-shot, a split-screen podcast): work out who is speaking
and cut to them, the way an editor would. Speaking is read from the picture and the sound
together: a talker's mouth area changes far more from frame to frame than the rest of
their face, and it does so while there is speech on the audio track. A listener who nods
or laughs moves their whole head, which the comparison with the upper face cancels out.
Short interjections don't pull the camera away; the speaker has to hold the floor first.
"""
from dataclasses import dataclass, field

import numpy as np

from . import config, jobs, util

FPS = 30                 # output frame rate the crop positions are generated for
COARSE_FPS = 5           # face positions
DENSE_FPS = 15           # mouth movement (speech moves the mouth at 3 to 8 Hz)
ROI_W, ROI_H = 40, 28    # mouth and reference patches are compared at this size
MIN_HOLD = 1.6           # seconds a speaker must hold the floor before the camera cuts to them
SWITCH_COST = 5.0        # how reluctant the camera is to change speaker

YUNET = config.BUNDLED_DATA / "face_detection_yunet_2023mar.onnx"


# ── detection ───────────────────────────────────────────────────────────────

def make_detector(cv2, w, h, min_face):
    """Returns detect(small_bgr, gray) -> [(x, y, w, h, landmarks or None)] in small-frame
    pixels. YuNet (bundled ONNX; handles profiles and busy backdrops, and gives eye, nose
    and mouth-corner points); Haar cascades as a fallback, without landmarks."""
    if hasattr(cv2, "FaceDetectorYN") and YUNET.exists():
        yn = cv2.FaceDetectorYN.create(str(YUNET), "", (w, h), 0.7, 0.3, 50)

        def detect(small, gray):
            _, found = yn.detect(small)
            if found is None:
                return []
            return [(float(f[0]), float(f[1]), float(f[2]), float(f[3]),
                     [float(v) for v in f[4:14]])
                    for f in found if f[2] >= min_face * 0.8]
        return detect

    front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")

    def detect(small, gray):
        faces = list(front.detectMultiScale(gray, 1.1, 6, minSize=(min_face, min_face)))
        if not faces:
            faces = list(profile.detectMultiScale(gray, 1.1, 6, minSize=(min_face, min_face)))
            flipped = cv2.flip(gray, 1)
            for (x, y, fw, fh) in profile.detectMultiScale(flipped, 1.1, 6,
                                                          minSize=(min_face, min_face)):
                faces.append((w - x - fw, y, fw, fh))
        return [(float(x), float(y), float(fw), float(fh), None) for x, y, fw, fh in faces]
    return detect


@dataclass
class Face:
    cx: float
    cy: float
    w: float
    h: float
    lm: list = None       # [rex, rey, lex, ley, nx, ny, rmx, rmy, lmx, lmy] in source pixels


@dataclass
class Sample:
    t: float
    faces: list
    cut: bool = False


@dataclass
class Track:
    """One person across a shot."""
    id: int
    pts: list = field(default_factory=list)      # (t, Face)

    @property
    def area(self):
        return float(np.median([f.w * f.h for _, f in self.pts]))

    @property
    def width(self):
        return float(np.median([f.w for _, f in self.pts]))

    def at(self, t):
        """(cx, cy) at time t, interpolated between sightings."""
        ts = [p[0] for p in self.pts]
        return (float(np.interp(t, ts, [p[1].cx for p in self.pts])),
                float(np.interp(t, ts, [p[1].cy for p in self.pts])))


class _Reader:
    """Sequential frame reader over [start, start + dur] that hands back every Nth frame."""

    def __init__(self, src, start, dur):
        import cv2
        self.cv2 = cv2
        self.cap = cv2.VideoCapture(str(src))
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.start, self.dur = start, dur

    def frames(self, per_second, t0=0.0, t1=None):
        """Yield (t, frame) at about `per_second`, t relative to the clip start."""
        t1 = self.dur if t1 is None else t1
        self.cap.set(self.cv2.CAP_PROP_POS_MSEC, (self.start + t0) * 1000)
        every = max(1, round(self.fps / per_second))
        i = 0
        while True:
            jobs.check()
            if not self.cap.grab():
                break
            t = self.cap.get(self.cv2.CAP_PROP_POS_MSEC) / 1000.0 - self.start
            if t > t1 + 0.1:
                break
            if t >= t0 - 0.05 and i % every == 0:
                ok, frame = self.cap.retrieve()
                if not ok:
                    break
                yield max(0.0, t), frame
            if t >= t0 - 0.05:
                i += 1

    def close(self):
        self.cap.release()


def _faces(detect, cv2, frame, small_w, small_h, scale):
    small = cv2.resize(frame, (small_w, small_h))
    gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
    out = []
    for x, y, w, h, lm in detect(small, gray):
        out.append(Face((x + w / 2) / scale, (y + h / 2) / scale, w / scale, h / scale,
                        [v / scale for v in lm] if lm else None))
    return out, gray


def picture_bounds(detail, src_w):
    """Old 4:3 interviews are often uploaded with bars down the sides, black or a blurred
    copy of the picture. `detail` is the average edge strength of each pixel column; bars
    have next to none. Returns (left, right) of the real picture in source pixels, or
    (0, src_w) when there are no bars."""
    n = len(detail)
    if n < 40:
        return 0, src_w
    smooth = np.convolve(detail, np.ones(5) / 5, mode="same")
    mid = float(np.median(smooth[int(n * 0.3):int(n * 0.7)]))
    if mid <= 0:
        return 0, src_w
    busy = smooth > 0.35 * mid

    def edge(cols):
        run = 0
        for i, c in enumerate(cols):
            run = run + 1 if c else 0
            if run >= 6:                      # six busy columns in a row: the picture starts here
                return i - 5
        return 0
    def bar(cols, sm):
        """Width of a bar at the start of `cols`, or 0. A bar ends at a hard line: detail
        jumps several-fold within a couple of columns. A plain wall at the side of a
        shot fades in gradually, and is left alone."""
        w = edge(cols)
        if w < 0.03 * len(cols) or w + 10 > len(cols):
            return 0
        outside = float(np.mean(sm[max(0, w - 10):max(1, w - 2)]))
        inside = float(np.mean(sm[w + 2:w + 10]))
        return w if inside >= 4 * max(outside, 1e-6) else 0
    # Each side is judged on its own: bars aren't always the same width.
    left, right = bar(busy, smooth), bar(busy[::-1], smooth[::-1])
    if left + right > 0.5 * n:
        return 0, src_w
    k = src_w / n
    return int(round(left * k)), int(round(src_w - right * k))


# ── shots and people ────────────────────────────────────────────────────────

def split_shots(samples):
    shots, cur = [], []
    for s in samples:
        if s.cut and cur:
            shots.append(cur)
            cur = []
        cur.append(s)
    if cur:
        shots.append(cur)
    return shots


def link_tracks(shot):
    """Group a shot's face sightings into people. A sighting joins the nearest person if it
    is within about half a face width of where they last were."""
    tracks = []
    for s in shot:
        free = list(tracks)
        for f in sorted(s.faces, key=lambda f: -f.w * f.h):
            best, best_d = None, None
            for tr in free:
                last = tr.pts[-1][1]
                d = ((f.cx - last.cx) ** 2 + (f.cy - last.cy) ** 2) ** 0.5
                if d < 0.6 * max(f.w, last.w) and (best is None or d < best_d):
                    best, best_d = tr, d
            if best is None:
                best = Track(len(tracks))
                tracks.append(best)
            else:
                free.remove(best)
            best.pts.append((s.t, f))
    return tracks


def people(tracks, n_samples):
    """The tracks that are really people in the shot: seen in a good share of it, and not
    tiny next to the biggest face (crowd faces and small prints drop out; a face on a poster
    that slips through never talks, so it is never chosen)."""
    solid = [tr for tr in tracks if len(tr.pts) >= max(3, 0.35 * n_samples)]
    if not solid:
        return []
    top = max(tr.area for tr in solid)
    return sorted((tr for tr in solid if tr.area >= 0.1 * top), key=lambda tr: tr.pts[0][1].cx)


# ── who is talking ──────────────────────────────────────────────────────────

def _patch(cv2, gray, cx, cy, w, h):
    """A contrast-normalized patch around (cx, cy), or None if it falls off the frame."""
    x0, y0 = int(round(cx - w / 2)), int(round(cy - h / 2))
    x1, y1 = x0 + max(4, int(round(w))), y0 + max(4, int(round(h)))
    if x0 < 0 or y0 < 0 or x1 > gray.shape[1] or y1 > gray.shape[0]:
        return None
    p = cv2.resize(gray[y0:y1, x0:x1], (ROI_W, ROI_H), interpolation=cv2.INTER_AREA).astype(np.float32)
    return (p - p.mean()) / (p.std() + 6.0)


def mouth_and_reference(cv2, gray, f):
    """Two patches from one face: the mouth and jaw, and the eyes and nose bridge.
    Speech changes the first and not the second; head movement changes both."""
    lm = f.lm
    mx, my = (lm[6] + lm[8]) / 2, (lm[7] + lm[9]) / 2          # between the mouth corners
    ex, ey = (lm[0] + lm[2]) / 2, (lm[1] + lm[3]) / 2          # between the eyes
    mw = max(0.42 * f.w, 1.5 * abs(lm[8] - lm[6]))
    mouth = _patch(cv2, gray, mx, my + 0.10 * f.h, mw, 0.42 * f.h)
    ref = _patch(cv2, gray, ex, ey + 0.04 * f.h, mw, 0.42 * f.h)
    return mouth, ref


def speech_envelope(src, start, dur, times):
    """Loudness of the clip's audio at each of `times` (seconds from the clip start), and
    which of those moments have speech in them. Returns (level, voiced) arrays, or
    (None, None) when there's no usable audio."""
    sr = 8000
    try:
        code, raw, _ = util.run_bytes(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}",
                                       "-t", f"{dur + 0.5:.3f}", "-i", str(src), "-vn", "-ac", "1",
                                       "-ar", str(sr), "-f", "s16le", "-"])
    except jobs.Cancelled:
        raise
    except Exception:
        return None, None
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if code != 0 or len(audio) < sr:
        return None, None
    audio = np.diff(audio, prepend=audio[:1])               # tilt away hum and rumble
    half = int(sr / DENSE_FPS / 2) + 1
    level = np.zeros(len(times), dtype=np.float32)
    for i, t in enumerate(times):
        c = int(t * sr)
        seg = audio[max(0, c - half):c + half]
        level[i] = float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0
    db = 20 * np.log10(level + 1e-6)
    floor, peak = np.percentile(db, 10), np.percentile(db, 95)
    voiced = db > max(floor + 6.0, peak - 30.0)
    return level, voiced


def _window_sum(x, n):
    k = np.ones(n, dtype=np.float32)
    return np.convolve(x, k, mode="same")


def choose_speakers(activity, voiced, fps=DENSE_FPS, hold=MIN_HOLD, switch_cost=SWITCH_COST):
    """activity: array [people, moments] of mouth movement (NaN where a face wasn't seen).
    voiced: which moments have speech. Returns the index of the person on camera at each
    moment: the path that follows the evidence with the fewest cuts (Viterbi), with any
    stretch shorter than `hold` folded into its neighbours."""
    k, n = activity.shape
    act = np.nan_to_num(activity, nan=0.0)
    seen = ~np.isnan(activity)
    # Put everyone on the same scale: a big, sharp close-up shows more mouth detail than a
    # small face across the table, so compare each person with their own lively moments.
    # (Capped, so someone who never speaks doesn't have their stillness blown up.)
    lively = np.array([np.percentile(act[i][seen[i]], 90) if seen[i].any() else 0.0 for i in range(k)])
    act = act / np.maximum(lively, 0.4 * lively.max() + 1e-6)[:, None]
    gate = voiced.astype(np.float32) if voiced is not None else np.ones(n, dtype=np.float32)
    win = max(3, int(round(1.0 * fps)))
    num = np.stack([_window_sum(act[i] * gate, win) for i in range(k)])
    heard = _window_sum(gate, win)
    share = num / (num.sum(axis=0, keepdims=True) + 1e-6)
    share = np.where(heard[None, :] >= 3, share, 1.0 / k)        # silence: no opinion
    # Someone who isn't on screen at a moment can't be the one shown.
    seen_w = np.stack([_window_sum(seen[i].astype(np.float32), win) for i in range(k)]) > 0
    emit = -np.log(share + 0.04)
    emit = np.where(seen_w, emit, emit + 4.0)

    cost = emit[:, 0].copy()
    back = np.zeros((k, n), dtype=np.int32)
    for t in range(1, n):
        stay = cost
        best_other = np.empty(k)
        arg_other = np.empty(k, dtype=np.int32)
        for i in range(k):
            others = [(cost[j] + switch_cost, j) for j in range(k) if j != i]
            best_other[i], arg_other[i] = min(others)
        take_other = best_other < stay
        back[:, t] = np.where(take_other, arg_other, np.arange(k))
        cost = np.where(take_other, best_other, stay) + emit[:, t]
    path = np.zeros(n, dtype=np.int32)
    path[-1] = int(np.argmin(cost))
    for t in range(n - 1, 0, -1):
        path[t - 1] = back[path[t], t]

    # Fold away stretches too short to be worth a cut.
    min_len = int(round(hold * fps))
    changed = True
    while changed:
        changed = False
        runs, s = [], 0
        for t in range(1, n + 1):
            if t == n or path[t] != path[s]:
                runs.append((s, t))
                s = t
        if len(runs) < 2:
            break
        for idx, (a, b) in enumerate(runs):
            if b - a < min_len:
                left = runs[idx - 1] if idx > 0 else None
                right = runs[idx + 1] if idx + 1 < len(runs) else None
                into = left if (left and (not right or (left[1] - left[0]) >= (right[1] - right[0]))) else right
                path[a:b] = path[into[0]]
                changed = True
                break
    return path


def mouth_activity(reader, detect, small_w, small_h, scale, who, t0, t1):
    """Mouth movement for each person in `who` across one shot. Returns (times, activity)
    with activity shaped [people, moments]."""
    cv2 = reader.cv2
    times, rows = [], []
    prev = [None] * len(who)          # (t, mouth patch, reference patch)
    for t, frame in reader.frames(DENSE_FPS, t0, t1):
        faces, _ = _faces(detect, cv2, frame, small_w, small_h, scale)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        row = [np.nan] * len(who)
        free = [f for f in faces if f.lm]
        for i, tr in enumerate(who):
            cx, cy = tr.at(t)
            near = [f for f in free if ((f.cx - cx) ** 2 + (f.cy - cy) ** 2) ** 0.5 < 0.6 * tr.width]
            if not near:
                prev[i] = None
                continue
            f = min(near, key=lambda f: (f.cx - cx) ** 2 + (f.cy - cy) ** 2)
            free.remove(f)
            mouth, ref = mouth_and_reference(cv2, gray, f)
            if mouth is None or ref is None:
                prev[i] = None
                continue
            if prev[i] is not None and t - prev[i][0] < 2.5 / DENSE_FPS:
                dm = float(np.mean(np.abs(mouth - prev[i][1])))
                dr = float(np.mean(np.abs(ref - prev[i][2])))
                row[i] = max(0.0, dm - dr)
            prev[i] = (t, mouth, ref)
        times.append(t)
        rows.append(row)
    if not rows:
        return np.array([]), np.zeros((len(who), 0))
    return np.array(times), np.array(rows, dtype=np.float32).T


# ── the crop path ───────────────────────────────────────────────────────────

def face_track(src, start, dur, src_w, src_h, crop_w, log=None, stats=None, speakers=True):
    """Return a list of (t, x_left) for the crop window, at 30 fps, relative to clip start.
    If a dict is passed as stats, it gets the median face centre-y and face height (source
    px), and "cuts": how many times the crop cut between speakers."""
    import cv2

    reader = _Reader(src, start, dur)
    scale = 480 / src_w
    small_w, small_h = 480, int(src_h * scale)
    min_face = max(24, int(small_h / 9))
    detect = make_detector(cv2, small_w, small_h, min_face)

    samples, prev_thumb = [], None
    detail = np.zeros(small_w - 1, dtype=np.float64)
    try:
        for t, frame in reader.frames(COARSE_FPS):
            faces, gray = _faces(detect, cv2, frame, small_w, small_h, scale)
            detail += np.abs(np.diff(gray.astype(np.int16), axis=1)).mean(axis=0)
            thumb = cv2.resize(gray, (64, 36)).astype(np.int16)
            cut = prev_thumb is not None and float(np.mean(np.abs(thumb - prev_thumb))) > 28
            prev_thumb = thumb
            samples.append(Sample(t, faces, cut))

        # Keep the crop inside the real picture when the upload has side bars.
        left, right = picture_bounds(detail, src_w) if samples else (0, src_w)
        if right - left < crop_w:
            left, right = 0, src_w
        lo, hi = left, right - crop_w
        center = (left + right) / 2
        if not samples or all(not s.faces for s in samples):
            if log:
                log("      no face found, using center crop")
            return [(0.0, int(center - crop_w / 2) // 2 * 2)]

        keyframes, ys, cuts = [], [], 0          # keyframes: (t, cx, snap)
        last_cx = None
        for shot in split_shots(samples):
            tracks = link_tracks(shot)
            who = people(tracks, len(shot))
            t0, t1 = shot[0].t, shot[-1].t
            path = None
            if (speakers and len(who) >= 2 and t1 - t0 >= 2 * MIN_HOLD
                    and all(any(f.lm for _, f in tr.pts) for tr in who)):
                times, activity = mouth_activity(reader, detect, small_w, small_h, scale, who, t0, t1)
                if activity.shape[1] >= DENSE_FPS:
                    _, voiced = speech_envelope(src, start, dur, times)
                    path = choose_speakers(activity, voiced)
            if path is not None:
                # Follow the chosen speaker; cut (snap) when the speaker changes.
                current = None
                for s in shot:
                    i = int(path[min(len(path) - 1, int(np.searchsorted(times, s.t)))])
                    cx, _ = who[i].at(s.t)
                    snap = current is None or i != current
                    if current is not None and i != current:
                        cuts += 1
                    current = i
                    keyframes.append((s.t, cx, snap))
                    last_cx = cx
                for tr in who:
                    ys += [(f.cy, f.h) for _, f in tr.pts]
            else:
                # One person (or no way to tell who's talking): follow the main face.
                main = max(who, key=lambda tr: tr.area) if who else \
                    (max(tracks, key=lambda tr: len(tr.pts)) if tracks else None)
                if main is None:
                    fill = last_cx if last_cx is not None else center
                    keyframes += [(s.t, fill, k == 0) for k, s in enumerate(shot)]
                    continue
                xs = [main.at(s.t)[0] for s in shot]
                med = [float(np.median(xs[max(0, k - 3):k + 4])) for k in range(len(xs))]
                keyframes += [(s.t, med[k], k == 0) for k, s in enumerate(shot)]
                last_cx = med[-1]
                ys += [(f.cy, f.h) for _, f in main.pts]
    finally:
        reader.close()

    if stats is not None:
        if ys:
            stats["cy"] = float(np.median([y for y, _ in ys]))
            stats["fh"] = float(np.median([h for _, h in ys]))
        stats["cuts"] = cuts
        stats["bounds"] = (left, right)
    if cuts and log:
        log(f"      following the conversation: {cuts} cut{'s' if cuts != 1 else ''} between speakers")

    # Dead zone + easing so the frame holds still while someone talks and glides when they move.
    dead = src_w * 0.04
    pos = keyframes[0][1]
    eased = []
    for t, cx, snap in keyframes:
        if snap:
            pos = cx
        elif abs(cx - pos) > dead:
            pos += (cx - pos) * 0.35
        eased.append((t, pos, snap))

    # Interpolate to every output frame. A snap must not be smeared into a fast pan, so
    # positions hold until the snap's own moment.
    out = []
    n = int(dur * FPS) + 1
    k = 0
    for f in range(n):
        t = f / FPS
        while k + 1 < len(eased) and eased[k + 1][0] <= t:
            k += 1
        if k + 1 < len(eased) and not eased[k + 1][2]:
            t_a, x_a, _ = eased[k]
            t_b, x_b, _ = eased[k + 1]
            cx = x_a + (x_b - x_a) * (t - t_a) / max(1e-6, t_b - t_a) if t >= t_a else x_a
        else:
            cx = eased[k][1]
        x = int(min(max(cx - crop_w / 2, lo), hi)) // 2 * 2
        if not out or out[-1][1] != x:
            out.append((round(t, 3), x))
    return out
