#!/usr/bin/env python3
"""Synchronize Homey's bundled ARM64 Tailscale binaries with upstream stable."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "bin" / "aarch64"
RELEASE_URL = "https://api.github.com/repos/tailscale/tailscale/releases/latest"
PACKAGE_URL = "https://pkgs.tailscale.com/stable/tailscale_{version}_arm64.tgz"
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
MAX_ARCHIVE_SIZE = 100 * 1024 * 1024


def checked_stable_version(tag: str) -> str:
    """Only accept stable vMAJOR.MINOR.PATCH tags (Tailscale stable minors are even)."""
    match = VERSION_RE.fullmatch(tag)
    if not match or int(match.group(2)) % 2:
        raise ValueError(f"Not a stable Tailscale release tag: {tag!r}")
    return ".".join(match.groups())


def get_latest_stable() -> str:
    req = urllib.request.Request(
        RELEASE_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "homey-tailscale-sync"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        release = json.load(response)
    if release.get("prerelease") or release.get("draft"):
        raise ValueError("GitHub latest release is not stable")
    return checked_stable_version(release["tag_name"])


def fetch_tarball(version: str) -> bytes:
    url = PACKAGE_URL.format(version=version)
    req = urllib.request.Request(url, headers={"User-Agent": "homey-tailscale-sync"})
    with urllib.request.urlopen(req, timeout=120) as response:
        payload = response.read(MAX_ARCHIVE_SIZE + 1)
    if len(payload) > MAX_ARCHIVE_SIZE:
        raise ValueError("Upstream archive exceeds size limit")
    return payload


def binaries_from_tarball(archive: bytes, version: str) -> dict[str, bytes]:
    """Read only the two expected files; never extract arbitrary archive entries."""
    prefix = f"tailscale_{version}_arm64/"
    binaries = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for name in ("tailscale", "tailscaled"):
            member = tar.getmember(prefix + name)
            if not member.isfile() or member.size < 1_000_000:
                raise ValueError(f"Missing or unexpectedly small binary: {name}")
            src = tar.extractfile(member)
            if src is None:
                raise ValueError(f"Cannot read {name}")
            data = src.read()
            # ELF 64-bit, little endian, machine 183 (AArch64)
            if not (data[:6] == b"\x7fELF\x02\x01" and data[18:20] == b"\xb7\x00"):
                raise ValueError(f"Upstream {name} is not an AArch64 ELF executable")
            binaries[name] = data
    return binaries


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sync(version: str) -> bool:
    version_file = TARGET / "VERSION"
    previous = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else ""
    if previous == version:
        print(f"Already on Tailscale {version}")
        return False

    archive = fetch_tarball(version)
    binaries = binaries_from_tarball(archive, version)
    for name, payload in binaries.items():
        print(f"{name}: {len(payload):,} bytes SHA256 {hashlib.sha256(payload).hexdigest()}")
    # Only replace the bundled executables after both passed validation.
    for name, payload in binaries.items():
        atomic_write(TARGET / name, payload, mode=0o755)
    atomic_write(version_file, (version + "\n").encode())

    readme = ROOT / "README.md"
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        text = re.sub(
            r"(?m)^(- Binaries: `bin/aarch64/` Tailscale )\S+",
            lambda match: match.group(1) + version,
            text,
        )
        readme.write_text(text, encoding="utf-8")
    print(f"Updated Tailscale {previous or '(not recorded)'} -> {version}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Pin a specific stable version instead of latest")
    args = parser.parse_args()
    version = checked_stable_version(args.version) if args.version else get_latest_stable()
    sync(version)


if __name__ == "__main__":
    main()
