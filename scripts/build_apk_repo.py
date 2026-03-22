#!/usr/bin/env python3
"""Build APK repositories from synced release assets."""

from __future__ import annotations

import argparse
import json
import os
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
    kernel_hash: Optional[str]
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
    kernel_hash: Optional[str]
    source_path: Path
    source_sha256: str
    suffix_after_version: str
    target: Optional[str]
    subtarget: Optional[str]
    payload_dir_rel: str


@dataclass
class CoverageSkipRule:
    rule_id: str
    reason: str
    openwrt_version: Optional[str]
    release_id: Optional[int]
    release_tag: Optional[str]
    target: Optional[str]
    subtarget: Optional[str]
    required_package_names: set[str]


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


def load_coverage_policy(cfg: Dict[str, Any]) -> Tuple[bool, set[str], set[str], List[CoverageSkipRule]]:
    policy = cfg.get("coverage_policy", {})
    if not isinstance(policy, dict):
        return True, set(), set(), []

    strict = bool(policy.get("strict", True))
    required = {
        str(x).strip()
        for x in policy.get("required_package_names", [])
        if isinstance(x, str) and x.strip()
    }
    optional = {
        str(x).strip()
        for x in policy.get("optional_package_names", [])
        if isinstance(x, str) and x.strip()
    }
    skip_rules: List[CoverageSkipRule] = []
    raw_rules = policy.get("skip_rules", [])
    if isinstance(raw_rules, list):
        for idx, raw in enumerate(raw_rules):
            if not isinstance(raw, dict):
                continue
            rule_id = str(raw.get("id") or f"rule-{idx + 1}").strip() or f"rule-{idx + 1}"
            reason = str(raw.get("reason") or "").strip()

            openwrt_version = raw.get("openwrt_version")
            if not isinstance(openwrt_version, str) or not openwrt_version.strip():
                openwrt_version = None
            else:
                openwrt_version = openwrt_version.strip()

            release_id = raw.get("release_id")
            release_id_int: Optional[int] = None
            if isinstance(release_id, int):
                release_id_int = release_id
            elif isinstance(release_id, str) and release_id.strip().isdigit():
                release_id_int = int(release_id.strip())

            release_tag = raw.get("release_tag")
            if not isinstance(release_tag, str) or not release_tag.strip():
                release_tag = None
            else:
                release_tag = release_tag.strip()

            target = raw.get("target")
            if not isinstance(target, str) or not target.strip():
                target = None
            else:
                target = target.strip()

            subtarget = raw.get("subtarget")
            if not isinstance(subtarget, str) or not subtarget.strip():
                subtarget = None
            else:
                subtarget = subtarget.strip()

            rule_packages = {
                str(x).strip()
                for x in raw.get("required_package_names", [])
                if isinstance(x, str) and x.strip()
            }

            skip_rules.append(
                CoverageSkipRule(
                    rule_id=rule_id,
                    reason=reason,
                    openwrt_version=openwrt_version,
                    release_id=release_id_int,
                    release_tag=release_tag,
                    target=target,
                    subtarget=subtarget,
                    required_package_names=rule_packages,
                )
            )

    return strict, required, optional, skip_rules


def coverage_skip_rule_matches(rule: CoverageSkipRule, ctx: Dict[str, Any]) -> bool:
    if rule.openwrt_version and rule.openwrt_version != ctx.get("openwrt_version"):
        return False
    if rule.release_id is not None and rule.release_id != ctx.get("release_id"):
        return False
    if rule.release_tag and rule.release_tag != ctx.get("release_tag"):
        return False
    if rule.target and rule.target != ctx.get("target"):
        return False
    if rule.subtarget and rule.subtarget != ctx.get("subtarget"):
        return False
    return True


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
            needs_pkginfo = not (
                isinstance(pkg_name, str)
                and pkg_name
                and isinstance(pkg_ver, str)
                and pkg_ver
                and isinstance(pkg_arch, str)
                and pkg_arch
            )
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

            kernel_hash = kernel_hash_from_depends(apk_info.get("depends", [])) if pkg_name.startswith("kmod-") else None

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
                    kernel_hash=kernel_hash,
                    source_path=source_path,
                    source_sha256=str(asset.get("sha256") or sync.sha256_file(source_path)),
                    suffix_after_version=full_suffix,
                    target=parsed_target,
                    subtarget=parsed_subtarget,
                    payload_dir_rel=payload_dir_rel,
                )
            )

    known_pairs_by_release: Dict[int, set[Tuple[str, str]]] = {}
    for cand in candidates:
        if cand.pkg_name.startswith("kmod-") and cand.target and cand.subtarget:
            known_pairs_by_release.setdefault(cand.release_id, set()).add((cand.target, cand.subtarget))
    for cand in candidates:
        known_pairs = known_pairs_by_release.get(cand.release_id, set())
        target, subtarget = cand.target, cand.subtarget
        if (
            not target
            or not subtarget
            or (known_pairs and (target, subtarget) not in known_pairs)
        ):
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
                kernel_hash=cand.kernel_hash,
                target=target,
                subtarget=subtarget,
                source_path=cand.source_path,
                source_sha256=cand.source_sha256,
                payload_dir_rel=cand.payload_dir_rel,
            )
        )

    return artifacts, errors


def copy_unique(src: Path, dst: Path, expected_sha256: str, force_overwrite: bool = False) -> Optional[str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        old = sync.sha256_file(dst)
        new = expected_sha256 or sync.sha256_file(src)
        if old != new:
            if force_overwrite:
                shutil.copy2(src, dst)
                return None
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


def destination_root_for(item: ApkArtifact, variant_root: Path, include_release_key: bool) -> Path:
    if include_release_key:
        release_key = normalize_release_key(item.release_id, item.release_tag)
        return variant_root / release_key / item.openwrt_version / "targets" / item.target / item.subtarget
    return variant_root / item.openwrt_version / "targets" / item.target / item.subtarget


def destination_paths_for(item: ApkArtifact, target_root: Path) -> List[Path]:
    cfile = canonical_name(item.pkg_name, item.pkg_ver)
    return [
        target_root / item.payload_dir_rel / cfile,
        target_root / cfile,
        target_root / item.payload_dir_rel / item.source_path.name,
    ]


def build_path_collision_report(
    artifacts: Iterable[ApkArtifact],
    variant_root: Path,
    include_release_key: bool,
) -> Dict[str, Any]:
    by_path: Dict[str, List[Dict[str, Any]]] = {}
    for item in artifacts:
        target_root = destination_root_for(item, variant_root, include_release_key)
        for dst in destination_paths_for(item, target_root):
            by_path.setdefault(str(dst), []).append(
                {
                    "sha256": item.source_sha256,
                    "release_id": item.release_id,
                    "release_tag": item.release_tag,
                    "openwrt_version": item.openwrt_version,
                    "target": item.target,
                    "subtarget": item.subtarget,
                    "file_name": item.source_path.name,
                }
            )

    duplicates: List[Dict[str, Any]] = []
    collisions: List[Dict[str, Any]] = []
    for path, rows in sorted(by_path.items()):
        if len(rows) <= 1:
            continue
        sha_set = sorted({r["sha256"] for r in rows if r.get("sha256")})
        entry = {"destination_path": path, "sha256_values": sha_set, "instances": rows}
        if len(sha_set) <= 1:
            duplicates.append(entry)
        else:
            collisions.append(entry)

    return {
        "paths_seen": len(by_path),
        "duplicate_paths": len(duplicates),
        "collision_paths": len(collisions),
        "duplicates": duplicates,
        "collisions": collisions,
    }


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


def resolve_tool(tool: str) -> Optional[str]:
    if "/" in tool:
        p = Path(tool).expanduser()
        if p.exists() and os.access(str(p), os.X_OK):
            return str(p.resolve())
        return None
    return shutil.which(tool)


def materialize_variant(
    artifacts: Iterable[ApkArtifact],
    variant_root: Path,
    apk_bin: str,
    sign_key: Optional[Path],
    keys_dir: Optional[Path],
    include_release_key: bool,
    force_collision_override: bool,
    split_indexes: bool,
    strict_coverage: bool,
    required_packages: set[str],
    optional_packages: set[str],
    skip_rules: List[CoverageSkipRule],
    variant_name: str,
) -> Tuple[List[str], int, List[Dict[str, Any]]]:
    errors: List[str] = []
    coverage_rows: List[Dict[str, Any]] = []

    aggregate_index_inputs: Dict[Path, Dict[str, str]] = {}
    split_index_inputs: Dict[Path, Dict[str, str]] = {}
    packages_by_target: Dict[Path, set[str]] = {}
    target_context: Dict[Path, Dict[str, Any]] = {}

    for item in artifacts:
        target_root = destination_root_for(item, variant_root, include_release_key)

        cfile = canonical_name(item.pkg_name, item.pkg_ver)

        payload_canonical = target_root / item.payload_dir_rel / cfile
        err = copy_unique(
            item.source_path,
            payload_canonical,
            item.source_sha256,
            force_overwrite=force_collision_override,
        )
        if err:
            errors.append(err)
            continue

        root_canonical = target_root / cfile
        err = copy_unique(
            item.source_path,
            root_canonical,
            item.source_sha256,
            force_overwrite=force_collision_override,
        )
        if err:
            errors.append(err)
            continue

        payload_original = target_root / item.payload_dir_rel / item.source_path.name
        err = copy_unique(
            item.source_path,
            payload_original,
            item.source_sha256,
            force_overwrite=force_collision_override,
        )
        if err:
            errors.append(err)
            continue

        agg_map = aggregate_index_inputs.setdefault(target_root, {})
        agg_map[cfile] = item.source_sha256
        packages_by_target.setdefault(target_root, set()).add(item.pkg_name)
        target_context.setdefault(
            target_root,
            {
                "variant": variant_name,
                "release_id": item.release_id,
                "release_tag": item.release_tag,
                "openwrt_version": item.openwrt_version,
                "target": item.target,
                "subtarget": item.subtarget,
            },
        )

        if split_indexes:
            split_dir = target_root / item.payload_dir_rel
            split_map = split_index_inputs.setdefault(split_dir, {})
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

    for target_root, present in sorted(packages_by_target.items(), key=lambda x: str(x[0])):
        missing_required = sorted(required_packages - present)
        present_optional = sorted(optional_packages & present)
        ctx = target_context.get(target_root, {"variant": variant_name})

        matched_skip_rules = [r for r in skip_rules if coverage_skip_rule_matches(r, ctx)]
        matched_skip_meta: List[Dict[str, Any]] = []
        skipped_missing_required: set[str] = set()
        for rule in matched_skip_rules:
            applied_pkgs = (
                set(missing_required)
                if not rule.required_package_names
                else set(missing_required) & rule.required_package_names
            )
            if not applied_pkgs:
                continue
            skipped_missing_required |= applied_pkgs
            matched_skip_meta.append(
                {
                    "id": rule.rule_id,
                    "reason": rule.reason,
                    "required_package_names": sorted(rule.required_package_names),
                    "applied_packages": sorted(applied_pkgs),
                }
            )

        effective_missing_required = sorted(set(missing_required) - skipped_missing_required)
        skipped_missing_required_sorted = sorted(skipped_missing_required)
        coverage_status = "ok"
        if effective_missing_required:
            coverage_status = "missing_required"
            if skipped_missing_required_sorted:
                coverage_status = "missing_required_partial_skip"
        elif skipped_missing_required_sorted:
            coverage_status = "missing_required_skipped"

        coverage_rows.append(
            {
                "target_root": str(target_root),
                "variant": ctx.get("variant"),
                "release_id": ctx.get("release_id"),
                "release_tag": ctx.get("release_tag"),
                "openwrt_version": ctx.get("openwrt_version"),
                "target": ctx.get("target"),
                "subtarget": ctx.get("subtarget"),
                "present_count": len(present),
                "missing_required": missing_required,
                "skipped_missing_required": skipped_missing_required_sorted,
                "effective_missing_required": effective_missing_required,
                "coverage_status": coverage_status,
                "skip_rules": matched_skip_meta,
                "present_optional": present_optional,
            }
        )
        if strict_coverage and effective_missing_required:
            errors.append(
                f"coverage check failed at {target_root}: missing required packages {effective_missing_required}"
            )

    return errors, indexed_dirs, coverage_rows


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
    parser.add_argument(
        "--force-collision-override",
        action="store_true",
        help="continue build and overwrite when checksum collisions are detected on destination paths",
    )
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
    strict_coverage, required_packages, optional_packages, skip_rules = load_coverage_policy(cfg)

    sign_key_value = args.sign_key or os.getenv("APK_SIGN_KEY_PATH")
    keys_dir_value = args.keys_dir or os.getenv("APK_KEYS_DIR")
    sign_key = Path(sign_key_value).expanduser().resolve() if sign_key_value else None
    keys_dir = Path(keys_dir_value).expanduser().resolve() if keys_dir_value else None

    manifests = load_release_manifests(manifest_root)
    errors: List[str] = []

    resolved_apk_bin = resolve_tool(args.apk_bin)
    if sign_key is not None:
        if not resolved_apk_bin:
            errors.append(f"signing requested but apk binary not found: {args.apk_bin}")
        if not sign_key.exists():
            errors.append(f"sign key not found: {sign_key}")
        if keys_dir is not None and not keys_dir.exists():
            errors.append(f"keys dir not found: {keys_dir}")
    apk_bin = resolved_apk_bin or args.apk_bin

    artifacts, collect_errors = collect_apk_artifacts(manifests, download_root, apk_bin=apk_bin)
    errors.extend(collect_errors)
    rolling_latest = select_latest_release_per_version(artifacts)
    rolling_artifacts = [a for a in artifacts if rolling_latest.get(a.openwrt_version) == a.release_id]
    release_collision_report = build_path_collision_report(
        artifacts,
        repo_root / "apk" / "releases",
        include_release_key=True,
    )
    rolling_collision_report = build_path_collision_report(
        rolling_artifacts,
        repo_root / "apk" / "openwrt",
        include_release_key=False,
    )
    collision_count = int(release_collision_report.get("collision_paths", 0)) + int(
        rolling_collision_report.get("collision_paths", 0)
    )
    if collision_count > 0 and not args.force_collision_override:
        errors.append(
            f"collision gate: detected {collision_count} checksum collision(s) on destination paths; "
            "use --force-collision-override to bypass"
        )
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

    sync.write_json(
        manifest_root / "index" / "apk_collision_report.json",
        {
            "generated_at": sync.now_iso(),
            "force_override": bool(args.force_collision_override),
            "summary": {
                "collision_paths": collision_count,
                "duplicate_paths": int(release_collision_report.get("duplicate_paths", 0))
                + int(rolling_collision_report.get("duplicate_paths", 0)),
            },
            "release_scope": release_collision_report,
            "rolling_scope": rolling_collision_report,
        },
    )

    release_errors, release_indexed_dirs, release_coverage = materialize_variant(
        artifacts=artifacts,
        variant_root=repo_root / "apk" / "releases",
        apk_bin=apk_bin,
        sign_key=sign_key,
        keys_dir=keys_dir,
        include_release_key=True,
        force_collision_override=args.force_collision_override,
        split_indexes=args.split_indexes,
        strict_coverage=strict_coverage,
        required_packages=required_packages,
        optional_packages=optional_packages,
        skip_rules=skip_rules,
        variant_name="release",
    )
    errors.extend(release_errors)

    rolling_errors, rolling_indexed_dirs, rolling_coverage = materialize_variant(
        artifacts=rolling_artifacts,
        variant_root=repo_root / "apk" / "openwrt",
        apk_bin=apk_bin,
        sign_key=sign_key,
        keys_dir=keys_dir,
        include_release_key=False,
        force_collision_override=args.force_collision_override,
        split_indexes=args.split_indexes,
        strict_coverage=strict_coverage,
        required_packages=required_packages,
        optional_packages=optional_packages,
        skip_rules=skip_rules,
        variant_name="rolling",
    )
    errors.extend(rolling_errors)

    coverage = {
        "strict": strict_coverage,
        "required_package_names": sorted(required_packages),
        "optional_package_names": sorted(optional_packages),
        "skip_rule_count": len(skip_rules),
        "release_targets_checked": len(release_coverage),
        "rolling_targets_checked": len(rolling_coverage),
        "release_missing_required_count": sum(
            1 for row in release_coverage if row["effective_missing_required"]
        ),
        "rolling_missing_required_count": sum(
            1 for row in rolling_coverage if row["effective_missing_required"]
        ),
        "release_skipped_missing_required_count": sum(
            1 for row in release_coverage if row["skipped_missing_required"]
        ),
        "rolling_skipped_missing_required_count": sum(
            1 for row in rolling_coverage if row["skipped_missing_required"]
        ),
    }
    coverage_notices: List[str] = []
    for row in [*release_coverage, *rolling_coverage]:
        if not row.get("skipped_missing_required"):
            continue
        location = row.get("target_root")
        skipped = row.get("skipped_missing_required")
        remaining = row.get("effective_missing_required")
        status = row.get("coverage_status")
        coverage_notices.append(
            "coverage check skipped by policy at "
            f"{location}: missing required packages {skipped} (status={status}, "
            f"remaining_required={remaining})"
        )

    report = {
        "generated_at": sync.now_iso(),
        "release_manifests": len(manifests),
        "apk_entries": len(artifacts),
        "indexed_dirs": release_indexed_dirs + rolling_indexed_dirs,
        "split_indexes": args.split_indexes,
        "rolling_latest_release_by_openwrt": rolling_latest,
        "coverage": coverage,
        "collision_summary": {
            "collision_paths": collision_count,
            "duplicate_paths": int(release_collision_report.get("duplicate_paths", 0))
            + int(rolling_collision_report.get("duplicate_paths", 0)),
            "force_override": bool(args.force_collision_override),
        },
        "notices": coverage_notices,
        "errors": errors,
    }

    report_path = manifest_root / "index" / "apk_repo_report.json"
    sync.write_json(report_path, report)
    sync.write_json(
        manifest_root / "index" / "apk_coverage_report.json",
        {
            "generated_at": sync.now_iso(),
            "policy": coverage,
            "rolling_targets": rolling_coverage,
            "release_targets": release_coverage,
        },
    )

    print(json.dumps({"status": "ok", **report}, indent=2))
    return 2 if errors and args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
