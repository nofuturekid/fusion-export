"""Names in the export tree. They decide where files land and whether a later run finds them
again, so they must be stable and safe to use from a shell and on Windows."""

import FusionExport as fe


def test_apostrophes_are_dropped_so_paths_work_in_shells():
    assert fe.safe("Tom's Team") == "Toms Team"
    assert fe.safe("Tom’s `Team`") == "Toms Team"


def test_spaces_and_brackets_are_kept_because_they_carry_meaning():
    assert fe.safe("Tractor Logo - Gardol (Outline)") == "Tractor Logo - Gardol (Outline)"


def test_characters_windows_forbids_become_underscores():
    assert fe.safe('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"


def test_trailing_dots_and_spaces_are_removed_because_windows_drops_them():
    assert fe.safe(" name. ") == "name"


def test_empty_names_still_give_a_usable_name():
    assert fe.safe("...") == "_"


def test_long_names_are_cut_to_keep_windows_paths_short():
    assert len(fe.safe("x" * 300)) == fe.MAX_NAME


def test_duplicate_names_in_one_folder_get_numbered_in_order():
    assert fe.unique_names(["Part", "Part", "part", "Other"]) == [
        "Part",
        "Part_2",
        "part_3",
        "Other",
    ]


def test_numbering_is_the_same_on_every_scan():
    # The export state and the skip check rely on a file getting the same name each time.
    names = ["A", "A", "B", "A"]
    assert fe.unique_names(names) == fe.unique_names(list(names))


def test_names_that_only_differ_after_cleaning_do_not_collide():
    assert fe.unique_names(["Tom's", "Toms"]) == ["Toms", "Toms_2"]


class Cloud:
    """Minimal stand-in for a Fusion data object."""

    def __init__(self, id, name):
        self.id, self.name = id, name


def test_sibling_folders_that_clean_to_one_name_stay_apart():
    # "Tom's" and "Toms" would otherwise share a target folder and mix their files.
    siblings = [Cloud("a", "Tom's"), Cloud("b", "Toms")]
    assert fe.name_among(siblings, siblings[0]) == "Toms"
    assert fe.name_among(siblings, siblings[1]) == "Toms_2"


def test_name_among_matches_the_names_a_scan_gives():
    # A folder export and a full scan must put a folder at the same path.
    siblings = [Cloud("a", "Part"), Cloud("b", "part"), Cloud("c", "Other")]
    scanned = fe.unique_names(s.name for s in siblings)
    assert [fe.name_among(siblings, s) for s in siblings] == scanned
