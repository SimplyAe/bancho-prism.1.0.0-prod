"""Guards rating a beatmap straight from its ``.osu`` text.

Hosting an uploaded beatmap means writing a ``maps`` row, and two of its columns
are values the ``.osu`` file never states: the star rating, and the max combo
(sliders and spinners contribute to combo, so it cannot be counted off the
hit-object lines). ``calculate_beatmap_difficulty`` gets both from the calculator
without touching the filesystem, so a difficulty can be rated before we decide to
keep it.

The important property is the failure mode: the calculator is third-party code
that raises on malformed input, and a submission endpoint must answer "this file
is unusable" rather than surfacing a 500. So every failure is converted to
``BeatmapDifficultyError``, and a NaN rating is treated as a failure too -- it
would otherwise be written to the database and compared against forever.
"""

from __future__ import annotations

import pytest

from app.services.performance import BeatmapDifficultyError
from app.services.performance import PerformanceService


def _osu_file(*, hit_objects: str, mode: int = 0, circle_size: str = "4") -> str:
    return "\n".join(
        [
            "osu file format v14",
            "",
            "[General]",
            f"Mode: {mode}",
            "",
            "[Metadata]",
            "Artist:a",
            "Title:t",
            "Creator:c",
            "Version:v",
            "",
            "[Difficulty]",
            "HPDrainRate:5",
            f"CircleSize:{circle_size}",
            "OverallDifficulty:8",
            "ApproachRate:9",
            "SliderMultiplier:1.4",
            "SliderTickRate:1",
            "",
            "[TimingPoints]",
            "0,300,4,2,0,60,1,0",
            "",
            "[HitObjects]",
            hit_objects,
            "",
        ],
    )


def test_rates_a_standard_beatmap_from_content() -> None:
    # four circles: max combo is one per object, and the map has a real rating.
    content = _osu_file(
        hit_objects="\n".join(
            [
                "256,192,0,1,0",
                "256,192,600,1,0",
                "256,192,1200,1,0",
                "256,192,1800,1,0",
            ],
        ),
    )

    attributes = PerformanceService().calculate_beatmap_difficulty(
        osu_file_content=content,
        mode=0,
    )

    assert attributes.max_combo == 4
    assert attributes.stars > 0


def test_rates_a_mania_beatmap() -> None:
    content = _osu_file(
        mode=3,
        circle_size="4",
        hit_objects="\n".join(
            [
                "64,192,0,1,0",
                "192,192,300,1,0",
                "320,192,600,1,0",
            ],
        ),
    )

    attributes = PerformanceService().calculate_beatmap_difficulty(
        osu_file_content=content,
        mode=3,
    )

    assert attributes.max_combo > 0
    assert attributes.stars > 0


def test_unparseable_content_raises_our_own_error() -> None:
    # the calculator's own exception must not escape to the caller as a 500.
    with pytest.raises(BeatmapDifficultyError):
        PerformanceService().calculate_beatmap_difficulty(
            osu_file_content="this is not a beatmap",
            mode=0,
        )


def test_empty_content_raises_our_own_error() -> None:
    with pytest.raises(BeatmapDifficultyError):
        PerformanceService().calculate_beatmap_difficulty(
            osu_file_content="",
            mode=0,
        )


def test_the_rating_is_storable_in_the_maps_column() -> None:
    # `maps.diff` is float(6,3); a value past 999.999 could not be written.
    content = _osu_file(hit_objects="256,192,0,1,0\n256,192,600,1,0")

    attributes = PerformanceService().calculate_beatmap_difficulty(
        osu_file_content=content,
        mode=0,
    )

    assert attributes.stars <= 999.999
    assert round(attributes.stars, 3) == attributes.stars
