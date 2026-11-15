"""Crop tracking: who is a person, who is talking, where the picture ends."""
import numpy as np
import pytest

from clipper import tracking as T
from conftest import needs_ffmpeg

FPS = T.DENSE_FPS


def conversation(turns, noise=0.05, lively=1.0, seed=3):
    """Mouth-movement signals for two people taking turns. turns: [(seconds, speaker)]."""
    rng = np.random.default_rng(seed)
    n = int(sum(d for d, _ in turns) * FPS)
    act = rng.random((2, n)) * noise
    truth = np.zeros(n, dtype=int)
    i = 0
    for d, who in turns:
        j = i + int(d * FPS)
        act[who, i:j] += lively * (0.4 + 0.6 * rng.random(j - i))
        truth[i:j] = who
        i = j
    return act.astype(np.float32), np.ones(n, dtype=bool), truth


def accuracy(path, truth, slack=1.0):
    edges = np.flatnonzero(np.diff(truth)) + 1
    far = np.ones(len(truth), dtype=bool)
    for e in edges:
        far[max(0, e - int(slack * FPS)):e + int(slack * FPS)] = False
    return float((path[far] == truth[far]).mean())


def test_follows_the_conversation():
    act, voiced, truth = conversation([(6, 0), (5, 1), (8, 0), (4, 1), (7, 0)])
    path = T.choose_speakers(act, voiced)
    assert accuracy(path, truth) > 0.97
    assert len(np.flatnonzero(np.diff(path))) == 4          # one cut per change of speaker


def test_short_interjections_do_not_pull_the_camera_away():
    act, voiced, truth = conversation([(8, 0), (0.7, 1), (8, 0)])
    path = T.choose_speakers(act, voiced)
    assert (path == 0).all()


def test_face_size_does_not_decide_who_is_talking():
    """A big close-up shows more mouth detail than a small face; that alone must not win."""
    act, voiced, truth = conversation([(6, 0), (6, 1), (6, 0), (6, 1)])
    act[1] *= 3.0                                           # person 1 is a tight close-up
    act[1] += 0.25 * (truth == 0)                           # and fidgets while listening
    path = T.choose_speakers(act, voiced)
    assert accuracy(path, truth) > 0.9


def test_silence_holds_the_shot():
    act, voiced, truth = conversation([(6, 0), (6, 1)])
    voiced[int(8 * FPS):] = False                           # nobody audible from 8 s on
    act[0, int(8 * FPS):] += 1.0                            # person 0 moves, but no speech
    path = T.choose_speakers(act, voiced)
    assert (path[int(9 * FPS):] == 1).all()


def test_someone_off_screen_is_not_chosen():
    act, voiced, truth = conversation([(6, 0), (6, 1)])
    act[1, :] = np.nan                                      # person 1 never detected
    assert (T.choose_speakers(act, voiced) == 0).all()


def face(cx, cy=200, w=100):
    return T.Face(cx, cy, w, w * 1.2, [cx - 20, cy - 10, cx + 20, cy - 10, cx, cy + 5, cx - 15, cy + 30, cx + 15, cy + 30])


def test_linking_and_people():
    shot = [T.Sample(i / 5, [face(300 + i), face(900 - i), face(1500, w=20)] if i % 2 else [face(300 + i), face(900 - i)])
            for i in range(20)]
    shot[7].faces.append(face(60))                          # a one-frame false detection
    tracks = T.link_tracks(shot)
    who = T.people(tracks, len(shot))
    assert len(who) == 2                                    # the tiny face and the blip drop out
    assert [round(tr.at(0)[0], -2) for tr in who] == [300, 900]      # left to right
    assert who[0].at(1.0)[0] == pytest.approx(305, abs=2)


def test_cuts_split_shots():
    samples = [T.Sample(i / 5, [], cut=(i in (4, 9))) for i in range(12)]
    assert [len(s) for s in T.split_shots(samples)] == [4, 5, 3]


def bars_profile(n=479, left=40, right=60, soft=False):
    d = np.full(n, 8.0)
    d[:left] = 0.5
    d[n - right:] = 0.5
    if soft:                                                # a plain wall: detail fades in gradually
        d[:left + 60] = np.linspace(0.5, 8.0, left + 60)
    return d


def test_side_bars_are_found_and_plain_walls_are_not():
    left, right = T.picture_bounds(bars_profile(), 1920)
    true_left, true_right = 40 / 479 * 1920, 1920 - 60 / 479 * 1920
    # just inside the picture on both sides: never a sliver of bar, never much picture lost
    assert true_left <= left <= true_left + 24 and true_right - 24 <= right <= true_right
    assert T.picture_bounds(np.full(479, 8.0), 1920) == (0, 1920)
    left, right = T.picture_bounds(bars_profile(right=0, soft=True), 1920)
    assert left == 0                                        # gradual edge: not a bar
    assert T.picture_bounds(bars_profile(left=5, right=5), 1920) == (0, 1920)      # slivers ignored


@needs_ffmpeg
def test_no_faces_means_a_centred_crop(media):
    stats = {}
    track = T.face_track(media.interview, 2.0, 4.0, 640, 360, 202, stats=stats)
    assert track == [(0.0, 218)]


@needs_ffmpeg
def test_tracking_can_be_stopped(media):
    from clipper import jobs
    jobs.request()
    with pytest.raises(jobs.Cancelled):
        T.face_track(media.interview, 0.0, 10.0, 640, 360, 202)
