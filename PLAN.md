# Implementation Plan

## Phase 1: Repository Scaffold
- [x] Create project structure (`scripts/`, `config/`, `state/`, `output/`).
- [x] Define repository URL/layout conventions for `opkg` and `apk`.
- [x] Add config file(s) for upstream source, output paths, and signing settings.

## Phase 2: Release Discovery and Sync
- [x] Implement script to fetch upstream releases/tags from GitHub API.
- [x] Parse release assets and classify by format (`.ipk`, `.apk`) and arch.
- [x] Extract package versions from package metadata (`.ipk` control `Version`, `.apk` `.PKGINFO` `pkgver`).
- [x] Detect OpenWrt version from release metadata using configured patterns (initially `v22.x.x` through `v26.x.x`).
- [x] Fail release processing when OpenWrt version cannot be detected unambiguously.
- [x] Detect and skip snapshot releases with explicit logging/metrics.
- [x] Add resumable state tracking (`processed_release_ids.json` or similar).
- [x] Support three modes: `backfill` (all eligible historical), `incremental` (new only), and `clean-rebuild` (clear+rebuild).

## Phase 3: OPKG Feed Generation
- [x] Materialize `.ipk` assets into deterministic feed directories.
- [x] Generate `Packages` and compressed index (`Packages.gz`).
- [x] Add optional feed signatures (`Packages.sig`) with managed key handling.
- [x] Validate feed integrity and index/package consistency.
- [x] Restructure OPKG output to target-first layout (`targets/<target>/<subtarget>/...`) aligned with OpenWrt conventions.
- [x] Keep payloads split as `packages/` and `kmods/<kernel-release-hash>/` directories.
- [x] Generate aggregate top-level `Packages(.gz)` per target/subtarget with nested `Filename` references to split payload paths.

## Phase 4: APK Feed Generation
- [ ] Materialize `.apk` assets into deterministic feed directories.
- [ ] Generate `packages.adb` index with proper signing.
- [ ] Publish/distribute public key material required by clients.
- [ ] Validate APK index readability and signature correctness.

## Phase 5: Coverage and Quality Gates
- [ ] Add architecture/version coverage report per release.
- [ ] Fail pipeline when expected assets are missing or malformed.
- [ ] Detect package collisions `(name, arch, version)` and compare checksums.
- [ ] On checksum mismatch for same package identity, block rolling-feed promotion and emit conflict report.
- [ ] Add checksum verification for downloaded assets.
- [ ] Add retry logic and partial-failure handling for network/API issues.

## Phase 6: CI/CD Automation
- [x] Add GitHub Actions workflow for scheduled incremental sync.
- [x] Add manual workflow dispatch for full backfill/rebuild.
- [x] Generate immutable release-scoped feeds plus rolling aliases per OpenWrt version during repo build.
- [ ] Add workflow caching/artifact strategy to reduce runtime.
- [ ] Publish immutable release-scoped feeds plus rolling aliases per OpenWrt version.
- [ ] Publish generated output to `published-repos`.

## Phase 7: Operations and Documentation
- [ ] Document client feed setup examples for `opkg` and `apk`.
- [ ] Document key rotation/signing key management process.
- [ ] Add runbooks for recovery (failed run, bad release, rollback).
- [ ] Add monitoring signals (last successful sync, release lag, error count).

## Done Criteria
- [ ] Full historical upstream releases processed successfully.
- [ ] New upstream releases are synced automatically without manual edits.
- [ ] `opkg` and `apk` clients can install packages from published feeds.
- [ ] Feed outputs are reproducible and hosting-provider agnostic.
