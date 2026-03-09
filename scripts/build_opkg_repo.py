#!/usr/bin/env python3
"""Build OPKG repositories from synced release assets."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import sync_releases as sync


KERNEL_DEP_RE = re.compile(r"(?:^|,)\s*kernel\s*\(=\s*([^)]+)\)")


@dataclass
class PackageArtifact:
    release_id: int
    release_tag: str
    published_at: str
    openwrt_version: str
    arch: str
    target: str
    subtarget: str
    file_name: str
    source_path: Path
    sha256: str
    fields: Dict[str, str]
    payload_rel: str


@dataclass
class IndexedPackage:
    fields: Dict[str, str]
    file_name: str
    rel_filename: str
    size: int
    sha256: str


@dataclass
class CandidateArtifact:
    release_id: int
    release_tag: str
    published_at: str
    openwrt_version: str
    arch: str
    file_name: str
    source_path: Path
    sha256: str
    fields: Dict[str, str]
    suffix_after_version: str
    target: Optional[str]
    subtarget: Optional[str]


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


def load_coverage_policy(cfg: Dict[str, Any]) -> Tuple[bool, set[str], set[str]]:
    policy = cfg.get("coverage_policy", {})
    if not isinstance(policy, dict):
        return True, set(), set()

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
    return strict, required, optional


def normalize_release_key(release_id: int, release_tag: str) -> str:
    tag = (release_tag or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in (".", "-", "_") else "_" for ch in tag)
    return f"{safe}--{release_id}" if safe else str(release_id)


def parse_target_subtarget(file_name: str, arch: str) -> Tuple[Optional[str], Optional[str]]:
    stem = file_name[:-4] if file_name.endswith(".ipk") else file_name
    parts = stem.split("_")

    version_idx = None
    for idx, token in enumerate(parts):
        if any(ch == "." for ch in token) and token.lstrip("v").replace(".", "").isdigit():
            version_idx = idx
            break
    if version_idx is None:
        return None, None

    suffix = "_".join(parts[version_idx + 1 :])
    if not suffix:
        return None, None

    rem = suffix[len(arch) + 1 :] if suffix.startswith(f"{arch}_") else suffix
    if "_" not in rem:
        return rem or None, "generic"
    target, subtarget = rem.split("_", 1)
    return target or None, subtarget or None


def suffix_after_version(file_name: str) -> Optional[str]:
    stem = file_name[:-4] if file_name.endswith(".ipk") else file_name
    parts = stem.split("_")
    for idx, token in enumerate(parts):
        if any(ch == "." for ch in token) and token.lstrip("v").replace(".", "").isdigit():
            return "_".join(parts[idx + 1 :]) or None
    return None


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


def extract_kernel_hash(fields: Dict[str, str]) -> Optional[str]:
    depends = fields.get("Depends", "")
    match = KERNEL_DEP_RE.search(depends)
    if match:
        return match.group(1).strip()
    return None


def payload_rel_path(fields: Dict[str, str], file_name: str) -> Tuple[Optional[str], Optional[str]]:
    pkg_name = fields.get("Package", "")
    if pkg_name.startswith("kmod-"):
        kernel_hash = extract_kernel_hash(fields)
        if not kernel_hash:
            return None, "kmod package missing kernel hash in Depends"
        return f"kmods/{kernel_hash}/{file_name}", None
    return f"packages/{file_name}", None


def collect_ipk_artifacts(manifests: List[Dict[str, Any]], download_root: Path) -> Tuple[List[PackageArtifact], List[str]]:
    artifacts: List[PackageArtifact] = []
    candidates: List[CandidateArtifact] = []
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

            parsed_target, parsed_subtarget = parse_target_subtarget(file_name, arch)
            suffix_full = suffix_after_version(file_name)
            if not suffix_full:
                errors.append(f"release {release_id} asset {file_name}: unable to parse postfix suffix")
                continue

            source_path = download_root / str(release_id) / file_name
            if not source_path.exists():
                errors.append(f"missing downloaded ipk: {source_path}")
                continue

            try:
                fields = sync.read_ipk_control_fields(source_path)
                rel_path, rel_err = payload_rel_path(fields, file_name)
                if rel_err:
                    errors.append(f"release {release_id} asset {file_name}: {rel_err}")
                    continue
                candidates.append(
                    CandidateArtifact(
                        release_id=release_id,
                        release_tag=release_tag,
                        published_at=published_at,
                        openwrt_version=openwrt_version,
                        arch=arch,
                        file_name=file_name,
                        source_path=source_path,
                        sha256=str(asset.get("sha256") or sync.sha256_file(source_path)),
                        fields=fields,
                        suffix_after_version=suffix_full,
                        target=parsed_target,
                        subtarget=parsed_subtarget,
                    ),
                )
            except Exception as exc:
                errors.append(f"release {release_id} asset {file_name}: {exc}")

    known_pairs_by_release: Dict[int, set[Tuple[str, str]]] = {}
    for cand in candidates:
        pkg_name = cand.fields.get("Package", "")
        if pkg_name.startswith("kmod-") and cand.target and cand.subtarget:
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
            errors.append(
                f"release {cand.release_id} asset {cand.file_name}: unable to infer target/subtarget"
            )
            continue

        rel_path, rel_err = payload_rel_path(cand.fields, cand.file_name)
        if rel_err:
            errors.append(f"release {cand.release_id} asset {cand.file_name}: {rel_err}")
            continue

        artifacts.append(
            PackageArtifact(
                release_id=cand.release_id,
                release_tag=cand.release_tag,
                published_at=cand.published_at,
                openwrt_version=cand.openwrt_version,
                arch=cand.arch,
                target=target,
                subtarget=subtarget,
                file_name=cand.file_name,
                source_path=cand.source_path,
                sha256=cand.sha256,
                fields=cand.fields,
                payload_rel=rel_path,
            )
        )

    return artifacts, errors


def copy_unique(src: Path, dst: Path, expected_sha256: str, force_overwrite: bool = False) -> Optional[str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        existing_sha = sync.sha256_file(dst)
        src_sha = expected_sha256 or sync.sha256_file(src)
        if existing_sha != src_sha:
            if force_overwrite:
                shutil.copy2(src, dst)
                return None
            return f"collision: {dst} has sha256={existing_sha} but source has sha256={src_sha}"
        return None
    shutil.copy2(src, dst)
    return None


def select_latest_release_per_version(artifacts: List[PackageArtifact]) -> Dict[str, int]:
    winner: Dict[str, Tuple[str, int]] = {}
    for item in artifacts:
        score = (item.published_at, item.release_id)
        current = winner.get(item.openwrt_version)
        if current is None or score > current:
            winner[item.openwrt_version] = score
    return {k: v[1] for k, v in winner.items()}


def destination_root_for(item: PackageArtifact, variant_root: Path, include_release_key: bool) -> Path:
    if include_release_key:
        release_key = normalize_release_key(item.release_id, item.release_tag)
        return variant_root / release_key / item.openwrt_version / "targets" / item.target / item.subtarget
    return variant_root / item.openwrt_version / "targets" / item.target / item.subtarget


def build_path_collision_report(
    artifacts: Iterable[PackageArtifact],
    variant_root: Path,
    include_release_key: bool,
) -> Dict[str, Any]:
    by_path: Dict[str, List[Dict[str, Any]]] = {}
    for item in artifacts:
        dst = destination_root_for(item, variant_root, include_release_key) / item.payload_rel
        row = {
            "sha256": item.sha256,
            "release_id": item.release_id,
            "release_tag": item.release_tag,
            "openwrt_version": item.openwrt_version,
            "target": item.target,
            "subtarget": item.subtarget,
            "file_name": item.file_name,
        }
        by_path.setdefault(str(dst), []).append(row)

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


def write_packages_files(index_dir: Path, packages: List[IndexedPackage]) -> List[str]:
    errors: List[str] = []
    stanzas: List[str] = []
    seen_arch: set[str] = set()

    for p in sorted(packages, key=lambda x: (x.fields.get("Package", ""), x.fields.get("Version", ""), x.rel_filename)):
        arch = p.fields.get("Architecture")
        if arch:
            seen_arch.add(arch)
        stanzas.append(
            control_fields_to_stanza(
                fields=p.fields,
                filename=p.rel_filename,
                size=p.size,
                sha256=p.sha256,
            )
        )

    non_all_arches = {a for a in seen_arch if a != "all"}
    if len(non_all_arches) > 1:
        errors.append(f"mixed package architectures in index dir {index_dir}: {sorted(seen_arch)}")

    payload = ("\n\n".join(stanzas) + "\n") if stanzas else ""
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "Packages").write_text(payload, encoding="utf-8")
    with (index_dir / "Packages.gz").open("wb") as out:
        with gzip.GzipFile(filename="Packages", mode="wb", compresslevel=9, mtime=0, fileobj=out) as gz:
            gz.write(payload.encode("utf-8"))

    return errors


def sign_packages_if_requested(index_dir: Path, usign_bin: str, sign_key: Optional[Path]) -> Optional[str]:
    if sign_key is None:
        return None
    resolved = resolve_opkg_signer(usign_bin)
    if resolved is None:
        return (
            "no compatible OPKG signer found; tried preferred/usign/signify-openbsd/signify. "
            "Install usign or signify-openbsd/signify, or pass --usign-bin."
        )
    signer_bin, signer_mode = resolved
    if not sign_key.exists():
        return f"sign key not found: {sign_key}"
    if signer_mode == "usign":
        cmd = [signer_bin, "-S", "-m", str(index_dir / "Packages"), "-s", str(sign_key), "-x", str(index_dir / "Packages.sig")]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return None
        except FileNotFoundError:
            return f"signer binary not found: {signer_bin}"
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip()
            return f"{signer_mode} signing failed for {index_dir / 'Packages'}: {detail}"

    # signify requires keyname.sec with matching keyname.pub naming.
    try:
        with tempfile.TemporaryDirectory(prefix="opkg-signify-") as tdir:
            tmp_base = Path(tdir) / "opkg-signing"
            tmp_sec = tmp_base.with_suffix(".sec")
            tmp_pub = tmp_base.with_suffix(".pub")
            shutil.copyfile(sign_key, tmp_sec)

            candidate_pub = sign_key.with_suffix(".pub")
            if candidate_pub.exists():
                shutil.copyfile(candidate_pub, tmp_pub)
            else:
                # Minimal placeholder to satisfy signify naming scheme checks.
                tmp_pub.write_text("untrusted comment: placeholder\n", encoding="utf-8")

            cmd = [signer_bin, "-S", "-s", str(tmp_sec), "-m", str(index_dir / "Packages"), "-x", str(index_dir / "Packages.sig")]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return None
    except FileNotFoundError:
        return f"signer binary not found: {signer_bin}"
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip()
        return f"{signer_mode} signing failed for {index_dir / 'Packages'}: {detail}"


def resolve_opkg_signer(preferred_bin: str) -> Optional[Tuple[str, str]]:
    candidates: List[str] = []
    if preferred_bin:
        candidates.append(preferred_bin)
    for c in ("usign", "signify-openbsd", "signify"):
        if c not in candidates:
            candidates.append(c)

    for cand in candidates:
        resolved: Optional[str] = None
        if "/" in cand:
            p = Path(cand).expanduser()
            if p.exists() and os.access(str(p), os.X_OK):
                resolved = str(p.resolve())
        else:
            which = shutil.which(cand)
            if which:
                resolved = which
        if not resolved:
            continue

        base = Path(resolved).name
        mode = "usign"
        if base in ("signify", "signify-openbsd"):
            mode = "signify"
        return (resolved, mode)

    return None


def materialize_variant(
    artifacts: Iterable[PackageArtifact],
    variant_root: Path,
    usign_bin: str,
    sign_key: Optional[Path],
    include_release_key: bool,
    force_collision_override: bool,
    strict_coverage: bool,
    required_packages: set[str],
    optional_packages: set[str],
) -> Tuple[List[str], int, List[Dict[str, Any]]]:
    errors: List[str] = []
    coverage_rows: List[Dict[str, Any]] = []

    aggregate: Dict[Path, List[IndexedPackage]] = {}
    split_indexes: Dict[Path, List[IndexedPackage]] = {}
    packages_by_target: Dict[Path, set[str]] = {}

    for item in artifacts:
        target_root = destination_root_for(item, variant_root, include_release_key)
        dst = target_root / item.payload_rel
        err = copy_unique(item.source_path, dst, item.sha256, force_overwrite=force_collision_override)
        if err:
            errors.append(err)
            continue

        idx = IndexedPackage(
            fields=item.fields,
            file_name=item.file_name,
            rel_filename=item.payload_rel,
            size=dst.stat().st_size,
            sha256=item.sha256 or sync.sha256_file(dst),
        )
        aggregate.setdefault(target_root, []).append(idx)
        pkg_name = item.fields.get("Package", "").strip()
        if pkg_name:
            packages_by_target.setdefault(target_root, set()).add(pkg_name)

        split_idx = IndexedPackage(
            fields=item.fields,
            file_name=item.file_name,
            rel_filename=item.file_name,
            size=dst.stat().st_size,
            sha256=item.sha256 or sync.sha256_file(dst),
        )
        split_indexes.setdefault(dst.parent, []).append(split_idx)

    indexed_dirs = 0

    for idx_dir, pkgs in split_indexes.items():
        errors.extend(write_packages_files(idx_dir, pkgs))
        sig_err = sign_packages_if_requested(idx_dir, usign_bin, sign_key)
        if sig_err:
            errors.append(sig_err)
        indexed_dirs += 1

    for idx_dir, pkgs in aggregate.items():
        errors.extend(write_packages_files(idx_dir, pkgs))
        sig_err = sign_packages_if_requested(idx_dir, usign_bin, sign_key)
        if sig_err:
            errors.append(sig_err)
        indexed_dirs += 1

    for target_root, present in sorted(packages_by_target.items(), key=lambda x: str(x[0])):
        missing_required = sorted(required_packages - present)
        present_optional = sorted(optional_packages & present)
        coverage_rows.append(
            {
                "target_root": str(target_root),
                "present_count": len(present),
                "missing_required": missing_required,
                "present_optional": present_optional,
            }
        )
        if strict_coverage and missing_required:
            errors.append(
                f"coverage check failed at {target_root}: missing required packages {missing_required}"
            )

    return errors, indexed_dirs, coverage_rows


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
    parser.add_argument(
        "--force-collision-override",
        action="store_true",
        help="continue build and overwrite when checksum collisions are detected on destination paths",
    )
    parser.add_argument("--sign-key", default=None, help="optional usign private key path")
    parser.add_argument(
        "--usign-bin",
        default="usign",
        help="preferred signer binary for OPKG signatures (fallback: usign -> signify-openbsd -> signify)",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    cfg = sync.load_config(Path(args.config))

    output_root = Path(args.output_root or cfg.get("output_root", "output"))
    manifest_root = Path(args.manifest_root or cfg.get("manifest_root", output_root / "manifests"))
    download_root = Path(args.download_root or cfg.get("download_root", output_root / "downloads"))
    repo_root = Path(args.repo_root or (output_root / "repos"))
    sign_key_value = args.sign_key or os.getenv("OPKG_SIGN_KEY_PATH")
    sign_key = Path(sign_key_value).expanduser().resolve() if sign_key_value else None
    strict_coverage, required_packages, optional_packages = load_coverage_policy(cfg)

    manifests = load_release_manifests(manifest_root)
    artifacts, errors = collect_ipk_artifacts(manifests, download_root)
    if sign_key is not None:
        if not sign_key.exists():
            errors.append(f"sign key not found: {sign_key}")
        if resolve_opkg_signer(args.usign_bin) is None:
            errors.append(
                "signing requested but no compatible OPKG signer found "
                "(tried preferred/usign/signify-openbsd/signify)"
            )
    rolling_latest = select_latest_release_per_version(artifacts)
    rolling_artifacts = [a for a in artifacts if rolling_latest.get(a.openwrt_version) == a.release_id]
    release_collision_report = build_path_collision_report(
        artifacts,
        repo_root / "opkg" / "releases",
        include_release_key=True,
    )
    rolling_collision_report = build_path_collision_report(
        rolling_artifacts,
        repo_root / "opkg" / "openwrt",
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
        print(json.dumps({
            "status": "ok",
            "mode": "dry-run",
            "release_manifests": len(manifests),
            "ipk_entries": len(artifacts),
            "errors": errors,
        }, indent=2))
        return 2 if errors and args.strict else 0

    if args.clean and (repo_root / "opkg").exists():
        shutil.rmtree(repo_root / "opkg")

    sync.write_json(
        manifest_root / "index" / "opkg_collision_report.json",
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
        artifacts,
        repo_root / "opkg" / "releases",
        usign_bin=args.usign_bin,
        sign_key=sign_key,
        include_release_key=True,
        force_collision_override=args.force_collision_override,
        strict_coverage=strict_coverage,
        required_packages=required_packages,
        optional_packages=optional_packages,
    )
    errors.extend(release_errors)

    rolling_errors, rolling_indexed_dirs, rolling_coverage = materialize_variant(
        rolling_artifacts,
        repo_root / "opkg" / "openwrt",
        usign_bin=args.usign_bin,
        sign_key=sign_key,
        include_release_key=False,
        force_collision_override=args.force_collision_override,
        strict_coverage=strict_coverage,
        required_packages=required_packages,
        optional_packages=optional_packages,
    )
    errors.extend(rolling_errors)

    coverage = {
        "strict": strict_coverage,
        "required_package_names": sorted(required_packages),
        "optional_package_names": sorted(optional_packages),
        "release_targets_checked": len(release_coverage),
        "rolling_targets_checked": len(rolling_coverage),
        "release_missing_required_count": sum(1 for row in release_coverage if row["missing_required"]),
        "rolling_missing_required_count": sum(1 for row in rolling_coverage if row["missing_required"]),
    }

    report = {
        "generated_at": sync.now_iso(),
        "release_manifests": len(manifests),
        "ipk_entries": len(artifacts),
        "indexed_dirs": release_indexed_dirs + rolling_indexed_dirs,
        "rolling_latest_release_by_openwrt": rolling_latest,
        "coverage": coverage,
        "collision_summary": {
            "collision_paths": collision_count,
            "duplicate_paths": int(release_collision_report.get("duplicate_paths", 0))
            + int(rolling_collision_report.get("duplicate_paths", 0)),
            "force_override": bool(args.force_collision_override),
        },
        "errors": errors,
    }

    report_path = manifest_root / "index" / "opkg_repo_report.json"
    sync.write_json(report_path, report)
    sync.write_json(
        manifest_root / "index" / "opkg_coverage_report.json",
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
