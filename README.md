# Fusion Export

An add-in for Autodesk Fusion that exports many designs and drawings at once: a whole folder,
a project, several projects or everything in your account. Fusion itself exports one document
at a time.

| Format | What you get |
|---|---|
| Fusion archive | `Name.f3d`, or `Name.f3z` for designs with external references: the full design with timeline, sketches and parameters |
| STEP | `Name.step`: the bodies, for other CAD software |
| Sketch DXF | `Name - Sketches/<sketch>.dxf`: every sketch of every component |
| Drawing PDF | `Drawing.pdf`: all sheets in one file |
| Drawing DWG | `Drawing.dwg` (uses a preview API of Fusion, see below) |
| Uploaded files | DXF, STEP, images and other files uploaded to Fusion, downloaded as they are |

Files land in `<target>/<hub>/<project>/<folders>/`, the same tree as in the Data Panel, so
partial exports add up to one tree; with *Keep cloud folder path* off they go straight into
the target folder instead.

## Install

1. Download `FusionExport-X.Y.Z.zip` from the
   [latest release](https://github.com/nofuturekid/fusion-export/releases/latest) and unpack it.
   You get a folder `FusionExport`.
2. Move that folder into Fusion's add-in folder:
   - Windows: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\`
   - macOS: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`
3. In Fusion: **Utilities > Add-Ins > Scripts and Add-Ins**, tab **Add-Ins**, select
   **FusionExport**, click **Run** and tick **Run on Startup**.

The **Fusion Export** button then sits in **Utilities > Add-Ins**.

Once a day the add-in asks GitHub for a newer release and offers to install it (the download is
checked against the release's `SHA256SUMS`, which catches broken downloads but is no proof of
origin, see #13; settings are kept; restart Fusion afterwards). *Skip
this version* stops asking for that version; *Check for updates once a day* in the dialog turns
the check off.

To update by hand, replace the folder and restart Fusion. Stop and Run in Scripts and Add-Ins
reloads the code too, but Fusion reads the version shown there only at start.

## Use

Click **Fusion Export** and choose:

- **Export**
  - *Active document*: the open design or drawing, exported right away.
  - *Folder open in the Data Panel*: open the folder in the Data Panel first.
  - *Project of the active document*
  - *Selected projects*: tick them in the list below.
  - *Everything*: all hubs and projects.
- **Include subfolders**: off exports only the files directly in the chosen folder or project.
  *Everything* always includes them.
- **Keep cloud folder path**: on (default) puts files under `<hub>/<project>/<folders>/` in the
  target, so exports of different folders add up to one tree. Off puts the chosen folder's or
  project's content straight into the target folder (with *Selected projects*, one folder per
  project); use it to hand a folder to someone. *Everything* always keeps the path.
- **Content**: what to write for each file.
  - *Complete backup* (default): Fusion archive, STEP and sketch DXF for designs, PDF for
    drawings, uploaded files as they are.
  - *Fusion archives and uploaded files*: everything needed to restore into Fusion.
  - *STEP only*: for other CAD software.
  - *Drawings as PDF only*
  - *Custom ...*: shows one checkbox per format, including drawing DWG.
- **Re-export**: write all ticked formats again, even when they are up to date.
- **Scan only**: list the files and count what is left to export, without exporting.

Then pick the target folder. The add-in remembers it and your choices.

### What a run exports

A run exports a file only when needed:

- never exported, or a ticked format is missing (or its file was deleted): only the missing
  formats are written;
- the design changed in the cloud since the last export (a new version): all ticked formats
  are written again;
- otherwise the file is up to date and skipped.

So a cancelled run continues where it stopped, ticking another format later adds just that
format, and running the same scope again refreshes changed designs: a simple backup.

### Files in `.fusion-export/`

| File | Purpose |
|---|---|
| `export-state.json` | cloud version and exported formats per file |
| `scan-cache.json` | file lists of earlier scans with their date. A run of the same scope asks: **Yes = Resume** uses the list and starts at once (for continuing a cancelled run), **No = Fresh scan** walks the cloud again. Only a fresh scan sees documents changed, added or renamed since: use it for backups. |
| `export-log.txt` | one line per file and run: `OK`, `SKIP` (up to date), `UNTICKED` (no ticked format applies to the file, nothing exported), `OTHER` (a native file the add-in cannot export, such as an electronics library) or `FAIL` with the error; `SCAN` lines with how long a scan took and where |

All three live in `.fusion-export/` inside the target folder (older exports that kept them in
the target folder itself are moved there on the next run). Keep that folder with the export:
moving the target folder moves the state with it.

### Names

File and folder names are the cloud names with these changes: apostrophes are removed
(`Tom's Team` becomes `Toms Team`), characters Windows forbids become `_`, names are cut to
80 characters, and a second file with the same name in one folder becomes `Name_2`.

## Limits

- STEP holds bodies only. A design with sketches but no bodies gives an empty STEP; use the
  Fusion archive or sketch DXF for it.
- Electronics files (libraries, schematics, boards) are not exported yet; the log lists them
  as `OTHER`.
- Drawings cannot be saved as an editable file outside the cloud; PDF and DWG are what Fusion
  offers. They can be recreated from the design archive.
- DWG export uses `createDWGExportOptions`, a
  [preview API](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/drawing_DrawingExportManager.htm)
  that Autodesk may change. If your Fusion lacks it, the log shows `FAIL` for DWG and the run
  goes on.
- Each document is opened in Fusion to export it, so a large export takes hours. Fusion is
  busy meanwhile; the progress dialog can cancel it.

## Development

The add-in is one Python file, `FusionExport/FusionExport.py`. The tests cover the logic that
does not need Fusion (names, what to export) with a stub of the `adsk` modules:

```sh
python -m pip install ruff==0.17.3 pytest==9.2.0
ruff format --check . && ruff check . && pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for issues, pull requests and releases.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). Not affiliated with Autodesk.
