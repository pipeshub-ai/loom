"""Phase 13, E6: the dependency audit, enforced instead of remembered.

`phases/phase-13-browser-automation.md` §2.2 ruled two libraries out and one in,
and every word of that reasoning is worthless the first time somebody adds a
convenient dependency without repeating it. So the conclusions are assertions:

**AGPL is a hard stop.** LOOM is MIT (``pyproject.toml``), and AGPL's network
clause reaches any host that serves LOOM over HTTP — which is what ``loom
serve`` is for. Skyvern and ``workflow-use`` are both AGPL-3.0, and note that
``workflow-use`` is AGPL *while ``browser-use`` itself is MIT*: the
organisation's licence is not a guide, so this list names distributions.

**No ``==`` pins.** A dependency is not a library you like, it is a constraint
you impose on every host. ``browser-use`` is MIT, excellent, and pins ~40
packages exactly — including ``pydantic==2.12.5`` against LOOM's
``pydantic>=2.0``. Taking it would mean an SDK that dictates a host's pydantic
patch version. It stays welcome through the ``loom_browser_provider`` entry
point, where it costs nothing.

**No vendor SDK arrives sideways.** ``openai`` belongs behind ``[openai]``,
where a host opts into it — never pulled in by an unrelated extra.

Static, over the declared specifiers, so it runs anywhere with no network and no
resolver. That is a real limit: a *transitive* AGPL dependency would not be
caught here. Recorded rather than papered over.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

#: Distributions that may not appear at any depth we control. Named
#: individually because an org's licence is not a guide — see the module
#: docstring on browser-use vs workflow-use.
FORBIDDEN: dict[str, str] = {
    "skyvern": "AGPL-3.0 — its network clause reaches anything serving LOOM",
    "workflow-use": "AGPL-3.0, despite browser-use itself being MIT",
    "browser-use": (
        "MIT but pins ~40 packages with == (pydantic==2.12.5, httpx==0.28.1, "
        "openai, anthropic, google-genai, groq, ollama, posthog). Excellent "
        "adapter, unusable dependency — register it through the "
        "loom_browser_provider entry point instead."
    ),
}

#: Extras allowed to depend on a model vendor's SDK. Anything else pulling one
#: in is an accident, and the kind that is invisible until a host's install
#: doubles in size.
VENDOR_SDKS = {"openai", "anthropic", "google-genai", "google-generativeai",
               "groq", "ollama", "cohere", "mistralai"}
VENDOR_EXTRAS = {"anthropic", "openai", "gemini", "langchain", "agno",
                 "pydantic-ai", "all", "dev", "testing"}

_NAME = re.compile(r"^\s*([A-Za-z0-9._-]+)")


def distribution(specifier: str) -> str:
    match = _NAME.match(specifier)
    return match.group(1).lower().replace("_", "-") if match else ""


@pytest.fixture(scope="module")
def project() -> dict:
    return tomllib.loads(PYPROJECT.read_text())["project"]


def all_specifiers(project: dict) -> list[tuple[str, str]]:
    """Every declared dependency, tagged with where it was declared."""
    found = [("<core>", spec) for spec in project.get("dependencies", [])]
    for extra, specs in project.get("optional-dependencies", {}).items():
        found.extend((extra, spec) for spec in specs)
    return found


def test_loom_is_mit(project: dict) -> None:
    """The premise the rest of this file rests on."""
    licence = project.get("license")
    text = licence.get("text") if isinstance(licence, dict) else licence
    assert text == "MIT", f"licence is {text!r}; the AGPL rule below assumes MIT"


def test_no_forbidden_distribution_is_declared(project: dict) -> None:
    for extra, spec in all_specifiers(project):
        name = distribution(spec)
        if name in FORBIDDEN:
            pytest.fail(
                f"{extra} declares {name!r}: {FORBIDDEN[name]}\n"
                f"    (specifier: {spec})")


def test_nothing_is_pinned_to_an_exact_version(project: dict) -> None:
    """A range is a constraint; a pin is a decision made for the host."""
    pinned = [
        f"{extra}: {spec}"
        for extra, spec in all_specifiers(project)
        if "==" in spec and "!=" not in spec.replace("==", "")
    ]
    assert not pinned, (
        "exact pins force a host's resolver into a corner and are how an SDK "
        "ends up dictating an application's pydantic patch version:\n  "
        + "\n  ".join(pinned))


def test_the_core_stays_small(project: dict) -> None:
    """Everything optional lives behind an extra.

    The property that makes ``pip install loomsdk`` a reasonable thing to do,
    and the one that quietly erodes if nobody asserts it.
    """
    core = {distribution(spec) for spec in project.get("dependencies", [])}
    assert core <= {"pydantic", "pydantic-settings"}, (
        f"core dependencies grew to {sorted(core)}. Everything a workflow can "
        "run without belongs behind an extra.")


def test_no_vendor_sdk_arrives_through_an_unrelated_extra(project: dict) -> None:
    strays = [
        f"{extra} pulls in {distribution(spec)}"
        for extra, spec in all_specifiers(project)
        if distribution(spec) in VENDOR_SDKS and extra not in VENDOR_EXTRAS
    ]
    assert not strays, (
        "a model vendor's SDK should only arrive through the extra a host opts "
        "into:\n  " + "\n  ".join(strays))


class TestTheBrowserExtra:
    """E6 proper: what phase 13 actually costs a host."""

    def test_it_is_playwright_and_nothing_else(self, project: dict) -> None:
        extra = project["optional-dependencies"]["browser"]
        assert {distribution(s) for s in extra} == {"playwright"}, (
            f"[browser] declares {extra}. The whole dependency argument in "
            "§2.2 is that driving a page costs one Apache-2.0 package.")

    def test_the_runtime_reuses_the_probe_s_extra(self, project: dict) -> None:
        """13.1 adds no dependency at all.

        ``BrowserProbe`` already needed Playwright for authoring-time
        observation; the runtime provider needs the same one. A second browser
        driver would have been a real cost and there is none.
        """
        declared = project["optional-dependencies"]["browser"]
        assert len(declared) == 1

    def test_stealth_if_declared_is_permissive_and_separate(
        self, project: dict
    ) -> None:
        """``patchright`` is Apache-2.0 and opt-in.

        Separate from ``[browser]`` because anti-detection is a posture a host
        adopts deliberately, not something an SDK should switch on for them.
        """
        extras = project["optional-dependencies"]
        if "stealth" not in extras:
            pytest.skip("no [stealth] extra declared yet")
        assert {distribution(s) for s in extras["stealth"]} == {"patchright"}
        assert "patchright" not in {
            distribution(s) for s in extras["browser"]}
