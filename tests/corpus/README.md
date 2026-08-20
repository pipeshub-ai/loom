# The tier-0 corpus

Phase 13 claims that `get_by_role(role, name=…)` — no model, no inference —
addresses the controls on real transactional pages. That claim is about **the
web**, not about LOOM, so it is measured here rather than argued in the design
doc. §1.3 of `phases/phase-13-browser-automation.md` declares in advance what
each result means and what it changes.

```bash
python scripts/capture_batch.py                    # attempt every target
python scripts/capture_corpus.py <url> --slug … --vendor … --category …
pytest tests/test_browser_corpus.py -s             # measure, and gate
```

## What is committed, and why

| File | Holds |
|---|---|
| `targets.json` | every page **attempted**, chosen before the resolver existed |
| `outcomes.json` | what happened to each, **including refusals** |
| `pages/<slug>.html` | the frozen page — no scripts, no network, tens of KB |
| `pages/<slug>.png` | the screenshot the labeller worked from |
| `pages/<slug>.meta.json` | source URL, capture date, control counts |
| `pages/<slug>.controls.json` | the hand-written labels |

`outcomes.json` is the honest half. A benchmark listing only the pages it
managed to capture is selecting for the pages that let it in — and the sites
that refuse a headless browser are exactly the commercially-defended
transactional ones this phase most wants to measure. **5 of 22 targets are
recorded as refused or unusable, and that ratio is itself a finding** about
whether a local Playwright provider is viable unaided.

## Choosing pages

Before the resolver was written, by a rule rather than by taste: booking,
checkout, signup, support and search pages across distinct vendors, capped at
two per vendor so no single framework can carry the score. Fixtures picked after
the fact select for what already works.

A capture is then **rejected mechanically**, never by preference, when it is:

- a **challenge page** — `github.com/signup` returned "Access is temporarily
  restricted… Automated (bot) activity on your network";
- a **404 or parked page** — an invented Calendly URL captured cleanly as a
  healthy 250 KiB document titled `404 | Calendly`;
- a **links directory, not a form** — fewer than three interactive controls;
- **under-rendered** — fewer than half of visible controls carry an accessible
  name after three settle attempts. `cal.com` still showed the raw i18n key
  `$Switch to monthly view` after 10 s, and a snapshot cannot tell "this app had
  not finished painting" from "this app is inaccessible". When the two are
  indistinguishable the page must not be scored.

## Labelling

**The single rule: the label is what a person reads off the screenshot.**

Deriving expectations from the DOM would measure the accessibility tree against
itself and report something near 100%. Every manifest names `labelled_from`, and
that file is a PNG.

Three consequences fall out of the rule rather than being decided:

- A control with **no visible name** — an icon-only month arrow — is recorded
  `"name": null, "visible_label": false` and scored `miss:unlabelled` *without
  being queried*. Querying it would need a guessed name, and guessing at the DOM
  is the circularity this exists to avoid. It is a legitimate tier-1 target.
- A **value is not a name.** Kayak's origin field shows `Bengaluru (BLR)`;
  that is its content, not its label, so it is recorded unlabelled.
- Scope is **the controls needed to complete the flow**, plus the page's main
  navigation. Not every control, which would let the corpus be padded with easy
  ones.

## Scoring

Three columns, because they answer three different questions and collapsing
them hides the finding.

| Column | Resolves by | Answers |
|---|---|---|
| `role` | `get_by_role(role, name)` | did the labeller's role *and* name both hold? |
| `anyrol` | the same name under any role | does the tree name it at all? |
| `+ph/lb` | plus `get_by_placeholder` / `get_by_label` | can a competent tier 0 reach it? |

`anyrol` exists because a natural-language intent is "click 24h", not "click the
element whose ARIA role is `button`". Proton's plan tabs scored 33% on `role`
and 100% on `anyrol`: every miss was the labeller predicting `link` where the
page uses `role="tab"`. That is a labelling error, and counting it against the
tree would be the one direction of error this measurement must not make.

**Ambiguity is a miss, never a hit.** Playwright's locators raise on multiple
matches rather than picking the first, which is the behaviour tier 0 wants.
Scoring two matches as success would credit the exact failure — clicking the
wrong button — that strictness exists to prevent.

## Wikipedia is the control

Included deliberately, and it earned its place immediately. The first run scored
it **47%**, which is not a plausible statement about one of the most
accessibility-conscious sites on the web — so the instrument was wrong, and it
was.

The capture was treating `opacity: 0` as hidden. But
`<input type="radio" class="visually-hidden">` beside a styled `<label>` is
*the* standard pattern for a custom radio, checkbox or switch: the input carries
the role and the name, the label carries the pixels. Playwright does not
consider those hidden — it excludes only `display:none`, `visibility:hidden`,
and zero-size boxes. The capture was deleting precisely the controls the premise
is about, across every page at once.

Fixing it moved Wikipedia 47% → 87%, GitLab 90% → 100%, and the corpus from 12
native controls to 47. **A control fixture whose expected score is known is the
cheapest defect detector in the whole method**, and `test_browser_corpus.py`
keeps it as a standing assertion rather than a one-off.

## The finding

The dominant failure is **not** a control the tree fails to name. It is a
control the tree names *differently from the text on screen*, because
`aria-label` overrides `placeholder` in accessible-name computation:

| Page | Placeholder — what a person reads | `aria-label` — what the tree says |
|---|---|---|
| substack | `Your email` | `Email` |
| kayak | `To?` | `Destination location` |
| heroku | `First name` | `First name` (and a real `<label>`) |

Heroku agrees on all three and scores 100%. The other two are unreachable by
what a person would write — until tier 0 also tries `get_by_placeholder` and
`get_by_label`, which costs nothing and no model call. That is the `+ph/lb`
column, and it is why the corpus was worth building before the resolver rather
than after.

## Limits, stated rather than discovered

- **N is small.** 8 pages, 82 controls. Enough to kill a bad premise, not enough
  to defend a precise number. The per-page spread (29%–100%) is wider than the
  mean is informative.
- **Frozen pages cannot test dynamic behaviour** — a date picker that fetches
  slots, a field that appears once another is filled. §7.4's nightly live checks
  are the only cover for those, and they are deliberately not a merge gate.
- **iframes are dropped at capture**, so controls inside them (reCAPTCHA,
  payment fields) are excluded and counted separately, not scored.
- **Survivorship bias remains** even after `outcomes.json`: sites that permit
  headless capture may be systematically better-behaved than those that do not.
