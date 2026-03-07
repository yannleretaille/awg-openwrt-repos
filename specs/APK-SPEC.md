# OpenWrt APK Repository Specification

## Scope
This document describes how OpenWrt package repositories work in APK mode (OpenWrt 25.12+), with focus on repository layout, index generation, signing, and client consumption.

Primary research repositories in `research/`:
- `openwrt` (build system and feed/index integration)
- `apk-tools` (native `apk mkndx` / repository behavior)
- `awg-openwrt` (upstream artifact naming conventions)

## Canonical Feed Model (OpenWrt)
OpenWrt in APK mode references repository indexes directly as `packages.adb` URLs, including:
- target packages: `%U/targets/%S/packages/packages.adb`
- target kmods: `%U/targets/%S/kmods/<linux-ver-release-vermagic>/packages.adb`
- arch base feed: `%U/packages/%A/base/packages.adb`
- additional feeds: `%U/packages/%A/<feed>/packages.adb`

Source evidence:
- `research/openwrt/include/feeds.mk` (`FeedSourcesAppendAPK`)

## Repository Layout and Filenames
OpenWrt publishes APK v3 indexes as lowercase `packages.adb`.

Observed live 25.12 example:
- `.../targets/x86/64/packages/packages.adb` exists
- `.../targets/x86/64/packages/Packages.adb` does not

Validation performed on:
- `https://downloads.openwrt.org/releases/25.12.0-rc1/targets/x86/64/packages/packages.adb`

Repository layout used by OpenWrt feeds:
- `targets/<target>/<subtarget>/packages/packages.adb`
- `targets/<target>/<subtarget>/kmods/<kernel-hash>/packages.adb`
- `packages/<arch>/base/packages.adb`
- `packages/<arch>/<feed>/packages.adb`

## Index Generation Rules
OpenWrt invokes native apk-tools for index generation:
- `apk mkndx ... --output packages.adb *.apk`
- optional signing via `--sign <private-key>`
- optional `apk adbdump --format json packages.adb` to derive `index.json`

Source evidence:
- `research/openwrt/package/Makefile` (`$(curdir)/index`, `$(curdir)/merge-index`)
- `research/openwrt/target/imagebuilder/files/Makefile` (`package_index`)

Native apk-tools behavior and options:
- `apk mkndx` creates v3 repository index files from package list
- supports reuse/filtering via `--index` and `--filter-spec`
- supports URL shaping via `--pkgname-spec`

Source evidence:
- `research/apk-tools/doc/apk-mkndx.8.scd`

## APK Repository Configuration
APK clients consume repository config from:
- `/etc/apk/repositories`
- `/etc/apk/repositories.d/*.list`
- `/lib/apk/repositories.d/*.list`

Entries can be direct index URLs (`ndx`) or repository/base forms (`v3`).
OpenWrt practical usage in feed generation is direct index URL style (`.../packages.adb`).

Source evidence:
- `research/apk-tools/doc/apk-repositories.5.scd`
- `research/openwrt/include/feeds.mk`

## Signature and Trust Model
OpenWrt signed-package flow for APK:
- generate local EC key pair (P-256) for imagebuilder/dev workflows
- sign index with `apk mkndx --sign <private-key>`
- install trusted public keys in `/etc/apk/keys/`

OpenWrt keyring package installs APK keys under:
- `/etc/apk/keys/`

Source evidence:
- `research/openwrt/package/Makefile` (`BUILD_KEY_APK_SEC/PUB`, mkndx signing)
- `research/openwrt/target/imagebuilder/files/Makefile` (`_check_keys`, `APK_KEYS`)
- `research/openwrt/package/system/openwrt-keyring/Makefile`

## OpenWrt Client Invocation Pattern
OpenWrt wraps APK operations with:
- `apk --root <root> --keys-dir <keys-dir> ...`
- package installation from repository index URLs via `--repository file://.../packages.adb`

Source evidence:
- `research/openwrt/include/rootfs.mk` (`apk = ...`)
- `research/openwrt/package/Makefile` (`$(call apk,...) add ... --repository file://.../packages.adb`)

## AWG Artifact Naming Contract
`awg-openwrt` artifact postfix:
- `v<tag>_<pkgarch>_<target>_<subtarget>`

Example:
- `luci-proto-amneziawg_v25.12.0_rc?_aarch64_generic_rockchip_armv8.apk` (shape)

Source evidence:
- `research/awg-openwrt/.github/workflows/build-module.yml`

## Required Invariants For This Project (APK)
1. Do not mix target/subtarget payload sets into a single APK index beyond the target/subtarget aggregate view.
2. Keep payloads physically segmented as:
   - `packages/*.apk`
   - `kmods/<kernel-hash>/*.apk`
3. Provide one primary target/subtarget index at:
   - `targets/<target>/<subtarget>/packages.adb`
4. Ensure the primary index is installable via a single repository URL per target/subtarget.
5. Generate `packages.adb` from actual `.apk` artifacts using `apk mkndx`.
6. For single-URL aggregate index compatibility, expose canonical package filenames (`<name>-<version>.apk`) at target/subtarget root.
7. Keep split payload copies for browsing/compatibility, with optional split `packages.adb` indexes.
8. Sign indexes when configured and publish matching public keys for clients.
9. Skip snapshot releases for this repository.

## Recommended Publishing Layout For AWG APK
Rolling layout:
- `apk/openwrt/<openwrt-version>/targets/<target>/<subtarget>/packages.adb` (primary single-URL index)
- `apk/openwrt/<openwrt-version>/targets/<target>/<subtarget>/<name>-<version>.apk` (canonical files used by primary index)
- `apk/openwrt/<openwrt-version>/targets/<target>/<subtarget>/packages/*.apk`
- `apk/openwrt/<openwrt-version>/targets/<target>/<subtarget>/kmods/<kernel-hash>/*.apk`
- optional compatibility indexes:
  - `apk/openwrt/<openwrt-version>/targets/<target>/<subtarget>/packages/packages.adb`
  - `apk/openwrt/<openwrt-version>/targets/<target>/<subtarget>/kmods/<kernel-hash>/packages.adb`

Immutable release layout:
- `apk/releases/<release-key>/<openwrt-version>/targets/<target>/<subtarget>/packages.adb`
- `apk/releases/<release-key>/<openwrt-version>/targets/<target>/<subtarget>/<name>-<version>.apk`
- `apk/releases/<release-key>/<openwrt-version>/targets/<target>/<subtarget>/packages/*.apk`
- `apk/releases/<release-key>/<openwrt-version>/targets/<target>/<subtarget>/kmods/<kernel-hash>/*.apk`

Client repository entries should reference index URLs directly.

Examples:
- single-feed convenience:
  - `https://<host>/apk/openwrt/25.12.0/targets/rockchip/armv8/packages.adb`
- optional split-feed compatibility:
  - `https://<host>/apk/openwrt/25.12.0/targets/rockchip/armv8/packages/packages.adb`
  - `https://<host>/apk/openwrt/25.12.0/targets/rockchip/armv8/kmods/<kernel-hash>/packages.adb`
