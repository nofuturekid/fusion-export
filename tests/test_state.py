"""What a run exports for a file. A run must add missing formats, export again when the design
changed in the cloud, and leave up-to-date files alone, so resumed and repeated runs are cheap
and backups stay current."""

import os

import FusionExport as fe

DESIGN_FMT = {"f3d": True, "step": True, "dxf": False, "pdf": True, "dwg": False}


def design(tmp_path, version=3):
    return (str(tmp_path), "Bracket", "id-1", "f3d", version)


def touch(tmp_path, *names):
    for name in names:
        (tmp_path / name).write_bytes(b"x")


def test_new_design_gets_every_ticked_design_format(tmp_path):
    assert fe.todo(design(tmp_path), DESIGN_FMT, {}) == ["f3d", "step"]


def test_drawing_formats_do_not_apply_to_designs_and_vice_versa(tmp_path):
    drawing = (str(tmp_path), "Sheet", "id-2", "f2d", 1)
    assert fe.todo(drawing, DESIGN_FMT, {}) == ["pdf"]


def test_files_get_nothing_when_their_format_is_not_ticked(tmp_path):
    pdf = (str(tmp_path), "Datasheet", "id-3", "pdf", 1)
    assert fe.todo(pdf, DESIGN_FMT, {}) == []


def test_up_to_date_design_is_skipped(tmp_path):
    touch(tmp_path, "Bracket.f3d", "Bracket.step")
    state = {"id-1": {"version": 3, "done": ["f3d", "step"]}}
    assert fe.todo(design(tmp_path), DESIGN_FMT, state) == []


def test_changed_cloud_version_exports_everything_again(tmp_path):
    touch(tmp_path, "Bracket.f3d", "Bracket.step")
    state = {"id-1": {"version": 2, "done": ["f3d", "step"]}}
    assert fe.todo(design(tmp_path, version=3), DESIGN_FMT, state) == ["f3d", "step"]


def test_newly_ticked_format_is_added_without_redoing_the_rest(tmp_path):
    touch(tmp_path, "Bracket.f3d", "Bracket.step")
    state = {"id-1": {"version": 3, "done": ["f3d", "step"]}}
    fmt = dict(DESIGN_FMT, dxf=True)
    assert fe.todo(design(tmp_path), fmt, state) == ["dxf"]


def test_deleted_export_file_is_written_again(tmp_path):
    touch(tmp_path, "Bracket.f3d")
    state = {"id-1": {"version": 3, "done": ["f3d", "step"]}}
    assert fe.todo(design(tmp_path), DESIGN_FMT, state) == ["step"]


def test_archive_counts_as_present_as_f3z_too(tmp_path):
    # Designs with external references are written as .f3z.
    touch(tmp_path, "Bracket.f3z", "Bracket.step")
    state = {"id-1": {"version": 3, "done": ["f3d", "step"]}}
    assert fe.todo(design(tmp_path), DESIGN_FMT, state) == []


def test_reexport_overwrites_up_to_date_files(tmp_path):
    touch(tmp_path, "Bracket.f3d", "Bracket.step")
    state = {"id-1": {"version": 3, "done": ["f3d", "step"]}}
    assert fe.todo(design(tmp_path), DESIGN_FMT, state, force=True) == ["f3d", "step"]


def test_record_adds_formats_for_the_same_version(tmp_path):
    state = {"id-1": {"version": 3, "done": ["f3d"]}}
    fe.record(state, design(tmp_path), ["step"])
    assert state["id-1"] == {"version": 3, "done": ["f3d", "step"]}


def test_record_forgets_formats_of_an_older_version(tmp_path):
    # Otherwise a format exported for v2 would count as done for v3.
    state = {"id-1": {"version": 2, "done": ["f3d", "step", "dxf"]}}
    fe.record(state, design(tmp_path, version=3), ["f3d"])
    assert state["id-1"] == {"version": 3, "done": ["f3d"]}


def test_state_survives_a_save_and_load(tmp_path):
    path = str(tmp_path / "export-state.json")
    fe.save_json(path, {"id-1": {"version": 3, "done": ["f3d"]}})
    assert fe.load_json(path, {}) == {"id-1": {"version": 3, "done": ["f3d"]}}


def test_broken_state_file_starts_empty_instead_of_failing(tmp_path):
    path = tmp_path / "export-state.json"
    path.write_text("{broken")
    assert fe.load_json(str(path), {}) == {}


def test_uploaded_files_are_downloaded_as_they_are(tmp_path):
    # DXF, STEP or images someone uploaded belong in a backup like the designs.
    upload = (str(tmp_path), "Door Panel Left.dxf", "id-4", "dxf", 1)
    fmt = dict(DESIGN_FMT, orig=True)
    assert fe.kind("dxf") == fe.kind("jpg") == "upload"
    assert fe.todo(upload, fmt, {}) == ["orig"]


def test_downloaded_upload_is_skipped_until_it_changes(tmp_path):
    upload = (str(tmp_path), "Door Panel Left.dxf", "id-4", "dxf", 1)
    fmt = dict(DESIGN_FMT, orig=True)
    state = {"id-4": {"version": 1, "done": ["orig"]}}
    assert fe.todo(upload, fmt, state) == []
    assert fe.todo(upload[:4] + (2,), fmt, state) == ["orig"]


def test_native_electronics_files_are_left_out(tmp_path):
    # Fusion refuses to download native files and the add-in has no export for them.
    library = (str(tmp_path), "Power_Symbols", "id-5", "flbr", 1)
    assert fe.kind("flbr") is None
    assert fe.todo(library, dict(DESIGN_FMT, orig=True), {}) == []


def test_upload_keeps_its_extension_once(tmp_path):
    named = (str(tmp_path), "Door Panel Left.dxf", "id-4", "dxf", 1)
    bare = (str(tmp_path), "Reflector 2", "id-6", "ste", 1)
    assert fe.upload_path(named) == str(tmp_path / "Door Panel Left.dxf")
    assert fe.upload_path(bare) == str(tmp_path / "Reflector 2.ste")


def test_upload_cannot_escape_its_folder(tmp_path):
    # Cloud names are not trusted: the export name comes from safe(), and so must the extension.
    item = (str(tmp_path), fe.safe("..\\..\\evil"), "id-7", "../x", 1)
    path = fe.upload_path(item)
    assert os.path.dirname(path) == str(tmp_path)
