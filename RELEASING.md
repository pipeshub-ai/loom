# Releasing loomsdk

Releases are published to PyPI by `.github/workflows/release.yml` using
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — no API
token is stored in this repository. That workflow needs one piece of one-time
setup on pypi.org before the first automated release, below.

## One-time setup (do this once, as a `loomsdk` project owner/maintainer on PyPI)

1. Sign in at [pypi.org](https://pypi.org) and go to the `loomsdk` project's
   **Settings → Publishing**.
2. Add a new trusted publisher:
   - Owner: `pipeshub-ai`
   - Repository: `loom`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`
3. In this GitHub repo, go to **Settings → Environments**, create an
   environment named `pypi`, and optionally add required reviewers — that
   makes every publish (even an automatic tag push) wait for a human to click
   "approve" before anything reaches PyPI.

No token, secret, or `.pypirc` is needed anywhere after this. If the trusted
publisher is ever removed on PyPI, the `publish` job fails closed (no
fallback token to reach for) rather than silently switching to a different
credential.

## Cutting a release

1. **Update the changelog.** Move the `## [Unreleased]` section in
   `CHANGELOG.md` to a new `## [X.Y.Z] - YYYY-MM-DD` header.
2. **Bump the version** in `pyproject.toml` (`[project] version = "X.Y.Z"`).
   Follow [SemVer](https://semver.org/): breaking changes to the public API in
   `loom/__init__.py` bump the major/minor as appropriate for a pre-1.0
   project (minor for anything user-visible, patch for fixes only).
3. Commit and tag:

   ```bash
   git add pyproject.toml CHANGELOG.md
   git commit -m "Release X.Y.Z"
   git tag vX.Y.Z
   git push origin main --tags
   ```

   Pushing the tag triggers the `build` job immediately; `publish` then waits
   on the `pypi` environment if you configured required reviewers.
4. **Verify:** `pip install loomsdk==X.Y.Z` in a scratch venv, and check
   <https://pypi.org/project/loomsdk/> shows the new version.
5. Optionally, turn the tag into a GitHub Release with the changelog section
   as the release notes (`gh release create vX.Y.Z --notes-file <(...)`) — the
   workflow also runs on `release: published` so this step alone is enough to
   re-trigger a publish if the tag push's run needs a retry.

## Manual fallback (no CI)

Only needed if the workflow itself is broken. Requires a PyPI API token
scoped to the `loomsdk` project (Account Settings → API tokens):

```bash
python3 -m pip install --upgrade build twine
python3 -m build                  # writes dist/*.whl and dist/*.tar.gz
python3 -m twine check dist/*
python3 -m twine upload dist/*     # username: __token__, password: pypi-...
```
