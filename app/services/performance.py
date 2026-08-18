from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from akatsuki_pp_py import Beatmap
from akatsuki_pp_py import Calculator

from app.constants.mods import Mods


@dataclass
class ScoreParams:
    mode: int
    mods: int | None = None
    combo: int | None = None

    # caller may pass either acc OR 300/100/50/geki/katu/miss
    # passing both will result in a value error being raised
    acc: float | None = None

    n300: int | None = None
    n100: int | None = None
    n50: int | None = None
    ngeki: int | None = None
    nkatu: int | None = None
    nmiss: int | None = None


@dataclass(frozen=True)
class PerformanceRating:
    pp: float
    pp_acc: float | None
    pp_aim: float | None
    pp_speed: float | None
    pp_flashlight: float | None
    effective_miss_count: float | None
    pp_difficulty: float | None


@dataclass(frozen=True)
class DifficultyRating:
    stars: float
    aim: float | None
    speed: float | None
    flashlight: float | None
    slider_factor: float | None
    speed_note_count: float | None
    stamina: float | None
    color: float | None
    rhythm: float | None
    peak: float | None


@dataclass(frozen=True)
class PerformanceResult:
    performance: PerformanceRating
    difficulty: DifficultyRating


class BeatmapDifficultyError(ValueError):
    """A beatmap's ``.osu`` content could not be rated.

    Raised in place of whatever the calculator threw, so a caller rating an
    uploaded beatmap can map "this file is unusable" onto a rejection instead of
    letting a third-party exception escape as a 500.
    """


@dataclass(frozen=True)
class BeatmapDifficultyAttributes:
    """The two map-level values a ``.osu`` file does not state itself.

    Both are needed to write a ``maps`` row and neither appears in the file:
    ``stars`` is the star rating, ``max_combo`` the theoretical maximum combo
    (which sliders and spinners contribute to, so it cannot be counted off the
    hit-object lines alone).
    """

    stars: float
    max_combo: int


# `maps.diff` is float(6,3), so a rating beyond this cannot be stored. Clamping
# (rather than raising) keeps a pathological map submittable but honestly rated.
_MAX_STORABLE_STARS = 999.999


class PerformanceService:
    def calculate_performances(
        self,
        osu_file_path: str,
        scores: Iterable[ScoreParams],
    ) -> list[PerformanceResult]:
        """\
        Calculate performance for multiple scores on a single beatmap.

        Typically most useful for mass-recalculation situations.

        TODO: Some level of error handling & returning to caller should be
        implemented here to handle cases where e.g. the beatmap file is invalid
        or there an issue during calculation.
        """
        calc_bmap = Beatmap(path=osu_file_path)

        results: list[PerformanceResult] = []

        for score in scores:
            if score.acc and (
                score.n300 or score.n100 or score.n50 or score.ngeki or score.nkatu
            ):
                raise ValueError(
                    "Must not specify accuracy AND 300/100/50/geki/katu. Only one or the other.",
                )

            # rosupp ignores NC and requires DT
            if score.mods is not None:
                if score.mods & Mods.NIGHTCORE:
                    score.mods |= Mods.DOUBLETIME

            calculator = Calculator(
                mode=score.mode,
                mods=score.mods or 0,
                combo=score.combo,
                acc=score.acc,
                n300=score.n300,
                n100=score.n100,
                n50=score.n50,
                n_geki=score.ngeki,
                n_katu=score.nkatu,
                n_misses=score.nmiss,
            )

            result = calculator.performance(calc_bmap)

            pp = result.pp

            if math.isnan(pp) or math.isinf(pp):
                # TODO: report to logserver
                pp = 0.0
            else:
                pp = round(pp, 3)

            results.append(
                PerformanceResult(
                    performance=PerformanceRating(
                        pp=pp,
                        pp_acc=result.pp_acc,
                        pp_aim=result.pp_aim,
                        pp_speed=result.pp_speed,
                        pp_flashlight=result.pp_flashlight,
                        effective_miss_count=result.effective_miss_count,
                        pp_difficulty=result.pp_difficulty,
                    ),
                    difficulty=DifficultyRating(
                        stars=result.difficulty.stars,
                        aim=result.difficulty.aim,
                        speed=result.difficulty.speed,
                        flashlight=result.difficulty.flashlight,
                        slider_factor=result.difficulty.slider_factor,
                        speed_note_count=result.difficulty.speed_note_count,
                        stamina=result.difficulty.stamina,
                        color=result.difficulty.color,
                        rhythm=result.difficulty.rhythm,
                        peak=result.difficulty.peak,
                    ),
                ),
            )

        return results

    def calculate_beatmap_difficulty(
        self,
        *,
        osu_file_content: str,
        mode: int,
    ) -> BeatmapDifficultyAttributes:
        """Rate a beatmap straight from its ``.osu`` text.

        Used when hosting an uploaded beatmap: the ``maps`` row needs a star
        rating and a max combo, and neither is stated in the file. The calculator
        accepts the content directly, so nothing has to touch the filesystem --
        the caller can rate a difficulty before deciding to keep it.

        Raises :class:`BeatmapDifficultyError` if the content cannot be rated.
        """
        try:
            calc_bmap = Beatmap(content=osu_file_content)
            attributes = Calculator(mode=mode).difficulty(calc_bmap)
            stars = attributes.stars
            max_combo = attributes.max_combo
        except Exception as exc:
            raise BeatmapDifficultyError(f"could not rate beatmap: {exc}") from exc

        # same guard as `calculate_performances` applies to pp: a NaN would be
        # written to the database and then compared against forever.
        if math.isnan(stars) or math.isinf(stars):
            raise BeatmapDifficultyError("beatmap rated as NaN/inf stars")

        return BeatmapDifficultyAttributes(
            stars=min(round(stars, 3), _MAX_STORABLE_STARS),
            max_combo=max(int(max_combo), 0),
        )
