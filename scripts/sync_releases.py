#!/usr/bin/env python3
"""Sync awg-openwrt GitHub releases and extract package metadata.

Phase 1/2 scope:
- Repository scaffold conventions and config loading.
- Release discovery from GitHub API.
- Snapshot filtering.
- OpenWrt version detection from release metadata.
- Asset classification (.ipk/.apk) and package metadata extraction.
- Stateful incremental/backfill/clean-rebuild execution.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_CONFIG = Path("config/settings.json")


class SyncError(Exception):
    """Raised for recoverable sync errors."""


@dataclass
class AssetMetadata:
    file_name: str
    file_type: str
    arch: Optional[str]
    package_name: Optional[str]
    package_version: Optional[str]
    sha256: str
    source_url: str
    source_size: int
    source_updated_at: Optional[str]


@dataclass
class NetworkRetryConfig:
    max_retries: int = 4
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 20.0
    timeout_api_seconds: int = 60
    timeout_asset_seconds: int = 180


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def log_progress(message: str) -> None:
    print(f"[sync] {now_iso()} {message}", file=sys.stderr, flush=True)


def load_config(path: Path) -> Dict[str, Any]:
    cfg = read_json(path, fallback=None)
    if not isinstance(cfg, dict):
        raise SyncError(f"Invalid config file: {path}")
    return cfg


def load_network_retry_config(cfg: Dict[str, Any]) -> NetworkRetryConfig:
    net = cfg.get("sync", {}).get("network", {})
    if not isinstance(net, dict):
        net = {}
    return NetworkRetryConfig(
        max_retries=max(0, int(net.get("max_retries", 4))),
        backoff_initial_seconds=max(0.1, float(net.get("backoff_initial_seconds", 1.0))),
        backoff_max_seconds=max(0.1, float(net.get("backoff_max_seconds", 20.0))),
        timeout_api_seconds=max(1, int(net.get("timeout_api_seconds", 60))),
        timeout_asset_seconds=max(1, int(net.get("timeout_asset_seconds", 180))),
    )


def is_retryable_network_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        code = getattr(exc, "code", None)
        return isinstance(code, int) and code in {408, 409, 425, 429, 500, 502, 503, 504}
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    return False


def run_with_retries(fn, retry_cfg: NetworkRetryConfig):
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= retry_cfg.max_retries or not is_retryable_network_error(exc):
                raise
            sleep_for = min(
                retry_cfg.backoff_max_seconds,
                retry_cfg.backoff_initial_seconds * (2 ** attempt),
            )
            time.sleep(sleep_for)
            attempt += 1


def http_json(url: str, token: Optional[str], retry_cfg: NetworkRetryConfig) -> Any:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "awg-openwrt-reops-sync")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        def do_request():
            with urllib.request.urlopen(req, timeout=retry_cfg.timeout_api_seconds) as resp:
                return resp.read()

        raw = run_with_retries(do_request, retry_cfg)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SyncError(f"GitHub API error {exc.code}: {body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"GitHub API connection failed: {exc}") from exc

    return json.loads(raw.decode("utf-8"))


def fetch_all_releases(
    api_base: str,
    repo: str,
    token: Optional[str],
    retry_cfg: NetworkRetryConfig,
) -> List[Dict[str, Any]]:
    releases: List[Dict[str, Any]] = []
    page = 1
    while True:
        url = f"{api_base}/repos/{repo}/releases?per_page=100&page={page}"
        page_payload = http_json(url, token, retry_cfg)
        if not isinstance(page_payload, list):
            raise SyncError("Unexpected GitHub API payload for releases")
        if not page_payload:
            break
        releases.extend(page_payload)
        page += 1
    releases.sort(key=lambda r: r.get("published_at") or "")
    return releases


def build_detection_patterns(cfg: Dict[str, Any]) -> List[re.Pattern[str]]:
    patterns = cfg.get("release_detection", {}).get("openwrt_version_patterns", [])
    compiled: List[re.Pattern[str]] = []
    for item in patterns:
        compiled.append(re.compile(item, re.IGNORECASE))
    if not compiled:
        raise SyncError("No release_detection.openwrt_version_patterns configured")
    return compiled


def is_snapshot_release(rel: Dict[str, Any], ignore_snapshot: bool) -> bool:
    if not ignore_snapshot:
        return False
    blob = "\n".join(
        [
            str(rel.get("tag_name") or ""),
            str(rel.get("name") or ""),
            str(rel.get("body") or ""),
        ]
    )
    return bool(re.search(r"\bsnapshot\b", blob, re.IGNORECASE))


def detect_openwrt_version(rel: Dict[str, Any], patterns: List[re.Pattern[str]]) -> str:
    haystack_fields = [
        str(rel.get("tag_name") or ""),
        str(rel.get("name") or ""),
        str(rel.get("body") or ""),
    ]
    found: set[str] = set()
    for field in haystack_fields:
        for pattern in patterns:
            for match in pattern.finditer(field):
                raw = match.group(1) if match.groups() else match.group(0)
                cleaned = raw.lstrip("vV")
                found.add(cleaned)

    if len(found) == 1:
        return next(iter(found))
    if not found:
        raise SyncError("openwrt version not detected in release metadata")
    raise SyncError(f"ambiguous openwrt version candidates: {sorted(found)}")


def classify_asset(name: str) -> Optional[str]:
    lower = name.lower()
    if lower.endswith(".ipk"):
        return "ipk"
    if lower.endswith(".apk"):
        return "apk"
    return None


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def download_asset(url: str, dst: Path, token: Optional[str], retry_cfg: NetworkRetryConfig) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/octet-stream")
    req.add_header("User-Agent", "awg-openwrt-reops-sync")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    tmp_dst = dst.with_suffix(f"{dst.suffix}.part")
    try:
        def do_download():
            with urllib.request.urlopen(req, timeout=retry_cfg.timeout_asset_seconds) as resp, tmp_dst.open("wb") as out:
                shutil.copyfileobj(resp, out)
            tmp_dst.replace(dst)

        run_with_retries(do_download, retry_cfg)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SyncError(f"Asset download error {exc.code} for {url}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"Asset download connection failed for {url}: {exc}") from exc
    finally:
        if tmp_dst.exists():
            tmp_dst.unlink(missing_ok=True)


def parse_control_blob(raw: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    current: Optional[str] = None
    for line in raw.splitlines():
        if not line:
            continue
        if line.startswith((" ", "\t")) and current:
            fields[current] = f"{fields[current]}\n{line.strip()}"
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        current = key.strip()
        fields[current] = val.strip()
    return fields


def parse_kv_blob(raw: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        fields[key.strip()] = val.strip()
    return fields


def extract_ar_members(data: bytes) -> Iterable[Tuple[str, bytes]]:
    # Debian ar archive format (used by .ipk): global header + member headers.
    if len(data) < 8 or data[:8] != b"!<arch>\n":
        raise SyncError("invalid ar archive header")
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset : offset + 60]
        offset += 60

        name = header[0:16].decode("utf-8", errors="replace").strip()
        size_raw = header[48:58].decode("ascii", errors="replace").strip()
        trailer = header[58:60]
        if trailer != b"`\n":
            raise SyncError("invalid ar member header trailer")
        try:
            size = int(size_raw)
        except ValueError as exc:
            raise SyncError(f"invalid ar member size: {size_raw}") from exc

        payload = data[offset : offset + size]
        if len(payload) != size:
            raise SyncError("truncated ar archive")
        offset += size
        if offset % 2 == 1:
            offset += 1

        cleaned_name = name.rstrip("/")
        yield cleaned_name, payload


def extract_ipk_metadata(path: Path) -> Dict[str, Optional[str]]:
    fields = read_ipk_control_fields(path)

    arch = fields.get("Architecture")
    name = fields.get("Package")
    version = fields.get("Version")
    return {"arch": arch, "package_name": name, "package_version": version}


def read_ipk_control_fields(path: Path) -> Dict[str, str]:
    data = path.read_bytes()
    control_data: Optional[bytes] = None

    if len(data) >= 8 and data[:8] == b"!<arch>\n":
        for member_name, payload in extract_ar_members(data):
            if member_name.startswith("control.tar"):
                control_data = payload
                break
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as outer:
            control_member = next(
                (
                    m
                    for m in outer.getmembers()
                    if m.isfile() and m.name.lstrip("./").startswith("control.tar")
                ),
                None,
            )
            if control_member is None:
                raise SyncError("control archive not found in ipk outer tar")
            raw = outer.extractfile(control_member)
            if raw is None:
                raise SyncError("failed reading ipk control archive from outer tar")
            control_data = raw.read()

    if control_data is None:
        raise SyncError("control archive not found in ipk")

    with tarfile.open(fileobj=io.BytesIO(control_data), mode="r:*") as control_tar:
        control_meta = next(
            (
                m
                for m in control_tar.getmembers()
                if m.isfile() and (m.name == "control" or m.name.endswith("/control"))
            ),
            None,
        )
        if control_meta is None:
            raise SyncError("control metadata file not found in ipk control archive")
        raw_meta = control_tar.extractfile(control_meta)
        if raw_meta is None:
            raise SyncError("failed reading ipk control metadata")
        return parse_control_blob(raw_meta.read().decode("utf-8", errors="replace"))


def extract_apk_metadata(path: Path) -> Dict[str, Optional[str]]:
    # OpenWrt/APK v3 packages are ADB-based; use apk-tools metadata dump.
    cmd = ["apk", "adbdump", str(path)]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise SyncError("apk binary not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise SyncError(f"apk adbdump failed: {detail}") from exc

    name: Optional[str] = None
    version: Optional[str] = None
    arch: Optional[str] = None

    in_info = False
    for line in proc.stdout.splitlines():
        if not in_info:
            if line.strip() == "info:":
                in_info = True
            continue
        if not line.startswith("  "):
            break
        body = line[2:]
        if ":" not in body:
            continue
        key, val = body.split(":", 1)
        key = key.strip()
        val = val.strip()
        if not val or val.startswith("#"):
            continue
        if key == "name":
            name = val
        elif key == "version":
            version = val
        elif key == "arch":
            arch = val

    return {"arch": arch, "package_name": name, "package_version": version}


def extract_metadata_for_asset(path: Path, file_type: str) -> Dict[str, Optional[str]]:
    if file_type == "ipk":
        return extract_ipk_metadata(path)
    if file_type == "apk":
        return extract_apk_metadata(path)
    raise SyncError(f"unsupported package type: {file_type}")


def infer_arch_from_filename(name: str, file_type: str) -> Optional[str]:
    if file_type == "ipk":
        # Typical ipk file: <pkg>_<version>_<arch>.ipk
        parts = name[:-4].split("_")
        if len(parts) >= 3:
            return parts[-1]
    return None


def load_state(path: Path, repo: str, publish_branch: str) -> Dict[str, Any]:
    state = read_json(path, fallback=None)
    if not isinstance(state, dict):
        state = {}
    state.setdefault("schema_version", 1)
    state.setdefault("upstream_repo", repo)
    state.setdefault("publish_branch", publish_branch)
    state.setdefault("processed_release_ids", [])
    state.setdefault("release_results", {})
    return state


def ensure_dirs(paths: Iterable[Path]) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def reset_for_clean_rebuild(state_path: Path, output_root: Path, state: Dict[str, Any]) -> Dict[str, Any]:
    if output_root.exists():
        shutil.rmtree(output_root)
    if state_path.exists():
        state_path.unlink()
    fresh = {
        "schema_version": state.get("schema_version", 1),
        "upstream_repo": state.get("upstream_repo"),
        "publish_branch": state.get("publish_branch"),
        "processed_release_ids": [],
        "release_results": {},
    }
    return fresh


def should_process_release(mode: str, rel_id: int, processed: set[int]) -> bool:
    if mode in {"backfill", "clean-rebuild"}:
        return True
    if mode == "incremental":
        return rel_id not in processed
    raise SyncError(f"unsupported mode: {mode}")


def process_release(
    rel: Dict[str, Any],
    cfg: Dict[str, Any],
    patterns: List[re.Pattern[str]],
    token: Optional[str],
    download_root: Path,
    manifest_root: Path,
    dry_run: bool,
    max_workers: int,
    retry_cfg: NetworkRetryConfig,
) -> Tuple[str, Dict[str, Any]]:
    rel_id = rel.get("id")
    rel_tag = rel.get("tag_name")
    if not isinstance(rel_id, int):
        raise SyncError("release missing numeric id")

    ignore_snapshot = bool(cfg.get("release_detection", {}).get("ignore_snapshot", True))
    if is_snapshot_release(rel, ignore_snapshot):
        return "skipped", {
            "release_id": rel_id,
            "tag_name": rel_tag,
            "status": "skipped",
            "skip_reason": "snapshot_release",
            "processed_at": now_iso(),
        }

    openwrt_version = detect_openwrt_version(rel, patterns)

    assets: List[AssetMetadata] = []
    errors: List[str] = []
    candidates: List[Tuple[int, Dict[str, Any], str, str]] = []
    for idx, asset in enumerate(rel.get("assets", [])):
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        file_type = classify_asset(name)
        if file_type is None:
            continue
        candidates.append((idx, asset, name, file_type))

    def process_asset_candidate(
        idx: int,
        raw_asset: Dict[str, Any],
        name: str,
        file_type: str,
    ) -> Tuple[int, Optional[AssetMetadata], Optional[str]]:
        url = raw_asset.get("browser_download_url")
        if not isinstance(url, str):
            return idx, None, f"asset {name}: missing browser_download_url"

        source_size = int(raw_asset.get("size") or 0)
        source_updated_at = raw_asset.get("updated_at")
        target_path = download_root / str(rel_id) / name

        metadata: Dict[str, Optional[str]] = {
            "arch": infer_arch_from_filename(name, file_type),
            "package_name": None,
            "package_version": None,
        }
        sha256 = ""

        try:
            if not dry_run:
                if not target_path.exists():
                    download_asset(url, target_path, token, retry_cfg)
                sha256 = sha256_file(target_path)
                parsed = extract_metadata_for_asset(target_path, file_type)
                metadata.update({k: v for k, v in parsed.items() if v})
            else:
                sha256 = "dry-run"
        except Exception as exc:
            return idx, None, f"asset {name}: {exc}"

        return (
            idx,
            AssetMetadata(
                file_name=name,
                file_type=file_type,
                arch=metadata.get("arch"),
                package_name=metadata.get("package_name"),
                package_version=metadata.get("package_version"),
                sha256=sha256,
                source_url=url,
                source_size=source_size,
                source_updated_at=source_updated_at,
            ),
            None,
        )

    processed_assets: List[Tuple[int, AssetMetadata]] = []
    if max_workers <= 1 or len(candidates) <= 1:
        for idx, raw_asset, name, file_type in candidates:
            pos, item, err = process_asset_candidate(idx, raw_asset, name, file_type)
            if err:
                errors.append(err)
                continue
            if item is not None:
                processed_assets.append((pos, item))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(process_asset_candidate, idx, raw_asset, name, file_type)
                for idx, raw_asset, name, file_type in candidates
            ]
            for fut in concurrent.futures.as_completed(futures):
                pos, item, err = fut.result()
                if err:
                    errors.append(err)
                    continue
                if item is not None:
                    processed_assets.append((pos, item))

    for _idx, item in sorted(processed_assets, key=lambda x: x[0]):
        assets.append(item)

    if errors:
        status = "error"
    else:
        status = "processed"

    record = {
        "release_id": rel_id,
        "tag_name": rel_tag,
        "name": rel.get("name"),
        "published_at": rel.get("published_at"),
        "status": status,
        "openwrt_version": openwrt_version,
        "asset_count": len(assets),
        "assets": [a.__dict__ for a in assets],
        "errors": errors,
        "processed_at": now_iso(),
    }

    if not dry_run:
        manifest_path = manifest_root / "releases" / f"{rel_id}.json"
        write_json(manifest_path, record)

    return status, record


def build_release_summary(state: Dict[str, Any], manifest_root: Path, dry_run: bool) -> None:
    by_version: Dict[str, Dict[str, Any]] = {}

    for value in state.get("release_results", {}).values():
        if not isinstance(value, dict):
            continue
        if value.get("status") != "processed":
            continue
        version = value.get("openwrt_version")
        if not isinstance(version, str):
            continue
        bucket = by_version.setdefault(
            version,
            {
                "openwrt_version": version,
                "release_ids": [],
                "releases": [],
            },
        )
        rid = value.get("release_id")
        bucket["release_ids"].append(rid)
        bucket["releases"].append(
            {
                "release_id": rid,
                "tag_name": value.get("tag_name"),
                "published_at": value.get("published_at"),
                "asset_count": value.get("asset_count"),
                "status": value.get("status"),
            }
        )

    if not dry_run:
        out = manifest_root / "index" / "openwrt_versions.json"
        write_json(out, {"generated_at": now_iso(), "versions": by_version})


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync upstream AWG OpenWrt releases")
    parser.add_argument(
        "--mode",
        choices=["incremental", "backfill", "clean-rebuild"],
        default="incremental",
        help="sync mode",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config json")
    parser.add_argument("--state-file", default=None, help="override state file path")
    parser.add_argument("--output-root", default=None, help="override output root path")
    parser.add_argument("--publish-branch", default=None, help="override publish branch name")
    parser.add_argument("--github-token", default=os.getenv("GITHUB_TOKEN"), help="GitHub token")
    parser.add_argument(
        "--release-id",
        action="append",
        type=int,
        default=[],
        help="optional release id filter (repeatable)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=6,
        help="max parallel asset workers per release (default: 6)",
    )
    parser.add_argument("--dry-run", action="store_true", help="discover only, no downloads/writes")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    cfg_path = Path(args.config)
    started_at = time.monotonic()

    try:
        cfg = load_config(cfg_path)

        repo = str(cfg.get("upstream_repo"))
        api_base = str(cfg.get("api_base", "https://api.github.com"))
        output_root = Path(args.output_root or cfg.get("output_root", "output"))
        download_root = Path(cfg.get("download_root", output_root / "downloads"))
        manifest_root = Path(cfg.get("manifest_root", output_root / "manifests"))
        publish_branch = args.publish_branch or str(cfg.get("publish_branch", "published-repos"))
        state_path = Path(args.state_file or cfg.get("state_file", "state/processed_release_ids.json"))
        jobs = int(cfg.get("sync", {}).get("jobs", args.jobs))
        if jobs < 1:
            raise SyncError("jobs must be >= 1")

        state = load_state(state_path, repo=repo, publish_branch=publish_branch)
        if args.mode == "clean-rebuild":
            state = reset_for_clean_rebuild(state_path, output_root, state)

        ensure_dirs([output_root, download_root, manifest_root, state_path.parent])

        patterns = build_detection_patterns(cfg)
        retry_cfg = load_network_retry_config(cfg)
        releases = fetch_all_releases(
            api_base=api_base,
            repo=repo,
            token=args.github_token,
            retry_cfg=retry_cfg,
        )
        log_progress(
            f"fetched {len(releases)} releases from {repo}; mode={args.mode}; dry_run={args.dry_run}; jobs={jobs}"
        )
        release_filter = set(args.release_id or [])
        processed_ids = {int(rid) for rid in state.get("processed_release_ids", [])}

        run_stats = {
            "seen_releases": 0,
            "processed": 0,
            "skipped": 0,
            "errors": 0,
        }

        for rel in releases:
            rel_id = rel.get("id")
            if not isinstance(rel_id, int):
                continue
            if release_filter and rel_id not in release_filter:
                continue
            run_stats["seen_releases"] += 1
            if not should_process_release(args.mode, rel_id, processed_ids):
                run_stats["skipped"] += 1
                log_progress(
                    f"release_id={rel_id} tag={rel.get('tag_name') or ''}: skipped (already processed in incremental mode)"
                )
                continue

            log_progress(
                f"release_id={rel_id} tag={rel.get('tag_name') or ''}: processing start"
            )
            try:
                status, record = process_release(
                    rel=rel,
                    cfg=cfg,
                    patterns=patterns,
                    token=args.github_token,
                    download_root=download_root,
                    manifest_root=manifest_root,
                    dry_run=args.dry_run,
                    max_workers=jobs,
                    retry_cfg=retry_cfg,
                )
            except Exception as exc:
                status = "error"
                record = {
                    "release_id": rel_id,
                    "tag_name": rel.get("tag_name"),
                    "status": "error",
                    "error": str(exc),
                    "processed_at": now_iso(),
                }

            state["release_results"][str(rel_id)] = record
            log_progress(
                "release_id={rid} tag={tag}: status={status} openwrt={ow} assets={assets} errors={errs}".format(
                    rid=rel_id,
                    tag=record.get("tag_name") or "",
                    status=status,
                    ow=record.get("openwrt_version") or "-",
                    assets=record.get("asset_count") or 0,
                    errs=len(record.get("errors") or []) + (1 if record.get("error") else 0),
                )
            )
            if status == "processed":
                run_stats["processed"] += 1
                if rel_id not in processed_ids:
                    state["processed_release_ids"].append(rel_id)
                    processed_ids.add(rel_id)
            elif status == "skipped":
                run_stats["skipped"] += 1
                if rel_id not in processed_ids:
                    state["processed_release_ids"].append(rel_id)
                    processed_ids.add(rel_id)
            else:
                run_stats["errors"] += 1

        state["publish_branch"] = publish_branch
        state["last_mode"] = args.mode
        state["last_run_at"] = now_iso()
        state["last_run_summary"] = run_stats

        if not args.dry_run:
            write_json(state_path, state)
            build_release_summary(state, manifest_root, dry_run=False)

        elapsed = time.monotonic() - started_at
        log_progress(
            f"completed run in {elapsed:.1f}s: seen={run_stats['seen_releases']} processed={run_stats['processed']} skipped={run_stats['skipped']} errors={run_stats['errors']}"
        )
        print(json.dumps({"status": "ok", "summary": run_stats, "mode": args.mode}, indent=2))
        if run_stats["errors"] > 0:
            return 2
        return 0
    except SyncError as exc:
        print(f"sync error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
