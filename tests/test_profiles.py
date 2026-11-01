"""Content profiles replace the format checkboxes for the common cases. A profile that claims to
be a complete backup must cover every kind of file the add-in can export."""

import FusionExport as fe

KINDS = {applies for _, _, _, applies in fe.FORMATS}


def test_complete_backup_covers_designs_drawings_and_uploads():
    fmt = fe.profile_formats("complete", {})
    covered = {applies for key, _, _, applies in fe.FORMATS if fmt[key]}
    assert covered == KINDS


def test_complete_backup_keeps_the_full_design():
    # STEP alone loses the timeline and sketches; the archive must be in a backup.
    assert fe.profile_formats("complete", {})["f3d"]


def test_complete_backup_leaves_out_the_preview_dwg_export():
    # DWG uses a preview API that may break; a backup should not fail on it by default.
    assert not fe.profile_formats("complete", {})["dwg"]


def test_custom_uses_exactly_the_checkboxes():
    custom = {"step": True, "dwg": True}
    fmt = fe.profile_formats("custom", custom)
    assert [k for k, v in fmt.items() if v] == ["step", "dwg"]


def test_unknown_profile_from_old_settings_falls_back_to_the_checkboxes():
    assert fe.profile_formats("gone", {"f3d": True})["f3d"]


def test_every_profile_names_only_known_formats():
    known = {key for key, _, _, _ in fe.FORMATS}
    for _, _, fmts in fe.PROFILES:
        assert fmts is None or fmts <= known
