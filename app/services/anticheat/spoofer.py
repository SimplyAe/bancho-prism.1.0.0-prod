"""A1 -- unified hardware-spoofer detection via a bounded-seed rainbow table.

osu! sends four hardware fingerprints at login (see
``app/repositories/client_hashes.py``): the osu! path hash, the network-adapter
hash, the uninstall id, and the disk-signature hash. Each is an MD5 hex of a
real hardware/string value. Multi-account spoofers (OsuSpoofer, Maple and their
forks) don't compute those from hardware -- they MD5 a *random integer drawn
from a small fixed band* so every alt presents a fresh but cheaply-generated
fingerprint. That band is the weakness: it is tiny (order 2**15), so we can
precompute ``md5(preimage(seed))`` for every seed once and then match any login
hash against the set in O(1). A hit recovers the exact seed.

Why a match is strong evidence -- and why we still only flag.
A legitimate fingerprint is ``md5`` of an unrelated hardware string; the chance
it collides with any of the ~32.8k precomputed hashes is about ``32769 / 2**128``
(~1e-34). So a match essentially *cannot* happen by chance: the preimage really
was one of the banded integers. The residual risk is not false positives, it is
the **preimage encoding**: the exact byte form the spoofer hashes (decimal ASCII?
zero-padded? some wrapper?) is not recoverable from the source on hand. Get the
encoding wrong and the table matches nothing (missed detections), not innocents.
So the table is built parameterised over candidate encodings, every default
encoding is marked provisional, and the signal -- like the movement detectors --
feeds staff review rather than an auto-ban until an encoding is confirmed against
known-spoofed accounts.

Everything here is pure. ``build_spoofer_hash_table`` does the one-time ~33k-hash
work; keep the result and pass it to ``detect_spoofed_hashes`` per login.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field

from app.services.anticheat.detectors import DetectionSignal
from app.services.anticheat.detectors import Severity

# The seed band the known spoofers draw from: [lo, hi] inclusive == 32,769
# candidates (hi - lo == 32768 == 2**15). Provisional: pinned from the plan's
# analysis of OsuSpoofer/Maple, to be re-confirmed against flagged accounts.
SPOOFER_SEED_LO = 1_000_000
SPOOFER_SEED_HI = 1_032_768

# The four login fingerprint fields, in the order osu! sends them. The osu! path
# hash is shared by legit clients (it is not hardware), so a match there is weak;
# the three hardware fields are the real signal. Kept as data so the detector and
# its tests agree on names.
HARDWARE_FIELDS = ("adapters_md5", "uninstall_md5", "disk_signature_md5")
_ALL_FIELDS = ("osu_path_md5", *HARDWARE_FIELDS)


def _decimal_preimage(seed: int) -> bytes:
    """``str(seed)`` as ASCII -- the most common spoofer form. Provisional."""
    return str(seed).encode("ascii")


def _decimal_upper_hex_wrapped(seed: int) -> bytes:
    """Alternate candidate: the seed's uppercase hex, no ``0x``. Provisional."""
    return format(seed, "X").encode("ascii")


# Candidate preimage encodings to build tables for. NONE is confirmed yet; the
# operator populates the table with whichever encoding backtesting validates.
PREIMAGE_ENCODERS: dict[str, Callable[[int], bytes]] = {
    "decimal": _decimal_preimage,
    "upper_hex": _decimal_upper_hex_wrapped,
}


@dataclass(frozen=True, slots=True)
class SpooferHashTable:
    """Precomputed ``md5(preimage(seed)) -> seed`` over one seed band/encoding.

    Immutable and hashable-by-identity; build once and reuse. ``lookup`` returns
    the recovered seed for a matching MD5 hex (case-insensitive), else ``None``.
    """

    encoding: str
    lo: int
    hi: int
    by_hash: dict[str, int]

    def __len__(self) -> int:
        return len(self.by_hash)

    def lookup(self, md5_hex: str) -> int | None:
        return self.by_hash.get(md5_hex.lower())

    def __contains__(self, md5_hex: str) -> bool:
        return md5_hex.lower() in self.by_hash


def build_spoofer_hash_table(
    *,
    encoding: str = "decimal",
    lo: int = SPOOFER_SEED_LO,
    hi: int = SPOOFER_SEED_HI,
) -> SpooferHashTable:
    """Precompute the rainbow table for one encoding over ``[lo, hi]`` inclusive.

    ~33k MD5s at the defaults -- do this once at startup, not per login. Raises
    on an unknown encoding so a typo can't silently yield an empty (never-match)
    table.
    """
    if encoding not in PREIMAGE_ENCODERS:
        raise ValueError(
            f"unknown preimage encoding {encoding!r}; "
            f"known: {sorted(PREIMAGE_ENCODERS)}",
        )
    if hi < lo:
        raise ValueError(f"empty seed band: hi ({hi}) < lo ({lo})")

    encode = PREIMAGE_ENCODERS[encoding]
    by_hash: dict[str, int] = {}
    for seed in range(lo, hi + 1):
        digest = hashlib.md5(encode(seed)).hexdigest()
        # first seed wins on the astronomically-unlikely intra-band collision,
        # so the mapping is deterministic regardless of iteration effects.
        by_hash.setdefault(digest, seed)
    return SpooferHashTable(encoding=encoding, lo=lo, hi=hi, by_hash=by_hash)


@dataclass(frozen=True, slots=True)
class SpooferMatch:
    """One login field whose hash was found in the rainbow table."""

    field_name: str
    md5_hex: str
    seed: int
    is_hardware: bool


@dataclass(frozen=True, slots=True)
class SpooferResult:
    """Outcome of checking one login's four hashes against the table."""

    matched: bool
    encoding: str
    matches: tuple[SpooferMatch, ...] = field(default_factory=tuple)

    @property
    def hardware_matches(self) -> tuple[SpooferMatch, ...]:
        return tuple(m for m in self.matches if m.is_hardware)


def detect_spoofed_hashes(
    *,
    osu_path_md5: str,
    adapters_md5: str,
    uninstall_md5: str,
    disk_signature_md5: str,
    table: SpooferHashTable,
) -> SpooferResult:
    """Check each login fingerprint against the rainbow table.

    Pure. A match on any of the three *hardware* fields is the real spoofer
    signal; a lone osu!-path match is noise-prone and left to the caller to
    weigh (it is reported but not treated as hardware). Returns every match so a
    reviewer sees the full picture and the recovered seeds.
    """
    provided = {
        "osu_path_md5": osu_path_md5,
        "adapters_md5": adapters_md5,
        "uninstall_md5": uninstall_md5,
        "disk_signature_md5": disk_signature_md5,
    }

    matches: list[SpooferMatch] = []
    for field_name in _ALL_FIELDS:
        md5_hex = provided[field_name]
        seed = table.lookup(md5_hex)
        if seed is not None:
            matches.append(
                SpooferMatch(
                    field_name=field_name,
                    md5_hex=md5_hex.lower(),
                    seed=seed,
                    is_hardware=field_name in HARDWARE_FIELDS,
                ),
            )

    return SpooferResult(
        matched=bool(matches),
        encoding=table.encoding,
        matches=tuple(matches),
    )


def spoofer_signal(result: SpooferResult) -> DetectionSignal:
    """Fold a spoofer result into the shared ``DetectionSignal`` shape.

    So a spoofer hit and the movement signals land in one uniform review record.
    Severity: any hardware-field match is HIGH (a banded-seed preimage is not a
    real fingerprint); an osu!-path-only match is LOW (shared/legit-prone).
    """
    hardware = result.hardware_matches
    evidence: dict[str, float] = {
        "match_count": float(len(result.matches)),
        "hardware_match_count": float(len(hardware)),
    }
    for match in result.matches:
        evidence[f"seed_{match.field_name}"] = float(match.seed)

    if hardware:
        fields = ", ".join(m.field_name for m in hardware)
        return DetectionSignal(
            code="A1_HARDWARE_SPOOFER",
            title="Hardware fingerprint spoofer",
            severity=Severity.HIGH,
            confidence=0.97,
            flagged=True,
            detail=(
                f"{fields} hash matches a banded spoofer seed "
                f"(encoding {result.encoding!r}) -- fingerprint is generated, "
                f"not real hardware"
            ),
            evidence=evidence,
        )
    if result.matches:
        return DetectionSignal(
            code="A1_HARDWARE_SPOOFER",
            title="Hardware fingerprint spoofer",
            severity=Severity.LOW,
            confidence=0.3,
            flagged=False,
            detail=(
                "only the osu!-path hash matched a banded seed -- weak on its own "
                "(that field is not hardware-derived)"
            ),
            evidence=evidence,
        )
    return DetectionSignal(
        code="A1_HARDWARE_SPOOFER",
        title="Hardware fingerprint spoofer",
        severity=Severity.NONE,
        confidence=0.0,
        flagged=False,
        detail="no login fingerprint matched the spoofer seed band",
        evidence=evidence,
    )
