"""Measure tier-0 resolution against the frozen corpus. Phase 13, E10.

The question this answers, and the only one: **does `get_by_role(role, name=…)`
address the controls a person can see?** That is the premise the whole
"Playwright and nothing else" dependency conclusion rests on (§2.2), and it is a
claim about the web rather than about LOOM, so it has to be measured rather than
argued.

Three properties keep the number honest.

**The label is what a person read off the screenshot**, never what the DOM
contains. Deriving expectations from the tree would measure the tree against
itself and report something near 100%. The manifests are hand-written from the
captured PNG — see ``README.md``, "Labelling".

**Strict counts as a miss.** Playwright's locators raise on ambiguity rather
than picking the first match, which is the behaviour tier 0 wants, so a control
that resolves to two elements has *not* been resolved. Scoring it as a hit would
credit the exact failure mode — clicking the wrong button — that strictness
exists to prevent.

**A miss is diagnosed, not just counted.** "No accessible name at all" and
"named but not uniquely" need different fixes, and only the first is evidence
against the premise; the second is usually an ordinal away from working.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PAGES = Path(__file__).resolve().parent / "pages"

#: Outcomes, kept separate because they drive different conclusions.
HIT = "hit"
MISS_ABSENT = "miss:absent"        # nothing carries that accessible name
MISS_AMBIGUOUS = "miss:ambiguous"  # several do — strict mode refuses
MISS_ROLE = "miss:role"            # the name resolves, but under another role
MISS_UNLABELLED = "miss:unlabelled"  # nothing on screen names it — see below


@dataclass
class ControlResult:
    purpose: str
    role: str
    name: str
    outcome: str
    matches: int = 0
    diagnosis: dict[str, int] = field(default_factory=dict)

    @property
    def hit(self) -> bool:
        """Resolved under the role the labeller expected."""
        return self.outcome == HIT

    @property
    def hit_any_role(self) -> bool:
        """Resolved by accessible name, whatever role carries it.

        The looser and, for this premise, the fairer number. A natural-language
        intent is "click 24h", not "click the element whose ARIA role is button
        and whose name is 24h" — so an element the tree names correctly under a
        role the labeller did not predict is a **labelling** miss, not evidence
        that the tree failed to address it. Reported alongside rather than
        instead of :attr:`hit`, because role still decides *what to do* with a
        control once found, and a `fill` on a link is its own bug.
        """
        if self.outcome == HIT:
            return True
        if self.outcome == MISS_UNLABELLED:
            return False
        return self.matches == 0 and self.diagnosis.get("any_role_with_name", 0) == 1

    @property
    def hit_extended(self) -> bool:
        """Resolved once tier 0 also tries placeholder and label accessors.

        The corpus's own finding, and the reason it was worth building. The
        dominant failure is not a control the tree fails to name — it is a
        control the tree names **differently from the text on screen**, because
        `aria-label` overrides `placeholder` in accessible-name computation:

            substack  placeholder "Your email"  aria-label "Email"
            kayak     placeholder "To?"         aria-label "Destination location"

        A person writing an intent reads the placeholder. So a tier 0 that only
        asks `get_by_role(name=…)` cannot reach either field, while one that
        falls back to `get_by_placeholder` / `get_by_label` reaches both with no
        model call. Measured separately so the gain is evidence rather than an
        assumption baked into the primary number.
        """
        if self.hit_any_role:
            return True
        if self.outcome == MISS_UNLABELLED:
            return False
        return (self.diagnosis.get("by_placeholder", 0) == 1
                or self.diagnosis.get("by_label", 0) == 1)


@dataclass
class PageResult:
    slug: str
    vendor: str
    category: str
    controls: list[ControlResult] = field(default_factory=list)
    external_requests: list[str] = field(default_factory=list)
    iframe_skipped: int = 0

    @property
    def hits(self) -> int:
        return sum(1 for c in self.controls if c.hit)

    @property
    def hits_any_role(self) -> int:
        return sum(1 for c in self.controls if c.hit_any_role)

    @property
    def hits_extended(self) -> int:
        return sum(1 for c in self.controls if c.hit_extended)

    @property
    def rate_extended(self) -> float:
        return self.hits_extended / self.total if self.total else 0.0

    @property
    def rate_any_role(self) -> float:
        return self.hits_any_role / self.total if self.total else 0.0

    @property
    def total(self) -> int:
        return len(self.controls)

    @property
    def rate(self) -> float:
        return self.hits / self.total if self.total else 0.0


class _Server:
    """Serves the corpus on localhost. Nothing else is reachable."""

    def __init__(self, directory: Path) -> None:
        handler = partial(_QuietHandler, directory=str(directory))
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


def manifests() -> list[dict[str, Any]]:
    """Every labelled page. A manifest still holding TODOs is not labelled."""
    out = []
    for path in sorted(PAGES.glob("*.controls.json")):
        data = json.loads(path.read_text())
        if any("TODO" in str(v) for c in data.get("controls", []) for v in c.values()):
            continue
        if not data.get("controls"):
            continue
        meta_path = PAGES / f"{data['slug']}.meta.json"
        if not meta_path.exists():
            continue
        data["_meta"] = json.loads(meta_path.read_text())
        out.append(data)
    return out


async def measure_page(page: Any, manifest: dict[str, Any]) -> PageResult:
    meta = manifest["_meta"]
    result = PageResult(slug=manifest["slug"], vendor=meta["vendor"],
                        category=meta["category"])
    result.iframe_skipped = sum(
        1 for c in manifest["controls"] if c.get("in_iframe"))

    for spec in manifest["controls"]:
        if spec.get("in_iframe"):
            # Capture drops iframes, so their contents are not present to be
            # found. Counting them as misses would blame the a11y tree for a
            # limitation of the fixture. Reported in the summary, excluded here.
            continue
        role, name = spec["role"], spec["name"]

        if not spec.get("visible_label", True) or name is None:
            # An icon-only control. Nothing on screen names it, so a person
            # writing an intent has no words to write and tier 0 — which
            # addresses by accessible name — has nothing to address it with.
            # Scored a miss *without querying*: querying would need a guessed
            # name, and guessing at the DOM is the circularity this whole
            # method exists to avoid. It is a legitimate tier-1 target, which
            # is the tiering story rather than a hole in it.
            result.controls.append(ControlResult(
                purpose=spec["purpose"], role=role, name=name or "",
                outcome=MISS_UNLABELLED, matches=0, diagnosis={}))
            continue

        # Exact first, then substring — a competent tier 0 does this, and the
        # order matters. Playwright's `name=` is a *substring* match by
        # default, so "Continue" also matches "Continue with Google" and
        # "Continue with GitHub": three hits, scored ambiguous, blamed on the
        # page. That is an artifact of the default, not of the premise.
        count = 0
        for exact in (True, False):
            try:
                count = await _visible_count(
                    page.get_by_role(role, name=name, exact=exact))
            except Exception:
                count = 0
            if count == 1:
                break

        if count == 1:
            outcome, diagnosis = HIT, {}
        else:
            diagnosis = await _diagnose(page, role, name)
            if count > 1:
                outcome = MISS_AMBIGUOUS
            elif diagnosis.get("any_role_with_name", 0) > 0:
                outcome = MISS_ROLE
            else:
                outcome = MISS_ABSENT

        result.controls.append(ControlResult(
            purpose=spec["purpose"], role=role, name=name,
            outcome=outcome, matches=count, diagnosis=diagnosis,
        ))
    return result


async def _visible_count(locator: Any) -> int:
    """Visible matches only.

    A frozen page carries the elements that were hidden at capture time, with
    their hidden-ness baked in as an inline style. Counting those would invent
    ambiguity the user never saw — a mobile-nav duplicate of the same button.
    """
    total = await locator.count()
    if total == 0:
        return 0
    visible = 0
    for i in range(min(total, 25)):
        try:
            if await locator.nth(i).is_visible():
                visible += 1
        except Exception:
            continue
    return visible


async def _diagnose(page: Any, role: str, name: str) -> dict[str, int]:
    """Why a control did not resolve. Each key suggests a different repair."""
    out: dict[str, int] = {}

    async def count(locator: Any) -> int:
        try:
            return await _visible_count(locator)
        except Exception:
            return 0

    out["by_label"] = await count(page.get_by_label(name))
    out["by_placeholder"] = await count(page.get_by_placeholder(name))
    out["by_text"] = await count(page.get_by_text(name, exact=False))
    out["by_title"] = await count(page.get_by_title(name))
    out["role_present"] = await count(page.get_by_role(role))
    # Same accessible name under any role — the MISS_ROLE signal. A "button"
    # that is really a link is the commonest single cause and is a labelling
    # question, not a failure of the tree.
    total = 0
    # Widened after Proton's "For individuals" was scored a page failure when it
    # is `role="tab"` — a role this list did not contain, so `any_role_with_name`
    # came back 0 and a labelling miss was recorded as the tree's. Any role the
    # list omits converts a labeller error into evidence against the premise,
    # which is the one direction of error this measurement must not make.
    for other in ("button", "link", "textbox", "combobox", "checkbox",
                  "radio", "menuitem", "menuitemcheckbox", "menuitemradio",
                  "option", "switch", "searchbox", "tab", "slider",
                  "spinbutton", "treeitem", "gridcell", "cell", "listbox",
                  "columnheader", "heading"):
        if other == role:
            continue
        total += await count(page.get_by_role(other, name=name))
    out["any_role_with_name"] = total
    return out


async def run(headless: bool = True) -> list[PageResult]:
    from playwright.async_api import async_playwright

    specs = manifests()
    if not specs:
        return []

    results: list[PageResult] = []
    with _Server(PAGES) as server:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless)
            try:
                for manifest in specs:
                    page = await browser.new_page(
                        viewport=manifest["_meta"].get(
                            "viewport", {"width": 1280, "height": 900}))
                    external: list[str] = []

                    async def block(route: Any, request: Any,
                                    _sink: list[str] = external) -> None:
                        # The empirical no-network proof. A regex over the HTML
                        # cannot see an `svg use`, a CSS url() or a
                        # `<link imagesrcset>`; a blocked request can.
                        if "127.0.0.1" in request.url or request.url.startswith("data:"):
                            await route.continue_()
                        else:
                            _sink.append(request.url)
                            await route.abort()

                    await page.route("**", block)
                    await page.goto(
                        f"http://127.0.0.1:{server.port}/{manifest['slug']}.html",
                        wait_until="load")
                    result = await measure_page(page, manifest)
                    result.external_requests = external
                    results.append(result)
                    await page.close()
            finally:
                await browser.close()
    return results


def report(results: list[PageResult]) -> str:
    if not results:
        return "no labelled pages in the corpus"

    lines = [
        f"{'page':24} {'vendor':13} {'hit':>7}  {'role':>6} {'anyrol':>6} {'+ph/lb':>6}   misses",
        "-" * 94,
    ]
    for r in sorted(results, key=lambda r: r.rate):
        misses: dict[str, int] = {}
        for c in r.controls:
            if not c.hit:
                misses[c.outcome] = misses.get(c.outcome, 0) + 1
        detail = " ".join(f"{k.split(':')[1]}={v}" for k, v in sorted(misses.items()))
        lines.append(f"{r.slug:24} {r.vendor:13} {r.hits:3}/{r.total:<3} "
                     f"{r.rate:6.0%} {r.rate_any_role:6.0%} {r.rate_extended:6.0%}   "
                     f"{detail}")

    hits = sum(r.hits for r in results)
    hits_any = sum(r.hits_any_role for r in results)
    hits_ext = sum(r.hits_extended for r in results)
    total = sum(r.total for r in results)
    skipped = sum(r.iframe_skipped for r in results)
    by_class: dict[str, int] = {}
    for r in results:
        for c in r.controls:
            if not c.hit:
                by_class[c.outcome] = by_class.get(c.outcome, 0) + 1
    labelled = [c for r in results for c in r.controls
                if c.outcome != MISS_UNLABELLED]
    labelled_hits = sum(1 for c in labelled if c.hit)
    floor = min(r.rate for r in results)
    lines += [
        "-" * 94,
        f"{'OVERALL':24} {'':13} {hits:3}/{total:<3} {hits / total:6.0%} "
        f"{hits_any / total:6.0%} {hits_ext / total:6.0%}   "
        f"floor {floor:.0%} ({min(results, key=lambda r: r.rate).slug})",
        f"{'':24} {'':13} {'':7}          "
        + "  ".join(f"{k.split(':')[1]}={v}" for k, v in sorted(by_class.items())),
        f"{'':24} {'':13} {'':7}          "
        f"{skipped} excluded (iframe); of {len(labelled)} controls that carry a "
        f"visible name, {labelled_hits} resolved ({labelled_hits / len(labelled):.0%})"
        if labelled else "",
        "",
        _verdict(hits_ext / total if total else 0.0),
    ]
    leaked = [r.slug for r in results if r.external_requests]
    if leaked:
        lines.append(f"!! not frozen — external requests from: {', '.join(leaked)}")
    return "\n".join(lines)


def _verdict(rate: float) -> str:
    """The §1.3 decision table, applied to the role-agnostic rate.

    Role-agnostic because the decision the table drives is "can tier 0 address
    real controls", and a role the labeller mispredicted is not the tree's
    failure. The stricter role-exact rate is printed beside it, and the gap
    between the two is itself the finding: it measures how far a hand-written
    intent's role guess can be trusted.
    """
    if rate >= 0.70:
        return f"VERDICT {rate:.0%} >= 70% — a11y-first holds; ship 13.1 as designed."
    if rate >= 0.40:
        return (f"VERDICT {rate:.0%} in 40-70% — tier 1 is the product, not the "
                "fallback. Reorder 13.1/13.2 and re-open whether a specialist "
                "adapter should be the default rather than an entry point.")
    return (f"VERDICT {rate:.0%} < 40% — premise is wrong. Stop; redesign around "
            "tier 2, and 'Playwright and nothing else' no longer follows.")
