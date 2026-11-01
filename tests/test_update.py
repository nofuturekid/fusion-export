"""Update check: it must only offer real upgrades, ask at most once a day, and install a
download only when its checksum matches the release's SHA256SUMS."""

import FusionExport as fe

DAY = fe.CHECK_INTERVAL


def test_versions_compare_as_numbers_not_text():
    # "0.10.0" sorts before "0.9.0" as text; as a version it is newer.
    assert fe.is_newer("0.10.0", "0.9.0")
    assert fe.is_newer("v1.0.0", "0.9.9")


def test_same_or_older_release_is_not_offered():
    assert not fe.is_newer("0.1.0", "0.1.0")
    assert not fe.is_newer("0.1.0", "0.2.0")


def test_tags_that_are_not_plain_versions_are_ignored():
    assert not fe.is_newer("v1.0.0-rc1", "0.1.0")
    assert not fe.is_newer("nightly", "0.1.0")


def test_check_runs_at_most_once_a_day():
    assert fe.check_due({}, now=1000)
    assert not fe.check_due({"update_checked": 1000}, now=1000 + DAY - 1)
    assert fe.check_due({"update_checked": 1000}, now=1000 + DAY)


def test_check_can_be_turned_off():
    assert not fe.check_due({"update_check": False}, now=10 * DAY)


def test_checksum_is_found_for_the_right_file_only():
    sums = "aaa111  FusionExport-0.2.0.zip\nbbb222 *FusionExport-0.2.0.zip.sig\nCCC333  other.zip\n"
    assert fe.checksum_for(sums, "FusionExport-0.2.0.zip") == "aaa111"
    assert fe.checksum_for(sums, "other.zip") == "ccc333"
    assert fe.checksum_for(sums, "missing.zip") is None


def make_release(tmp_path, version, manifest_version=None):
    """A release zip like release.yml builds it, plus its SHA256SUMS."""
    import hashlib
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "FusionExport/FusionExport.manifest",
            json.dumps({"version": manifest_version or version}),
        )
        z.writestr("FusionExport/FusionExport.py", "# new code\n")
    data = buf.getvalue()
    name = f"FusionExport-{version}.zip"
    sums = f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()
    files = {f"https://x/{name}": data, "https://x/SHA256SUMS": sums}
    assets = {name: f"https://x/{name}", "SHA256SUMS": "https://x/SHA256SUMS"}
    return files, assets


def installed_addin(tmp_path):
    addin = tmp_path / "FusionExport"
    addin.mkdir()
    (addin / "FusionExport.py").write_text("# old code\n")
    (addin / "settings.json").write_text('{"root": "D:/export"}')
    return addin


def test_install_replaces_the_code_and_keeps_the_settings(tmp_path, monkeypatch):
    addin = installed_addin(tmp_path)
    files, assets = make_release(tmp_path, "0.2.0")
    monkeypatch.setattr(fe, "fetch", lambda url, timeout=20: files[url])
    monkeypatch.setattr(fe, "ADDIN_DIR", str(addin))
    fe.install_release("0.2.0", assets)
    assert (addin / "FusionExport.py").read_text() == "# new code\n"
    assert (addin / "settings.json").read_text() == '{"root": "D:/export"}'


def test_tampered_download_installs_nothing(tmp_path, monkeypatch):
    addin = installed_addin(tmp_path)
    files, assets = make_release(tmp_path, "0.2.0")
    files["https://x/FusionExport-0.2.0.zip"] += b"tampered"
    monkeypatch.setattr(fe, "fetch", lambda url, timeout=20: files[url])
    monkeypatch.setattr(fe, "ADDIN_DIR", str(addin))
    try:
        fe.install_release("0.2.0", assets)
    except RuntimeError as e:
        assert "checksum" in str(e)
    else:
        raise AssertionError("tampered download was installed")
    assert (addin / "FusionExport.py").read_text() == "# old code\n"


def test_zip_with_a_different_version_inside_installs_nothing(tmp_path, monkeypatch):
    addin = installed_addin(tmp_path)
    files, assets = make_release(tmp_path, "0.2.0", manifest_version="0.1.9")
    monkeypatch.setattr(fe, "fetch", lambda url, timeout=20: files[url])
    monkeypatch.setattr(fe, "ADDIN_DIR", str(addin))
    try:
        fe.install_release("0.2.0", assets)
    except RuntimeError:
        pass
    else:
        raise AssertionError("mismatched archive was installed")
    assert (addin / "FusionExport.py").read_text() == "# old code\n"
