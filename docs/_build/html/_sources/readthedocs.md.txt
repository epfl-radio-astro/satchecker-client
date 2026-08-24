# Publishing the Documentation

These docs are published on [Read the Docs](https://readthedocs.org), which
builds them straight from the repository. Every push to an activated branch, and
every new release tag, triggers a build of that version; readers switch between
versions with the version flyout that Read the Docs injects into every page.

## Versions

Read the Docs treats each git branch or tag as a separate *version* with its own
URL under `https://satchecker-client.readthedocs.io/en/<version>/`.

| Version | Built from | Purpose |
| --- | --- | --- |
| `stable` | the highest `vX.Y.Z` tag | The default version — what visitors get from the bare project URL. |
| `latest` | the `main` branch | The current development docs, ahead of any release. |
| `vX.Y.Z` | that release tag | Frozen docs for a published version. |

Two consequences worth knowing:

- **A version is only buildable if `.readthedocs.yaml` exists on it.** The
  `v0.1.0` tag predates this setup and cannot be built, so `stable` will not
  exist until the first release tagged after the docs landed on `main`. Until
  then the default version has to be `latest`.
- The version shown in the sidebar comes from the installed package metadata
  (`satchecker_client/_version.py`). Branch builds append the branch name —
  `0.1.0 (latest)` — since that number is the release being worked towards
  rather than one whose docs these are.

### Publishing a release version

1. Bump `__version__` in `satchecker_client/_version.py` and merge it.
2. Publish a GitHub release tagged `vX.Y.Z` (which also publishes to PyPI; the
   release workflow checks the tag against `__version__`).
3. Activate the new version in the Read the Docs admin (**Versions**), unless an
   automation rule already activates tags. `stable` follows the highest tag
   automatically once it is active.

## One-time project setup

Only needed until the project exists on Read the Docs:

1. Sign in to <https://readthedocs.org> with the GitHub account that can
   administer `epfl-radio-astro/satchecker-client` and import the repository.
   The project slug decides the domain, so pick `satchecker-client` if it is
   free.
2. Under **Admin → Settings**, set the default version to `latest` (the `main`
   branch) — `stable` does not exist until the first release tag cut after this
   setup — and set the default branch to `main`.
3. Under **Admin → Settings**, enable *Build pull requests for this project* so
   documentation changes get a preview build on each pull request.
4. Under **Versions**, activate `main` (published as `latest`).
5. After the next `vX.Y.Z` release, change the default version to `stable` so
   the bare project URL serves the latest release.
6. Add the resulting URL to the repository description and check it matches
   `[project.urls]` in `pyproject.toml`.

## Building the docs locally

```bash
pip install -e ".[docs]"
sphinx-build -b html -W --keep-going docs docs/_build/html
```

Then open `docs/_build/html/index.html`. Read the Docs and the docs job in the
Tests workflow both build with warnings treated as errors, so it is worth
reproducing that before pushing.
