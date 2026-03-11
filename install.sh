#!/bin/sh
set -eu

BASE_URL_DEFAULT="https://yannleretaille.github.io/awg-openwrt-repos/repos"
UPSTREAM_RELEASE_URL="https://github.com/Slava-Shchipunov/awg-openwrt/releases"
BASE_URL="${AWG_BASE_URL:-$BASE_URL_DEFAULT}"
FEED_NAME="${AWG_FEED_NAME:-awg}"
PKG_MANAGER_OVERRIDE="${AWG_PKG_MANAGER:-}"
SKIP_LUCI=0
ASSUME_YES=0
NO_REBOOT=0

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --base-url <url>      Base URL for generated repos (default: project GitHub Pages /repos path)
  --feed-name <name>    Feed alias name (default: awg)
  --pkg-manager <pm>    Force package manager: opkg|apk
  --skip-luci           Do not install luci-proto-amneziawg
  --yes                 Auto-confirm reboot prompt
  --no-reboot           Do not reboot at end
  -h, --help            Show this help
EOF
}

log() {
  printf '[awg-install] %s\n' "$*"
}

print_banner() {
  cat <<'EOF'
   █████╗ ███╗   ███╗███╗   ██╗███████╗███████╗██╗ █████╗ ██╗    ██╗ ██████╗ 
  ██╔══██╗████╗ ████║████╗  ██║██╔════╝╚══███╔╝██║██╔══██╗██║    ██║██╔════╝ 
  ███████║██╔████╔██║██╔██╗ ██║█████╗    ███╔╝ ██║███████║██║ █╗ ██║██║  ███╗
  ██╔══██║██║╚██╔╝██║██║╚██╗██║██╔══╝   ███╔╝  ██║██╔══██║██║███╗██║██║   ██║
  ██║  ██║██║ ╚═╝ ██║██║ ╚████║███████╗███████╗██║██║  ██║╚███╔███╔╝╚██████╔╝
  ╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚═╝╚═╝  ╚═╝ ╚══╝╚══╝  ╚═════╝ 
EOF
}

die() {
  printf '[awg-install] ERROR: %s\n' "$*" >&2
  exit 1
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

fetch_to_file() {
  url="$1"
  dst="$2"
  if have_cmd wget; then
    wget -qO "$dst" "$url" || return 1
    return 0
  fi
  if have_cmd uclient-fetch; then
    uclient-fetch -q -O "$dst" "$url" || return 1
    return 0
  fi
  if have_cmd curl; then
    curl -fsSL "$url" -o "$dst" || return 1
    return 0
  fi
  return 1
}

probe_url() {
  url="$1"
  if have_cmd wget; then
    wget -q --spider "$url" >/dev/null 2>&1 && return 0
  fi
  if have_cmd uclient-fetch; then
    uclient-fetch --spider "$url" >/dev/null 2>&1 && return 0
  fi
  if have_cmd curl; then
    curl -fsIL "$url" >/dev/null 2>&1 && return 0
  fi
  return 1
}

normalize_version() {
  raw="$1"
  printf '%s\n' "$raw" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1
}

get_board_json() {
  if have_cmd ubus; then
    ubus call system board 2>/dev/null || true
  else
    true
  fi
}

json_field() {
  payload="$1"
  field="$2"
  if ! have_cmd jsonfilter; then
    printf ''
    return 0
  fi
  [ -n "$payload" ] || {
    printf ''
    return 0
  }
  printf '%s' "$payload" | jsonfilter -e "$field" 2>/dev/null || true
}

detect_system_identity() {
  board_json="$(get_board_json)"

  version="$(json_field "$board_json" '@.release.version')"
  target_path="$(json_field "$board_json" '@.release.target')"

  if [ -z "$version" ] || [ -z "$target_path" ]; then
    if [ -f /etc/openwrt_release ]; then
      # shellcheck disable=SC1091
      . /etc/openwrt_release
      if [ -z "$version" ]; then
        version="${DISTRIB_RELEASE:-}"
      fi
      if [ -z "$target_path" ]; then
        target_path="${DISTRIB_TARGET:-}"
      fi
    fi
  fi

  [ -n "$version" ] || die "Unable to detect OpenWrt version (ubus and /etc/openwrt_release both failed)."
  [ -n "$target_path" ] || die "Unable to detect OpenWrt target/subtarget."

  lower_version="$(printf '%s' "$version" | tr '[:upper:]' '[:lower:]')"
  case "$lower_version" in
    *snapshot*)
      die "Snapshot systems are not supported by this installer."
      ;;
  esac

  ow_version="$(normalize_version "$version")"
  [ -n "$ow_version" ] || die "Unable to normalize OpenWrt version from '$version'."

  case "$target_path" in
    */*)
      target="${target_path%%/*}"
      subtarget="${target_path#*/}"
      ;;
    *)
      die "Invalid target format '$target_path' (expected target/subtarget)."
      ;;
  esac

  [ -n "$target" ] || die "Detected empty target."
  [ -n "$subtarget" ] || die "Detected empty subtarget."

  DETECTED_OW_VERSION="$ow_version"
  DETECTED_TARGET="$target"
  DETECTED_SUBTARGET="$subtarget"
}

detect_pkg_manager() {
  if [ -n "$PKG_MANAGER_OVERRIDE" ]; then
    case "$PKG_MANAGER_OVERRIDE" in
      opkg|apk)
        DETECTED_PM="$PKG_MANAGER_OVERRIDE"
        return 0
        ;;
      *)
        die "Invalid --pkg-manager value '$PKG_MANAGER_OVERRIDE' (expected opkg|apk)."
        ;;
    esac
  fi

  has_opkg=0
  has_apk=0
  have_cmd opkg && has_opkg=1
  have_cmd apk && has_apk=1

  if [ "$has_opkg" -eq 1 ] && [ "$has_apk" -eq 0 ]; then
    DETECTED_PM="opkg"
    return 0
  fi
  if [ "$has_apk" -eq 1 ] && [ "$has_opkg" -eq 0 ]; then
    DETECTED_PM="apk"
    return 0
  fi
  if [ "$has_apk" -eq 1 ] && [ "$has_opkg" -eq 1 ]; then
    major="${DETECTED_OW_VERSION%%.*}"
    if [ "$major" -ge 25 ]; then
      DETECTED_PM="apk"
    else
      DETECTED_PM="opkg"
    fi
    return 0
  fi

  die "Neither opkg nor apk was found on PATH."
}

derive_keys_base() {
  b="${BASE_URL%/}"
  case "$b" in
    */repos) KEYS_BASE="${b%/repos}" ;;
    *) KEYS_BASE="$b" ;;
  esac
}

probe_feed() {
  if [ "$DETECTED_PM" = "opkg" ]; then
    FEED_URL="${BASE_URL%/}/opkg/openwrt/${DETECTED_OW_VERSION}/targets/${DETECTED_TARGET}/${DETECTED_SUBTARGET}"
    FEED_PROBE_URL="${FEED_URL}/Packages.gz"
  else
    FEED_URL="${BASE_URL%/}/apk/openwrt/${DETECTED_OW_VERSION}/targets/${DETECTED_TARGET}/${DETECTED_SUBTARGET}/packages.adb"
    FEED_PROBE_URL="${FEED_URL}"
  fi

  log "Probing feed availability: ${FEED_PROBE_URL}"
  if ! probe_url "$FEED_PROBE_URL"; then
    die "No feed found for detected OpenWrt ${DETECTED_OW_VERSION} (${DETECTED_TARGET}/${DETECTED_SUBTARGET}). Check upstream release availability: ${UPSTREAM_RELEASE_URL}"
  fi
}

refresh_key_opkg() {
  have_cmd usign || die "usign is required to enroll OPKG key."
  key_tmp="/tmp/awg-opkg-usign.pub"
  key_url="${KEYS_BASE}/keys/opkg-usign.pub"
  log "Installing OPKG trust key from ${key_url}"
  fetch_to_file "$key_url" "$key_tmp" || die "Failed to download OPKG public key from $key_url"
  key_fp="$(usign -F -p "$key_tmp" 2>/dev/null || true)"
  [ -n "$key_fp" ] || die "Failed to compute OPKG key fingerprint."
  mkdir -p /etc/opkg/keys
  cp "$key_tmp" "/etc/opkg/keys/${key_fp}"
  rm -f "$key_tmp"
}

refresh_key_apk() {
  key_url="${KEYS_BASE}/keys/apk-signing.rsa.pub"
  log "Installing APK trust key from ${key_url}"
  mkdir -p /etc/apk/keys
  fetch_to_file "$key_url" "/etc/apk/keys/awg-openwrt-repos.rsa.pub" || die "Failed to download APK public key from $key_url"
}

configure_feed_opkg() {
  log "Adding OPKG feed '${FEED_NAME}' to /etc/opkg/customfeeds.conf"
  mkdir -p /etc/opkg
  touch /etc/opkg/customfeeds.conf
  sed -i "/^[[:space:]]*src\\(\\/gz\\)\\?[[:space:]]\\+${FEED_NAME}[[:space:]]\\+/d" /etc/opkg/customfeeds.conf
  printf 'src/gz %s %s\n' "$FEED_NAME" "$FEED_URL" >> /etc/opkg/customfeeds.conf
}

configure_feed_apk() {
  log "Adding APK repository '${FEED_NAME}'"
  mkdir -p /etc/apk/repositories.d
  rm -f "/etc/apk/repositories.d/${FEED_NAME}.list"

  if [ -f /etc/apk/repositories ]; then
    sed -i '\|/repos/apk/openwrt/|d' /etc/apk/repositories
  fi

  for f in /etc/apk/repositories.d/*.list; do
    [ -f "$f" ] || continue
    sed -i '\|/repos/apk/openwrt/|d' "$f"
  done

  printf '%s\n' "$FEED_URL" > "/etc/apk/repositories.d/${FEED_NAME}.list"
}

pkg_available_opkg() {
  pkg="$1"
  opkg list 2>/dev/null | grep -q "^${pkg} - "
}

pkg_available_apk() {
  pkg="$1"
  apk search -x "$pkg" >/dev/null 2>&1
}

install_required_pkg() {
  pkg="$1"
  log "Installing required package: ${pkg}"
  if [ "$DETECTED_PM" = "opkg" ]; then
    opkg install "$pkg" || die "Failed to install required package: $pkg"
  else
    apk add "$pkg" || die "Failed to install required package: $pkg"
  fi
}

install_optional_luci() {
  if [ "$SKIP_LUCI" -eq 1 ]; then
    log "Skipping luci-proto-amneziawg because --skip-luci was set."
    return 0
  fi

  pkg="luci-proto-amneziawg"
  log "Installing LuCI package (default): ${pkg}"
  if [ "$DETECTED_PM" = "opkg" ]; then
    if pkg_available_opkg "$pkg"; then
      opkg install "$pkg" || die "Failed to install optional package: $pkg"
    else
      log "Optional package $pkg not available in selected feed; skipping."
    fi
  else
    if pkg_available_apk "$pkg"; then
      apk add "$pkg" || die "Failed to install optional package: $pkg"
    else
      log "Optional package $pkg not available in selected feed; skipping."
    fi
  fi
}

update_indexes() {
  if [ "$DETECTED_PM" = "opkg" ]; then
    log "Updating OPKG package indexes"
    opkg update || die "opkg update failed."
  else
    log "Updating APK package indexes"
    apk update || die "apk update failed."
  fi
}

maybe_reboot() {
  if [ "$NO_REBOOT" -eq 1 ]; then
    log "Skipping reboot (--no-reboot)."
    return 0
  fi

  if [ "$ASSUME_YES" -eq 1 ]; then
    log "Rebooting (--yes)."
    reboot
    return 0
  fi

  answer=""
  if [ -c /dev/tty ]; then
    printf 'Reboot now? [y/N] ' > /dev/tty
    read -r answer < /dev/tty || true
  else
    log "No interactive TTY found; skipping reboot."
    return 0
  fi

  case "$answer" in
    y|Y|yes|YES)
      reboot
      ;;
    *)
      log "Reboot skipped."
      ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-url)
      [ "$#" -ge 2 ] || die "--base-url requires a value"
      BASE_URL="$2"
      shift 2
      ;;
    --feed-name)
      [ "$#" -ge 2 ] || die "--feed-name requires a value"
      FEED_NAME="$2"
      shift 2
      ;;
    --pkg-manager)
      [ "$#" -ge 2 ] || die "--pkg-manager requires a value"
      PKG_MANAGER_OVERRIDE="$2"
      shift 2
      ;;
    --skip-luci)
      SKIP_LUCI=1
      shift
      ;;
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --no-reboot)
      NO_REBOOT=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

BASE_URL="${BASE_URL%/}"
print_banner
derive_keys_base
detect_system_identity
detect_pkg_manager

log "Detected OpenWrt ${DETECTED_OW_VERSION} target=${DETECTED_TARGET}/${DETECTED_SUBTARGET} pkgmgr=${DETECTED_PM}"

probe_feed

log "Using feed URL: ${FEED_URL}"

if [ "$DETECTED_PM" = "opkg" ]; then
  refresh_key_opkg
  configure_feed_opkg
else
  refresh_key_apk
  configure_feed_apk
fi

update_indexes
install_required_pkg "kmod-amneziawg"
install_required_pkg "amneziawg-tools"
install_optional_luci

log "Installation complete."
log "For future OpenWrt upgrades, run:"
log "  owut upgrade --verbose -V <version> -r luci-proto-amneziawg,kmod-amneziawg,amneziawg-tools"
log "After sysupgrade completes, run this installer again to restore the AmneziaWG installation."
maybe_reboot
