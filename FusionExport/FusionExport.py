"""Fusion add-in: bulk export of designs and drawings.

Adds "Fusion Export" to Utilities > Add-Ins. The dialog exports the active document, the folder
open in the Data Panel, the active document's project, ticked projects or everything, with or
without subfolders, as Fusion archive (.f3d, or .f3z with external references), STEP, sketch DXF
and drawing PDF/DWG.

Files land in <target>/<hub>/<project>/<folders>/, so partial exports merge into one tree. The
target folder also holds:
  export-state.json  cloud version and exported formats per file: a design is exported again
                     when its cloud version changed, otherwise only missing formats are added
  scan-cache.json    file lists of earlier scans, so a scope can be exported without a new scan
  export-log.txt     one line per file and run
"""

import json
import os
import re
import shutil
import traceback

import adsk.core
import adsk.drawing
import adsk.fusion

CMD_ID = "thomasFusionExport"
EVENT_ID = "thomasFusionExportRun"
PANEL_ID = "SolidScriptsAddinsPanel"
MAX_NAME = 80  # keep Windows paths below 260 characters
SETTINGS = os.path.join(os.path.dirname(__file__), "settings.json")
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
FORMATS = [  # (key, dialog label, default, file kind it applies to)
    ("f3d", "Fusion archive (f3d/f3z)", True, "design"),
    ("step", "STEP", True, "design"),
    ("dxf", "Sketches as DXF", False, "design"),
    ("pdf", "Drawings as PDF", False, "drawing"),
    ("dwg", "Drawings as DWG (preview API)", False, "drawing"),
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
    return None


def output_exists(folder, name, fmt_key):
    base = os.path.join(folder, name)
    if fmt_key == "f3d":
        return os.path.exists(base + ".f3d") or os.path.exists(base + ".f3z")
    if fmt_key == "dxf":
        return True  # a design without sketches has no DXF; trust the state
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


def load_settings():
    return load_json(SETTINGS, {})


def save_settings(settings):
    save_json(SETTINGS, settings)


# --- cloud: where things are and where they go ---------------------------------


def project_path(root, project):
    return os.path.join(root, safe(project.parentHub.name), safe(project.name))


def folder_path(root, folder):
    """Target path of a cloud folder: project path plus its folder chain."""
    parts = []
    while not folder.isRoot:
        parts.append(safe(folder.name))
        folder = folder.parentFolder
    return os.path.join(project_path(root, folder.parentProject), *reversed(parts))


def collect(folder, path, out, tick, recursive):
    """Append (target folder, export name, file id, extension, version) for the files of
    folder, and of its subfolders when recursive."""
    tick(path)
    files = [folder.dataFiles.item(i) for i in range(folder.dataFiles.count)]
    for data_file, name in zip(files, unique_names(f.name for f in files), strict=True):
        out.append((path, name, data_file.id, data_file.fileExtension, data_file.versionNumber))
    if recursive:
        for i in range(folder.dataFolders.count):
            sub = folder.dataFolders.item(i)
            collect(sub, os.path.join(path, safe(sub.name)), out, tick, recursive)


def unique_name(data_file):
    """Export name collect() gives this file inside its folder."""
    files = data_file.parentFolder.dataFiles
    files = [files.item(i) for i in range(files.count)]
    for f, name in zip(files, unique_names(f.name for f in files), strict=True):
        if f.id == data_file.id:
            return name
    return safe(data_file.name)


def all_projects(app):
    projects = []
    hubs = app.data.dataHubs
    for h in range(hubs.count):
        dps = hubs.item(h).dataProjects
        for p in range(dps.count):
            projects.append(dps.item(p))
    return projects


def scan(ui, root, start, recursive):
    """start: list of (DataFolder, target path). Returns items, or None when cancelled."""
    dlg = ui.createProgressDialog()
    dlg.isCancelButtonShown = True
    dlg.show("Fusion export: scanning", "Scanning ...", 0, 1)
    items = []

    def tick(path):
        if dlg.wasCancelled:
            raise Cancelled()
        dlg.message = f"Scanning, {len(items)} files found so far\n{os.path.relpath(path, root)}"
        adsk.doEvents()

    try:
        for folder, path in start:
            collect(folder, path, items, tick, recursive)
    except Cancelled:
        return None
    finally:
        dlg.hide()
    return items


def scan_cached(ui, root, key, start, recursive):
    """scan() with the result kept in scan-cache.json under key, so repeating an export of the
    same scope can skip the slow cloud walk. A reused list has the cloud versions of its scan:
    rescan to pick up changed designs."""
    cache_path = os.path.join(root, CACHE_FILE)
    cache = load_json(cache_path, {})
    cached = cache.get(key)
    if cached and all(len(row) == 5 for row in cached):
        answer = ui.messageBox(
            f"This scope was scanned before ({len(cached)} files).\n"
            "Reuse that file list?\n\n"
            "Yes: start right away.\n"
            "No: scan the cloud again (needed to find new, renamed or changed designs).",
            "Fusion export",
            adsk.core.MessageBoxButtonTypes.YesNoButtonType,
        )
        if answer == adsk.core.DialogResults.DialogYes:
            return [(os.path.join(root, r), n, i, e, v) for r, n, i, e, v in cached]
    items = scan(ui, root, start, recursive)
    if items is not None:
        cache[key] = [(os.path.relpath(d, root), n, i, e, v) for d, n, i, e, v in items]
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
        self.state_path = os.path.join(root, STATE_FILE)
        self.state = load_json(self.state_path, {})
        self.log = open(os.path.join(root, LOG_FILE), "a", encoding="utf-8")

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
                doc = app.documents.open(data_file, True)
                try:
                    run.export(doc, item, keys)
                finally:
                    doc.close(False)
            except Exception:
                run.note("FAIL", item, traceback.format_exc().strip().splitlines()[-1])
    finally:
        progress.hide()


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
            "projects": selected,
            "force": inputs.itemById("force").value,
            "scan_only": inputs.itemById("scan_only").value,
        }
        settings = load_settings()
        settings.update({k: v for k, v in params.items() if k not in ("force", "scan_only")})
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
    force, scan_only, recursive = params["force"], params["scan_only"], params["subfolders"]
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
            folder_path(root, active.parentFolder),
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
        start = [
            (p.rootFolder, project_path(root, p))
            for p in all_projects(app)
            if p.id in params["projects"]
        ]
    elif scope == FOLDER_SCOPE:
        key = "folder:" + panel_folder.id
        start = [(panel_folder, folder_path(root, panel_folder))]
    else:
        project = active.parentProject
        key = "project:" + project.id
        start = [(project.rootFolder, project_path(root, project))]
    if not recursive:
        key += ":flat"
    items = scan_cached(ui, root, key, start, recursive)
    if items is None:
        ui.messageBox("Scan cancelled, nothing exported.")
        return

    if scan_only:
        state = load_json(os.path.join(root, STATE_FILE), {})
        designs = sum(kind(it[3]) == "design" for it in items)
        drawings = sum(kind(it[3]) == "drawing" for it in items)
        left = sum(bool(kind(it[3]) and todo(it, fmt, state, force)) for it in items)
        ui.messageBox(
            "Scan finished and saved, nothing exported.\n\n"
            f"files: {len(items)}\ndesigns: {designs}\ndrawings: {drawings}\n"
            f"other files: {len(items) - designs - drawings}\n"
            f"to export with the ticked formats: {left}\n\n"
            "Run the same scope again and answer 'Yes' to export without rescanning."
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
        f"other files: {c['OTHER']}\nfailed: {c['FAIL']}\n\n"
        f"Details: {os.path.join(root, LOG_FILE)}"
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
            args.inputs.itemById("subfolders").isEnabled = index != ACTIVE_SCOPE


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
        sub.isEnabled = scope != ACTIVE_SCOPE
        saved = settings.get("fmt", {})
        for key, label, default, _ in FORMATS:
            inputs.addBoolValueInput(key, label, True, "", saved.get(key, default))
        inputs.addBoolValueInput("force", "Re-export (overwrite up-to-date files)", True, "", False)
        inputs.addBoolValueInput("scan_only", "Scan only (no export)", True, "", False)

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
    except Exception:
        ui.messageBox("Fusion Export failed to start:\n" + traceback.format_exc())


def stop(context):
    app = adsk.core.Application.get()
    ui = app.userInterface
    app.unregisterCustomEvent(EVENT_ID)
    panel = ui.allToolbarPanels.itemById(PANEL_ID)
    if panel:
        control = panel.controls.itemById(CMD_ID)
        if control:
            control.deleteMe()
    cmd_def = ui.commandDefinitions.itemById(CMD_ID)
    if cmd_def:
        cmd_def.deleteMe()
    handlers.clear()
