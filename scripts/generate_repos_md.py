#!/usr/bin/env python3
"""Generate output/REPOS.md and output/RELEASES.md with feed URLs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import sync_releases as sync


def norm_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def openwrt_sort_key(version: str) -> Tuple[int, int, int]:
    parts = str(version).split(".")
    nums: List[int] = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def collect_opkg_rolling(repo_root: Path) -> List[Tuple[str, str, str, str]]:
    rows: List[Tuple[str, str, str, str]] = []
    for p in sorted((repo_root / "opkg" / "openwrt").glob("*/targets/*/*/Packages")):
        parts = p.relative_to(repo_root).parts
        # opkg/openwrt/<ow>/targets/<target>/<subtarget>/Packages
        if len(parts) != 7:
            continue
        _, _, openwrt_version, _, target, subtarget, _ = parts
        rel_url = f"opkg/openwrt/{openwrt_version}/targets/{target}/{subtarget}"
        rows.append((openwrt_version, target, subtarget, rel_url))
    return rows


def collect_opkg_releases(repo_root: Path) -> List[Tuple[str, str, str, str, str]]:
    rows: List[Tuple[str, str, str, str, str]] = []
    for p in sorted((repo_root / "opkg" / "releases").glob("*/**/Packages")):
        parts = p.relative_to(repo_root).parts
        # opkg/releases/<release_key>/<ow>/targets/<target>/<subtarget>/Packages
        if len(parts) != 8:
            continue
        if parts[4] != "targets":
            continue
        _, _, release_key, openwrt_version, _, target, subtarget, _ = parts
        rel_url = f"opkg/releases/{release_key}/{openwrt_version}/targets/{target}/{subtarget}"
        rows.append((release_key, openwrt_version, target, subtarget, rel_url))
    return sorted(rows)


def collect_apk_rolling(repo_root: Path) -> List[Tuple[str, str, str, str]]:
    rows: List[Tuple[str, str, str, str]] = []
    for p in sorted((repo_root / "apk" / "openwrt").glob("*/targets/*/*/packages.adb")):
        parts = p.relative_to(repo_root).parts
        # apk/openwrt/<ow>/targets/<target>/<subtarget>/packages.adb
        if len(parts) != 7:
            continue
        _, _, openwrt_version, _, target, subtarget, _ = parts
        rel_url = f"apk/openwrt/{openwrt_version}/targets/{target}/{subtarget}/packages.adb"
        rows.append((openwrt_version, target, subtarget, rel_url))
    return rows


def collect_apk_releases(repo_root: Path) -> List[Tuple[str, str, str, str, str]]:
    rows: List[Tuple[str, str, str, str, str]] = []
    for p in sorted((repo_root / "apk" / "releases").glob("*/**/packages.adb")):
        parts = p.relative_to(repo_root).parts
        # apk/releases/<release_key>/<ow>/targets/<target>/<subtarget>/packages.adb
        if len(parts) != 8:
            continue
        if parts[4] != "targets":
            continue
        _, _, release_key, openwrt_version, _, target, subtarget, _ = parts
        rel_url = f"apk/releases/{release_key}/{openwrt_version}/targets/{target}/{subtarget}/packages.adb"
        rows.append((release_key, openwrt_version, target, subtarget, rel_url))
    return sorted(rows)


def render_url(base_url: str, rel_url: str) -> str:
    if base_url:
        url = f"{base_url}/{rel_url}"
        return f"<{url}>"
    return f"`{rel_url}`"


def add_table(lines: List[str], headers: List[str], rows: List[List[str]]) -> None:
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")


def group_rolling_rows(
    opkg_roll: List[Tuple[str, str, str, str]],
    apk_roll: List[Tuple[str, str, str, str]],
) -> Dict[str, List[Tuple[str, str, str, str]]]:
    merged: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    for ow, target, subtarget, rel_url in opkg_roll:
        merged.setdefault((ow, target, subtarget), {})["opkg"] = rel_url
    for ow, target, subtarget, rel_url in apk_roll:
        merged.setdefault((ow, target, subtarget), {})["apk"] = rel_url

    by_version: Dict[str, List[Tuple[str, str, str, str]]] = {}
    for (ow, target, subtarget), urls in sorted(merged.items()):
        by_version.setdefault(ow, []).append(
            (target, subtarget, urls.get("opkg", ""), urls.get("apk", ""))
        )
    return by_version


def group_release_rows(
    opkg_rel: List[Tuple[str, str, str, str, str]],
    apk_rel: List[Tuple[str, str, str, str, str]],
) -> Dict[str, List[Tuple[str, str, str, str, str]]]:
    merged: Dict[Tuple[str, str, str, str], Dict[str, str]] = {}
    for release_key, ow, target, subtarget, rel_url in opkg_rel:
        merged.setdefault((ow, release_key, target, subtarget), {})["opkg"] = rel_url
    for release_key, ow, target, subtarget, rel_url in apk_rel:
        merged.setdefault((ow, release_key, target, subtarget), {})["apk"] = rel_url

    by_version: Dict[str, List[Tuple[str, str, str, str, str]]] = {}
    for (ow, release_key, target, subtarget), urls in sorted(merged.items()):
        by_version.setdefault(ow, []).append(
            (release_key, target, subtarget, urls.get("opkg", ""), urls.get("apk", ""))
        )
    return by_version


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate output/REPOS.md and output/RELEASES.md")
    p.add_argument("--config", default="config/settings.json", help="path to config json")
    p.add_argument("--repo-root", default=None, help="override repos root (default output/repos)")
    p.add_argument("--output", default=None, help="rolling output markdown path (default output/REPOS.md)")
    p.add_argument(
        "--releases-output",
        default=None,
        help="immutable release output markdown path (default output/RELEASES.md)",
    )
    p.add_argument("--base-url", default=None, help="override public base URL")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = sync.load_config(Path(args.config))

    output_root = Path(cfg.get("output_root", "output"))
    repo_root = Path(args.repo_root or (output_root / "repos"))
    out_path = Path(args.output or (output_root / "REPOS.md"))
    releases_out_path = Path(args.releases_output or (output_root / "RELEASES.md"))

    base_url = norm_base_url(args.base_url if args.base_url is not None else str(cfg.get("public_base_url", "")))

    opkg_roll = collect_opkg_rolling(repo_root)
    apk_roll = collect_apk_rolling(repo_root)
    opkg_rel = collect_opkg_releases(repo_root)
    apk_rel = collect_apk_releases(repo_root)

    generated_at = sync.now_iso()

    lines: List[str] = []
    lines.append("# Repository URLs")
    lines.append("")
    lines.append(f"Generated at: `{generated_at}`")
    lines.append(f"Base URL: `{base_url or '(not set; relative paths shown)'}`")
    lines.append("")
    rolling_by_version = group_rolling_rows(opkg_roll, apk_roll)
    for ow in sorted(rolling_by_version, key=openwrt_sort_key, reverse=True):
        lines.append(f"### OpenWrt {ow}")
        lines.append("")
        add_table(
            lines,
            ["Target", "Subtarget", "OPKG", "APK"],
            [
                [
                    target,
                    subtarget,
                    render_url(base_url, opkg_url) if opkg_url else "",
                    render_url(base_url, apk_url) if apk_url else "",
                ]
                for target, subtarget, opkg_url, apk_url in rolling_by_version[ow]
            ],
        )
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")

    release_lines: List[str] = []
    release_lines.append("# Immutable Release URLs")
    release_lines.append("")
    release_lines.append(f"Generated at: `{generated_at}`")
    release_lines.append(f"Base URL: `{base_url or '(not set; relative paths shown)'}`")
    release_lines.append("")
    release_by_version = group_release_rows(opkg_rel, apk_rel)
    for ow in sorted(release_by_version, key=openwrt_sort_key, reverse=True):
        release_lines.append(f"### OpenWrt {ow}")
        release_lines.append("")
        add_table(
            release_lines,
            ["Release Key", "Target", "Subtarget", "OPKG", "APK"],
            [
                [
                    release_key,
                    target,
                    subtarget,
                    render_url(base_url, opkg_url) if opkg_url else "",
                    render_url(base_url, apk_url) if apk_url else "",
                ]
                for release_key, target, subtarget, opkg_url, apk_url in release_by_version[ow]
            ],
        )
        release_lines.append("")

    releases_out_path.parent.mkdir(parents=True, exist_ok=True)
    releases_out_path.write_text("\n".join(release_lines), encoding="utf-8")

    print(
        json.dumps(
            {
            "status": "ok",
            "output": str(out_path),
            "releases_output": str(releases_out_path),
            "opkg_rolling": len(opkg_roll),
            "apk_rolling": len(apk_roll),
            "opkg_releases": len(opkg_rel),
            "apk_releases": len(apk_rel),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
