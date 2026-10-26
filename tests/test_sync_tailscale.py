import io
from pathlib import Path
import sys
import tarfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_tailscale


class SyncTailscaleTests(unittest.TestCase):
    def test_stable_version_rules(self):
        self.assertEqual(sync_tailscale.checked_stable_version("v1.104.2"), "1.104.2")
        for bad in ("v1.105.1", "v1.104.2-rc1", "unstable-v1.105.3", "../1.104.2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sync_tailscale.checked_stable_version(bad)

    def test_extract_only_expected_aarch64_binaries(self):
        version = "1.104.2"
        payload = b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\x00" + bytes(1_000_000)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name in ("tailscale", "tailscaled"):
                info = tarfile.TarInfo(f"tailscale_{version}_arm64/{name}")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        extracted = sync_tailscale.binaries_from_tarball(buffer.getvalue(), version)
        self.assertEqual(extracted, {"tailscale": payload, "tailscaled": payload})

    def test_reject_wrong_architecture(self):
        version = "1.104.2"
        payload = b"\x7fELF\x02\x01" + bytes(12) + b"\x3e\x00" + bytes(1_000_000)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name in ("tailscale", "tailscaled"):
                info = tarfile.TarInfo(f"tailscale_{version}_arm64/{name}")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        with self.assertRaises(ValueError):
            sync_tailscale.binaries_from_tarball(buffer.getvalue(), version)


if __name__ == "__main__":
    unittest.main()
