# Contributing

Every change goes through an issue and a pull request, also the maintainer's:

1. Open an issue: what is wrong or missing, how to reproduce it, which Fusion version.
2. Open a pull request against `main` whose description starts with `Fixes #<issue>`.
3. CI must be green: `ruff format --check .`, `ruff check .`, `pytest` and
   `scripts/check-version.sh` (the check is called `required`). Tests state why a behaviour
   matters, not only what it does.
4. Commits are signed; `main` only accepts signed commits and a linear history.
5. The maintainer merges fast-forward only, by pushing the green PR head to `main`, so your
   commits keep their hashes and signatures. The merge button is not used.
   A separate ruleset without bypass requires the `required` check on the pushed commit, so
   not even the maintainer can push to `main` before CI is green.

Changes that come from someone else (a merged pull request, or an issue or report that led to
the change) are credited at the end of their `CHANGELOG.md` line as `(#N, thanks @handle)`.

Contributions written with AI assistance are welcome; say so in the pull request.

## Releases

Versions follow [Semantic Versioning](https://semver.org/). A release is a signed tag `vX.Y.Z`
on `main`:

1. In a pull request, set the version in `FusionExport/FusionExport.manifest` and
   `pyproject.toml`, and move the `[Unreleased]` entries of `CHANGELOG.md` under
   `## [X.Y.Z] - YYYY-MM-DD`.
2. After it is on `main`: `git tag -s vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z`.

The release workflow checks that the tag is on `main`, that both files carry the version and
that the changelog has the section, runs the CI checks, and publishes
`FusionExport-X.Y.Z.zip` with `SHA256SUMS` and a build provenance attestation.
