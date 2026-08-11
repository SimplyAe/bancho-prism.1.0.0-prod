"""Tests for the A1 hardware-spoofer rainbow-table detector.

Tables are built over a *tiny* seed band so the test is fast and exact -- the
production band is ~33k seeds, but the mechanism is identical. Each test hashes a
known preimage the same way the builder does, then asserts the detector recovers
the seed (or, for a genuine hardware hash, does not).
"""

from __future__ import annotations

import hashlib

from app.services.anticheat.detectors import Severity
from app.services.anticheat.spoofer import build_spoofer_hash_table
from app.services.anticheat.spoofer import detect_spoofed_hashes
from app.services.anticheat.spoofer import spoofer_signal

_LO = 1_000_000
_HI = 1_000_099  # 100 seeds -- enough to be meaningful, tiny to be fast.


def _decimal_md5(seed: int) -> str:
    return hashlib.md5(str(seed).encode("ascii")).hexdigest()


def _table():  # type: ignore[no-untyped-def]
    return build_spoofer_hash_table(encoding="decimal", lo=_LO, hi=_HI)


# a hash of something clearly not a banded integer -- a real fingerprint.
_LEGIT = hashlib.md5(b"a-real-hardware-adapter-string").hexdigest()


# --- table construction -----------------------------------------------------


def test_table_covers_the_inclusive_band() -> None:
    table = _table()
    assert len(table) == (_HI - _LO + 1)
    assert _decimal_md5(_LO) in table
    assert _decimal_md5(_HI) in table  # endpoint is inclusive
    assert table.lookup(_decimal_md5(_LO + 42)) == _LO + 42


def test_unknown_encoding_raises_rather_than_empty_table() -> None:
    try:
        build_spoofer_hash_table(encoding="nope", lo=_LO, hi=_HI)
    except ValueError as exc:
        assert "unknown preimage encoding" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected ValueError for unknown encoding")


def test_lookup_is_case_insensitive() -> None:
    table = _table()
    assert table.lookup(_decimal_md5(_LO + 5).upper()) == _LO + 5


# --- detection --------------------------------------------------------------


def test_legit_hardware_hashes_do_not_match() -> None:
    result = detect_spoofed_hashes(
        osu_path_md5=_LEGIT,
        adapters_md5=_LEGIT,
        uninstall_md5=_LEGIT,
        disk_signature_md5=_LEGIT,
        table=_table(),
    )
    assert result.matched is False
    assert result.matches == ()


def test_spoofed_disk_serial_is_detected_with_seed() -> None:
    seed = _LO + 7
    result = detect_spoofed_hashes(
        osu_path_md5=_LEGIT,
        adapters_md5=_LEGIT,
        uninstall_md5=_LEGIT,
        disk_signature_md5=_decimal_md5(seed),
        table=_table(),
    )
    assert result.matched is True
    assert len(result.hardware_matches) == 1
    match = result.hardware_matches[0]
    assert match.field_name == "disk_signature_md5"
    assert match.seed == seed
    assert match.is_hardware is True


def test_multiple_spoofed_fields_all_recovered() -> None:
    result = detect_spoofed_hashes(
        osu_path_md5=_LEGIT,
        adapters_md5=_decimal_md5(_LO + 1),
        uninstall_md5=_decimal_md5(_LO + 2),
        disk_signature_md5=_decimal_md5(_LO + 3),
        table=_table(),
    )
    assert len(result.hardware_matches) == 3
    seeds = {m.field_name: m.seed for m in result.hardware_matches}
    assert seeds == {
        "adapters_md5": _LO + 1,
        "uninstall_md5": _LO + 2,
        "disk_signature_md5": _LO + 3,
    }


def test_osu_path_only_match_is_not_a_hardware_match() -> None:
    result = detect_spoofed_hashes(
        osu_path_md5=_decimal_md5(_LO + 9),
        adapters_md5=_LEGIT,
        uninstall_md5=_LEGIT,
        disk_signature_md5=_LEGIT,
        table=_table(),
    )
    assert result.matched is True
    assert result.hardware_matches == ()


# --- signal folding ---------------------------------------------------------


def test_signal_is_high_for_hardware_match() -> None:
    result = detect_spoofed_hashes(
        osu_path_md5=_LEGIT,
        adapters_md5=_decimal_md5(_LO + 4),
        uninstall_md5=_LEGIT,
        disk_signature_md5=_LEGIT,
        table=_table(),
    )
    signal = spoofer_signal(result)
    assert signal.code == "A1_HARDWARE_SPOOFER"
    assert signal.severity is Severity.HIGH
    assert signal.flagged is True
    assert signal.evidence["seed_adapters_md5"] == float(_LO + 4)


def test_signal_is_low_for_osu_path_only_match() -> None:
    result = detect_spoofed_hashes(
        osu_path_md5=_decimal_md5(_LO + 9),
        adapters_md5=_LEGIT,
        uninstall_md5=_LEGIT,
        disk_signature_md5=_LEGIT,
        table=_table(),
    )
    signal = spoofer_signal(result)
    assert signal.severity is Severity.LOW
    assert signal.flagged is False


def test_signal_is_none_when_nothing_matches() -> None:
    result = detect_spoofed_hashes(
        osu_path_md5=_LEGIT,
        adapters_md5=_LEGIT,
        uninstall_md5=_LEGIT,
        disk_signature_md5=_LEGIT,
        table=_table(),
    )
    signal = spoofer_signal(result)
    assert signal.severity is Severity.NONE
    assert signal.flagged is False
