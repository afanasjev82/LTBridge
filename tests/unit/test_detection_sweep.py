"""Recorded zero-divergence gate for the pure-Python whatlang port.

``tests/fixtures/detection_sweep.json.gz`` holds a 16k-case corpus together
with the output the vendored PyO3 ``whatlang_py`` binding produced for each
case while it still existed (crate whatlang 0.18.0). The binding — and the
whole Rust crate — is gone; this replay is what keeps the port pinned to it.

Regenerate with::

    uv run python scripts/sweep_detection_parity.py --oracle binding --dump

which requires the binding back in the tree, so in practice a change here means
the port changed and must be re-justified, not that the fixture is stale.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from ltengine.core import _whatlang_data as D
from ltengine.core import whatlang
from ltengine.core.languages import _DETECT_ALLOWLIST

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "detection_sweep.json.gz"


@pytest.fixture(scope="module")
def recorded() -> dict:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def test_fixture_matches_the_generated_tables(recorded: dict) -> None:
    """A crate bump must re-record the sweep, not silently reuse the old one."""
    meta = recorded["meta"]
    assert meta["crate"] == D.CRATE
    assert meta["crate_version"] == D.CRATE_VERSION
    assert meta["allowlist_size"] == len(_DETECT_ALLOWLIST)
    assert len(recorded["cases"]) >= 5000


def test_port_reproduces_every_recorded_oracle_result(recorded: dict) -> None:
    divergences: list[tuple[str, object, object]] = []
    for text, want in recorded["cases"]:
        got = whatlang.detect(text, _DETECT_ALLOWLIST)
        as_recorded = list(got) if got is not None else None
        if as_recorded != want:
            divergences.append((text, want, as_recorded))

    assert not divergences, (
        f"{len(divergences)} of {len(recorded['cases'])} cases diverge from the "
        f"recorded whatlang oracle; first 5: {divergences[:5]}"
    )
