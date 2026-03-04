# AWG OpenWrt Repository Automation Spec

## Overview
This project mirrors upstream releases from `Slava-Shchipunov/awg-openwrt` and publishes ready-to-use package feeds for:
- `opkg` consumers (OpenWrt pre-25.12 package workflows, `.ipk`)
- `apk` consumers (OpenWrt 25.12+ package workflows, `.apk`)

The system must:
- Backfill all historical releases once.
- Continue incrementally as new releases appear.
- Cover all architectures provided upstream.
- Publish reliably through static hosting, starting with a dedicated publishing branch.

## Scope
- Ingest release assets from upstream GitHub releases.
- Build feed metadata/indexes/signatures so clients can use the feed directly.
- Publish generated repositories to static hosting.
- Track processing state to keep runs idempotent and resumable.

## Out of Scope (for now)
- Building packages from source (only ingest upstream release assets).
- Multi-provider deployment in v1 (start with one hosting target; keep layout portable).

## High-Level Requirements
- Support both formats: `.ipk` and `.apk`.
- Deterministic output layout by OpenWrt version and architecture.
- Signed feed metadata where required by clients.
- Automated CI execution for both backfill and incremental sync.
- Validation checks for missing assets/architectures per release.
- Package version must be extracted from package metadata, not release names/tags.
- OpenWrt version must be detected from release metadata (tag/title/body) with strict validation.
- Ignore snapshot releases/assets entirely.
- Support both immutable per-release outputs and rolling per-OpenWrt-version outputs.

## Version Detection Rules
- For `.ipk`, read package version from control metadata field `Version` (plus related identity fields).
- For `.apk`, read package version from `.PKGINFO` field `pkgver` (plus related identity fields).
- Detect OpenWrt version from release metadata using configured patterns (initially `v22.x.x` through `v26.x.x` styles).
- If OpenWrt version cannot be determined unambiguously for a release, fail that release processing and surface a clear error.
- Persist provenance fields for each package entry (`source_release_tag`, `source_release_id`, `published_at`).
- If release metadata indicates `snapshot`, skip the release and log the skip reason.

## Conflict and Update Policy
- Multiple upstream releases may target the same OpenWrt version; this is expected.
- Publish immutable release-scoped feeds keyed by upstream release tag/id.
- Publish a rolling alias per OpenWrt version that points to the latest successful release feed.
- Detect package identity collisions by `(name, architecture, package_version)` with checksum comparison.
- If same identity and same checksum: treat as duplicate and skip.
- If same identity and different checksum: mark as conflict and fail promotion to rolling alias by default.
- Optional override mode may force promotion, but must emit a high-severity warning and conflict report.
- Do not mutate package metadata to fabricate a package `-rN` release value; for binary assets this would require rebuild/repack and is out of scope for v1.

## Rebuild and Recovery Modes
- Incremental mode: process only unseen/changed upstream releases.
- Backfill mode: process all eligible historical releases.
- Clean rebuild mode: clear generated output and state, then rebuild from scratch deterministically.

## Hosting Target (v1)
- GitHub Pages (publishing from `published-repos` branch) serving static feed files.
- Directory structure designed to be reusable on S3/R2 later.
