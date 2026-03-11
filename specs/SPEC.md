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
- For OPKG, preserve target/subtarget and kernel-hash payload separation while also providing a top-level aggregate `Packages(.gz)` view per target/subtarget for easier client configuration.
- For APK, preserve target/subtarget and kernel-hash payload separation while also providing a top-level aggregate `packages.adb` view per target/subtarget for easier single-URL client configuration.

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

## OPKG Layout Policy
- Maintain target-first OPKG tree with split payload locations:
- `targets/<target>/<subtarget>/packages/*.ipk`
- `targets/<target>/<subtarget>/kmods/<kernel-release-hash>/*.ipk`
- Generate a top-level aggregate index per target/subtarget:
- `targets/<target>/<subtarget>/Packages` and `Packages.gz`
- Aggregate index entries must use nested `Filename` paths (for example `packages/...` and `kmods/<kernel-hash>/...`).
- Keep `kmod-*` packages isolated by kernel-hash path on disk; do not flatten kmods across hashes.
- OPKG `Packages` stanzas must remain parser-safe for multiline metadata:
- emit `Filename`, `Size`, and `SHA256sum` before multiline `Description`;
- encode continuation lines with a leading space and encode blank description paragraph lines as ` .`.
- OPKG `Packages` files must end with an explicit trailing blank stanza separator (`\n\n`) so LuCI package manager parsing (`cgi-exec` -> `/usr/libexec/package-manager-call list-available`) does not drop the last package entry.

## Rebuild and Recovery Modes
- Incremental mode: process only unseen release IDs or previously seen releases whose asset set changed.
- Incremental change detection gate uses upstream release assets (`name`, `browser_download_url`, `size`, `updated_at`) and `last_successful_sync_at`:
- new release ID -> process;
- known release with changed asset fingerprint or asset `updated_at` newer than last successful sync -> process;
- otherwise skip release download/build/publish.
- Backfill mode: process all eligible historical releases.
- Clean rebuild mode: clear generated output and state, then rebuild from scratch deterministically.

## Hosting Target (v1)
- GitHub Pages (publishing from `published-repos` branch) serving static feed files.
- Directory structure designed to be reusable on S3/R2 later.

## Client Installer (`install.sh`)
- Provide a single canonical installer script at `installer/install.sh`.
- Publish that script verbatim as `install.sh` at the root of `published-repos` (for `curl | ash` usage).
- Installer behavior requirements:
- Detect package manager mode (`opkg` or `apk`) from runtime environment.
- Detect OpenWrt `version`, `target`, and `subtarget` using native runtime sources:
- primary: `ubus call system board`;
- fallback: `/etc/openwrt_release` (`DISTRIB_RELEASE`, `DISTRIB_TARGET`).
- Compute feed URL from configured base URL + detected version/target/subtarget.
- Validate feed availability before config mutation:
- OPKG: probe `.../Packages.gz`;
- APK: probe `.../packages.adb`.
- Remove existing AWG feed entries first, then add the new feed entry (avoid stale feeds across upgrades).
- Re-enroll signing keys on every run before update/install.
- Run package index update and install package set:
- required: `kmod-amneziawg`, `amneziawg-tools`;
- default-install: `luci-proto-amneziawg`; skip only when explicitly disabled (`--skip-luci`) or when unavailable in the selected feed.
- Prompt user to reboot (`y/n`) at end, with non-interactive-safe default behavior.

## Incremental Publish Gate Extension
- Incremental sync/publish gating must trigger publish when either:
- upstream releases changed (new or changed release assets), or
- `installer/install.sh` content changed.
- This prevents no-op publish PRs while still propagating installer updates promptly.
