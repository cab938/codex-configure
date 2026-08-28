#!/usr/bin/env python3
"""Create the versioned Dynamic Core release archive and checksum."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path


EXECUTABLES = ("codex", "codex-code-mode-host")
TARGET = "linux-x86_64"
MINIMUM_GLIBC = "2.35"
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def tar_info(name: str, *, size: int, mode: int, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    if directory:
        info.type = tarfile.DIRTYPE
    return info


def add_path(bundle: tarfile.TarFile, source: Path, name: str, mode: int) -> None:
    info = tar_info(name, size=source.stat().st_size, mode=mode)
    with source.open("rb") as content:
        bundle.addfile(info, content)


def add_bytes(bundle: tarfile.TarFile, content: bytes, name: str, mode: int) -> None:
    info = tar_info(name, size=len(content), mode=mode)
    bundle.addfile(info, io.BytesIO(content))


def build_archive(
    *,
    release_directory: Path,
    output_directory: Path,
    version: str,
    target: str,
    upstream_commit: str,
    patch_file: Path,
    license_file: Path,
    notice_file: Path,
) -> tuple[Path, Path]:
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid release version: {version}")
    if target != TARGET:
        raise ValueError(f"unsupported release target: {target}")
    if not _COMMIT_RE.fullmatch(upstream_commit):
        raise ValueError("upstream commit must be a lowercase 40-character SHA")
    for source in (patch_file, license_file, notice_file):
        if not source.is_file() or not source.stat().st_size:
            raise ValueError(f"required release input is missing or empty: {source}")

    binaries = {name: release_directory / name for name in EXECUTABLES}
    for name, path in binaries.items():
        if not path.is_file() or not os.access(path, os.X_OK) or not path.stat().st_size:
            raise ValueError(f"release executable is missing, empty, or non-executable: {path}")

    stem = f"codex-configure-core-{version}-{target}"
    archive = output_directory / f"{stem}.tar.gz"
    checksum = output_directory / f"{archive.name}.sha256"
    manifest = {
        "schema_version": 1,
        "package_version": version,
        "target": target,
        "minimum_glibc": MINIMUM_GLIBC,
        "upstream_commit": upstream_commit,
        "patch_sha256": file_sha256(patch_file),
        "files": {
            name: {"sha256": file_sha256(path), "size": path.stat().st_size}
            for name, path in binaries.items()
        },
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    output_directory.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{archive.name}.", dir=output_directory, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            with gzip.GzipFile(filename="", mode="wb", fileobj=temporary, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as bundle:
                    bundle.addfile(tar_info(stem, size=0, mode=0o755, directory=True))
                    for name in EXECUTABLES:
                        add_path(bundle, binaries[name], f"{stem}/{name}", 0o755)
                    add_bytes(bundle, manifest_bytes, f"{stem}/manifest.json", 0o644)
                    add_path(bundle, license_file, f"{stem}/LICENSE", 0o644)
                    add_path(bundle, notice_file, f"{stem}/NOTICE", 0o644)
        assert temporary_path is not None
        os.replace(temporary_path, archive)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    checksum_text = f"{file_sha256(archive)}  {archive.name}\n"
    checksum.write_text(checksum_text, encoding="ascii")
    return archive, checksum


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--release-dir", required=True, type=Path)
    result.add_argument("--output-dir", required=True, type=Path)
    result.add_argument("--version", required=True)
    result.add_argument("--target", default=TARGET)
    result.add_argument("--upstream-commit", required=True)
    result.add_argument("--patch-file", required=True, type=Path)
    result.add_argument("--license-file", required=True, type=Path)
    result.add_argument("--notice-file", required=True, type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        archive, checksum = build_archive(
            release_directory=args.release_dir.resolve(),
            output_directory=args.output_dir.resolve(),
            version=args.version,
            target=args.target,
            upstream_commit=args.upstream_commit,
            patch_file=args.patch_file.resolve(),
            license_file=args.license_file.resolve(),
            notice_file=args.notice_file.resolve(),
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"package_core_release.py: {exc}") from exc
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
