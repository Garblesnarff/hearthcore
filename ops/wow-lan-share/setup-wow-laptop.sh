#!/usr/bin/env bash
# Verified WoW 3.3.5a (Hearthguard / AzerothCore) setup for Ubuntu / Xubuntu laptops (Issue #70).
# Safe download & verify workflow (NEVER pipe unverified curl into bash):
#   1. Start the on-demand share on the server: python3 ops/wow-lan-share/serve.py
#   2. Copy the printed one-time SHARE_TOKEN and SHA-256 digest onto the laptop:
#        curl -fsSL "http://<SERVER_IP>:8788/${SHARE_TOKEN}/setup" -o setup-wow-laptop.sh
#        echo "<PRINTED_SHA256>  setup-wow-laptop.sh" | sha256sum -c -
#        SERVER_IP=<SERVER_IP> SHARE_TOKEN="<TOKEN>" bash setup-wow-laptop.sh
set -euo pipefail

SERVER_IP="${SERVER_IP:-}"
SHARE_PORT="${SHARE_PORT:-8788}"
SHARE_TOKEN="${SHARE_TOKEN:-}"
WOW_DIR="${HOME}/WoW335a"
WINE_DIR="${HOME}/wine-wotlk"

if [[ -z "${SERVER_IP}" ]]; then
  echo "ERROR: SERVER_IP is required (e.g., SERVER_IP=192.168.1.3)." >&2
  exit 1
fi
if [[ -z "${SHARE_TOKEN}" ]]; then
  echo "ERROR: SHARE_TOKEN is required (printed by on-demand serve.py)." >&2
  exit 1
fi

SHARE_URL="http://${SERVER_IP}:${SHARE_PORT}/${SHARE_TOKEN}"

echo "============================================================"
echo "  Hearthguard WoW 3.3.5a Laptop Setup (Server: ${SERVER_IP})"
echo "============================================================"

# 0. Fetch published SHA-256 checksums before downloading binaries
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
CHECKSUM_FILE="${TMP_DIR}/checksums.sha256"
echo "[0/5] Fetching published SHA-256 checksums from ${SHARE_URL}/checksums.sha256..."
curl -fsSL "${SHARE_URL}/checksums.sha256" -o "${CHECKSUM_FILE}"

verify_checksum() {
  local filename="$1"
  local target_path="$2"
  local expected
  expected="$(awk -v f="${filename}" '$2 == f {print $1}' "${CHECKSUM_FILE}")"
  if [[ -z "${expected}" ]]; then
    echo "ERROR: Missing SHA-256 digest for ${filename} in checksums.sha256" >&2
    exit 1
  fi
  local actual
  actual="$(sha256sum "${target_path}" | awk '{print $1}')"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "ERROR: SHA-256 mismatch for ${filename}! expected=${expected} actual=${actual}" >&2
    exit 1
  fi
  echo "      Verified SHA-256 for ${filename} (${actual:0:16}...)"
}

# 1. Install 32-bit Wine + OpenGL/Vulkan runtime + CLI prereqs
echo ""
echo "[1/5] Installing Wine, 32-bit graphics libraries, and prereqs (sudo password may be requested)..."
sudo dpkg --add-architecture i386
sudo apt-get update -qq
sudo apt-get install -y \
  wine wine32:i386 \
  libgl1-mesa-dri:i386 libgl1:i386 \
  mesa-vulkan-drivers mesa-vulkan-drivers:i386 \
  libvulkan1 libvulkan1:i386 \
  vulkan-tools curl wget git jq ripgrep ca-certificates tar

# 2. Stream WoW 3.3.5a client over LAN (~17 GB, skips Cache/Logs/Account) and verify Wow.exe SHA-256
echo ""
if [[ -f "${WOW_DIR}/Wow.exe" ]]; then
  echo "[2/5] Found existing WoW client at ${WOW_DIR}/Wow.exe — verifying checksum..."
  verify_checksum "Wow.exe" "${WOW_DIR}/Wow.exe"
else
  echo "[2/5] Copying WoW 3.3.5a client (~17 GB) over LAN from ${SHARE_URL}/wow.tar..."
  mkdir -p "${WOW_DIR}"
  curl -fL --progress-bar "${SHARE_URL}/wow.tar" | tar --no-same-owner --no-same-permissions -xf - -C "${WOW_DIR}"
  verify_checksum "Wow.exe" "${WOW_DIR}/Wow.exe"
fi

# 3. Point realmlist.wtf at SERVER_IP and ensure clean laptop Config.wtf
echo ""
echo "[3/5] Configuring realmlist.wtf (${SERVER_IP}) and laptop-friendly Config.wtf..."
mkdir -p "${WOW_DIR}/Data/enUS" "${WOW_DIR}/WTF"
printf 'set realmlist %s\n' "${SERVER_IP}" > "${WOW_DIR}/Data/enUS/realmlist.wtf"

cat > "${WOW_DIR}/WTF/Config.wtf" <<EOF
SET locale "enUS"
SET realmList "${SERVER_IP}"
SET realmName "AzerothCore"
SET Sound_OutputDriverName "System Default"
SET hwDetect "0"
SET gxWindow "1"
SET gxRefresh "60"
SET gxVSync "1"
SET gxFixLag "0"
SET maxFPS "60"
SET maxFPSBk "15"
SET videoOptionsVersion "3"
SET movie "0"
SET readTOS "1"
SET readEULA "1"
SET accounttype "LK"
SET checkAddonVersion "0"
SET farclip "550.000000"
SET groundEffectDensity "24"
SET projectedTextures "1"
EOF

# 4. Initialize 32-bit Wine prefix + smart Vulkan/DXVK auto-detection (SHA-256 verified before install)
echo ""
echo "[4/5] Initializing 32-bit Wine prefix at ${WINE_DIR}..."
export WINEPREFIX="${WINE_DIR}"
export WINEARCH=win32
export WINEDLLOVERRIDES="mscoree,mshtml="
wineboot -u >/dev/null 2>&1 || true
wineserver -w >/dev/null 2>&1 || true

USE_DXVK=0
if command -v vulkaninfo >/dev/null 2>&1; then
  if vulkaninfo --summary 2>/dev/null | grep -i "deviceName" | grep -viE "llvmpipe|softpipe|lavapipe" >/dev/null 2>&1; then
    USE_DXVK=1
  fi
fi

if [[ "${USE_DXVK}" -eq 1 ]]; then
  echo "      Hardware Vulkan GPU detected — downloading & verifying DXVK d3d9.dll..."
  DXVK_TMP="${TMP_DIR}/dxvk-d3d9.dll"
  curl -fsSL "${SHARE_URL}/dxvk-d3d9.dll" -o "${DXVK_TMP}"
  verify_checksum "dxvk-d3d9.dll" "${DXVK_TMP}"
  install -m 0644 "${DXVK_TMP}" "${WINE_DIR}/drive_c/windows/system32/d3d9.dll"
  cat > "${HOME}/wow.sh" <<'EOF'
#!/usr/bin/env bash
cd "$HOME/WoW335a" || exit 1
WINEDLLOVERRIDES="d3d9=n,b" \
DXVK_STATE_CACHE_PATH="$HOME/wine-wotlk" \
WINEPREFIX="$HOME/wine-wotlk" \
exec wine Wow.exe "$@"
EOF
else
  echo "      Older pre-Vulkan GPU detected — using Wine OpenGL/WineD3D (safe mode, no DXVK)."
  rm -f "${WINE_DIR}/drive_c/windows/system32/d3d9.dll"
  cat > "${HOME}/wow.sh" <<'EOF'
#!/usr/bin/env bash
cd "$HOME/WoW335a" || exit 1
WINEDLLOVERRIDES="d3d9=b" \
WINEPREFIX="$HOME/wine-wotlk" \
exec wine Wow.exe "$@"
EOF
fi
chmod +x "${HOME}/wow.sh"

# 5. Desktop launcher & Application Menu shortcuts
echo ""
echo "[5/5] Creating Desktop & Menu shortcuts..."
mkdir -p "${HOME}/Desktop" "${HOME}/.local/share/applications"

WOW_DESKTOP="[Desktop Entry]
Type=Application
Name=WoW - Hearthguard (3.3.5a)
Comment=Launch World of Warcraft 3.3.5a on Hearthguard (${SERVER_IP})
Exec=${HOME}/wow.sh
Icon=applications-games
Terminal=false
Categories=Game;
"
printf '%s' "${WOW_DESKTOP}" > "${HOME}/Desktop/WoW-Hearthguard.desktop"
printf '%s' "${WOW_DESKTOP}" > "${HOME}/.local/share/applications/wow-hearthguard.desktop"
chmod +x "${HOME}/Desktop/WoW-Hearthguard.desktop" "${HOME}/.local/share/applications/wow-hearthguard.desktop"

AW_DESKTOP="[Desktop Entry]
Type=Link
Name=Azeroth Watch Dashboard
Comment=Open Hearthguard live dashboard
URL=http://${SERVER_IP}:8787
Icon=web-browser
"
printf '%s' "${AW_DESKTOP}" > "${HOME}/Desktop/Azeroth-Watch.desktop"
chmod +x "${HOME}/Desktop/Azeroth-Watch.desktop"

echo ""
echo "============================================================"
echo "  DONE! Ready to play on Hearthguard (${SERVER_IP})!"
echo "============================================================"
echo "  Launch WoW:  double-click 'WoW - Hearthguard' on Desktop, or run: ~/wow.sh"
echo "  Dashboard:   http://${SERVER_IP}:8787"
echo "  Optional AI CLIs: install separately from their official verified packages if desired."
echo ""
