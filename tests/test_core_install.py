from __future__ import annotations

import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path

from codex_configure.core_install import CoreInstaller
from codex_configure.errors import UserFacingError
from codex_configure.patcher import PatchResources


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = REPOSITORY_ROOT / "scripts" / "package_core_release.py"
SPEC = importlib.util.spec_from_file_location("package_core_release", PACKAGE_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PACKAGE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE_MODULE)


class CoreInstallerTests(unittest.TestCase):
    def make_release(
        self,
        root: Path,
        *,
        version: str = "0.3.0",
        upstream_commit: str | None = None,
    ) -> tuple[Path, Path, dict[str, bytes]]:
        release_directory = root / "release-bin"
        release_directory.mkdir()
        contents = {
            "codex": b"#!/bin/sh\necho codex test\n",
            "codex-code-mode-host": b"#!/bin/sh\necho host test\n",
        }
        for name, content in contents.items():
            path = release_directory / name
            path.write_bytes(content)
            path.chmod(0o700)

        resources = PatchResources.discover()
        assets = root / "releases" / f"v{version}"
        archive, checksum = PACKAGE_MODULE.build_archive(
            release_directory=release_directory,
            output_directory=assets,
            version=version,
            target="linux-x86_64",
            upstream_commit=upstream_commit or resources.upstream_commit,
            patch_file=resources.patch_file,
            license_file=REPOSITORY_ROOT / "LICENSE",
            notice_file=REPOSITORY_ROOT / "NOTICE",
        )
        return archive, checksum, contents

    def test_installs_verified_release_and_reuses_it_without_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, checksum, contents = self.make_release(root)
            home = root / "home"
            installer = CoreInstaller(
                home=home,
                version="0.3.0",
                release_base_url=(root / "releases").as_uri(),
                platform_name="Linux",
                machine="x86_64",
                libc_name="glibc",
                libc_version="2.35",
            )

            result = installer.install()

            self.assertFalse(result.reused)
            self.assertEqual(result.binary_path.read_bytes(), contents["codex"])
            self.assertEqual(
                result.code_mode_host_path.read_bytes(), contents["codex-code-mode-host"]
            )
            current = home / ".codex-configure" / "cores" / "current"
            self.assertTrue(current.is_symlink())
            self.assertEqual(current.resolve(), result.install_directory)
            self.assertEqual(stat.S_IMODE(result.install_directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(result.binary_path.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((result.install_directory / "manifest.json").stat().st_mode),
                0o600,
            )

            archive.unlink()
            checksum.unlink()
            reused = installer.install()
            self.assertTrue(reused.reused)
            self.assertEqual(reused.install_directory, result.install_directory)

    def test_rejects_archive_with_wrong_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, checksum, _ = self.make_release(root)
            checksum.write_text("0" * 64 + "\n", encoding="ascii")
            home = root / "home"
            installer = CoreInstaller(
                home=home,
                version="0.3.0",
                release_base_url=(root / "releases").as_uri(),
                platform_name="Linux",
                machine="amd64",
                libc_name="glibc",
                libc_version="2.35",
            )

            with self.assertRaisesRegex(UserFacingError, "SHA-256"):
                installer.install()

            installed = home / ".codex-configure" / "cores" / installer.asset_stem
            self.assertFalse(installed.exists())

    def test_rejects_manifest_for_a_different_upstream_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_release(root, upstream_commit="0" * 40)
            installer = CoreInstaller(
                home=root / "home",
                version="0.3.0",
                release_base_url=(root / "releases").as_uri(),
                platform_name="Linux",
                machine="x86_64",
                libc_name="glibc",
                libc_version="2.35",
            )

            with self.assertRaisesRegex(UserFacingError, "packaged upstream pin"):
                installer.install()

    def test_rejects_unsupported_platform_before_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = CoreInstaller(
                home=Path(temporary),
                platform_name="Darwin",
                machine="arm64",
            )
            with self.assertRaisesRegex(UserFacingError, "Linux x86_64"):
                installer.install()


if __name__ == "__main__":
    unittest.main()
