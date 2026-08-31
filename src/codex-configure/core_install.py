"""Install a verified prebuilt Dynamic Picker Core release."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

from . import __version__
from .errors import UserFacingError
from .patcher import PatchResources


RELEASE_BASE_URL = "https://github.com/cab938/codex-configure/releases/download"
CORES_DIRECTORY = Path(".codex-configure/cores")
TARGET = "linux-x86_64"
MACOS_TARGET = "macos-arm64"
SUPPORTED_TARGETS = {TARGET, MACOS_TARGET}
MINIMUM_GLIBC = "2.35"
EXECUTABLES = ("codex", "codex-code-mode-host")
METADATA_FILES = ("manifest.json", "LICENSE", "NOTICE")
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 3 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


@dataclass(frozen=True)
class InstallResult:
    install_directory: Path
    binary_path: Path
    code_mode_host_path: Path
    version: str
    target: str
    reused: bool


class CoreInstaller:
    """Download and atomically activate one versioned Core bundle."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        version: str = __version__,
        release_base_url: str = RELEASE_BASE_URL,
        platform_name: str | None = None,
        machine: str | None = None,
        libc_name: str | None = None,
        libc_version: str | None = None,
        opener: Callable[..., BinaryIO] = urllib.request.urlopen,
    ) -> None:
        self.home = Path(home).expanduser().resolve() if home is not None else Path.home()
        self.version = version
        self.release_base_url = release_base_url.rstrip("/")
        self.platform_name = platform_name or platform.system()
        self.machine = (machine or platform.machine()).casefold()
        detected_libc_name, detected_libc_version = platform.libc_ver()
        self.libc_name = libc_name if libc_name is not None else detected_libc_name
        self.libc_version = libc_version if libc_version is not None else detected_libc_version
        self.opener = opener

    @staticmethod
    def default_root(home: Path | None = None) -> Path:
        base = Path(home).expanduser() if home is not None else Path.home()
        return (base / CORES_DIRECTORY).absolute()

    @classmethod
    def current_binary(cls, home: Path | None = None) -> Path:
        return cls.default_root(home) / "current" / "codex"

    @classmethod
    def versioned_directory(
        cls,
        home: Path | None = None,
        version: str = __version__,
        target: str = TARGET,
    ) -> Path:
        if target not in SUPPORTED_TARGETS:
            raise ValueError(f"unsupported Dynamic Core target: {target}")
        return cls.default_root(home) / f"codex-configure-core-{version}-{target}"

    @property
    def target(self) -> str:
        system = self.platform_name.casefold()
        if system == "darwin" and self.machine in {"arm64", "aarch64"}:
            return MACOS_TARGET
        if system == "linux" and self.machine in {"x86_64", "amd64"}:
            try:
                glibc_version = tuple(int(part) for part in self.libc_version.split(".")[:2])
            except ValueError:
                glibc_version = ()
            if self.libc_name.casefold() != "glibc" or glibc_version < (2, 35):
                raise UserFacingError(
                    f"Prebuilt Dynamic Core requires glibc {MINIMUM_GLIBC} or newer. "
                    "Use `codex-configure patch` for a local source build."
                )
            return TARGET
        raise UserFacingError(
            "Prebuilt Dynamic Core is available for Linux x86_64 and macOS Apple Silicon. "
            "Use `codex-configure patch` for a local source build."
        )

    @property
    def asset_stem(self) -> str:
        return f"codex-configure-core-{self.version}-{self.target}"

    @property
    def asset_name(self) -> str:
        return f"{self.asset_stem}.tar.gz"

    def install(self) -> InstallResult:
        if not _VERSION_RE.fullmatch(self.version):
            raise UserFacingError(f"Invalid codex-configure release version: {self.version}")
        target = self.target
        root = self.default_root(self.home)
        self._prepare_root(root)
        destination = self.versioned_directory(self.home, self.version, target)

        if destination.exists() or destination.is_symlink():
            self._validate_install(destination, target)
            self._set_permissions(destination)
            self._activate(root, destination)
            return self._result(destination, target, reused=True)

        archive_url = f"{self.release_base_url}/v{self.version}/{self.asset_name}"
        checksum_url = f"{archive_url}.sha256"
        expected_archive_sha = self._fetch_checksum(checksum_url)

        try:
            with tempfile.TemporaryDirectory(prefix=".core-install-", dir=root) as temporary:
                work = Path(temporary)
                archive = work / self.asset_name
                actual_archive_sha = self._download(archive_url, archive)
                if actual_archive_sha != expected_archive_sha:
                    raise UserFacingError(
                        "Dynamic Core download failed its SHA-256 check; nothing was installed."
                    )
                extracted = self._extract(archive, work)
                self._validate_install(extracted, target)
                self._set_permissions(extracted)
                try:
                    extracted.rename(destination)
                except FileExistsError as exc:
                    raise UserFacingError(
                        f"Dynamic Core destination appeared during installation: {destination}"
                    ) from exc
                except OSError as exc:
                    raise UserFacingError(
                        f"Could not install Dynamic Core at {destination}: {exc}"
                    ) from exc
        except UserFacingError:
            raise
        except OSError as exc:
            raise UserFacingError(f"Could not prepare Dynamic Core installation: {exc}") from exc

        self._activate(root, destination)
        return self._result(destination, target, reused=False)

    def _result(self, destination: Path, target: str, *, reused: bool) -> InstallResult:
        return InstallResult(
            install_directory=destination,
            binary_path=destination / "codex",
            code_mode_host_path=destination / "codex-code-mode-host",
            version=self.version,
            target=target,
            reused=reused,
        )

    def _prepare_root(self, root: Path) -> None:
        if root.is_symlink():
            raise UserFacingError(f"Refusing symlinked Dynamic Core directory: {root}")
        try:
            root.mkdir(parents=True, mode=0o700, exist_ok=True)
            root.chmod(0o700)
        except OSError as exc:
            raise UserFacingError(f"Could not create Dynamic Core directory {root}: {exc}") from exc
        if not root.is_dir():
            raise UserFacingError(f"Dynamic Core path is not a directory: {root}")
        current = root / "current"
        if (current.exists() or current.is_symlink()) and not current.is_symlink():
            raise UserFacingError(f"Refusing to replace non-symlink Dynamic Core pointer: {current}")

    def _fetch_checksum(self, url: str) -> str:
        try:
            with self.opener(url, timeout=30) as response:
                data = response.read(4097)
        except (OSError, urllib.error.URLError) as exc:
            raise UserFacingError(f"Could not download Dynamic Core checksum: {exc}") from exc
        if len(data) > 4096:
            raise UserFacingError("Dynamic Core checksum file is unexpectedly large.")
        try:
            fields = data.decode("ascii").strip().split()
        except UnicodeDecodeError as exc:
            raise UserFacingError("Dynamic Core checksum file is not ASCII text.") from exc
        if len(fields) not in {1, 2} or not _SHA256_RE.fullmatch(fields[0]):
            raise UserFacingError("Dynamic Core checksum file is malformed.")
        if len(fields) == 2 and fields[1].lstrip("*") != self.asset_name:
            raise UserFacingError("Dynamic Core checksum names a different release asset.")
        return fields[0]

    def _download(self, url: str, destination: Path) -> str:
        digest = hashlib.sha256()
        size = 0
        try:
            with self.opener(url, timeout=60) as response, destination.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise UserFacingError("Dynamic Core archive exceeds the supported size limit.")
                    output.write(chunk)
                    digest.update(chunk)
        except UserFacingError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise UserFacingError(f"Could not download Dynamic Core release: {exc}") from exc
        if not size:
            raise UserFacingError("Dynamic Core release archive is empty.")
        return digest.hexdigest()

    def _extract(self, archive: Path, work: Path) -> Path:
        root_name = self.asset_stem
        expected = [root_name, *(f"{root_name}/{name}" for name in (*EXECUTABLES, *METADATA_FILES))]
        extracted = work / root_name
        try:
            with tarfile.open(archive, mode="r:gz") as bundle:
                members = bundle.getmembers()
                names = [member.name for member in members]
                if names != expected:
                    raise UserFacingError(
                        "Dynamic Core archive has an unexpected or unsafe file layout."
                    )
                if not members[0].isdir() or any(not member.isreg() for member in members[1:]):
                    raise UserFacingError("Dynamic Core archive contains an unsupported entry type.")
                if any(
                    PurePosixPath(member.name).is_absolute() or ".." in PurePosixPath(member.name).parts
                    for member in members
                ):
                    raise UserFacingError("Dynamic Core archive contains an unsafe path.")
                expanded_size = sum(member.size for member in members[1:])
                if expanded_size > MAX_EXPANDED_BYTES:
                    raise UserFacingError("Dynamic Core archive expands beyond the supported size limit.")
                if any(member.size <= 0 for member in members[1:]):
                    raise UserFacingError("Dynamic Core archive contains an empty required file.")
                if any(member.size > MAX_METADATA_BYTES for member in members[3:]):
                    raise UserFacingError("Dynamic Core archive metadata exceeds the size limit.")

                extracted.mkdir(mode=0o700)
                for member in members[1:]:
                    source = bundle.extractfile(member)
                    if source is None:
                        raise UserFacingError(f"Could not read {member.name} from Dynamic Core archive.")
                    destination = extracted / PurePosixPath(member.name).name
                    with source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    if destination.stat().st_size != member.size:
                        raise UserFacingError(f"Dynamic Core archive entry is truncated: {member.name}")
        except UserFacingError:
            raise
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise UserFacingError(f"Could not unpack Dynamic Core release: {exc}") from exc
        return extracted

    def _validate_install(self, directory: Path, target: str) -> None:
        if directory.is_symlink() or not directory.is_dir():
            raise UserFacingError(f"Dynamic Core installation is not a regular directory: {directory}")
        expected_names = {*EXECUTABLES, *METADATA_FILES}
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise UserFacingError(f"Could not inspect Dynamic Core installation: {exc}") from exc
        if {entry.name for entry in entries} != expected_names or len(entries) != len(expected_names):
            raise UserFacingError(f"Dynamic Core installation has unexpected contents: {directory}")
        if any(entry.is_symlink() or not entry.is_file() for entry in entries):
            raise UserFacingError(f"Dynamic Core installation contains a non-regular file: {directory}")

        manifest_path = directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise UserFacingError(f"Dynamic Core manifest is unreadable: {exc}") from exc
        resources = PatchResources.discover()
        expected_patch_sha = _file_sha256(resources.patch_file)
        if not isinstance(manifest, dict):
            raise UserFacingError("Dynamic Core manifest is not a JSON object.")
        if manifest.get("schema_version") != 1:
            raise UserFacingError("Dynamic Core manifest has an unsupported schema version.")
        if manifest.get("package_version") != self.version or manifest.get("target") != target:
            raise UserFacingError("Dynamic Core manifest does not match this package and platform.")
        if target == TARGET:
            if manifest.get("minimum_glibc") != MINIMUM_GLIBC:
                raise UserFacingError("Dynamic Core manifest has an unexpected glibc baseline.")
        elif "minimum_glibc" in manifest:
            raise UserFacingError("Dynamic Core manifest has unexpected Linux metadata.")
        if (
            manifest.get("upstream_commit") != resources.upstream_commit
            or not _COMMIT_RE.fullmatch(str(manifest.get("upstream_commit", "")))
        ):
            raise UserFacingError("Dynamic Core manifest does not match the packaged upstream pin.")
        if manifest.get("patch_sha256") != expected_patch_sha:
            raise UserFacingError("Dynamic Core manifest does not match the packaged Core patch.")

        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != set(EXECUTABLES):
            raise UserFacingError("Dynamic Core manifest has an invalid executable list.")
        for name in EXECUTABLES:
            record = files.get(name)
            path = directory / name
            if not isinstance(record, dict):
                raise UserFacingError(f"Dynamic Core manifest has no valid record for {name}.")
            size = record.get("size")
            sha256 = record.get("sha256")
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
                or path.stat().st_size != size
                or not isinstance(sha256, str)
                or not _SHA256_RE.fullmatch(sha256)
                or _file_sha256(path) != sha256
            ):
                raise UserFacingError(f"Dynamic Core executable failed verification: {name}")
        for name in ("LICENSE", "NOTICE"):
            if not (directory / name).stat().st_size:
                raise UserFacingError(f"Dynamic Core release contains an empty {name} file.")

    def _set_permissions(self, directory: Path) -> None:
        try:
            directory.chmod(0o700)
            for name in EXECUTABLES:
                (directory / name).chmod(0o700)
            for name in METADATA_FILES:
                (directory / name).chmod(0o600)
        except OSError as exc:
            raise UserFacingError(f"Could not protect Dynamic Core installation: {exc}") from exc

    def _activate(self, root: Path, destination: Path) -> None:
        current = root / "current"
        if (current.exists() or current.is_symlink()) and not current.is_symlink():
            raise UserFacingError(f"Refusing to replace non-symlink Dynamic Core pointer: {current}")
        temporary = root / f".current-{uuid.uuid4().hex}"
        try:
            os.symlink(destination.name, temporary, target_is_directory=True)
            os.replace(temporary, current)
        except OSError as exc:
            raise UserFacingError(f"Could not activate Dynamic Core {destination}: {exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise UserFacingError(f"Could not hash {path}: {exc}") from exc
    return digest.hexdigest()
