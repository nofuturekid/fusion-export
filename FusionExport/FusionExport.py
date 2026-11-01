"""Fusion add-in: bulk export of designs and drawings.

Adds "Fusion Export" to Utilities > Add-Ins. The dialog exports the active document, the folder
open in the Data Panel, the active document's project, ticked projects or everything, with or
without subfolders, as Fusion archive (.f3d, or .f3z with external references), STEP, sketch DXF
and drawing PDF/DWG.

Files land in <target>/<hub>/<project>/<folders>/, so partial exports merge into one tree, or,
with "Keep cloud folder path" off, straight in <target>. <target>/.fusion-export/ holds:
  export-state.json  cloud version and exported formats per file: a design is exported again
                     when its cloud version changed, otherwise only missing formats are added
  scan-cache.json    file lists of earlier scans, so a scope can be exported without a new scan
  export-log.txt     one line per file and run
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile

import adsk.core
import adsk.drawing
import adsk.fusion

CMD_ID = "thomasFusionExport"
EVENT_ID = "thomasFusionExportRun"
UPDATE_EVENT_ID = "thomasFusionExportUpdate"
REPO = "nofuturekid/fusion-export"
ADDIN_DIR = os.path.dirname(os.path.abspath(__file__))
CHECK_INTERVAL = 24 * 3600  # seconds between update checks
PANEL_ID = "SolidScriptsAddinsPanel"
MAX_NAME = 80  # keep Windows paths below 260 characters
SETTINGS = os.path.join(os.path.dirname(__file__), "settings.json")
META_DIR = ".fusion-export"  # in the target folder: state, scan cache and log
STATE_FILE = "export-state.json"
CACHE_FILE = "scan-cache.json"
LOG_FILE = "export-log.txt"

SCOPES = [
    "Active document",
    "Folder open in the Data Panel",
    "Project of the active document",
    "Selected projects",
    "Everything (all hubs and projects)",
]
ACTIVE_SCOPE, FOLDER_SCOPE, PROJECT_SCOPE, PROJECTS_SCOPE, EVERYTHING_SCOPE = range(5)

DESIGN_EXTENSIONS = ("f3d", "f3z")
DRAWING_EXTENSIONS = ("f2d",)
# Native Fusion files other than designs and drawings (electronics, ...): DataFile.download
# refuses them and there is no export for them yet.
NATIVE_EXTENSIONS = ("flbr", "fsch", "fbrd", "f3dt")
FORMATS = [  # (key, dialog label, default, file kind it applies to)
    ("f3d", "Fusion archive (f3d/f3z)", True, "design"),
    ("step", "STEP", True, "design"),
    ("dxf", "Sketches as DXF", False, "design"),
    ("pdf", "Drawings as PDF", False, "drawing"),
    ("dwg", "Drawings as DWG (preview API)", False, "drawing"),
    ("orig", "Other files as uploaded (DXF, STEP, images, ...)", True, "upload"),
]

handlers = []
project_ids = {}  # dialog input id -> cloud project id (cloud ids are not valid input ids)


class Cancelled(Exception):
    pass


# --- pure logic (tested in tests/) ---------------------------------------------


def safe(name):
    """File or folder name for the export tree: no quotes (they break shell use), nothing
    Windows forbids, at most MAX_NAME characters."""
    name = re.sub(r"['`‘’]", "", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name or "_")[:MAX_NAME]


def unique_names(names):
    """Export names for the files of one cloud folder, in order. Fusion allows the same name
    twice in a folder; later ones get _2, _3. Rescans must give the same result, or the
    export state would point at the wrong files."""
    used = set()
    result = []
    for raw in names:
        base = safe(raw)
        name, n = base, 2
        while name.lower() in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name.lower())
        result.append(name)
    return result


def kind(ext):
    if ext in DESIGN_EXTENSIONS:
        return "design"
    if ext in DRAWING_EXTENSIONS:
        return "drawing"
    if ext in NATIVE_EXTENSIONS:
        return None
    return "upload"  # a file someone uploaded: downloadable as it is


def output_exists(folder, name, fmt_key):
    base = os.path.join(folder, name)
    if fmt_key == "f3d":
        return os.path.exists(base + ".f3d") or os.path.exists(base + ".f3z")
    if fmt_key in ("dxf", "orig"):
        return True  # no fixed file name (sketch folder, uploaded name); trust the state
    return os.path.exists(f"{base}.{fmt_key}")


def todo(item, fmt, state, force=False):
    """Formats to (re)write for a scanned item (folder, name, file id, ext, version).

    Everything ticked when forced, never exported, or the cloud version changed since the last
    export; otherwise only ticked formats that are not exported yet or whose file is gone."""
    folder, name, file_id, ext, version = item
    k = kind(ext)
    ticked = [key for key, _, _, applies in FORMATS if applies == k and fmt.get(key)]
    entry = state.get(file_id)
    if force or not entry or entry.get("version") != version:
        return ticked
    done = set(entry.get("done", []))
    return [key for key in ticked if key not in done or not output_exists(folder, name, key)]


def record(state, item, written_keys):
    """Remember what was exported for this file version."""
    _, _, file_id, _, version = item
    entry = state.get(file_id)
    if not entry or entry.get("version") != version:
        entry = {"version": version, "done": []}
    entry["done"] = sorted(set(entry["done"]) | set(written_keys))
    state[file_id] = entry


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def meta_path(root, name):
    """Path of a metadata file in <root>/.fusion-export/. Files from versions that kept them
    in <root> itself are moved there."""
    folder = os.path.join(root, META_DIR)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    old = os.path.join(root, name)
    if os.path.exists(old) and not os.path.exists(path):
        os.replace(old, path)
    return path


def append_log(root, line):
    with open(meta_path(root, LOG_FILE), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_settings():
    return load_json(SETTINGS, {})


def save_settings(settings):
    save_json(SETTINGS, settings)


# --- cloud: where things are and where they go ---------------------------------


def items_of(collection):
    """All items of a Fusion collection. asArray() fetches them in one call where the
    collection has it, item(i) one by one otherwise."""
    if hasattr(collection, "asArray"):
        return list(collection.asArray())
    return [collection.item(i) for i in range(collection.count)]


def name_among(siblings, target):
    """Export name of target among its siblings (cloud objects with .id and .name), so two
    siblings whose names only differ in removed characters or case get separate folders."""
    for sibling, name in zip(siblings, unique_names(s.name for s in siblings), strict=True):
        if sibling.id == target.id:
            return name
    return safe(target.name)


def project_path(root, project):
    hub = project.parentHub
    hubs = items_of(adsk.core.Application.get().data.dataHubs)
    return os.path.join(
        root, name_among(hubs, hub), name_among(items_of(hub.dataProjects), project)
    )


def folder_path(root, folder):
    """Target path of a cloud folder: project path plus its folder chain."""
    parts = []
    while not folder.isRoot:
        parent = folder.parentFolder
        parts.append(name_among(items_of(parent.dataFolders), folder))
        folder = parent
    return os.path.join(project_path(root, folder.parentProject), *reversed(parts))


class ScanTimer:
    """Where a scan spends its time, to decide whether a faster source is worth it."""

    def __init__(self):
        self.folders = 0
        self.seconds = {"list files": 0.0, "read file details": 0.0, "list folders": 0.0}

    def measure(self, phase, fn):
        start = time.perf_counter()
        result = fn()
        self.seconds[phase] += time.perf_counter() - start
        return result

    def summary(self, files):
        total = sum(self.seconds.values())
        parts = ", ".join(f"{k} {v:.1f} s" for k, v in self.seconds.items())
        return f"{self.folders} folders, {files} files in {total:.1f} s ({parts})"


def collect(folder, path, out, tick, recursive, timer):
    """Append (target folder, export name, file id, extension, version) for the files of
    folder, and of its subfolders when recursive."""
    tick(path)
    timer.folders += 1
    files = timer.measure("list files", lambda: items_of(folder.dataFiles))
    details = timer.measure(
        "read file details",
        lambda: [(f.name, f.id, f.fileExtension, f.versionNumber) for f in files],
    )
    for (_, file_id, ext, version), name in zip(
        details, unique_names(d[0] for d in details), strict=True
    ):
        out.append((path, name, file_id, ext, version))
    if recursive:
        subs = timer.measure("list folders", lambda: items_of(folder.dataFolders))
        for sub, name in zip(subs, unique_names(s.name for s in subs), strict=True):
            collect(sub, os.path.join(path, name), out, tick, recursive, timer)


def unique_name(data_file):
    """Export name collect() gives this file inside its folder."""
    return name_among(items_of(data_file.parentFolder.dataFiles), data_file)


def all_projects(app):
    projects = []
    hubs = app.data.dataHubs
    for h in range(hubs.count):
        dps = hubs.item(h).dataProjects
        for p in range(dps.count):
            projects.append(dps.item(p))
    return projects


def scan(ui, root, start, recursive, label):
    """start: list of (DataFolder, target path). Returns items, or None when cancelled."""
    dlg = ui.createProgressDialog()
    dlg.isCancelButtonShown = True
    dlg.show("Fusion export: scanning", "Scanning ...", 0, 1)
    items = []
    timer = ScanTimer()

    def tick(path):
        if dlg.wasCancelled:
            raise Cancelled()
        dlg.message = f"Scanning, {len(items)} files found so far\n{os.path.relpath(path, root)}"
        adsk.doEvents()

    try:
        for folder, path in start:
            collect(folder, path, items, tick, recursive, timer)
    except Cancelled:
        append_log(root, f"SCAN\t{label} (cancelled)\t{timer.summary(len(items))}")
        return None
    finally:
        dlg.hide()
    append_log(root, f"SCAN\t{label}\t{timer.summary(len(items))}")
    return items


def scan_cached(ui, root, key, start, recursive):
    """scan() with the result kept in scan-cache.json under key, so a cancelled export of the
    same scope can resume without the slow cloud walk. A reused list carries the cloud versions
    of its scan, so it cannot see changes made since; the question says so and shows its age."""
    cache_path = meta_path(root, CACHE_FILE)
    cache = load_json(cache_path, {})
    cached = cache.get(key)
    if isinstance(cached, dict) and all(len(row) == 5 for row in cached.get("items", [])):
        answer = ui.messageBox(
            f"This scope was scanned on {cached.get('scanned', '?')} "
            f"({len(cached['items'])} files).\n\n"
            "Yes = Resume: use that list and start right away. Designs changed, added or "
            "renamed since then are not seen.\n\n"
            "No = Fresh scan: walk the cloud folders again. Slow, but finds every change. "
            "Use this for backups.",
            "Fusion export",
            adsk.core.MessageBoxButtonTypes.YesNoButtonType,
        )
        if answer == adsk.core.DialogResults.DialogYes:
            return [(os.path.join(root, r), n, i, e, v) for r, n, i, e, v in cached["items"]]
    items = scan(ui, root, start, recursive, key)
    if items is not None:
        cache[key] = {
            "scanned": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "items": [(os.path.relpath(d, root), n, i, e, v) for d, n, i, e, v in items],
        }
        save_json(cache_path, cache)
    return items


# --- export --------------------------------------------------------------------


def has_references(design):
    occs = design.rootComponent.allOccurrences
    return any(occs.item(i).isReferencedComponent for i in range(occs.count))


def export_sketches(design, folder, name):
    """All sketches of all components as DXF into '<name> - Sketches', replacing an older
    export. Returns the count."""
    target = os.path.join(folder, f"{name} - Sketches")
    if os.path.isdir(target):
        shutil.rmtree(target)
    comps = design.allComponents
    sketches = []
    for c in range(comps.count):
        comp = comps.item(c)
        prefix = "" if comp.id == design.rootComponent.id else comp.name + " - "
        for i in range(comp.sketches.count):
            sketch = comp.sketches.item(i)
            sketches.append((prefix + sketch.name, sketch))
    names = unique_names(label for label, _ in sketches)
    for fname, (_, sketch) in zip(names, sketches, strict=True):
        os.makedirs(target, exist_ok=True)
        if not sketch.saveAsDXF(os.path.join(target, fname + ".dxf")):
            raise RuntimeError(f"DXF export of sketch '{sketch.name}' returned False")
    return len(sketches)


def export_design(design, folder, name, keys):
    em = design.exportManager
    written, notes = [], []
    if "f3d" in keys:
        ext = ".f3z" if has_references(design) else ".f3d"
        opts = em.createFusionArchiveExportOptions(
            os.path.join(folder, name + ext), design.rootComponent
        )
        if not em.execute(opts):
            raise RuntimeError("archive export returned False")
        other = os.path.join(folder, name + (".f3d" if ext == ".f3z" else ".f3z"))
        if os.path.exists(other):  # references were added or removed since the last export
            os.remove(other)
        written.append("f3d")
        notes.append(ext)
    if "step" in keys:
        opts = em.createSTEPExportOptions(
            os.path.join(folder, name + ".step"), design.rootComponent
        )
        if not em.execute(opts):
            raise RuntimeError("STEP export returned False")
        written.append("step")
        notes.append(".step")
    if "dxf" in keys:
        notes.append(f"{export_sketches(design, folder, name)} sketch DXF")
        written.append("dxf")
    return written, notes


def export_drawing(drawing, folder, name, keys):
    em = drawing.exportManager
    written, notes = [], []
    if "pdf" in keys:
        opts = em.createPDFExportOptions(os.path.join(folder, name + ".pdf"))
        opts.openPDF = False
        if not em.execute(opts):
            raise RuntimeError("PDF export returned False")
        written.append("pdf")
        notes.append(".pdf")
    if "dwg" in keys:
        if not hasattr(em, "createDWGExportOptions"):
            raise RuntimeError("this Fusion version has no DWG export in its API")
        opts = em.createDWGExportOptions(os.path.join(folder, name + ".dwg"))
        if not em.execute(opts):
            raise RuntimeError("DWG export returned False")
        written.append("dwg")
        notes.append(".dwg")
    return written, notes


def export_document(doc, folder, name, keys):
    os.makedirs(folder, exist_ok=True)
    design = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
    if design:
        return export_design(design, folder, name, keys)
    drawing_doc = adsk.drawing.DrawingDocument.cast(doc)
    if drawing_doc and drawing_doc.drawing:
        return export_drawing(drawing_doc.drawing, folder, name, keys)
    raise RuntimeError("neither a design nor a drawing")


class Run:
    """One export run: log, counters and the export state."""

    def __init__(self, root, fmt, force):
        self.root, self.fmt, self.force = root, fmt, force
        self.counts = {"OK": 0, "SKIP": 0, "OTHER": 0, "FAIL": 0}
        self.state_path = meta_path(root, STATE_FILE)
        self.state = load_json(self.state_path, {})
        self.log = open(meta_path(root, LOG_FILE), "a", encoding="utf-8")

    def rel(self, item):
        return os.path.relpath(os.path.join(item[0], item[1]), self.root)

    def note(self, status, item, msg=""):
        self.counts[status] += 1
        self.log.write(f"{status}\t{self.rel(item)}\t{msg}\n")
        self.log.flush()

    def export(self, doc, item, keys):
        written, notes = export_document(doc, item[0], item[1], keys)
        record(self.state, item, written)
        save_json(self.state_path, self.state)
        self.note("OK", item, f"v{item[4]} " + " ".join(notes))
        return notes

    def download(self, data_file, item):
        """An uploaded file as it is, into its folder under its own name."""
        os.makedirs(item[0], exist_ok=True)
        if not data_file.download(item[0], None):
            raise RuntimeError("download returned False")
        record(self.state, item, ["orig"])
        save_json(self.state_path, self.state)
        self.note("OK", item, f"v{item[4]} downloaded")

    def close(self):
        self.log.close()


def export_items(app, ui, run, items):
    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = True
    progress.show("Fusion export", "%v / %m", 0, len(items))
    try:
        for idx, item in enumerate(items):
            if progress.wasCancelled:
                break
            progress.progressValue = idx
            progress.message = f"%v / %m\n{run.rel(item)}"
            adsk.doEvents()
            if not kind(item[3]):
                run.note("OTHER", item, item[3])
                continue
            keys = todo(item, run.fmt, run.state, run.force)
            if not keys:
                run.note("SKIP", item)
                continue
            try:
                data_file = app.data.findFileById(item[2])
                if not data_file:
                    raise RuntimeError("file not found in the cloud, rescan")
                if kind(item[3]) == "upload":
                    run.download(data_file, item)
                    continue
                doc = app.documents.open(data_file, True)
                try:
                    run.export(doc, item, keys)
                finally:
                    doc.close(False)
            except Exception:
                run.note("FAIL", item, traceback.format_exc().strip().splitlines()[-1])
    finally:
        progress.hide()


# --- update check ----------------------------------------------------------------


def parse_version(text):
    """'v1.2.3' or '1.2.3' -> (1, 2, 3); None if it is not X.Y.Z."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def installed_version():
    return load_json(os.path.join(ADDIN_DIR, "FusionExport.manifest"), {}).get("version", "0.0.0")


def is_newer(candidate, current):
    new, cur = parse_version(candidate), parse_version(current)
    return bool(new and cur and new > cur)


def check_due(settings, now):
    """Whether to ask GitHub now: the check is on, and it never ran or the last one is a
    day old."""
    if not settings.get("update_check", True):
        return False
    last = settings.get("update_checked")
    return last is None or now - last >= CHECK_INTERVAL


def checksum_for(sums_text, filename):
    """SHA-256 for filename from a sha256sum-style SHA256SUMS file, or None."""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0].lower()
    return None


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "fusion-export-addin"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def latest_release():
    """(version, notes, {asset name: url}) of the latest GitHub release."""
    data = json.loads(fetch(f"https://api.github.com/repos/{REPO}/releases/latest"))
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    return data["tag_name"].lstrip("v"), data.get("body") or "", assets


def install_release(version, assets):
    """Download, verify and unpack the release zip over the add-in folder. settings.json
    stays. The new code runs after a Fusion restart."""
    zip_name = f"FusionExport-{version}.zip"
    if zip_name not in assets or "SHA256SUMS" not in assets:
        raise RuntimeError(f"release {version} lacks {zip_name} or SHA256SUMS")
    expected = checksum_for(fetch(assets["SHA256SUMS"]).decode(), zip_name)
    payload = fetch(assets[zip_name], timeout=120)
    if not expected or hashlib.sha256(payload).hexdigest() != expected:
        raise RuntimeError("checksum mismatch, nothing installed")
    with tempfile.TemporaryDirectory() as tmp:
        zpath = os.path.join(tmp, zip_name)
        with open(zpath, "wb") as f:
            f.write(payload)
        with zipfile.ZipFile(zpath) as z:
            for member in z.namelist():  # no absolute paths or ../ in the archive
                if member.startswith("/") or ".." in member.split("/"):
                    raise RuntimeError(f"unsafe path in archive: {member}")
            z.extractall(tmp)
        src = os.path.join(tmp, "FusionExport")
        manifest = load_json(os.path.join(src, "FusionExport.manifest"), {})
        if manifest.get("version") != version:
            raise RuntimeError("archive does not hold the expected version")
        shutil.copytree(src, ADDIN_DIR, dirs_exist_ok=True)


def check_in_background():
    """Ask GitHub in a worker thread so Fusion's start is not delayed; the answer goes to the
    main thread through a custom event."""
    settings = load_settings()
    now = time.time()
    if not check_due(settings, now):
        return
    settings["update_checked"] = now
    save_settings(settings)

    def work():
        try:
            version, notes, assets = latest_release()
        except Exception:
            return  # offline or rate limited: try again tomorrow
        if is_newer(version, installed_version()) and version != settings.get("update_skip"):
            payload = json.dumps({"version": version, "notes": notes, "assets": assets})
            adsk.core.Application.get().fireCustomEvent(UPDATE_EVENT_ID, payload)

    threading.Thread(target=work, daemon=True).start()


class UpdateHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        ui = adsk.core.Application.get().userInterface
        try:
            info = json.loads(args.additionalInfo)
            version, notes = info["version"], info["notes"].strip()
            if len(notes) > 1200:
                notes = notes[:1200] + " ..."
            answer = ui.messageBox(
                f"Fusion Export {version} is available (installed: {installed_version()}).\n\n"
                f"{notes}\n\n"
                "Yes = Install now (takes effect after restarting Fusion)\n"
                "No = Later (asks again tomorrow)\n"
                "Cancel = Skip this version",
                "Fusion Export update",
                adsk.core.MessageBoxButtonTypes.YesNoCancelButtonType,
            )
            if answer == adsk.core.DialogResults.DialogCancel:
                settings = load_settings()
                settings["update_skip"] = version
                save_settings(settings)
            elif answer == adsk.core.DialogResults.DialogYes:
                install_release(version, info["assets"])
                ui.messageBox(f"Fusion Export {version} is installed. Restart Fusion to use it.")
        except Exception:
            ui.messageBox("Update failed, nothing changed:\n" + traceback.format_exc())


# --- command -------------------------------------------------------------------


class ExecuteHandler(adsk.core.CommandEventHandler):
    """Only reads the dialog. The export runs from a custom event after the command has ended,
    because opening and closing documents inside a running command is not reliable."""

    def notify(self, args):
        inputs = args.command.commandInputs
        group = inputs.itemById("projects")
        selected = [
            project_ids[group.children.item(i).id]
            for i in range(group.children.count)
            if group.children.item(i).value
        ]
        params = {
            "scope": inputs.itemById("scope").selectedItem.index,
            "fmt": {key: inputs.itemById(key).value for key, _, _, _ in FORMATS},
            "subfolders": inputs.itemById("subfolders").value,
            "cloud_path": inputs.itemById("cloud_path").value,
            "projects": selected,
            "force": inputs.itemById("force").value,
            "scan_only": inputs.itemById("scan_only").value,
        }
        settings = load_settings()
        settings.update({k: v for k, v in params.items() if k not in ("force", "scan_only")})
        settings["update_check"] = inputs.itemById("update_check").value
        save_settings(settings)
        adsk.core.Application.get().fireCustomEvent(EVENT_ID, json.dumps(params))


def ask_root(ui):
    settings = load_settings()
    dlg = ui.createFolderDialog()
    dlg.title = "Target folder for the export"
    if settings.get("root") and os.path.isdir(settings["root"]):
        dlg.initialDirectory = settings["root"]
    if dlg.showDialog() != adsk.core.DialogResults.DialogOK:
        return None
    settings["root"] = dlg.folder
    save_settings(settings)
    return dlg.folder


def run_export(app, ui, params):
    scope, fmt = params["scope"], params["fmt"]
    force, scan_only = params["force"], params["scan_only"]
    recursive = params["subfolders"] or scope == EVERYTHING_SCOPE
    # Off: the chosen folder's or project's content goes straight into the target folder.
    cloud_path = params.get("cloud_path", True) or scope == EVERYTHING_SCOPE
    if scan_only and scope == ACTIVE_SCOPE:
        ui.messageBox("'Scan only' needs a folder, project or 'Everything' scope.")
        return
    if not (any(fmt.values()) or scan_only):
        ui.messageBox("Nothing to do: no format is ticked.")
        return
    if scope == PROJECTS_SCOPE and not params["projects"]:
        ui.messageBox("No project ticked.")
        return
    active = app.activeDocument.dataFile if app.activeDocument else None
    panel_folder = None
    if scope == FOLDER_SCOPE:
        panel_folder = getattr(app.data, "activeFolder", None)
        if not panel_folder and active:
            panel_folder = active.parentFolder
    if (scope in (ACTIVE_SCOPE, PROJECT_SCOPE) and not active) or (
        scope == FOLDER_SCOPE and not panel_folder
    ):
        ui.messageBox("Open a saved document first, or choose another scope.")
        return

    root = ask_root(ui)
    if not root:
        return

    if scope == ACTIVE_SCOPE:
        item = (
            folder_path(root, active.parentFolder) if cloud_path else root,
            unique_name(active),
            active.id,
            active.fileExtension,
            active.versionNumber,
        )
        run = Run(root, fmt, force=True)  # the user asked for this one document
        try:
            keys = todo(item, fmt, run.state, force=True)
            if not keys:
                ui.messageBox("No ticked format applies to this document.")
                return
            notes = run.export(app.activeDocument, item, keys)
        finally:
            run.close()
        ui.messageBox(f"Exported {' '.join(notes)}\n{os.path.join(item[0], item[1])}")
        return

    if scope == EVERYTHING_SCOPE:
        key = "everything"
        start = [(p.rootFolder, project_path(root, p)) for p in all_projects(app)]
    elif scope == PROJECTS_SCOPE:
        key = "projects:" + ",".join(sorted(params["projects"]))
        chosen = [p for p in all_projects(app) if p.id in params["projects"]]
        if cloud_path:
            start = [(p.rootFolder, project_path(root, p)) for p in chosen]
        else:  # one folder per project
            names = unique_names(p.name for p in chosen)
            start = [
                (p.rootFolder, os.path.join(root, n)) for p, n in zip(chosen, names, strict=True)
            ]
    elif scope == FOLDER_SCOPE:
        key = "folder:" + panel_folder.id
        start = [(panel_folder, folder_path(root, panel_folder) if cloud_path else root)]
    else:
        project = active.parentProject
        key = "project:" + project.id
        start = [(project.rootFolder, project_path(root, project) if cloud_path else root)]
    if not recursive:
        key += ":flat"
    if not cloud_path:
        key += ":short"
    items = scan_cached(ui, root, key, start, recursive)
    if items is None:
        ui.messageBox("Scan cancelled, nothing exported.")
        return

    if scan_only:
        state = load_json(meta_path(root, STATE_FILE), {})
        designs = sum(kind(it[3]) == "design" for it in items)
        drawings = sum(kind(it[3]) == "drawing" for it in items)
        uploads = sum(kind(it[3]) == "upload" for it in items)
        left = sum(bool(kind(it[3]) and todo(it, fmt, state, force)) for it in items)
        ui.messageBox(
            "Scan finished and saved, nothing exported.\n\n"
            f"files: {len(items)}\ndesigns: {designs}\ndrawings: {drawings}\n"
            f"uploaded files (DXF, STEP, images, ...): {uploads}\n"
            f"not exportable (electronics, ...): {len(items) - designs - drawings - uploads}\n"
            f"to export with the ticked formats: {left}\n\n"
            "Run the same scope again and answer 'Yes' (Resume) to export without rescanning.\n"
            f"Scan timing: SCAN line in {meta_path(root, LOG_FILE)}"
        )
        return

    run = Run(root, fmt, force)
    try:
        export_items(app, ui, run, items)
    finally:
        run.close()
    c = run.counts
    ui.messageBox(
        f"{len(items)} files in scope\n"
        f"exported: {c['OK']}\nup to date: {c['SKIP']}\n"
        f"not exportable (electronics, ...): {c['OTHER']}\nfailed: {c['FAIL']}\n\n"
        f"Details: {meta_path(root, LOG_FILE)}"
    )


class RunExportHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        app = adsk.core.Application.get()
        try:
            run_export(app, app.userInterface, json.loads(args.additionalInfo))
        except Exception:
            app.userInterface.messageBox("Export aborted:\n" + traceback.format_exc())


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        if args.input.id == "scope":
            index = args.input.selectedItem.index
            args.inputs.itemById("projects").isVisible = index == PROJECTS_SCOPE
            args.inputs.itemById("subfolders").isEnabled = index not in (
                ACTIVE_SCOPE,
                EVERYTHING_SCOPE,
            )
            args.inputs.itemById("cloud_path").isEnabled = index != EVERYTHING_SCOPE


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        settings = load_settings()
        scope = settings.get("scope", ACTIVE_SCOPE)
        cmd = args.command
        inputs = cmd.commandInputs
        dd = inputs.addDropDownCommandInput(
            "scope", "Export", adsk.core.DropDownStyles.TextListDropDownStyle
        )
        for i, label in enumerate(SCOPES):
            dd.listItems.add(label, i == scope)
        sub = inputs.addBoolValueInput(
            "subfolders", "Include subfolders", True, "", settings.get("subfolders", True)
        )
        sub.isEnabled = scope not in (ACTIVE_SCOPE, EVERYTHING_SCOPE)
        keep = inputs.addBoolValueInput(
            "cloud_path",
            "Keep cloud folder path (hub/project/folders)",
            True,
            "",
            settings.get("cloud_path", True),
        )
        keep.isEnabled = scope != EVERYTHING_SCOPE
        saved = settings.get("fmt", {})
        for key, label, default, _ in FORMATS:
            inputs.addBoolValueInput(key, label, True, "", saved.get(key, default))
        inputs.addBoolValueInput("force", "Re-export (overwrite up-to-date files)", True, "", False)
        inputs.addBoolValueInput("scan_only", "Scan only (no export)", True, "", False)
        inputs.addBoolValueInput(
            "update_check",
            "Check for updates once a day",
            True,
            "",
            settings.get("update_check", True),
        )

        group = inputs.addGroupCommandInput("projects", "Projects")
        group.isExpanded = True
        group.isVisible = scope == PROJECTS_SCOPE
        ticked = set(settings.get("projects", []))
        projects = all_projects(adsk.core.Application.get())
        hubs = {p.parentHub.name for p in projects}
        project_ids.clear()
        for n, project in enumerate(projects):
            project_ids[f"p_{n}"] = project.id
            label = project.name
            if len(hubs) > 1:
                label = f"{project.parentHub.name} / {project.name}"
            group.children.addBoolValueInput(f"p_{n}", label, True, "", project.id in ticked)

        for event, handler in (
            (cmd.inputChanged, InputChangedHandler()),
            (cmd.execute, ExecuteHandler()),
        ):
            event.add(handler)
            handlers.append(handler)


def run(context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        try:
            app.unregisterCustomEvent(EVENT_ID)  # leftover from a crashed session
        except Exception:
            pass
        on_run = RunExportHandler()
        app.registerCustomEvent(EVENT_ID).add(on_run)
        handlers.append(on_run)
        try:
            app.unregisterCustomEvent(UPDATE_EVENT_ID)
        except Exception:
            pass
        on_update = UpdateHandler()
        app.registerCustomEvent(UPDATE_EVENT_ID).add(on_update)
        handlers.append(on_update)
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = ui.commandDefinitions.addButtonDefinition(
                CMD_ID,
                "Fusion Export",
                "Export the active document, a folder, projects or everything as Fusion "
                "archive, STEP, sketch DXF and drawing PDF/DWG.",
                os.path.join(os.path.dirname(__file__), "resources"),
            )
        on_created = CreatedHandler()
        cmd_def.commandCreated.add(on_created)
        handlers.append(on_created)
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            control = panel.controls.addCommand(cmd_def)
            control.isPromotedByDefault = True
            control.isPromoted = True
        check_in_background()
    except Exception:
        ui.messageBox("Fusion Export failed to start:\n" + traceback.format_exc())


def stop(context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    app.unregisterCustomEvent(EVENT_ID)
    app.unregisterCustomEvent(UPDATE_EVENT_ID)
    panel = ui.allToolbarPanels.itemById(PANEL_ID)
    if panel:
        control = panel.controls.itemById(CMD_ID)
        if control:
            control.deleteMe()
    cmd_def = ui.commandDefinitions.itemById(CMD_ID)
    if cmd_def:
        cmd_def.deleteMe()
    handlers.clear()
