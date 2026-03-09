#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEYS_DIR="${ROOT_DIR}/keys"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

need_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

maybe_remove() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    if [[ "${FORCE}" -eq 1 ]]; then
      rm -f "${path}"
    else
      echo "Refusing to overwrite existing file: ${path}" >&2
      echo "Run with --force to overwrite existing keys." >&2
      exit 1
    fi
  fi
}

need_cmd usign
need_cmd openssl

mkdir -p "${KEYS_DIR}"

OPKG_PRIV="${KEYS_DIR}/PRIVATE_opkg-usign.key"
OPKG_PUB="${KEYS_DIR}/opkg-usign.pub"
APK_PRIV="${KEYS_DIR}/PRIVATE_apk-signing.rsa"
APK_PUB="${KEYS_DIR}/apk-signing.rsa.pub"

maybe_remove "${OPKG_PRIV}"
maybe_remove "${OPKG_PUB}"
maybe_remove "${APK_PRIV}"
maybe_remove "${APK_PUB}"

usign -G -s "${OPKG_PRIV}" -p "${OPKG_PUB}" -c "awg-openwrt-repos opkg signing key"
chmod 600 "${OPKG_PRIV}"

openssl genrsa -out "${APK_PRIV}" 4096 >/dev/null 2>&1
openssl rsa -in "${APK_PRIV}" -pubout -out "${APK_PUB}" >/dev/null 2>&1
chmod 600 "${APK_PRIV}"

echo "Generated key pairs in: ${KEYS_DIR}"
echo "OPKG public key: ${OPKG_PUB}"
echo "APK public key:  ${APK_PUB}"
echo "Private keys are prefixed with PRIVATE_ and ignored by keys/.gitignore."
