import json
import stat
import pytest

from scripts.recovery import backup, restore


def test_synthetic_backup_manifest_and_no_clobber_restore(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    (source / "qbo_tokens.json").write_text('{"synthetic":true}\n')
    (source / "qbo_operations.json").write_text('{"operations":[]}\n')
    (source / "qbo_push.json").write_text('{"subscriptions":[]}\n')
    (source / "qbo_audit.json").write_text('{"events":[]}\n')
    archive = tmp_path / "backup"
    manifest_path = backup(source, archive)
    manifest = json.loads(manifest_path.read_text())
    expected = {"qbo_tokens.json", "qbo_operations.json", "qbo_push.json", "qbo_audit.json"}
    assert {item["name"] for item in manifest["files"]} == expected
    assert stat.S_IMODE((archive / "qbo_tokens.json").stat().st_mode) == 0o600
    restored = tmp_path / "restored"
    assert set(restore(archive, restored)) == expected
    for name in expected:
        assert (restored / name).read_bytes() == (source / name).read_bytes()
        assert stat.S_IMODE((restored / name).stat().st_mode) == 0o600
    assert (restored / "qbo_tokens.json").read_text() == (source / "qbo_tokens.json").read_text()
    try:
        restore(archive, restored)
    except FileExistsError:
        pass
    else:
        raise AssertionError("restore must refuse to clobber by default")


def test_restore_rejects_traversal_before_writing(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    (source / "token").write_text("synthetic-token")
    archive = tmp_path / "backup"; backup(source, archive, names=("token",))
    manifest = json.loads((archive / "manifest.json").read_text())
    manifest["files"][0]["name"] = "../escaped"
    (archive / "manifest.json").write_text(json.dumps(manifest))
    destination = tmp_path / "restore"
    try:
        restore(archive, destination)
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal manifest must be rejected")
    assert not destination.exists()


def test_backup_rejects_empty_source_links_and_unsafe_names(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    archive = tmp_path / "backup"
    with pytest.raises(ValueError, match="no local app data"):
        backup(source, archive)
    assert not archive.exists()
    (tmp_path / "secret").write_text("synthetic")
    for name in ("../secret", "..", ".", "manifest.json", "other\\secret"):
        with pytest.raises(ValueError, match="unsafe filename"):
            backup(source, archive, names=(name,))
    (source / "qbo_tokens.json").symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError, match="link or non-file"):
        backup(source, archive)
    assert not archive.exists()


def test_restore_checks_all_files_before_writing_and_rejects_links(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    (source / "qbo_tokens.json").write_text("synthetic-token")
    (source / "qbo_operations.json").write_text("synthetic-journal")
    archive = tmp_path / "backup"; backup(source, archive)
    (archive / "qbo_operations.json").write_text("changed")
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="integrity"):
        restore(archive, destination)
    assert not destination.exists()
    (archive / "qbo_operations.json").unlink()
    (archive / "qbo_operations.json").symlink_to(source / "qbo_operations.json")
    with pytest.raises(ValueError, match="integrity"):
        restore(archive, destination)
    assert not destination.exists()


def test_supplied_root_links_cannot_redirect_backup_or_restore(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    (source / "qbo_tokens.json").write_text("synthetic")
    source_link = tmp_path / "source-link"; source_link.symlink_to(source)
    archive = tmp_path / "archive"
    with pytest.raises(ValueError, match="symbolic links"):
        backup(source_link, archive)
    assert not archive.exists()
    backup(source, archive)
    archive_link = tmp_path / "archive-link"; archive_link.symlink_to(archive)
    target = tmp_path / "target"; target.mkdir()
    target_link = tmp_path / "target-link"; target_link.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic links"):
        backup(source, target_link)
    with pytest.raises(ValueError, match="symbolic links"):
        restore(archive, target_link)
    with pytest.raises(ValueError, match="symbolic links"):
        restore(archive_link, target)
    assert list(target.iterdir()) == []
