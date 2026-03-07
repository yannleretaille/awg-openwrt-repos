#!/usr/bin/env python3
"""Build APK repositories from synced release assets."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import sync_releases as sync


VERSION_TOKEN_RE = re.compile(r"^v?\d+\.\d+\.\d+$", re.IGNORECASE)


@dataclass
class ApkArtifact:
    release_id: int
    release_tag: str
    published_at: str
    openwrt_version: str
    pkg_name: str
    pkg_ver: str
    pkg_arch: str
    target: str
    subtarget: str
    source_path: Path
    source_sha256: str
    payload_dir_rel: str


@dataclass
class CandidateArtifact:
    release_id: int
    release_tag: str
    published_at: str
    openwrt_version: str
    pkg_name: str
    pkg_ver: str
    pkg_arch: str
    source_path: Path
    source_sha256: str
    suffix_after_version: str
    target: Optional[str]
    subtarget: Optional[str]
    payload_dir_rel: str


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
    return f"{safe}--{release_id}" if safe else str(release_id)


def canonical_name(pkg_name: str, pkg_ver: str) -> str:
    return f"{pkg_name}-{pkg_ver}.apk"


def parse_apk_info(path: Path, apk_bin: str) -> Dict[str, List[str]]:
    cmd = [apk_bin, "adbdump", str(path)]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise sync.SyncError(f"apk binary not found: {apk_bin}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise sync.SyncError(f"apk adbdump failed for {path}: {detail}") from exc

    out: Dict[str, List[str]] = {}
    in_info = False
    current_list_key: Optional[str] = None

    for line in proc.stdout.splitlines():
        if not in_info:
            if line.strip() == "info:":
                in_info = True
            continue

        if not line.startswith("  "):
            break

        if line.startswith("    - "):
            if current_list_key:
                out.setdefault(current_list_key, []).append(line[6:].strip())
            continue

        body = line[2:]
        if ":" not in body:
            continue
        key, val = body.split(":", 1)
        key = key.strip()
        val = val.strip()
        if not val or val.startswith("#"):
            out.setdefault(key, [])
            current_list_key = key
            continue
        out.setdefault(key, []).append(val)
        current_list_key = None

    return out


def pkginfo_first(pkginfo: Dict[str, List[str]], key: str) -> Optional[str]:
    vals = pkginfo.get(key)
    if not vals:
        return None
    return vals[0]


def split_suffix_after_version(file_name: str) -> Optional[List[str]]:
    stem = file_name[:-4] if file_name.endswith(".apk") else file_name
    parts = stem.split("_")
    for idx, token in enumerate(parts):
        if VERSION_TOKEN_RE.match(token):
            return parts[idx + 1 :]
    return None


def suffix_after_version(file_name: str) -> Optional[str]:
    parts = split_suffix_after_version(file_name)
    if not parts:
        return None
    return "_".join(parts) or None


def parse_target_subtarget(file_name: str, pkg_arch: str) -> Tuple[Optional[str], Optional[str]]:
    suffix_parts = split_suffix_after_version(file_name)
    if not suffix_parts:
        return None, None

    prefix = pkg_arch.split("_")
    i = 0
    while i < len(prefix) and i < len(suffix_parts) and suffix_parts[i] == prefix[i]:
        i += 1
    rem = suffix_parts[i:]

    if len(rem) >= 2:
        return rem[0], "_".join(rem[1:])
    if len(rem) == 1:
        return rem[0], "generic"
    return None, None


def infer_target_subtarget_from_pairs(
    suffix_full: str,
    known_pairs: set[Tuple[str, str]],
) -> Tuple[Optional[str], Optional[str]]:
    best: Optional[Tuple[str, str]] = None
    best_len = -1
    for target, subtarget in known_pairs:
        marker = f"_{target}_{subtarget}"
        if suffix_full.endswith(marker):
            score = len(target) + len(subtarget)
            if score > best_len:
                best_len = score
                best = (target, subtarget)
    if best:
        return best
    return None, None


def kernel_hash_from_depends(depends: Iterable[str]) -> Optional[str]:
    for dep in depends:
        if dep.startswith("kernel="):
            return dep.split("=", 1)[1].strip()
    return None


def payload_dir_rel_path(pkg_name: str, apk_info: Dict[str, List[str]]) -> Tuple[Optional[str], Optional[str]]:
    if pkg_name.startswith("kmod-"):
        kh = kernel_hash_from_depends(apk_info.get("depends", []))
        if not kh:
            return None, "kmod package missing kernel= dependency in apk info"
        return f"kmods/{kh}", None
    return "packages", None


def collect_apk_artifacts(
    manifests: List[Dict[str, Any]],
    download_root: Path,
    apk_bin: str,
) -> Tuple[List[ApkArtifact], List[str]]:
    candidates: List[CandidateArtifact] = []
    artifacts: List[ApkArtifact] = []
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
            if asset.get("file_type") != "apk":
                continue
            file_name = asset.get("file_name")
            if not isinstance(file_name, str):
                errors.append(f"release {release_id}: apk asset without file_name")
                continue

            source_path = download_root / str(release_id) / file_name
            if not source_path.exists():
                errors.append(f"missing downloaded apk: {source_path}")
                continue

            pkg_name = asset.get("package_name")
            pkg_ver = asset.get("package_version")
            pkg_arch = asset.get("arch")

            apk_info: Dict[str, List[str]] = {}
            needs_pkginfo = not (isinstance(pkg_name, str) and pkg_name and isinstance(pkg_ver, str) and pkg_ver and isinstance(pkg_arch, str) and pkg_arch)
            if not needs_pkginfo and isinstance(pkg_name, str) and pkg_name.startswith("kmod-"):
                needs_pkginfo = True

            if needs_pkginfo:
                try:
                    apk_info = parse_apk_info(source_path, apk_bin=apk_bin)
                except Exception as exc:
                    errors.append(f"release {release_id} asset {file_name}: {exc}")
                    continue

            if not isinstance(pkg_name, str) or not pkg_name:
                pkg_name = pkginfo_first(apk_info, "name")
            if not isinstance(pkg_ver, str) or not pkg_ver:
                pkg_ver = pkginfo_first(apk_info, "version")
            if not isinstance(pkg_arch, str) or not pkg_arch:
                pkg_arch = pkginfo_first(apk_info, "arch")

            if not pkg_name or not pkg_ver or not pkg_arch:
                errors.append(f"release {release_id} asset {file_name}: missing pkgname/pkgver/arch")
                continue

            if not apk_info and pkg_name.startswith("kmod-"):
                try:
                    apk_info = parse_apk_info(source_path, apk_bin=apk_bin)
                except Exception as exc:
                    errors.append(f"release {release_id} asset {file_name}: {exc}")
                    continue

            payload_dir_rel, perr = payload_dir_rel_path(pkg_name, apk_info)
            if perr:
                errors.append(f"release {release_id} asset {file_name}: {perr}")
                continue

            parsed_target, parsed_subtarget = parse_target_subtarget(file_name, pkg_arch)
            full_suffix = suffix_after_version(file_name)
            if not full_suffix:
                errors.append(f"release {release_id} asset {file_name}: unable to parse postfix suffix")
                continue

            candidates.append(
                CandidateArtifact(
                    release_id=release_id,
                    release_tag=release_tag,
                    published_at=published_at,
                    openwrt_version=openwrt_version,
                    pkg_name=pkg_name,
                    pkg_ver=pkg_ver,
                    pkg_arch=pkg_arch,
                    source_path=source_path,
                    source_sha256=str(asset.get("sha256") or sync.sha256_file(source_path)),
                    suffix_after_version=full_suffix,
                    target=parsed_target,
                    subtarget=parsed_subtarget,
                    payload_dir_rel=payload_dir_rel,
                )
            )

    known_pairs: set[Tuple[str, str]] = set()
    for cand in candidates:
        if cand.pkg_name.startswith("kmod-") and cand.target and cand.subtarget:
            known_pairs.add((cand.target, cand.subtarget))
    known_targets = {target for target, _ in known_pairs}

    for cand in candidates:
        target, subtarget = cand.target, cand.subtarget
        if not target or not subtarget or (known_targets and target not in known_targets):
            t2, s2 = infer_target_subtarget_from_pairs(cand.suffix_after_version, known_pairs)
            if t2 and s2:
                target, subtarget = t2, s2

        if not target or not subtarget:
            errors.append(f"release {cand.release_id} asset {cand.source_path.name}: unable to infer target/subtarget")
            continue

        artifacts.append(
            ApkArtifact(
                release_id=cand.release_id,
                release_tag=cand.release_tag,
                published_at=cand.published_at,
                openwrt_version=cand.openwrt_version,
                pkg_name=cand.pkg_name,
                pkg_ver=cand.pkg_ver,
                pkg_arch=cand.pkg_arch,
                target=target,
                subtarget=subtarget,
                source_path=cand.source_path,
                source_sha256=cand.source_sha256,
                payload_dir_rel=cand.payload_dir_rel,
            )
        )

    return artifacts, errors


def copy_unique(src: Path, dst: Path, expected_sha256: str) -> Optional[str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        old = sync.sha256_file(dst)
        new = expected_sha256 or sync.sha256_file(src)
        if old != new:
            return f"collision: {dst} has sha256={old} but source has sha256={new}"
        return None
    shutil.copy2(src, dst)
    return None


def select_latest_release_per_version(artifacts: List[ApkArtifact]) -> Dict[str, int]:
    winner: Dict[str, Tuple[str, int]] = {}
    for item in artifacts:
        score = (item.published_at, item.release_id)
        cur = winner.get(item.openwrt_version)
        if cur is None or score > cur:
            winner[item.openwrt_version] = score
    return {k: v[1] for k, v in winner.items()}


def run_mkndx(
    target_root: Path,
    apk_bin: str,
    sign_key: Optional[Path],
    keys_dir: Optional[Path],
    package_files: List[str],
) -> Optional[str]:
    if not package_files:
        return None

    cmd = [apk_bin, "mkndx", "--allow-untrusted", "--output", "packages.adb"]
    if sign_key is not None:
        cmd.extend(["--sign", str(sign_key)])
        if keys_dir is not None:
            cmd.extend(["--keys-dir", str(keys_dir)])
    cmd.extend(sorted(package_files))

    try:
        subprocess.run(cmd, cwd=target_root, check=True, capture_output=True, text=True)
        return None
    except FileNotFoundError:
        return f"apk binary not found: {apk_bin}"
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        return f"apk mkndx failed in {target_root}: {detail}"


def materialize_variant(
    artifacts: Iterable[ApkArtifact],
    variant_root: Path,
    apk_bin: str,
    sign_key: Optional[Path],
    keys_dir: Optional[Path],
    include_release_key: bool,
    split_indexes: bool,
) -> Tuple[List[str], int]:
    errors: List[str] = []

    aggregate_index_inputs: Dict[Path, Dict[str, str]] = {}
    split_index_inputs: Dict[Path, Dict[str, str]] = {}

    for item in artifacts:
        if include_release_key:
            release_key = normalize_release_key(item.release_id, item.release_tag)
            target_root = variant_root / release_key / item.openwrt_version / "targets" / item.target / item.subtarget
        else:
            target_root = variant_root / item.openwrt_version / "targets" / item.target / item.subtarget

        cfile = canonical_name(item.pkg_name, item.pkg_ver)

        payload_canonical = target_root / item.payload_dir_rel / cfile
        err = copy_unique(item.source_path, payload_canonical, item.source_sha256)
        if err:
            errors.append(err)
            continue

        root_canonical = target_root / cfile
        err = copy_unique(item.source_path, root_canonical, item.source_sha256)
        if err:
            errors.append(err)
            continue

        payload_original = target_root / item.payload_dir_rel / item.source_path.name
        err = copy_unique(item.source_path, payload_original, item.source_sha256)
        if err:
            errors.append(err)
            continue

        agg_map = aggregate_index_inputs.setdefault(target_root, {})
        prev_agg_sha = agg_map.get(cfile)
        if prev_agg_sha and prev_agg_sha != item.source_sha256:
            errors.append(f"canonical apk collision in {target_root}: {cfile}")
        else:
            agg_map[cfile] = item.source_sha256

        if split_indexes:
            split_dir = target_root / item.payload_dir_rel
            split_map = split_index_inputs.setdefault(split_dir, {})
            prev_split_sha = split_map.get(cfile)
            if prev_split_sha and prev_split_sha != item.source_sha256:
                errors.append(f"canonical apk collision in {split_dir}: {cfile}")
            else:
                split_map[cfile] = item.source_sha256

    indexed_dirs = 0

    if split_indexes:
        for split_dir, mapping in split_index_inputs.items():
            mkndx_err = run_mkndx(
                target_root=split_dir,
                apk_bin=apk_bin,
                sign_key=sign_key,
                keys_dir=keys_dir,
                package_files=list(mapping.keys()),
            )
            if mkndx_err:
                errors.append(mkndx_err)
                continue
            indexed_dirs += 1

    for target_root, mapping in aggregate_index_inputs.items():
        mkndx_err = run_mkndx(
            target_root=target_root,
            apk_bin=apk_bin,
            sign_key=sign_key,
            keys_dir=keys_dir,
            package_files=list(mapping.keys()),
        )
        if mkndx_err:
            errors.append(mkndx_err)
            continue
        indexed_dirs += 1

    return errors, indexed_dirs


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build APK feed trees and indexes")
    parser.add_argument("--config", default="config/settings.json", help="path to config json")
    parser.add_argument("--output-root", default=None, help="override output root")
    parser.add_argument("--manifest-root", default=None, help="override manifest root")
    parser.add_argument("--download-root", default=None, help="override download root")
    parser.add_argument("--repo-root", default=None, help="override repo root (defaults to output/repos)")
    parser.add_argument("--clean", action="store_true", help="remove existing apk output before rebuild")
    parser.add_argument("--dry-run", action="store_true", help="plan only; do not write files")
    parser.add_argument("--strict", action="store_true", default=True, help="exit non-zero on errors")
    parser.add_argument("--no-strict", action="store_false", dest="strict", help="allow errors")
    parser.add_argument("--sign-key", default=None, help="optional apk private key path")
    parser.add_argument("--keys-dir", default=None, help="optional apk keys directory")
    parser.add_argument("--apk-bin", default="apk", help="apk binary path")
    parser.add_argument(
        "--split-indexes",
        action="store_true",
        default=True,
        help="also generate compatibility indexes for split package directories",
    )
    parser.add_argument(
        "--no-split-indexes",
        action="store_false",
        dest="split_indexes",
        help="disable split compatibility index generation",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    cfg = sync.load_config(Path(args.config))

    output_root = Path(args.output_root or cfg.get("output_root", "output"))
    manifest_root = Path(args.manifest_root or cfg.get("manifest_root", output_root / "manifests"))
    download_root = Path(args.download_root or cfg.get("download_root", output_root / "downloads"))
    repo_root = Path(args.repo_root or (output_root / "repos"))

    sign_key = Path(args.sign_key) if args.sign_key else None
    keys_dir = Path(args.keys_dir) if args.keys_dir else None

    manifests = load_release_manifests(manifest_root)
    artifacts, errors = collect_apk_artifacts(manifests, download_root, apk_bin=args.apk_bin)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "dry-run",
                    "release_manifests": len(manifests),
                    "apk_entries": len(artifacts),
                    "split_indexes": args.split_indexes,
                    "errors": errors,
                },
                indent=2,
            )
        )
        return 2 if errors and args.strict else 0

    if args.clean and (repo_root / "apk").exists():
        shutil.rmtree(repo_root / "apk")

    rolling_latest = select_latest_release_per_version(artifacts)

    release_errors, release_indexed_dirs = materialize_variant(
        artifacts=artifacts,
        variant_root=repo_root / "apk" / "releases",
        apk_bin=args.apk_bin,
        sign_key=sign_key,
        keys_dir=keys_dir,
        include_release_key=True,
        split_indexes=args.split_indexes,
    )
    errors.extend(release_errors)

    rolling_artifacts = [a for a in artifacts if rolling_latest.get(a.openwrt_version) == a.release_id]
    rolling_errors, rolling_indexed_dirs = materialize_variant(
        artifacts=rolling_artifacts,
        variant_root=repo_root / "apk" / "openwrt",
        apk_bin=args.apk_bin,
        sign_key=sign_key,
        keys_dir=keys_dir,
        include_release_key=False,
        split_indexes=args.split_indexes,
    )
    errors.extend(rolling_errors)

    report = {
        "generated_at": sync.now_iso(),
        "release_manifests": len(manifests),
        "apk_entries": len(artifacts),
        "indexed_dirs": release_indexed_dirs + rolling_indexed_dirs,
        "split_indexes": args.split_indexes,
        "rolling_latest_release_by_openwrt": rolling_latest,
        "errors": errors,
    }

    report_path = manifest_root / "index" / "apk_repo_report.json"
    sync.write_json(report_path, report)

    print(json.dumps({"status": "ok", **report}, indent=2))
    return 2 if errors and args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
