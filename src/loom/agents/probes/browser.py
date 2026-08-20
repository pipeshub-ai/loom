"""Look at a page the way a browser sees it.

The probe written for a specific failure. Asked to "collect every visible form
control", the agent generated a ``querySelectorAll('input, textarea, select')``
— correct for the spec, and it returned nothing, because the page renders its
controls as ``div``s. The workflow ran, completed, and reported zero fields; the
screenshot showed four. Nothing in the code was wrong, and nothing short of
rendering the page could have said so.

So the census counts **both** populations and reports them side by side. "0
native, 4 role-based" is the sentence that was missing: it is not a description
of the page, it is the reason the obvious selector will not work on it.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from loom.agents.probes.base import Observation, ProbeError
from loom.blobs.attachment import Attachment

__all__ = ["BrowserProbe"]

#: Read the page, and nothing else. There is no click, type, or submit here, so
#: no prompt can talk this class into performing one.
_CENSUS = """
() => {
    const visible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 &&
               style.visibility !== 'hidden' && style.display !== 'none';
    };
    const label = (el) =>
        (el.labels && el.labels[0] && el.labels[0].innerText.trim()) ||
        el.getAttribute('aria-label') ||
        el.getAttribute('placeholder') ||
        (el.innerText || '').trim().slice(0, 60) || null;

    const native = [...document.querySelectorAll('input, textarea, select')]
        .filter(visible)
        .map((el) => ({
            tag: el.tagName.toLowerCase(),
            name_or_id: el.name || el.id || null,
            type: el.getAttribute('type') || el.tagName.toLowerCase(),
            label: label(el),
            required: el.required === true ||
                      el.getAttribute('aria-required') === 'true',
        }));

    const widgets = [...document.querySelectorAll(
            '[role="button"], [role="combobox"], [role="listbox"], ' +
            '[role="textbox"], [role="checkbox"], [role="radio"], ' +
            '[role="switch"], [contenteditable="true"]')]
        .filter(visible)
        .map((el) => ({
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute('role') || 'contenteditable',
            label: label(el),
        }));

    const buttons = [...document.querySelectorAll('button, [type="submit"]')]
        .filter(visible)
        .map((el) => (el.innerText || el.value || '').trim())
        .filter(Boolean);

    // Role and accessible name for every visible control — the same two
    // fields `Target` addresses by, and the same shape `PageSnapshot.tree`
    // carries. That is deliberate: it makes an authoring observation directly
    // usable as a smoke fixture, so the page the agent *looked at* is the page
    // its generated code is tested against. No parallel fixture set to drift.
    const roleOf = (el) => {
        const explicit = el.getAttribute('role');
        if (explicit) return explicit;
        const tag = el.tagName.toLowerCase();
        if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
        if (tag === 'button') return 'button';
        if (tag === 'select') return 'combobox';
        if (tag === 'textarea') return 'textbox';
        if (tag === 'input') {
            const t = (el.getAttribute('type') || 'text').toLowerCase();
            return {checkbox:'checkbox', radio:'radio', search:'searchbox',
                    submit:'button', button:'button', reset:'button',
                    range:'slider', number:'spinbutton'}[t] || 'textbox';
        }
        return 'generic';
    };
    const tree = [...document.querySelectorAll(
            'a[href],button,input,select,textarea,[role]')]
        .filter(visible)
        .slice(0, 300)
        .map((el) => ({
            role: roleOf(el),
            name: label(el) || '',
            value: (el.value || '').slice(0, 80),
            disabled: el.disabled === true ||
                      el.getAttribute('aria-disabled') === 'true',
        }));

    return {
        title: document.title,
        url: location.href,
        tree: tree,
        native_controls: native,
        role_widgets: widgets,
        buttons: buttons,
        headings: [...document.querySelectorAll('h1, h2')]
            .filter(visible).map((el) => el.innerText.trim()).slice(0, 10),
        text_excerpt: (document.body.innerText || '').trim().slice(0, 800),
    };
}
"""


class BrowserProbe:
    """Render a page and report what a selector would actually find.

    Needs the ``browser`` extra (playwright, with its Chromium downloaded). Not
    installed means this probe is simply absent, and the agent is back to the
    behaviour it had before — which is the right degradation: a check that
    cannot run has found nothing.
    """

    id = "browser"

    def __init__(self, *, timeout: float = 30.0, settle_ms: int = 1500) -> None:
        self._timeout = timeout
        self._settle_ms = settle_ms

    def supports(self, target: str) -> bool:
        return target.startswith(("http://", "https://"))

    async def observe(self, target: str, *, hint: str = "") -> Observation:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ProbeError(
                "the browser probe needs playwright: pip install 'loomsdk[browser]' "
                "&& playwright install chromium"
            ) from exc

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(
                        target, wait_until="domcontentloaded",
                        timeout=int(self._timeout * 1000),
                    )
                    with contextlib.suppress(Exception):
                        # Never fires on some sites; the settle below covers it.
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    # Client-rendered controls paint after the network settles,
                    # and the whole point of this probe is to see them.
                    await page.wait_for_timeout(self._settle_ms)

                    census: dict[str, Any] = await page.evaluate(_CENSUS)
                    shot = await page.screenshot(full_page=True)
                finally:
                    await browser.close()
        except ProbeError:
            raise
        except Exception as exc:
            raise ProbeError(f"could not render {target}: {exc}") from exc

        return Observation(
            target=target,
            summary=_summarise(census),
            detail=json.dumps(census, indent=2),
            evidence=(_screenshot(shot),),
            probe=self.id,
        )


def _summarise(census: dict[str, Any]) -> str:
    """The one line that would have prevented the original failure."""
    native = len(census.get("native_controls", ()))
    widgets = len(census.get("role_widgets", ()))
    buttons = len(census.get("buttons", ()))

    parts = [
        f"{census.get('title') or 'untitled'!r}",
        f"{native} native form control(s)",
        f"{widgets} role-based widget(s)",
        f"{buttons} button(s)",
    ]
    tree = census.get("tree") or ()
    named = sum(1 for node in tree if node.get("name"))
    if tree:
        parts.append(f"{named}/{len(tree)} addressable by name")
    line = ", ".join(parts)
    if tree and not named:
        # The sentence that decides whether tier 0 can drive this page at all.
        line += (
            ". Nothing here carries an accessible name, so a Target(role=…, "
            "name=…) will not resolve — this page needs a different approach, "
            "not a better guess."
        )
    if native == 0 and widgets:
        line += (
            ". A selector over input/textarea/select will find nothing here — "
            "the controls are custom widgets, addressable by role or by text."
        )
    return line


def _screenshot(data: bytes) -> Attachment:
    return Attachment.from_bytes("page.png", data, mime="image/png")
