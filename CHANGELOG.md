# Changelog

All notable changes to this add-in. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/).

Changes that come from someone else (a merged pull request, or an issue or report that led to
the change) are credited at the end of their line as `(#N, thanks @handle)`.

## [Unreleased]

## [0.1.0] - 2026-11-01

### Added

- Fusion Export button in Utilities > Add-Ins with a dialog for the scope (active document,
  Data Panel folder, project, selected projects, everything), subfolders on or off, content
  profiles (complete backup, archives, STEP, drawing PDF, or custom formats), re-export and
  scan only (#26).
- Exports designs as Fusion archive (`.f3d`, or `.f3z` with external references), STEP and
  sketch DXF, and drawings as PDF and DWG.
- Files uploaded to Fusion (DXF, STEP, images, ...) are downloaded as they are (#18).
- Export tree `<hub>/<project>/<folders>/` with names safe for shells and Windows.
- "Keep cloud folder path" off exports a folder or project straight into the target folder
  (#10).
- `export-state.json`: designs changed in the cloud are exported again, up-to-date ones are
  skipped, newly ticked formats are added.
- `scan-cache.json`: a cancelled export can resume without walking the cloud folders again;
  the question shows the age of the list and that only a fresh scan finds changes (#1).
- Sibling folders, projects or hubs whose names only differ in removed characters or case get
  separate target folders (`Name`, `Name_2`) (#2).
- Update check once a day with Install, Later and Skip this version; downloads are checked
  against the release's `SHA256SUMS` (#9).
- `export-log.txt` with one line per file and run, and `SCAN` lines with the scan time per
  phase (#4).
- State, scan cache and log live in `.fusion-export/` inside the target folder (#5).
