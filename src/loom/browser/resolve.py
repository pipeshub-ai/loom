"""Tier 0: finding a control without a model.

The chain below is not a guess. It is what ``tests/corpus`` measured across 114
hand-labelled controls on 10 real pages, and each step earned its place:

| Step | Accessor | Why it is there |
|---|---|---|
| 1 | ``get_by_role(role, name, exact=True)`` | the precise case |
| 2 | ``get_by_role(role, name)`` | Playwright's substring default; a page's
      accessible name often carries more than the visible label |
| 3 | the same name under **any** role | a caller who said ``link`` where the
      page uses ``tab`` — their mistake, not the page's |
| 4 | ``get_by_placeholder(name)`` | **the finding** — see below |
| 5 | ``get_by_label(name)`` | a label associated but not surfaced as the name |
| 6 | ``target.css`` | the escape hatch, never the default |

**Step 4 is why measuring before building was worth it.** The dominant failure
on real pages is not a control the tree fails to name — it is a control the tree
names *differently from the text on screen*, because ``aria-label`` overrides
``placeholder`` in accessible-name computation:

    substack   placeholder "Your email"   aria-label "Email"
    kayak      placeholder "To?"          aria-label "Destination location"
    heroku     placeholder "First name"   aria-label "First name" + a <label>

Heroku agrees on all three and resolves every field. The other two are
unreachable by anything a person would write, until steps 4 and 5 — which cost
nothing and no model call, and were worth 26 points on those pages. Without the
corpus this would have shipped as a silent hole.

**Visibility, and one specific trap.** Only visible matches count, and
"visible" here is Playwright's definition: a non-empty bounding box and no
``visibility: hidden``. **Opacity is not part of it**, and treating it as part
of it is a real defect with a wide blast radius:
``<input type=radio class=visually-hidden>`` beside a styled ``<label>`` is the
standard pattern for every custom radio, checkbox and switch on the web, and
those inputs are routinely ``opacity: 0``. The corpus capture made exactly that
mistake and suppressed those controls on every page at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loom.browser.base import Target

__all__ = ["ANY_ROLES", "Resolution", "resolve"]

#: Roles tried in step 3, when role-and-name finds nothing but the name exists.
#: A role omitted here turns a caller's role mistake into "no such control",
#: which is the one direction of error worth avoiding: the page is fine and the
#: message says it is not.
ANY_ROLES: tuple[str, ...] = (
    "button", "link", "textbox", "searchbox", "combobox", "listbox",
    "checkbox", "radio", "switch", "tab", "menuitem", "menuitemcheckbox",
    "menuitemradio", "option", "slider", "spinbutton", "treeitem",
    "gridcell", "cell", "columnheader", "heading",
)


@dataclass(frozen=True)
class Resolution:
    """What tier 0 found, and how."""

    locator: Any | None
    matches: int
    accessor: str = ""
    """Which step answered — recorded so the tier-0 chain's own value is
    measurable in production, not just in the corpus."""

    @property
    def unique(self) -> bool:
        return self.matches == 1


async def visible_count(locator: Any, *, cap: int = 30) -> int:
    """Visible matches, counted one by one.

    ``cap`` bounds the walk: a target matching 400 nodes is already ambiguous,
    and the caller only ever needs to know "one, none, or several".
    """
    try:
        total = await locator.count()
    except Exception:
        return 0
    if total == 0:
        return 0
    seen = 0
    for index in range(min(total, cap)):
        try:
            if await locator.nth(index).is_visible():
                seen += 1
        except Exception:
            continue
        if seen > 1:
            break  # "several" is as much as any caller needs
    return seen


async def resolve(page: Any, target: Target) -> Resolution:
    """Find *target* on *page*. No model, no network beyond the page itself.

    Returns the first accessor that finds **exactly one** visible control. When
    none does, returns the last non-empty result so the caller can report "three
    matched" rather than "nothing matched", which are different problems.
    """
    attempts: list[tuple[str, Any]] = []

    if target.name:
        attempts.append((
            "role+name exact",
            page.get_by_role(target.role, name=target.name, exact=True),
        ))
        if not target.exact:
            attempts.append((
                "role+name",
                page.get_by_role(target.role, name=target.name),
            ))
        for other in ANY_ROLES:
            if other != target.role:
                attempts.append((
                    f"role={other}+name",
                    page.get_by_role(other, name=target.name),
                ))
        attempts.append(("placeholder", page.get_by_placeholder(target.name)))
        attempts.append(("label", page.get_by_label(target.name)))
    else:
        # No name: the role alone is the target. Only meaningful with an
        # ordinal or on a page with exactly one such control.
        attempts.append(("role", page.get_by_role(target.role)))

    if target.css:
        attempts.append(("css", page.locator(target.css)))

    fallback = Resolution(locator=None, matches=0)
    for accessor, locator in attempts:
        found = await visible_count(locator)
        if found == 1:
            return Resolution(locator=locator, matches=1, accessor=accessor)
        if found > 1:
            if target.ordinal:
                # The caller said they know it repeats and which one they want.
                # This is the only route past AmbiguousTarget without a
                # selector, and it is deliberately explicit: an automation that
                # silently takes the first match is the failure strictness
                # exists to prevent.
                return Resolution(
                    locator=_nth_visible(locator, target.ordinal),
                    matches=1,
                    accessor=f"{accessor}[{target.ordinal}]",
                )
            if fallback.matches == 0:
                fallback = Resolution(locator=locator, matches=found,
                                      accessor=accessor)
    return fallback


def _nth_visible(locator: Any, ordinal: int) -> Any:
    """The *ordinal*-th match, 1-based.

    1-based because ``Target.ordinal`` uses 0 to mean "there must be exactly
    one", so there is no 0th and an off-by-one here would silently select a
    neighbour — which is the class of bug this module exists to make impossible.
    """
    return locator.nth(max(0, ordinal - 1))
