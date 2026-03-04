#!/usr/bin/env python3
"""Build OPKG repositories from synced release assets (Phase 3)."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import sync_releases as sync


@dataclass
class PackageEntry:
    release_id: int
    release_tag: str
    published_at: str
    openwrt_version: str
    arch: str
    file_name: str
    source_path: Path
    sha256: str


def load_release_manifests(manifest_root: Path) -> List[Dict[str, Any]]:
    releases_dir = manifest_root / "releases"
    if not releases_dir.exists():
        return []

    manifests: List[Dict[str, Any]] = []
    for path in sorted(releases_dir.glob("*.json")):
        payload = sync.read_json(path, fallback=None)
        if isinstance(payload, dict):
            manifests.append(payload)
    return manifests


def normalize_release_key(release_id: int, release_tag: str) -> str:
    tag = (release_tag or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in (".", "-", "_") else "_" for ch in tag)
    if safe:
        return f"{safe}--{release_id}"
    return str(release_id)


def collect_ipk_entries(
    manifests: List[Dict[str, Any]],
    download_root: Path,
) -> Tuple[List[PackageEntry], List[str]]:
    entries: List[PackageEntry] = []
    errors: List[str] = []

    for rel in manifests:
        if rel.get("status") != "processed":
            continue
        try:
            release_id = int(rel["release_id"])
            openwrt_version = str(rel["openwrt_version"])
        except Exception:
            errors.append(f"release manifest missing ids/version: {rel.get('release_id')}")
            continue

        release_tag = str(rel.get("tag_name") or "")
        published_at = str(rel.get("published_at") or "")

        for asset in rel.get("assets", []):
            if asset.get("file_type") != "ipk":
                continue
            file_name = asset.get("file_name")
            if not isinstance(file_name, str):
                errors.append(f"release {release_id}: ipk asset without file_name")
                continue

            arch = asset.get("arch")
            if not isinstance(arch, str) or not arch:
                errors.append(f"release {release_id} asset {file_name}: missing arch")
                continue

            sha256 = str(asset.get("sha256") or "")
            source_path = download_root / str(release_id) / file_name
            if not source_path.exists():
                errors.append(f"missing downloaded ipk: {source_path}")
                continue

            entries.append(
                PackageEntry(
                    release_id=release_id,
                    release_tag=release_tag,
                    published_at=published_at,
                    openwrt_version=openwrt_version,
                    arch=arch,
                    file_name=file_name,
                    source_path=source_path,
                    sha256=sha256,
                )
            )

    return entries, errors


def copy_unique(src: Path, dst: Path, expected_sha256: str) -> Optional[str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        existing_sha = sync.sha256_file(dst)
        src_sha = expected_sha256 or sync.sha256_file(src)
        if existing_sha != src_sha:
            return f"collision: {dst} has sha256={existing_sha} but source has sha256={src_sha}"
        return None

    shutil.copy2(src, dst)
    return None


def materialize_release_tree(entries: List[PackageEntry], repo_root: Path) -> List[str]:
    errors: List[str] = []
    for entry in entries:
        release_key = normalize_release_key(entry.release_id, entry.release_tag)
        dst = (
            repo_root
            / "opkg"
            / "releases"
            / release_key
            / entry.openwrt_version
            / entry.arch
            / entry.file_name
        )
        err = copy_unique(entry.source_path, dst, entry.sha256)
        if err:
            errors.append(err)
    return errors


def select_latest_release_per_version(entries: List[PackageEntry]) -> Dict[str, int]:
    winner: Dict[str, Tuple[str, int]] = {}
    for entry in entries:
        current = winner.get(entry.openwrt_version)
        score = (entry.published_at, entry.release_id)
        if current is None or score > current:
            winner[entry.openwrt_version] = score
    return {version: rid for version, (_published, rid) in winner.items()}


def materialize_rolling_tree(entries: List[PackageEntry], repo_root: Path) -> Tuple[List[str], Dict[str, int]]:
    errors: List[str] = []
    latest = select_latest_release_per_version(entries)

    for entry in entries:
        if latest.get(entry.openwrt_version) != entry.release_id:
            continue
        dst = repo_root / "opkg" / "openwrt" / entry.openwrt_version / entry.arch / entry.file_name
        err = copy_unique(entry.source_path, dst, entry.sha256)
        if err:
            errors.append(err)

    return errors, latest


def extract_ipk_control_fields(path: Path) -> Dict[str, str]:
    return sync.read_ipk_control_fields(path)


def control_fields_to_stanza(fields: Dict[str, str], filename: str, size: int, sha256: str) -> str:
    priority = [
        "Package",
        "Version",
        "Depends",
        "Provides",
        "Source",
        "Section",
        "Category",
        "Submenu",
        "Title",
        "Maintainer",
        "License",
        "LicenseFiles",
        "Architecture",
        "Installed-Size",
        "Description",
    ]
    lines: List[str] = []

    for key in priority:
        if key in fields:
            lines.append(f"{key}: {fields[key]}")

    for key in sorted(k for k in fields if k not in set(priority)):
        lines.append(f"{key}: {fields[key]}")

    lines.append(f"Filename: {filename}")
    lines.append(f"Size: {size}")
    lines.append(f"SHA256sum: {sha256}")
    return "\n".join(lines)


def build_packages_index(arch_dir: Path) -> List[str]:
    errors: List[str] = []
    stanzas: List[str] = []

    for ipk_path in sorted(arch_dir.glob("*.ipk")):
        try:
            fields = extract_ipk_control_fields(ipk_path)
            pkg = fields.get("Package")
            ver = fields.get("Version")
            arch = fields.get("Architecture")
            if not pkg or not ver or not arch:
                raise sync.SyncError(f"missing required package metadata in {ipk_path.name}")

            if arch_dir.name != arch:
                raise sync.SyncError(
                    f"arch mismatch for {ipk_path.name}: dir={arch_dir.name} control={arch}"
                )

            stanza = control_fields_to_stanza(
                fields=fields,
                filename=ipk_path.name,
                size=ipk_path.stat().st_size,
                sha256=sync.sha256_file(ipk_path),
            )
            stanzas.append(stanza)
        except Exception as exc:
            errors.append(str(exc))

    packages_path = arch_dir / "Packages"
    payload = ("\n\n".join(stanzas) + "\n") if stanzas else ""
    packages_path.write_text(payload, encoding="utf-8")

    with (arch_dir / "Packages.gz").open("wb") as gz_out:
        with gzip.GzipFile(
            filename="Packages",
            mode="wb",
            compresslevel=9,
            mtime=0,
            fileobj=gz_out,
        ) as fh:
            fh.write(payload.encode("utf-8"))

    return errors


def sign_packages_if_requested(arch_dir: Path, usign_bin: str, sign_key: Optional[Path]) -> Optional[str]:
    if sign_key is None:
        return None
    if not sign_key.exists():
        return f"sign key not found: {sign_key}"

    packages = arch_dir / "Packages"
    sig = arch_dir / "Packages.sig"
    cmd = [usign_bin, "-S", "-m", str(packages), "-s", str(sign_key), "-x", str(sig)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return None
    except FileNotFoundError:
        return f"usign binary not found: {usign_bin}"
    except subprocess.CalledProcessError as exc:
        return f"usign failed for {packages}: {exc.stderr.strip() or exc.stdout.strip()}"


def iter_arch_dirs(repo_root: Path) -> Iterable[Path]:
    for top in [repo_root / "opkg" / "releases", repo_root / "opkg" / "openwrt"]:
        if not top.exists():
            continue
        for path in sorted(top.rglob("*")):
            if path.is_dir() and any(path.glob("*.ipk")):
                yield path


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build OPKG feed trees and indexes")
    parser.add_argument("--config", default="config/settings.json", help="path to config json")
    parser.add_argument("--output-root", default=None, help="override output root")
    parser.add_argument("--manifest-root", default=None, help="override manifest root")
    parser.add_argument("--download-root", default=None, help="override download root")
    parser.add_argument("--repo-root", default=None, help="override repo root (defaults to output/repos)")
    parser.add_argument("--clean", action="store_true", help="remove existing opkg output before rebuild")
    parser.add_argument("--dry-run", action="store_true", help="plan only; do not write files")
    parser.add_argument("--strict", action="store_true", default=True, help="exit non-zero on errors")
    parser.add_argument("--no-strict", action="store_false", dest="strict", help="allow errors")
    parser.add_argument("--sign-key", default=None, help="optional usign private key path")
    parser.add_argument("--usign-bin", default="usign", help="usign binary path")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    cfg = sync.load_config(Path(args.config))

    output_root = Path(args.output_root or cfg.get("output_root", "output"))
    manifest_root = Path(args.manifest_root or cfg.get("manifest_root", output_root / "manifests"))
    download_root = Path(args.download_root or cfg.get("download_root", output_root / "downloads"))
    repo_root = Path(args.repo_root or (output_root / "repos"))
    sign_key = Path(args.sign_key) if args.sign_key else None

    manifests = load_release_manifests(manifest_root)
    entries, errors = collect_ipk_entries(manifests, download_root)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "dry-run",
                    "release_manifests": len(manifests),
                    "ipk_entries": len(entries),
                    "errors": errors,
                },
                indent=2,
            )
        )
        return 2 if errors and args.strict else 0

    if args.clean and (repo_root / "opkg").exists():
        shutil.rmtree(repo_root / "opkg")

    release_errors = materialize_release_tree(entries, repo_root)
    rolling_errors, rolling_latest = materialize_rolling_tree(entries, repo_root)
    errors.extend(release_errors)
    errors.extend(rolling_errors)

    arch_dirs = list(iter_arch_dirs(repo_root))
    index_errors: List[str] = []
    sign_errors: List[str] = []

    for arch_dir in arch_dirs:
        index_errors.extend(build_packages_index(arch_dir))
        sig_err = sign_packages_if_requested(arch_dir, args.usign_bin, sign_key)
        if sig_err:
            sign_errors.append(sig_err)

    errors.extend(index_errors)
    errors.extend(sign_errors)

    report = {
        "generated_at": sync.now_iso(),
        "release_manifests": len(manifests),
        "ipk_entries": len(entries),
        "indexed_arch_dirs": len(arch_dirs),
        "rolling_latest_release_by_openwrt": rolling_latest,
        "errors": errors,
    }

    report_path = manifest_root / "index" / "opkg_repo_report.json"
    sync.write_json(report_path, report)

    print(json.dumps({"status": "ok", **report}, indent=2))
    if errors and args.strict:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
