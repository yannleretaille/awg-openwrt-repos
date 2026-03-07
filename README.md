# awg-openwrt-reops

Automation for ingesting upstream `Slava-Shchipunov/awg-openwrt` releases and producing normalized metadata/state for repository publishing.

Implemented scope currently covers Phase 1 + Phase 2:
- release discovery from GitHub API
- snapshot exclusion
- OpenWrt version detection from release metadata
- `.ipk` / `.apk` asset classification
- package metadata extraction from package internals
- sync modes: `incremental`, `backfill`, `clean-rebuild`
- resumable state and release manifests

Implemented scope now also includes core Phase 3:
- materialize `opkg` feed trees (immutable release-scoped and rolling OpenWrt-version scoped)
- generate `Packages` and `Packages.gz` indexes per architecture
- optional `Packages` signing support via `usign`
- consistency checks for arch/metadata/index generation

Implemented scope now also includes core Phase 4:
- materialize `apk` feed trees (immutable release-scoped and rolling OpenWrt-version scoped)
- generate top-level aggregate `packages.adb` per target/subtarget for single-URL client setup
- preserve split payload trees (`packages/` and `kmods/<kernel-hash>/`) with optional split compatibility indexes
- optional `packages.adb` signing support via `apk mkndx --sign`

## Configuration
Default config is [`config/settings.json`](config/settings.json).

`publish_branch` is configurable in two ways:
- config value: `publish_branch`
- CLI override: `--publish-branch <branch>`

`public_base_url` controls URL prefixing in generated `output/REPOS.md`.
Example: `https://<user>.github.io/<repo>`

`coverage_policy` controls strict per-target package presence checks during repo generation:
- `strict` (default `true`): fail build if any required package is missing for a target/subtarget
- `required_package_names`: package names expected for every target/subtarget
- `optional_package_names`: tracked in coverage reports but not required

## Usage
Run incremental sync:

```bash
./scripts/sync_releases.py --mode incremental
```

Run full backfill:

```bash
./scripts/sync_releases.py --mode backfill
```

Clear state/output and rebuild from scratch:

```bash
./scripts/sync_releases.py --mode clean-rebuild
```

Dry-run discovery only (no downloads/writes):

```bash
./scripts/sync_releases.py --mode incremental --dry-run
```

Optional token (recommended for API limits):

```bash
GITHUB_TOKEN=... ./scripts/sync_releases.py --mode incremental
```

Target a specific release id (useful for testing):

```bash
./scripts/sync_releases.py --mode incremental --release-id 170097744
```

Build `opkg` repositories from synced manifests/assets:

```bash
./scripts/build_opkg_repo.py --clean
```

Build `apk` repositories from synced manifests/assets:

```bash
./scripts/build_apk_repo.py --clean
```

Generate feed URL index markdown:

```bash
./scripts/generate_repos_md.py
```

## Local workflow testing with act
List jobs:

```bash
act -l
```

Run workflow dispatch locally:

```bash
act workflow_dispatch -W .github/workflows/sync-releases.yml \
  -e <(cat <<'JSON'
{
  "inputs": {
    "mode": "incremental",
    "publish_branch": "published-repos",
    "dry_run": "true"
  }
}
JSON
)
```
