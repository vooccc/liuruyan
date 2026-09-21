#!/usr/bin/env python3
"""Bootstrap ripgrep into vendor/ripgrep/<arch-os>/ for the current platform.

下载并放置匹配当前机器架构的 ripgrep 二进制，使 backend.tools.builtin.file_tools._find_rg()
能在不依赖系统 PATH 的情况下直接使用。对齐 Claude Code 的 vendor/ripgrep/<arch-os>/ 约定。

Usage:
    python scripts/bootstrap_ripgrep.py                # current arch, skip if present
    python scripts/bootstrap_ripgrep.py --force        # re-download even if present
    python scripts/bootstrap_ripgrep.py --all          # all known platforms
    python scripts/bootstrap_ripgrep.py --targets x64-win32 arm64-darwin

Exit codes:
    0 = all requested targets installed (or already present)
    1 = at least one target failed
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = PROJECT_ROOT / "vendor" / "ripgrep"

RG_API_LATEST = "https://api.github.com/repos/BurntSushi/ripgrep/releases/latest"
USER_AGENT = "liuruyan-bootstrap-ripgrep/1.0"

# arch-os tag -> (asset triple substring, archive format)
PLATFORM_ASSETS: dict[str, Tuple[str, str]] = {
    "x64-win32":    ("x86_64-pc-windows-msvc",     "zip"),
    "arm64-win32":  ("aarch64-pc-windows-msvc",    "zip"),
    "ia32-win32":   ("i686-pc-windows-msvc",       "zip"),
    "x64-darwin":   ("x86_64-apple-darwin",        "tar.gz"),
    "arm64-darwin": ("aarch64-apple-darwin",       "tar.gz"),
    "x64-linux":    ("x86_64-unknown-linux-musl",  "tar.gz"),
    "arm64-linux":  ("aarch64-unknown-linux-gnu",  "tar.gz"),
}


def detect_arch_tag() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "ia32"
    else:
        arch = machine
    if sys.platform == "win32":
        plat = "win32"
    elif sys.platform == "darwin":
        plat = "darwin"
    else:
        plat = "linux"
    return f"{arch}-{plat}"


def bin_name_for(arch_tag: str) -> str:
    return "rg.exe" if arch_tag.endswith("-win32") else "rg"


def fetch_latest_release() -> dict:
    req = urllib.request.Request(RG_API_LATEST, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def find_asset(release: dict, triple: str, fmt: str) -> Optional[dict]:
    suffix = "." + fmt
    for a in release.get("assets", []):
        name = a.get("name", "")
        if triple in name and name.endswith(suffix):
            return a
    return None


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def extract_rg(data: bytes, fmt: str, dest_dir: Path, bin_name: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / bin_name
    if fmt == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            target = None
            for member in zf.namelist():
                if member.endswith("/" + bin_name) or member == bin_name:
                    target = member
                    break
            if target is None:
                raise RuntimeError(f"{bin_name} not found in zip archive")
            with zf.open(target) as src, open(dest_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    elif fmt == "tar.gz":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            target = None
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                if member.name.endswith("/" + bin_name) or member.name == bin_name:
                    target = member
                    break
            if target is None:
                raise RuntimeError(f"{bin_name} not found in tar.gz archive")
            src = tf.extractfile(target)
            if src is None:
                raise RuntimeError(f"failed to read {target.name} from tar.gz")
            with open(dest_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        raise ValueError(f"unknown archive format: {fmt}")

    if os.name != "nt":
        st = os.stat(dest_path)
        os.chmod(dest_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest_path


def install_one(arch_tag: str, release: dict, force: bool) -> bool:
    if arch_tag not in PLATFORM_ASSETS:
        print(f"[skip] {arch_tag}: unknown arch tag")
        return False
    triple, fmt = PLATFORM_ASSETS[arch_tag]
    bname = bin_name_for(arch_tag)
    dest_dir = VENDOR_DIR / arch_tag
    dest_path = dest_dir / bname

    if dest_path.is_file() and not force:
        print(f"[ok]   {arch_tag}: already present ({dest_path})")
        return True

    asset = find_asset(release, triple, fmt)
    if not asset:
        print(f"[miss] {arch_tag}: no asset matching '{triple}.{fmt}' in {release.get('tag_name')}")
        return False

    url = asset["browser_download_url"]
    print(f"[get]  {arch_tag}: {asset['name']}")
    try:
        data = http_get(url)
    except urllib.error.URLError as e:
        print(f"[fail] {arch_tag}: download error: {e}")
        return False
    try:
        extract_rg(data, fmt, dest_dir, bname)
    except Exception as e:
        print(f"[fail] {arch_tag}: extract error: {e}")
        return False
    size_mb = dest_path.stat().st_size / (1024 * 1024)
    print(f"[done] {arch_tag}: -> {dest_path} ({size_mb:.2f} MB)")
    return True


def verify(arch_tag: str) -> None:
    bname = bin_name_for(arch_tag)
    rg = VENDOR_DIR / arch_tag / bname
    if not rg.is_file():
        print(f"[verify] {arch_tag}: MISSING")
        return
    if arch_tag != detect_arch_tag():
        print(f"[verify] {arch_tag}: present (cross-arch, not executed here)")
        return
    try:
        out = subprocess.run([str(rg), "--version"], capture_output=True, text=True, timeout=10)
        first = (out.stdout or out.stderr).splitlines()[0] if (out.stdout or out.stderr) else "(no output)"
        print(f"[verify] {arch_tag}: {first}")
    except Exception as e:
        print(f"[verify] {arch_tag}: failed to run -- {e}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Bootstrap ripgrep into vendor/ripgrep/<arch-os>/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--force", action="store_true", help="re-download even if file exists")
    ap.add_argument("--all", action="store_true", help="download for all known platforms")
    ap.add_argument("--targets", nargs="*", metavar="ARCH-OS",
                    help="explicit arch tags (e.g. x64-win32 arm64-darwin)")
    args = ap.parse_args()

    if args.targets:
        targets = args.targets
    elif args.all:
        targets = list(PLATFORM_ASSETS.keys())
    else:
        targets = [detect_arch_tag()]

    print(f"project root: {PROJECT_ROOT}")
    print(f"vendor dir:   {VENDOR_DIR}")
    print(f"targets:      {targets}")
    print("fetching latest ripgrep release info...")
    try:
        release = fetch_latest_release()
    except Exception as e:
        print(f"[fatal] cannot reach GitHub API: {e}", file=sys.stderr)
        return 1
    print(f"latest:       {release.get('tag_name')}")
    print()

    ok_count = 0
    for t in targets:
        if install_one(t, release, args.force):
            ok_count += 1
    print()
    for t in targets:
        verify(t)
    print()
    print(f"summary: {ok_count}/{len(targets)} target(s) installed")
    return 0 if ok_count == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
