#!/usr/bin/env python3
"""Generate output/REPOS.md with feed URLs for OPKG/APK repos."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Tuple

import sync_releases as sync


def norm_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


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
        return f"{base_url}/{rel_url}"
    return rel_url


def add_table(lines: List[str], headers: List[str], rows: List[List[str]]) -> None:
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate output/REPOS.md")
    p.add_argument("--config", default="config/settings.json", help="path to config json")
    p.add_argument("--repo-root", default=None, help="override repos root (default output/repos)")
    p.add_argument("--output", default=None, help="output markdown path (default output/REPOS.md)")
    p.add_argument("--base-url", default=None, help="override public base URL")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = sync.load_config(Path(args.config))

    output_root = Path(cfg.get("output_root", "output"))
    repo_root = Path(args.repo_root or (output_root / "repos"))
    out_path = Path(args.output or (output_root / "REPOS.md"))

    base_url = norm_base_url(args.base_url if args.base_url is not None else str(cfg.get("public_base_url", "")))

    opkg_roll = collect_opkg_rolling(repo_root)
    apk_roll = collect_apk_rolling(repo_root)
    opkg_rel = collect_opkg_releases(repo_root)
    apk_rel = collect_apk_releases(repo_root)

    lines: List[str] = []
    lines.append("# Repository URLs")
    lines.append("")
    lines.append(f"Generated at: `{sync.now_iso()}`")
    lines.append(f"Base URL: `{base_url or '(not set; relative paths shown)'}`")
    lines.append("")

    lines.append("## Rolling (latest per OpenWrt version)")
    lines.append("")
    lines.append("### OPKG")
    lines.append("")
    add_table(
        lines,
        ["OpenWrt", "Target", "Subtarget", "Combined Feed URL"],
        [[ow, t, s, render_url(base_url, u)] for ow, t, s, u in opkg_roll],
    )
    lines.append("")

    lines.append("### APK")
    lines.append("")
    add_table(
        lines,
        ["OpenWrt", "Target", "Subtarget", "Combined Feed URL"],
        [[ow, t, s, render_url(base_url, u)] for ow, t, s, u in apk_roll],
    )
    lines.append("")

    lines.append("## Immutable Release-Scoped")
    lines.append("")
    lines.append("### OPKG")
    lines.append("")
    add_table(
        lines,
        ["Release Key", "OpenWrt", "Target", "Subtarget", "Combined Feed URL"],
        [[rk, ow, t, s, render_url(base_url, u)] for rk, ow, t, s, u in opkg_rel],
    )
    lines.append("")

    lines.append("### APK")
    lines.append("")
    add_table(
        lines,
        ["Release Key", "OpenWrt", "Target", "Subtarget", "Combined Feed URL"],
        [[rk, ow, t, s, render_url(base_url, u)] for rk, ow, t, s, u in apk_rel],
    )
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")

    print(
        json.dumps(
            {
            "status": "ok",
            "output": str(out_path),
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
