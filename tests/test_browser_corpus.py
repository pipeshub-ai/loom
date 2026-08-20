"""Phase 13, E10: does tier-0 resolution reach real controls?

The gate over ``tests/corpus``. See that directory's README for the method; the
part worth repeating here is that the number this produces was *declared
meaningful in advance* — `phases/phase-13-browser-automation.md` §1.3 states
what each band means and what it changes — so this file is a decision procedure
rather than a metric nobody acts on.

Four things are asserted, and only one of them is the headline rate.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

CORPUS = Path(__file__).parent / "corpus"
PAGES = CORPUS / "pages"

playwright = pytest.importorskip(
    "playwright", reason="the corpus needs the [browser] extra"
)

from corpus import measure  # noqa: E402  (after importorskip, deliberately)

#: The §1.3 floor. Below this the phase's dependency conclusion — "Playwright
#: and nothing else" — no longer follows, so this is a design gate rather than
#: a quality metric.
MINIMUM_RATE = 0.70

#: Wikipedia is the control fixture, and this is the canary. Its expected score
#: is known independently: it is among the most accessibility-conscious sites on
#: the web, so a low number here indicts the instrument, not the page. The first
#: run of this suite scored it 47% and that is exactly how the capture's
#: opacity-is-hidden defect was found — a defect that was suppressing custom
#: radios, checkboxes and switches on every page at once.
CONTROL_FIXTURE = "wikipedia-search"
CONTROL_MINIMUM = 0.80


@pytest.fixture(scope="module")
def results():
    out = asyncio.run(measure.run())
    if not out:
        pytest.skip("no labelled pages in the corpus")
    return out


def test_every_capture_is_labelled_or_explained() -> None:
    """A page in the corpus with no manifest is a page nobody scored.

    Silent exclusion is how a benchmark drifts towards the fixtures that
    flatter it, so an unlabelled capture has to be visible as a gap.
    """
    captured = {p.stem.removesuffix(".meta") for p in PAGES.glob("*.meta.json")}
    labelled = {json.loads(p.read_text())["slug"]
                for p in PAGES.glob("*.controls.json")}
    unlabelled = sorted(captured - labelled)
    assert not unlabelled, (
        f"captured but never labelled: {unlabelled}. Label them from the "
        "screenshot, or delete the capture and record why in outcomes.json."
    )


def test_manifests_are_labelled_from_a_screenshot() -> None:
    """The anti-circularity rule, enforced rather than trusted.

    Labels derived from the DOM would measure the accessibility tree against
    itself. Every manifest must name the PNG it was written from, and that PNG
    must exist.
    """
    for path in PAGES.glob("*.controls.json"):
        data = json.loads(path.read_text())
        source = data.get("labelled_from", "")
        assert source.endswith(".png"), f"{path.name}: labelled_from must be a screenshot"
        assert (PAGES / source).exists(), f"{path.name}: {source} is missing"
        for control in data["controls"]:
            assert "TODO" not in json.dumps(control), f"{path.name}: unfinished label"
            if control.get("visible_label", True):
                assert control.get("name"), (
                    f"{path.name}: {control['purpose']!r} claims a visible label "
                    "but carries no name"
                )
            else:
                assert control.get("name") is None, (
                    f"{path.name}: {control['purpose']!r} has no visible label, so "
                    "its name must be null — a guessed name is a DOM read"
                )


def test_the_corpus_is_frozen(results) -> None:
    """No page may reach the network. The empirical form of the claim.

    A regex over the HTML cannot see an `svg use`, a CSS `url()` or a
    `<link imagesrcset>`; a blocked request can. A snapshot that phones home is
    not frozen, and its failure would present as flakiness rather than as a
    broken fixture.
    """
    leaked = {r.slug: r.external_requests[:3] for r in results if r.external_requests}
    assert not leaked, f"external requests from frozen pages: {leaked}"


def test_measurement_is_deterministic() -> None:
    """Same fixtures, same answer — twice.

    The corpus is frozen and the resolver does no inference, so any variation
    means a race in the harness: a visibility check running before layout, or a
    settle that is doing real work. A number that moves between runs cannot
    support the §1.3 decision whichever way it lands.
    """
    def fingerprint(rs):
        return json.dumps(
            [{"slug": r.slug,
              "controls": [[c.purpose, c.outcome, c.matches] for c in r.controls]}
             for r in sorted(rs, key=lambda r: r.slug)],
            sort_keys=True)

    first = fingerprint(asyncio.run(measure.run()))
    second = fingerprint(asyncio.run(measure.run()))
    assert first == second, "tier-0 measurement is not reproducible"


def test_control_fixture_still_scores_high(results) -> None:
    """Wikipedia is the canary. See CONTROL_MINIMUM."""
    control = next((r for r in results if r.slug == CONTROL_FIXTURE), None)
    if control is None:
        pytest.skip(f"{CONTROL_FIXTURE} not in the corpus")
    assert control.rate_extended >= CONTROL_MINIMUM, (
        f"{CONTROL_FIXTURE} resolved {control.rate_extended:.0%}, below "
        f"{CONTROL_MINIMUM:.0%}. This page is known-accessible, so suspect the "
        "harness before the page — that is what this fixture is for."
    )


def test_tier_zero_clears_the_design_floor(results) -> None:
    """E10. Prints the full table either way — the per-page spread is the finding."""
    print("\n" + measure.report(results))

    hits = sum(r.hits_extended for r in results)
    total = sum(r.total for r in results)
    rate = hits / total

    assert rate >= MINIMUM_RATE, (
        f"tier-0 resolution is {rate:.0%}, below the {MINIMUM_RATE:.0%} floor "
        "declared in phase-13 §1.3. That is a design decision, not a flaky "
        "test: re-open whether tier 1 is the product and whether 'Playwright "
        "and nothing else' still follows."
    )
