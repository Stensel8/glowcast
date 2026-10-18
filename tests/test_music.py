"""Music engine: feature extraction and light effect, with synthetic audio."""

import itertools
import random

import pytest

from glowcast.audio import SPECTRUM_BANDS, AudioFrame
from glowcast.colors import hex_to_oklab, oklab_to_xy
from glowcast.music import AudioFeatureExtractor, AudioFeatures, MusicEffect
from glowcast.presets import MUSIC_PALETTES, MUSIC_PRESETS

FPS = 50
MODERATE = MUSIC_PRESETS["moderate"]
EMBER = MUSIC_PALETTES["ember"].colors
QUIET = AudioFeatures(bass=0, mid=0, high=0, pan=0, minor_event=0, major_event=0, silent=False)
SILENT = AudioFeatures(bass=0, mid=0, high=0, pan=0, minor_event=0, major_event=0, silent=True)
# The user's real layout: three unplaced lights plus three placed ones
LAYOUT = {
    0: {"x": 0, "y": 0, "z": 0},
    1: {"x": 0, "y": 0, "z": 0},
    2: {"x": 0, "y": 0, "z": 0},
    3: {"x": -0.04, "y": 0.77, "z": -0.41},
    4: {"x": -0.74, "y": -0.71, "z": 1.0},
    5: {"x": -0.10, "y": -0.81, "z": 1.0},
}


def beat_frame(t: float, rms=(-20.0, -20.0)) -> AudioFrame:
    """Background noise, a kick every 0.5 s and a snare in between."""
    mags = [-60.0 + random.uniform(-1.5, 1.5) for _ in range(SPECTRUM_BANDS)]
    kick = t % 0.5
    if kick < 0.06:
        mags[1:5] = [-20.0] * 4
    elif kick < 0.15:
        mags[1:5] = [-20.0 - (kick - 0.06) * 300] * 4
    if (t + 0.25) % 0.5 < 0.05:
        mags[6:60] = [-40.0] * 54
    return AudioFrame(mags, rms)


def silent_frame() -> AudioFrame:
    return AudioFrame([-90.0] * SPECTRUM_BANDS, (-90.0, -90.0))


@pytest.fixture(scope="module")
def beat_features():
    random.seed(1)
    extractor = AudioFeatureExtractor()
    return [extractor.process(beat_frame(n / FPS), n / FPS, MODERATE) for n in range(10 * FPS)]


def test_big_hits_land_on_kicks_and_respect_shuffle_delay(beat_features):
    majors = [n / FPS for n, f in enumerate(beat_features) if f.major_event]
    assert 3 <= len(majors) <= 5
    assert all(b - a >= MODERATE.shuffle_delay_s for a, b in itertools.pairwise(majors))
    assert all(round(t % 0.5, 2) <= 0.06 for t in majors)


def test_small_hits_on_kicks_and_snares(beat_features):
    minors = [f for f in beat_features if f.minor_event]
    assert 30 <= len(minors) <= 42  # ~4 per second


def test_bass_level_follows_kicks(beat_features):
    warm = list(enumerate(beat_features))[2 * FPS :]
    on_kick = [f.bass for n, f in warm if (n / FPS) % 0.5 < 0.04]
    between = [f.bass for n, f in warm if 0.25 < (n / FPS) % 0.5 < 0.45]
    assert min(on_kick) > 0.8
    assert max(between) < 0.2
    assert not any(f.silent for f in beat_features)


def test_silence_after_hold_without_hits():
    extractor = AudioFeatureExtractor()
    features = [extractor.process(silent_frame(), n / FPS, MODERATE) for n in range(3 * FPS)]
    first_silent = next(n / FPS for n, f in enumerate(features) if f.silent)
    assert 1.9 <= first_silent <= 2.1
    assert not any(f.major_event or f.minor_event for f in features)


def test_louder_left_channel_pans_left():
    features = AudioFeatureExtractor().process(beat_frame(0.3, rms=(-20.0, -30.0)), 0.3, MODERATE)
    assert -0.6 < features.pan < -0.4


def test_light_groups_split_left_to_right():
    effect = MusicEffect(LAYOUT, rng=random.Random(3))
    effect.update(QUIET, EMBER, MODERATE, 254, 0, True)
    assert len(set(effect._group_of.values())) == 6  # one group per light
    effect.update(QUIET, EMBER, MODERATE, 254, 2, True)
    assert {c for c, g in effect._group_of.items() if g == 0} == {3, 4, 5}


def test_big_hit_rotates_colours():
    effect = MusicEffect(LAYOUT)
    effect.update(QUIET, EMBER, MODERATE, 254, 2, True)
    before = list(effect._group_color)
    effect.update(AudioFeatures(0.5, 0.5, 0.5, 0, 0, 1.0, False), EMBER, MODERATE, 254, 2, True)
    assert effect._group_color == [(c + 1) % 5 for c in before]


def test_silence_settles_at_ambient_level():
    effect = MusicEffect(LAYOUT)
    for _ in range(400):
        colors = effect.update(SILENT, EMBER, MODERATE, 200, 0, True)
    target = round(200 * MODERATE.silence_level)
    assert all(abs(brightness - target) <= 1 for _xy, brightness in colors.values())


@pytest.mark.parametrize("stereo", [True, False])
def test_stereo_shifts_energy_to_the_louder_side(stereo):
    effect = MusicEffect({1: {"x": -0.8, "z": 0}, 2: {"x": 0.8, "z": 0}})
    right_panned = AudioFeatures(0.5, 0.5, 0.5, pan=1.0, minor_event=0, major_event=0, silent=False)
    for _ in range(100):
        colors = effect.update(right_panned, EMBER, MODERATE, 254, 0, stereo)
    left, right = colors[1][1], colors[2][1]
    if stereo:
        assert right > left + 40
    else:
        assert abs(right - left) <= 1


def test_oklab_round_trip_keeps_srgb_red():
    x, y = oklab_to_xy(hex_to_oklab("#FF0000"))
    assert x == pytest.approx(0.64, abs=0.002)
    assert y == pytest.approx(0.33, abs=0.002)


def listener(intensity="moderate"):
    from glowcast.music import AudioListener

    audio = AudioListener("test")
    audio.intensity = intensity
    return audio


def level(audio, x=0.0):
    return audio.gain(x)


def test_audio_effect_boosts_on_a_burst_and_fades_back():
    audio = listener()
    for _ in range(5 * FPS):  # warm up on steady background sound
        audio.step(flux=1.0, pan=0.0, silent=False, dt=1 / FPS)
    assert level(audio) == pytest.approx(1.0, abs=0.05)  # the screen's own brightness

    audio.step(flux=12.0, pan=0.0, silent=False, dt=1 / FPS)  # an explosion
    audio.step(flux=12.0, pan=0.0, silent=False, dt=1 / FPS)
    assert level(audio) > 2.0  # Moderate boosts up to 2.5x
    for _ in range(FPS):
        audio.step(flux=1.0, pan=0.0, silent=False, dt=1 / FPS)
    assert level(audio) == pytest.approx(1.0, abs=0.05)


def test_audio_effect_rapid_fire_is_a_shimmer_not_a_strobe():
    audio = listener()
    levels = []
    for n in range(6 * FPS):  # a beat every 0.12 s
        audio.step(flux=5.0 if n % 6 == 0 else 1.0, pan=0.0, silent=False, dt=1 / FPS)
        levels.append(level(audio))
    last_second = levels[-FPS:]
    assert max(last_second) - min(last_second) < 0.6


def test_audio_effect_steps_aside_in_silence():
    audio = listener()
    for _ in range(3 * FPS):
        audio.step(flux=1.0, pan=0.0, silent=False, dt=1 / FPS)
    for _ in range(6 * FPS):
        audio.step(flux=0.0, pan=0.0, silent=True, dt=1 / FPS)
    assert audio.gain() == pytest.approx(1.0, abs=0.02)


def test_audio_effect_follows_stereo():
    audio = listener("high")
    for _ in range(3 * FPS):
        audio.step(flux=1.0, pan=0.0, silent=False, dt=1 / FPS)
    audio.step(flux=4.0, pan=0.8, silent=False, dt=1 / FPS)  # a shot on the right
    assert level(audio, x=1.0) > level(audio, x=-1.0)
