# OpenWrt OPKG Repository Specification

## Scope
This document describes how OpenWrt OPKG feeds are structured, generated, signed, and consumed, based on upstream source code and current feed conventions.

Primary source repositories cloned in `research/`:
- `openwrt` (main build system and feed/index generation)
- `packages` (feed package collection)
- `opkg-lede` (OPKG client implementation used by OpenWrt)
- `awg-openwrt` (upstream package artifact naming conventions used by this project)

## Canonical Feed Types
OpenWrt uses `src/gz` feed entries (compressed package indexes):
- `targets/<target>/<subtarget>/packages` (target-specific packages)
- `targets/<target>/<subtarget>/kmods/<kernel-version-release-vermagic>` (kernel modules)
- `packages/<arch>/base` (base feed)
- `packages/<arch>/<feed>` (additional feeds)

Source evidence:
- `research/openwrt/include/feeds.mk`

## OPKG Feed Directory Layout
Canonical URL-style layout used by OpenWrt:
- `<base>/targets/<target>/<subtarget>/packages/Packages`
- `<base>/targets/<target>/<subtarget>/packages/Packages.gz`
- `<base>/targets/<target>/<subtarget>/packages/Packages.sig` (when signed)
- `<base>/targets/<target>/<subtarget>/kmods/<kernel-release-hash>/Packages*`
- `<base>/packages/<arch>/<feed>/Packages*`

Important compatibility rule:
- `kmod-*` packages must be segregated by kernel release/vermagic path to avoid incompatible installs.

Convenience layout allowed for this project:
- Keep package payloads split on disk:
- `targets/<target>/<subtarget>/packages/*.ipk`
- `targets/<target>/<subtarget>/kmods/<kernel-release-hash>/*.ipk`
- Publish a top-level aggregate index at:
- `targets/<target>/<subtarget>/Packages` and `Packages.gz`
- In that aggregate index, use nested `Filename` values, e.g.:
- `Filename: packages/<pkg>.ipk`
- `Filename: kmods/<kernel-release-hash>/<kmod>.ipk`
- This enables a single `src/gz` feed URL per target/subtarget while preserving kernel-hash isolation on disk.

Source evidence:
- `research/openwrt/include/feeds.mk`
- `research/openwrt/scripts/size_compare.sh`

## Package File Format
OpenWrt `.ipk` packages are read as tar archives with `control.tar.gz` and `./control` for metadata in the index generation script.

Relevant fields from package control metadata include:
- `Package`
- `Version`
- `Architecture`
- `Depends`
- `Description`
- other control fields

Source evidence:
- `research/openwrt/scripts/ipkg-make-index.sh`

## Index Generation Rules
OpenWrt index generation for OPKG:
1. Enumerate `*.ipk` in a feed directory.
2. Skip `kernel` and `libc` package names in index generation script.
3. Extract control metadata.
4. Append generated fields:
- `Filename`
- `Size`
- `SHA256sum`
5. Write `Packages`.
6. Write `Packages.gz` via gzip.

Control-stanza serialization requirements for parser compatibility:
- Multiline field values must use RFC822-style folding:
- first line as `Key: value`
- continuation lines prefixed with one leading space
- blank paragraph lines encoded as ` .` (space + dot)
- For this project's generated OPKG indexes, emit `Filename`, `Size`, and `SHA256sum` before `Description` to avoid parser breakage from malformed multiline blocks.

OpenWrt additionally creates:
- `Packages.manifest`
- `index.json`

OpenWrt removes selected metadata fields when producing final `Packages` in build output:
- `Maintainer`
- `LicenseFiles`
- `Source`
- `SourceName`
- `Require`
- `SourceDateEpoch`

OpenWrt also applies a specific padding workaround for a historical usign SHA-512 bug before signing in some flows.

Source evidence:
- `research/openwrt/package/Makefile`
- `research/openwrt/scripts/ipkg-make-index.sh`
- `research/openwrt/scripts/make-index-json.py`

## Signature Rules
When signed packages are enabled, OpenWrt signs `Packages` with `usign`:
- Signing command form: `usign -S -m Packages -s <private-key>`
- Signature file: `Packages.sig`

Key distribution and verification model:
- Public keys live in `/etc/opkg/keys/` (filename is key fingerprint).
- OPKG verifies feed index against `Packages.sig` when `option check_signature` is enabled.

Source evidence:
- `research/openwrt/package/Makefile`
- `research/openwrt/target/imagebuilder/files/Makefile`
- `research/openwrt/package/system/opkg/files/opkg-key`
- `research/openwrt/package/system/opkg/Makefile`
- `research/opkg-lede/libopkg/opkg_cmd.c`

## OPKG Client Consumption Rules
OPKG configuration supports feed declarations:
- `src <name> <url>` (plain `Packages`)
- `src/gz <name> <url>` (`Packages.gz`)

Update behavior for `src/gz` feeds:
- Download `<url>/Packages.gz` to `lists_dir/<feed-name>`
- If signature checking enabled, download `<url>/Packages.sig` to `lists_dir/<feed-name>.sig`
- Verify list file against signature
- Signature failures may remove both list and sig unless forced

Filename path behavior:
- OPKG accepts `Filename` values containing subdirectories and downloads from `<feed-base-url>/<Filename>`.
- Therefore, aggregate top-level indexes that reference `packages/...` and `kmods/<hash>/...` are compatible.

Default OpenWrt config locations:
- `lists_dir ext /var/opkg-lists`
- `/etc/opkg.conf`
- `/etc/opkg/customfeeds.conf`

Source evidence:
- `research/opkg-lede/libopkg/opkg_conf.c`
- `research/opkg-lede/libopkg/opkg_cmd.c`
- `research/opkg-lede/libopkg/opkg.c`
- `research/opkg-lede/libopkg/opkg_download.c`
- `research/openwrt/package/system/opkg/files/opkg.conf`

## AWG Artifact Naming Contract
`awg-openwrt` publishes artifacts with postfix:
- `v<tag>_<pkgarch>_<target>_<subtarget>`

Example:
- `kmod-amneziawg_v23.05.4_aarch64_generic_rockchip_armv8.ipk`

Parsed components:
- OpenWrt version: `23.05.4`
- pkgarch: `aarch64_generic`
- target: `rockchip`
- subtarget: `armv8`

Repository generation for AWG artifacts must preserve target/subtarget partitioning and must not merge cross-target kmods into one feed directory.

Source evidence:
- `research/awg-openwrt/.github/workflows/build-module.yml`

## Required Invariants For This Project
1. Do not mix package sets across target/subtarget feed directories.
2. Keep kmods isolated by kernel release/vermagic-compatible feed path.
3. Generate aggregate top-level `Packages` and `Packages.gz` per target/subtarget for easy client configuration.
4. Ensure aggregate `Filename` entries correctly reference nested payload paths (`packages/...`, `kmods/<hash>/...`).
5. If signing enabled, generate `Packages.sig` for each feed directory and publish matching public keys.
6. Preserve deterministic feed paths and stable URLs for clients.
7. Treat snapshots as out of scope for this repository.

## Recommended Publishing Layout For AWG OPKG
Target-first layout to mirror OpenWrt semantics:
- `opkg/openwrt/<openwrt-version>/targets/<target>/<subtarget>/packages/`
- `opkg/openwrt/<openwrt-version>/targets/<target>/<subtarget>/kmods/<kernel-release-hash>/`
- `opkg/openwrt/<openwrt-version>/targets/<target>/<subtarget>/Packages`
- `opkg/openwrt/<openwrt-version>/targets/<target>/<subtarget>/Packages.gz`
- Optional `Packages.sig` for the aggregate index

Optional immutable release-scoped variant:
- `opkg/releases/<release-key>/<openwrt-version>/targets/<target>/<subtarget>/packages/`
- `opkg/releases/<release-key>/<openwrt-version>/targets/<target>/<subtarget>/kmods/<kernel-release-hash>/`
- `opkg/releases/<release-key>/<openwrt-version>/targets/<target>/<subtarget>/Packages*`

Client feed example:
- Single-feed convenience:
- `src/gz awg https://<host>/opkg/openwrt/23.05.4/targets/rockchip/armv8`
- Split-feed (official-style) also possible:
- `src/gz awg_core https://<host>/opkg/openwrt/23.05.4/targets/rockchip/armv8/packages`
- `src/gz awg_kmods https://<host>/opkg/openwrt/23.05.4/targets/rockchip/armv8/kmods/<kernel-release-hash>`
