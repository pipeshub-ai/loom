#!/usr/bin/env python3
"""Attempt every target in ``tests/corpus/targets.json`` and record the outcome.

The outcome file is the honest half of the corpus. A benchmark that lists only
the pages it managed to capture is selecting for the pages that let it in — and
the sites that block a headless browser are exactly the commercially-defended
transactional ones this phase most wants to measure. Recording refusals turns
that bias from an invisible flaw into a stated one, and the refusal rate is
itself a finding about whether a local Playwright provider is viable at all.

    python scripts/capture_batch.py            # attempt everything not yet captured
    python scripts/capture_batch.py --retry    # attempt the previously blocked too
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_corpus import CORPUS, capture

TARGETS = CORPUS.parent / "targets.json"
OUTCOMES = CORPUS.parent / "outcomes.json"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retry", action="store_true",
                    help="re-attempt targets that previously failed")
    ap.add_argument("--only", default="", help="substring filter on slug")
    args = ap.parse_args()

    targets = json.loads(TARGETS.read_text())
    outcomes = json.loads(OUTCOMES.read_text()) if OUTCOMES.exists() else {}

    for t in targets:
        slug = t["slug"]
        if args.only and args.only not in slug:
            continue
        if (CORPUS / f"{slug}.html").exists():
            print(f"= {slug:28} already captured")
            continue
        if slug in outcomes and not outcomes[slug]["ok"] and not args.retry:
            print(f"- {slug:28} previously {outcomes[slug]['reason'][:40]}")
            continue

        try:
            meta = await asyncio.wait_for(
                capture(t["url"], slug=slug, vendor=t["vendor"],
                        category=t["category"],
                        settle_ms=t.get("settle_ms", 2500)),
                timeout=90,
            )
        except Exception as exc:
            outcomes[slug] = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200],
                              "url": t["url"], "vendor": t["vendor"]}
            print(f"x {slug:28} {type(exc).__name__}")
            continue

        if meta.get("rejected"):
            outcomes[slug] = {"ok": False, "reason": meta["rejected"],
                              "url": t["url"], "vendor": t["vendor"]}
            print(f"x {slug:28} {meta['rejected'][:50]}")
        else:
            c = meta["counts"]
            outcomes[slug] = {
                "ok": True, "url": t["url"], "vendor": t["vendor"],
                "category": t["category"], "captured": meta["captured"],
                "bytes": meta["bytes"], "counts": c,
            }
            print(f"* {slug:28} {meta['bytes']//1024:4} KiB  "
                  f"{c['native_controls']:3} native {c['role_widgets']:3} role "
                  f"{c['buttons']:3} btn")

        OUTCOMES.write_text(json.dumps(outcomes, indent=2, sort_keys=True) + "\n")

    ok = sum(1 for v in outcomes.values() if v["ok"])
    print(f"\n{ok}/{len(outcomes)} captured; "
          f"{len(outcomes) - ok} refused or unusable")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
