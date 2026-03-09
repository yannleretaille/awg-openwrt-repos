# AmneziaWG OpenWrt Repositories

Mirrors releases from [Slava-Shchipunov/awg-openwrt](https://github.com/Slava-Shchipunov/awg-openwrt) into generated OpenWrt OPKG/APK repositories.

### OPKG feed format:

```bash
src/gz awg https://yannleretaille.github.io/awg-openwrt-repos/repos/opkg/openwrt/<openwrt-version>/targets/<target>/<subtarget>/
```

### APK repository format:

```bash
src https://yannleretaille.github.io/awg-openwrt-repos/repos/apk/openwrt/<openwrt-version>/targets/<target>/<subtarget>/packages.adb
```

### Install OPKG trust key:

```bash
wget -O /tmp/opkg-usign.pub https://yannleretaille.github.io/awg-openwrt-repos/keys/opkg-usign.pub
key_id="$(usign -F -p /tmp/opkg-usign.pub)"
cp /tmp/opkg-usign.pub "/etc/opkg/keys/${key_id}"
```

### Install APK trust key:

```bash
mkdir -p /etc/apk/keys
wget -O /etc/apk/keys/awg-openwrt-repos.rsa.pub https://yannleretaille.github.io/awg-openwrt-repos/keys/apk-signing.rsa.pub
```

### Full release/target/subtarget overview: [REPOS.md](https://github.com/yannleretaille/awg-openwrt-repos/blob/published-repos/REPOS.md)
