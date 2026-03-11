# AmneziaWG OpenWrt Repositories

Mirrors releases from [Slava-Shchipunov/awg-openwrt](https://github.com/Slava-Shchipunov/awg-openwrt) into generated OpenWrt OPKG/APK repositories.

## One-line install

```bash
wget -qO- "https://yannleretaille.github.io/awg-openwrt-repos/install.sh" | sh
```

## Manual Install

### OPKG

1. Install OPKG trust key:
   ```bash
   wget -qO /tmp/opkg-usign.pub "https://yannleretaille.github.io/awg-openwrt-repos/keys/opkg-usign.pub" && cp /tmp/opkg-usign.pub "/etc/opkg/keys/$(usign -F -p /tmp/opkg-usign.pub)"
   ```
2. Add to `/etc/opkg/customfeeds.conf`:
   ```bash
   src/gz awg "https://yannleretaille.github.io/awg-openwrt-repos/repos/opkg/openwrt/<openwrt-version>/targets/<target>/<subtarget>/"
   ```
3. Install
   ```bash
   opkg update
   opkg install kmod-amneziawg amneziawg-tools luci-proto-amneziawg
   ```

### APK (OpenWrt 25.12+)

1. Install APK trust key:
   ```bash
   mkdir -p /etc/apk/keys && wget -O /etc/apk/keys/awg-openwrt-repos.rsa.pub "https://yannleretaille.github.io/awg-openwrt-repos/keys/apk-signing.rsa.pub"
   ```
2. Create `/etc/apk/keys/awg-openwrt-repos.rsa.pub` with:
   ```bash
   src https://yannleretaille.github.io/awg-openwrt-repos/repos/apk/openwrt/<openwrt-version>/targets/<target>/<subtarget>/packages.adb
   ```
3. Install
   ```bash
   apk update
   apk add kmod-amneziawg amneziawg-tools luci-proto-amneziawg
   ```

## Upgrading

To upgrade, install and run `owut` while excluding AWG packages:

```bash
owut upgrade --verbose -V <VERSION> -r luci-proto-amneziawg,kmod-amneziawg,amneziawg-tools
```

After sysupgrade completes, run the installer again.

## Full release/target/subtarget overview: [REPOS.md](https://github.com/yannleretaille/awg-openwrt-repos/blob/published-repos/REPOS.md)
