"""Build a small, deterministic source release archive.

The archive is intentionally assembled from an explicit set of runtime paths.
That keeps local Git metadata, virtual environments, caches, tests, and runtime
data out of the distributable without requiring a packaging framework.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable, Iterator, Sequence


MANIFEST_NAME = "RELEASE_MANIFEST.sha256"

ROOT_RELEASE_FILES = (
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
RELEASE_DIRECTORIES = ("averon_import", "docs")
RELEASE_SCRIPT_FILES = (
    "scripts/check_environment.py",
    "scripts/install_accurate_ocr_models.ps1",
)

EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    "venv",
    ".pytest_cache",
    "__pycache__",
    "tmp",
    "temp",
    "workspace",
    "workspaces",
    "dist",
    "build",
}
EXCLUDED_FILE_NAMES = {
    MANIFEST_NAME,
    "setup_log.txt",
    "credentials.json",
    "secrets.json",
    "settings.local.json",
}
EXCLUDED_FILE_SUFFIXES = {".db", ".log", ".pyc", ".pyo", ".sqlite", ".sqlite3"}
TEXT_SUFFIXES = {
    ".bat",
    ".cmd",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
BINARY_SUFFIXES = {
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".traineddata",
    ".zip",
}


def _is_excluded(relative_path: Path) -> bool:
    """Return whether a source path belongs to local/dev-only data."""

    parts = relative_path.parts
    if any(
        part in EXCLUDED_DIRECTORY_NAMES or part.startswith(".pytest-")
        for part in parts[:-1]
    ):
        return True

    name = relative_path.name
    if name in EXCLUDED_FILE_NAMES or name.startswith(".env"):
        return True
    if relative_path.suffix.lower() in EXCLUDED_FILE_SUFFIXES:
        return True
    return False


def _iter_directory_files(directory: Path, project_root: Path) -> Iterator[Path]:
    """Yield regular files below *directory* in deterministic order."""

    for current, directories, filenames in os.walk(directory):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if not _is_excluded((current_path / name).relative_to(project_root))
        )
        for filename in sorted(filenames):
            path = current_path / filename
            relative_path = path.relative_to(project_root)
            if path.is_file() and not path.is_symlink() and not _is_excluded(relative_path):
                yield path


def collect_release_sources(project_root: Path) -> list[Path]:
    """Return the explicit, runtime-only source set for the release."""

    project_root = project_root.resolve()
    candidates: list[Path] = []

    for relative_name in ROOT_RELEASE_FILES + RELEASE_SCRIPT_FILES:
        path = project_root / relative_name
        if not path.is_file():
            raise FileNotFoundError(f"Required release file is missing: {relative_name}")
        candidates.append(path)

    for relative_name in RELEASE_DIRECTORIES:
        directory = project_root / relative_name
        if not directory.is_dir():
            raise FileNotFoundError(f"Required release directory is missing: {relative_name}")
        candidates.extend(_iter_directory_files(directory, project_root))

    unique_paths = {path.resolve() for path in candidates if not _is_excluded(path.relative_to(project_root))}
    return sorted(unique_paths, key=lambda path: path.relative_to(project_root).as_posix())


def normalize_release_bytes(relative_path: Path, data: bytes) -> bytes:
    """Make release text deterministic while leaving binary data untouched."""

    if relative_path.suffix.lower() in BINARY_SUFFIXES or b"\x00" in data[:8192]:
        return data
    if relative_path.suffix.lower() not in TEXT_SUFFIXES:
        return data

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if relative_path.suffix.lower() in {".bat", ".cmd"}:
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def generate_manifest(entries: Iterable[tuple[str, bytes]]) -> bytes:
    """Generate sorted SHA-256 entries without including the manifest itself."""

    lines = []
    for relative_name, data in sorted(entries, key=lambda item: item[0]):
        if relative_name == MANIFEST_NAME:
            continue
        digest = hashlib.sha256(data).hexdigest()
        lines.append(f"{digest}  {relative_name}\n")
    return "".join(lines).encode("utf-8")


def _staged_entries(stage_directory: Path) -> list[tuple[str, bytes]]:
    entries = []
    for path in sorted(stage_directory.rglob("*"), key=lambda item: item.relative_to(stage_directory).as_posix()):
        if path.is_file():
            entries.append((path.relative_to(stage_directory).as_posix(), path.read_bytes()))
    return entries


def _write_deterministic_zip(stage_directory: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        if not output_path.is_file():
            raise IsADirectoryError(f"Release output is not a file: {output_path}")
        output_path.unlink()

    with zipfile.ZipFile(
        output_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for relative_name, data in _staged_entries(stage_directory):
            info = zipfile.ZipInfo(relative_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)


def parse_manifest(data: bytes) -> dict[str, str]:
    """Parse the project's two-space SHA-256 manifest format."""

    entries: dict[str, str] = {}
    for line_number, raw_line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            digest, relative_name = raw_line.split("  ", 1)
        except ValueError as exc:
            raise ValueError(f"Malformed manifest line {line_number}") from exc
        if relative_name == MANIFEST_NAME:
            raise ValueError("Manifest must not contain its own hash")
        if relative_name in entries:
            raise ValueError(f"Duplicate manifest entry: {relative_name}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid SHA-256 digest on line {line_number}")
        entries[relative_name] = digest
    return entries


def verify_archive(archive_path: Path, external_manifest_path: Path | None = None) -> int:
    """Verify archive contents against its generated manifest and return a count."""

    with zipfile.ZipFile(archive_path, mode="r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Release archive contains duplicate paths")
        if MANIFEST_NAME not in names:
            raise ValueError("Release archive does not contain RELEASE_MANIFEST.sha256")

        manifest_data = archive.read(MANIFEST_NAME)
        if external_manifest_path is not None and external_manifest_path.read_bytes() != manifest_data:
            raise ValueError("External manifest does not match the manifest in the release archive")

        manifest_entries = parse_manifest(manifest_data)
        archive_files = {name for name in names if name != MANIFEST_NAME and not name.endswith("/")}
        if set(manifest_entries) != archive_files:
            missing = sorted(archive_files - set(manifest_entries))
            extra = sorted(set(manifest_entries) - archive_files)
            raise ValueError(f"Manifest/archive mismatch; missing={missing}, extra={extra}")

        for relative_name, expected_digest in manifest_entries.items():
            actual_digest = hashlib.sha256(archive.read(relative_name)).hexdigest()
            if actual_digest != expected_digest:
                raise ValueError(f"SHA-256 mismatch for {relative_name}")
        return len(manifest_entries)


def read_project_version(project_root: Path) -> str:
    """Read the PEP 440 project version from pyproject.toml without dependencies."""

    content = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    matches = re.findall(r"(?m)^version\s*=\s*[\"']([^\"']+)[\"']\s*$", content)
    if len(matches) != 1:
        raise ValueError("pyproject.toml must contain exactly one project version")
    return matches[0]


def build_release(
    project_root: Path,
    output_path: Path | None = None,
    manifest_path: Path | None = None,
) -> tuple[Path, Path, int]:
    """Build and verify a release archive, returning paths and file count."""

    project_root = project_root.resolve()
    version = read_project_version(project_root)
    output_path = output_path or project_root / "dist" / f"averon-import-{version}.zip"
    manifest_path = manifest_path or project_root / MANIFEST_NAME
    output_path = output_path.resolve()
    manifest_path = manifest_path.resolve()
    sources = collect_release_sources(project_root)

    with tempfile.TemporaryDirectory(prefix=".release-stage-", dir=project_root) as temporary_directory:
        stage_directory = Path(temporary_directory)
        for source_path in sources:
            relative_path = source_path.relative_to(project_root)
            destination = stage_directory / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(normalize_release_bytes(relative_path, source_path.read_bytes()))

        manifest_data = generate_manifest(
            (relative_name, data)
            for relative_name, data in _staged_entries(stage_directory)
        )
        (stage_directory / MANIFEST_NAME).write_bytes(manifest_data)
        _write_deterministic_zip(stage_directory, output_path)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_data)
    file_count = verify_archive(output_path, manifest_path)
    return output_path, manifest_path, file_count


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="ZIP output path")
    parser.add_argument("--manifest", type=Path, help="external manifest output path")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="build the archive and run the explicit archive verification step",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    output_path, manifest_path, file_count = build_release(
        project_root,
        output_path=args.output,
        manifest_path=args.manifest,
    )
    if args.verify:
        file_count = verify_archive(output_path, manifest_path)
    print(f"Release archive: {output_path}")
    print(f"Manifest: {manifest_path}")
    print(f"Files in archive (excluding manifest): {file_count}")
    print(f"Archive size: {output_path.stat().st_size} bytes")
    print("Manifest verification: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
