from __future__ import annotations

import importlib.util
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from codex_configure.patcher import PatchResources


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = REPOSITORY_ROOT / "scripts" / "package_core_release.py"
SPEC = importlib.util.spec_from_file_location("package_core_release_macos_test", PACKAGE_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PACKAGE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE_MODULE)


class PackageCoreReleaseTests(unittest.TestCase):
    def test_packages_macos_arm64_without_linux_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release_directory = root / "release"
            release_directory.mkdir()
            for name in PACKAGE_MODULE.EXECUTABLES:
                executable = release_directory / name
                executable.write_bytes(b"#!/bin/sh\nexit 0\n")
                executable.chmod(0o700)

            resources = PatchResources.discover()
            archive, checksum = PACKAGE_MODULE.build_archive(
                release_directory=release_directory,
                output_directory=root / "dist",
                version="0.3.0",
                target="macos-arm64",
                upstream_commit=resources.upstream_commit,
                patch_file=resources.patch_file,
                license_file=REPOSITORY_ROOT / "LICENSE",
                notice_file=REPOSITORY_ROOT / "NOTICE",
            )

            with tarfile.open(archive, "r:gz") as bundle:
                manifest_file = bundle.extractfile(
                    "codex-configure-core-0.3.0-macos-arm64/manifest.json"
                )
                assert manifest_file is not None
                manifest = json.load(manifest_file)

            self.assertEqual(manifest["target"], "macos-arm64")
            self.assertNotIn("minimum_glibc", manifest)
            self.assertEqual(checksum.read_text(encoding="ascii").split()[1], archive.name)


if __name__ == "__main__":
    unittest.main()
