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

## Configuration
Default config is [`config/settings.json`](config/settings.json).

`publish_branch` is configurable in two ways:
- config value: `publish_branch`
- CLI override: `--publish-branch <branch>`

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
