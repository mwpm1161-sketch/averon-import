from __future__ import annotations

import zipfile
from pathlib import Path

from averon_import import __version__
from scripts.build_release import (
    MANIFEST_NAME,
    build_release,
    collect_release_sources,
    generate_manifest,
    parse_manifest,
    read_project_version,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_release_fixture(root: Path) -> None:
    root_files = (
        "README.md",
        "START_HERE.txt",
        "CHANGELOG.md",
        "NOTICE.md",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
        "requirements.txt",
        "run.py",
        "build_windows.bat",
        "setup_windows.bat",
        "start_windows.bat",
        "install_accurate_ocr_models.bat",
    )
    for relative_name in root_files:
        path = root / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'version = "1.0.0rc7"\n' if relative_name == "pyproject.toml" else "fixture\n",
            encoding="utf-8",
        )
    for relative_name in (
        "scripts/check_environment.py",
        "scripts/install_accurate_ocr_models.ps1",
        "averon_import/__init__.py",
        "averon_import/services/runtime.py",
        "docs/USER_GUIDE.md",
    ):
        path = root / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")


def test_manifest_is_sorted_deterministic_and_not_self_referential() -> None:
    entries = [("z.txt", b"z"), ("a.txt", b"a")]

    assert generate_manifest(entries) == generate_manifest(reversed(entries))
    manifest = generate_manifest(entries).decode("utf-8")
    assert manifest.splitlines()[0].endswith("  a.txt")
    assert MANIFEST_NAME not in manifest
    assert set(parse_manifest(manifest.encode("utf-8"))) == {"a.txt", "z.txt"}


def test_release_source_selection_excludes_dev_and_runtime_files(tmp_path: Path) -> None:
    _write_release_fixture(tmp_path)
    (tmp_path / "averon_import" / "__pycache__").mkdir()
    (tmp_path / "averon_import" / "__pycache__" / "bad.pyc").write_bytes(b"cache")
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_bytes(b"runtime")
    (tmp_path / ".pytest_cache" / "lastfailed").mkdir(parents=True)
    (tmp_path / ".pytest_cache" / "lastfailed" / "data").write_text("cache")
    (tmp_path / "tmp" / "local.json").mkdir(parents=True)
    (tmp_path / "tmp" / "local.json" / "data").write_text("runtime")
    (tmp_path / "setup_log.txt").write_text("secret-ish setup output")
    (tmp_path / "averon_import" / "secrets.json").write_text("runtime secret")
    (tmp_path / "averon_import" / "local.sqlite3").write_bytes(b"runtime store")
    (tmp_path / "tests" / "test_local.py").parent.mkdir()
    (tmp_path / "tests" / "test_local.py").write_text("test")

    selected = {
        path.relative_to(tmp_path).as_posix() for path in collect_release_sources(tmp_path)
    }

    assert "averon_import/services/runtime.py" in selected
    assert "averon_import/__pycache__/bad.pyc" not in selected
    assert ".venv/Scripts/python.exe" not in selected
    assert ".pytest_cache/lastfailed/data" not in selected
    assert "tmp/local.json/data" not in selected
    assert "setup_log.txt" not in selected
    assert "averon_import/secrets.json" not in selected
    assert "averon_import/local.sqlite3" not in selected
    assert "tests/test_local.py" not in selected


def test_built_archive_and_manifest_cover_the_same_files(tmp_path: Path) -> None:
    _write_release_fixture(tmp_path)
    output_path, manifest_path, file_count = build_release(
        tmp_path,
        output_path=tmp_path / "out" / "release.zip",
        manifest_path=tmp_path / "out" / MANIFEST_NAME,
    )

    with zipfile.ZipFile(output_path) as archive:
        names = set(archive.namelist())
        assert MANIFEST_NAME in names
        assert all(not name.startswith((".git/", ".venv/", ".pytest_", "__pycache__/")) for name in names)
        assert "setup_log.txt" not in names
        assert file_count == len(names) - 1
        assert manifest_path.read_bytes() == archive.read(MANIFEST_NAME)

    assert set(parse_manifest(manifest_path.read_bytes())) == names - {MANIFEST_NAME}


def test_metadata_version_matches_human_runtime_version() -> None:
    metadata_version = read_project_version(ROOT)
    assert metadata_version.replace("-", "") == __version__.replace("-", "")
