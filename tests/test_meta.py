"""Metadata lives in <target>/.fusion-export/ so the export tree only holds exported files."""

import os

import FusionExport as fe


def test_metadata_goes_into_its_own_folder(tmp_path):
    path = fe.meta_path(str(tmp_path), fe.STATE_FILE)
    assert path == os.path.join(str(tmp_path), ".fusion-export", fe.STATE_FILE)
    assert os.path.isdir(os.path.dirname(path))


def test_metadata_from_older_versions_is_moved_not_lost(tmp_path):
    # Exports made before 0.1.0 kept their state in the target folder itself; losing it
    # would export everything again.
    (tmp_path / fe.STATE_FILE).write_text('{"id-1": {"version": 3, "done": ["f3d"]}}')
    path = fe.meta_path(str(tmp_path), fe.STATE_FILE)
    assert not (tmp_path / fe.STATE_FILE).exists()
    assert fe.load_json(path, {}) == {"id-1": {"version": 3, "done": ["f3d"]}}


def test_existing_metadata_folder_wins_over_a_stray_old_file(tmp_path):
    meta = tmp_path / ".fusion-export"
    meta.mkdir()
    (meta / fe.STATE_FILE).write_text('{"new": 1}')
    (tmp_path / fe.STATE_FILE).write_text('{"old": 1}')
    assert fe.load_json(fe.meta_path(str(tmp_path), fe.STATE_FILE), {}) == {"new": 1}


class FakeCollection:
    def __init__(self, items, has_array):
        self._items = items
        self.count = len(items)
        if has_array:
            self.asArray = lambda: list(items)

    def item(self, i):
        return self._items[i]


def test_items_of_gives_the_same_items_with_or_without_asarray():
    items = ["a", "b", "c"]
    assert fe.items_of(FakeCollection(items, True)) == items
    assert fe.items_of(FakeCollection(items, False)) == items
