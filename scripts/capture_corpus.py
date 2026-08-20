#!/usr/bin/env python3
"""Freeze a real page into the tier-0 corpus.

Phase 13's E10 asks whether the accessibility tree addresses the controls on
real transactional forms. Answering that needs pages that do not move: a
measurement against live sites reports somebody else's deploy as our
regression, which is `verify_probe`'s own rule — *"point it at a fixture you
control, not at a third party's site, so a red test means your probe broke
rather than someone else's server did."*

What a snapshot keeps, and what it drops, follows from what tier 0 reads:

- **Role** comes from the tag and ARIA attributes. Kept.
- **Accessible name** comes from ``<label>``, ``aria-label``, ``aria-labelledby``,
  ``alt``, ``title``, ``placeholder`` and text content. All kept — which is why
  images are neutralised to a transparent pixel *with their ``alt`` intact*
  rather than removed.
- **Visibility** normally comes from CSS, and that is the one thing a stripped
  page would get wrong: drop the stylesheets and a mobile nav duplicate that was
  hidden at capture time becomes a second match, turning a clean resolution into
  a false ambiguity. So visibility is **computed at capture time and baked into
  the element** as an inline style. The snapshot is then correct about what was
  on screen without carrying a single stylesheet.

Everything else goes: scripts (the DOM is captured *after* they have run, so
keeping them would let the page mutate itself on replay), iframes, stylesheets,
fonts, media. A snapshot is tens of kilobytes and reaches the network never.

Usage::

    python scripts/capture_corpus.py https://example.com/book \\
        --slug acme-booking --vendor acme --category booking
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "corpus" / "pages"

#: A 1x1 transparent GIF. Images keep their `alt` because alt text *is* the
#: accessible name of an image control — an icon button labelled only by its
#: alt would otherwise become unaddressable, which would be our bug, not the
#: page's.
BLANK_PIXEL = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

#: Runs in the page, after it has settled. Bakes computed visibility into the
#: DOM and removes everything that could reach the network or mutate on replay.
#: Deliberately *not* a Python-side HTML rewrite: the browser has already parsed
#: this document, and a regex over real-world markup is how you silently drop a
#: control.
_FREEZE = """
(blankPixel) => {
    // Playwright's own definition of visible, and deliberately nothing more:
    // a non-empty bounding box and no `visibility: hidden`. **Opacity is not
    // part of it**, and getting that wrong is not a rounding error.
    //
    // `<input type=radio class=visually-hidden>` beside a styled `<label>` is
    // the standard accessible pattern for a custom control: the input carries
    // the role and the name, the label carries the pixels. Those inputs are
    // routinely `opacity: 0`. An earlier version of this function treated
    // opacity 0 as hidden and so deleted them — and since that pattern is what
    // every design system uses for radios, checkboxes and switches, the corpus
    // reported those controls as unaddressable across every page at once. The
    // control fixture caught it: Wikipedia scored 47%, which indicts the
    // instrument rather than Wikipedia.
    const hidden = (el) => {
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return true;
        const r = el.getBoundingClientRect();
        return r.width === 0 && r.height === 0;
    };

    // 1. Bake visibility. Done before anything is removed, so the computed
    //    values are the ones that were true on screen.
    let marked = 0;
    for (const el of document.querySelectorAll('body *')) {
        if (hidden(el)) {
            el.setAttribute('data-loom-hidden', '1');
            el.style.setProperty('display', 'none', 'important');
            marked++;
        }
    }

    // 2. Drop what executes, what fetches, and what frames.
    let dropped = 0;
    for (const sel of ['script', 'noscript', 'iframe', 'object', 'embed',
                       'link', 'style', 'source', 'track', 'video', 'audio']) {
        for (const el of document.querySelectorAll(sel)) { el.remove(); dropped++; }
    }

    // 3. Neutralise images, keeping alt — see the module docstring.
    for (const img of document.querySelectorAll('img')) {
        img.setAttribute('src', blankPixel);
        img.removeAttribute('srcset');
        img.removeAttribute('loading');
    }
    for (const el of document.querySelectorAll('[style*="url("]')) {
        el.style.backgroundImage = 'none';
    }

    // 4. Anything still pointing outward becomes inert. An href is kept as a
    //    fragment so link *roles and names* survive, which is what tier 0 reads.
    for (const a of document.querySelectorAll('a[href]')) {
        a.setAttribute('data-loom-href', a.getAttribute('href'));
        a.setAttribute('href', '#');
    }
    for (const f of document.querySelectorAll('form[action]')) {
        f.setAttribute('data-loom-action', f.getAttribute('action'));
        f.removeAttribute('action');
    }

    const counts = {
        marked_hidden: marked,
        dropped_nodes: dropped,
        native_controls: document.querySelectorAll(
            'input:not([data-loom-hidden]), textarea:not([data-loom-hidden]), ' +
            'select:not([data-loom-hidden])').length,
        role_widgets: document.querySelectorAll(
            '[role]:not([data-loom-hidden])').length,
        buttons: document.querySelectorAll(
            'button:not([data-loom-hidden])').length,
    };
    return { html: document.documentElement.outerHTML, counts };
}
"""

#: What fraction of visible buttons and links carry a non-empty accessible
#: name. Low means the app has not finished painting its labels, not that the
#: page is inaccessible — the two are indistinguishable in a snapshot, which is
#: why this is checked before freezing rather than diagnosed afterwards.
_NAMED_FRACTION = """
() => {
    const visible = (el) => {
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 || r.height > 0;
    };
    const els = [...document.querySelectorAll('button, a[href], [role="button"]')]
        .filter(visible);
    if (els.length < 5) return 1;   // too few to judge; do not stall on it
    const named = els.filter((el) =>
        ((el.getAttribute('aria-label') || el.innerText || el.title || '').trim().length > 0)
        && !(el.innerText || '').trim().startsWith('$'));
    return named.length / els.length;
}
"""

_RESET = (
    "<style>/* capture reset: visibility is baked inline, see capture_corpus.py */"
    "*{animation:none!important;transition:none!important}</style>"
)


async def capture(
    url: str,
    *,
    slug: str,
    vendor: str,
    category: str,
    timeout: float = 45.0,
    settle_ms: int = 2500,
    viewport: tuple[int, int] = (1280, 900),
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(
                viewport={"width": viewport[0], "height": viewport[1]},
                # A real UA: several sites serve a degraded no-JS shell to
                # headless defaults, and capturing that would measure tier 0
                # against a page no user ever sees.
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
            )
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=int(timeout * 1000))
            # `networkidle` never fires on some sites; the settle below covers
            # it. Lifted verbatim from BrowserProbe, which already learned this.
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=8000)
            await page.wait_for_timeout(settle_ms)

            # A page captured mid-hydration looks healthy — controls are
            # present and visible — while its *labels* are not yet painted, so
            # every accessible name comes back empty and the corpus records a
            # total tier-0 failure that is entirely our own. Caught on cal.com,
            # which at 2.5s still had `$Switch to monthly view` (a raw i18n key)
            # and time-slot buttons whose text children were display:none.
            named_fraction = 1.0
            for _ in range(3):
                named_fraction = await page.evaluate(_NAMED_FRACTION)
                if named_fraction >= 0.5:
                    break
                await page.wait_for_timeout(3000)

            title = await page.title()
            final_url = page.url
            shot = await page.screenshot(full_page=True)
            body_text = await page.evaluate(
                "() => (document.body.innerText || '').slice(0, 4000)")
            frozen = await page.evaluate(_FREEZE, BLANK_PIXEL)
        finally:
            await browser.close()

    html = frozen["html"]
    if "<head>" in html:
        html = html.replace("<head>", "<head>" + _RESET, 1)
    html = "<!doctype html>\n" + html

    meta = {
        "slug": slug,
        "source": url,
        "final_url": final_url,
        "title": title,
        "vendor": vendor,
        "category": category,
        "captured": date.today().isoformat(),
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "bytes": len(html.encode("utf-8")),
        "named_fraction": round(named_fraction, 3),
        "counts": frozen["counts"],
    }
    rejection = validate(meta, body_text)
    if rejection:
        # Nothing is written. A rejected capture that left files behind would
        # be indistinguishable from an accepted one on the next `ls`.
        meta["rejected"] = rejection
        return meta

    CORPUS.mkdir(parents=True, exist_ok=True)
    (CORPUS / f"{slug}.html").write_text(html, encoding="utf-8")
    (CORPUS / f"{slug}.png").write_bytes(shot)
    (CORPUS / f"{slug}.meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )

    leaks = _external_refs(html)
    if leaks:
        meta["external_refs"] = leaks[:10]
        print(f"  !! {len(leaks)} external ref(s) survived: {leaks[:3]}",
              file=sys.stderr)

    return meta


#: Anything that would make a "frozen" page reach the network. Asserted by the
#: corpus test as well, because a snapshot that phones home is not frozen and
#: the failure looks like flakiness rather than like a broken fixture.
_EXTERNAL = re.compile(r'(?<![-\w])(?:src|href|poster|data)\s*=\s*["\'](https?:|//)', re.I)


def _external_refs(html: str) -> list[str]:
    return [m.group(0) for m in _EXTERNAL.finditer(html)]


#: Phrases that mean a bot wall answered instead of the page. Captured on the
#: first real run: github.com/signup returned "Access is temporarily restricted
#: ... Automated (bot) activity on your network" — a 315-byte document with zero
#: controls. A corpus that silently accepts those reports a tier-0 rate for
#: interstitials, which is worse than having no corpus, because it looks like a
#: measurement.
_CHALLENGE = (
    "access is temporarily restricted", "unusual activity", "are you a robot",
    "verify you are human", "checking your browser", "enable javascript",
    "access denied", "just a moment", "ddos protection", "cf-browser-verification",
    "captcha", "temporarily unavailable", "request blocked", "bot detected",
)


def validate(meta: dict[str, Any], text: str) -> str:
    """`""` when the capture is usable, else why it is not.

    Two independent signals, because each misses cases the other catches: a
    challenge page can carry a form (a CAPTCHA is a form), and a real page can
    briefly be text-only. Both must pass.
    """
    counts = meta["counts"]
    interactive = (
        counts["native_controls"] + counts["role_widgets"] + counts["buttons"]
    )
    lowered = text.lower()
    for phrase in _CHALLENGE:
        if phrase in lowered:
            return f"challenge page (matched {phrase!r})"
    if interactive == 0:
        return "no interactive controls — shell, challenge, or failed render"
    if meta["bytes"] < 2_000:
        return f"document is {meta['bytes']} bytes — almost certainly not the page"

    # A 404 renders fine and carries controls (nav, search), so neither check
    # above sees it. Caught on the first batch: an invented Calendly URL was
    # captured as a healthy 250 KiB page titled "404 | Calendly".
    title = (meta.get("title") or "").lower()
    for phrase in ("404", "not found", "page missing", "opening soon",
                   "coming soon", "under construction", "error"):
        if phrase in title:
            return f"title says {meta['title']!r} — not the page that was asked for"

    # A corpus about resolving *form* controls should not contain link
    # directories. gov.uk/contact and mozilla.org/contact are both real, both
    # captured cleanly, and neither is a transactional form.
    # An under-rendered SPA. `$Switch to monthly view` is a raw i18n key, and a
    # visible button with no accessible name at all is a label that never
    # painted. Both mean the snapshot shows a page mid-hydration, and a corpus
    # cannot tell that apart from a genuinely inaccessible page — so it must not
    # score it. Recorded in outcomes.json rather than dropped silently, because
    # "we could not render it" and "it resolved badly" are different findings.
    named = meta.get("named_fraction")
    if named is not None and named < 0.5:
        return (f"under-rendered: only {named:.0%} of visible controls carry an "
                "accessible name after three settle attempts")

    if interactive < 3:
        return (f"only {interactive} interactive control(s) — a links page or a "
                "shell, not a form worth measuring against")
    return ""


def _stub_manifest(meta: dict[str, Any]) -> str:
    """A manifest for a person to fill in from the screenshot.

    Deliberately empty of controls. Generating them from the page would make
    the measurement circular — see tests/corpus/README.md, "Labelling".
    """
    return json.dumps(
        {
            "slug": meta["slug"],
            "source": meta["source"],
            "labelled_by": "TODO",
            "labelled_from": f"{meta['slug']}.png",
            "controls": [
                {
                    "purpose": "TODO: what a person would call this control",
                    "role": "TODO: button | textbox | combobox | checkbox | radio | link",
                    "name": "TODO: the visible label, exactly as it reads",
                    "required_for_flow": True,
                }
            ],
        },
        indent=2,
    ) + "\n"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--vendor", required=True)
    ap.add_argument("--category", required=True,
                    choices=["booking", "checkout", "signup", "support", "search"])
    ap.add_argument("--settle-ms", type=int, default=2500)
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.slug):
        print("slug must be lowercase kebab-case", file=sys.stderr)
        return 2

    print(f"capturing {args.url}")
    meta = await capture(
        args.url, slug=args.slug, vendor=args.vendor,
        category=args.category, settle_ms=args.settle_ms,
    )
    if meta.get("rejected"):
        print(f"  REJECTED: {meta['rejected']}", file=sys.stderr)
        return 1
    c = meta["counts"]
    print(f"  {meta['bytes'] // 1024} KiB   "
          f"{c['native_controls']} native, {c['role_widgets']} role, "
          f"{c['buttons']} buttons   ({c['marked_hidden']} hidden baked)")

    manifest = CORPUS / f"{args.slug}.controls.json"
    if manifest.exists():
        print(f"  manifest exists, left alone: {manifest.name}")
    else:
        manifest.write_text(_stub_manifest(meta), encoding="utf-8")
        print(f"  wrote stub manifest: {manifest.name} — label it from "
              f"{args.slug}.png, not from the HTML")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
