#!/usr/bin/env bash
# One-command WoW 3.3.5a (Hearthguard / AzerothCore) setup for Xubuntu / Ubuntu 26.04 laptops.
# Run on the laptop:
#   curl -fsSL http://192.168.1.3:8788/setup | bash
set -euo pipefail

SERVER_IP="192.168.1.3"
SHARE_URL="http://${SERVER_IP}:8788"
WOW_DIR="${HOME}/WoW335a"
WINE_DIR="${HOME}/wine-wotlk"

echo "============================================================"
echo "  Hearthguard WoW 3.3.5a Laptop Setup (Server: ${SERVER_IP})"
echo "============================================================"

# 1. Install 32-bit Wine + OpenGL/Vulkan runtime + CLI prereqs
echo ""
echo "[1/6] Installing Wine, 32-bit graphics libraries, and dev CLI tools (sudo password may be requested)..."
sudo dpkg --add-architecture i386
sudo apt-get update -qq
sudo apt-get install -y \
  wine wine32:i386 \
  libgl1-mesa-dri:i386 libgl1:i386 \
  mesa-vulkan-drivers mesa-vulkan-drivers:i386 \
  libvulkan1 libvulkan1:i386 \
  vulkan-tools curl wget git jq ripgrep ca-certificates tar

# 2. Stream WoW 3.3.5a client over LAN from Acer (~17 GB, skips Cache/Logs/Account)
echo ""
if [[ -f "${WOW_DIR}/Wow.exe" ]]; then
  echo "[2/6] Found existing WoW client at ${WOW_DIR}/Wow.exe — skipping LAN transfer."
else
  echo "[2/6] Copying WoW 3.3.5a client (~17 GB) over LAN from ${SHARE_URL}/wow.tar..."
  echo "      (Includes Questie-335, Bagnon, LoganReads, Skada. Plug into Ethernet if available for max speed!)"
  mkdir -p "${WOW_DIR}"
  curl -fL --progress-bar "${SHARE_URL}/wow.tar" | tar -xf - -C "${WOW_DIR}"
fi

# 3. Point realmlist.wtf at 192.168.1.3 and ensure clean laptop Config.wtf
echo ""
echo "[3/6] Configuring realmlist.wtf (${SERVER_IP}) and laptop-friendly Config.wtf..."
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

# 4. Initialize 32-bit Wine prefix + smart Vulkan/DXVK auto-detection
echo ""
echo "[4/6] Initializing 32-bit Wine prefix at ${WINE_DIR}..."
export WINEPREFIX="${WINE_DIR}"
export WINEARCH=win32
export WINEDLLOVERRIDES="mscoree,mshtml="
wineboot -u >/dev/null 2>&1 || true
wineserver -w >/dev/null 2>&1 || true

USE_DXVK=0
if command -v vulkaninfo >/dev/null 2>&1; then
  # Check for a real hardware Vulkan device (exclude CPU software rasterizer llvmpipe)
  if vulkaninfo --summary 2>/dev/null | grep -i "deviceName" | grep -viE "llvmpipe|softpipe|lavapipe" >/dev/null 2>&1; then
    USE_DXVK=1
  fi
fi

if [[ "${USE_DXVK}" -eq 1 ]]; then
  echo "      Hardware Vulkan GPU detected — installing DXVK d3d9.dll for best FPS!"
  curl -fsSL "${SHARE_URL}/dxvk-d3d9.dll" -o "${WINE_DIR}/drive_c/windows/system32/d3d9.dll"
  cat > "${HOME}/wow.sh" <<'EOF'
#!/usr/bin/env bash
cd "$HOME/WoW335a" || exit 1
WINEDLLOVERRIDES="d3d9=n,b" \
DXVK_STATE_CACHE_PATH="$HOME/wine-wotlk" \
WINEPREFIX="$HOME/wine-wotlk" \
exec wine Wow.exe "$@"
EOF
else
  echo "      Older pre-Vulkan GPU detected (classic Win7 laptop) — using Wine OpenGL/WineD3D (safe mode, no DXVK)."
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

# 5. Desktop launcher & Application Menu shortcuts (WoW + Azeroth Watch)
echo ""
echo "[5/6] Creating Desktop & Menu shortcuts..."
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

# 6. Install Antigravity CLI (agy) & Claude Code CLI (claude)
echo ""
echo "[6/6] Installing Antigravity CLI (agy) and Claude Code CLI (claude)..."
mkdir -p "${HOME}/.local/bin"
export PATH="${HOME}/.local/bin:${PATH}"
grep -qsF 'export PATH="$HOME/.local/bin:$PATH"' "${HOME}/.bashrc" || \
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "${HOME}/.bashrc"

if command -v agy >/dev/null 2>&1; then
  echo "      Antigravity CLI (agy) already installed."
else
  echo "      Installing Antigravity CLI (agy)..."
  curl -fsSL https://antigravity.google/cli/install.sh | bash || \
    echo "      WARNING: Antigravity CLI install failed — retry later with: curl -fsSL https://antigravity.google/cli/install.sh | bash"
fi

if command -v claude >/dev/null 2>&1; then
  echo "      Claude Code CLI (claude) already installed."
else
  echo "      Installing Claude Code CLI (claude)..."
  curl -fsSL https://claude.ai/install.sh | bash || \
    echo "      WARNING: Claude Code CLI install failed — retry later with: curl -fsSL https://claude.ai/install.sh | bash"
fi

echo ""
echo "============================================================"
echo "  DONE! Ready to play & code on Hearthguard (${SERVER_IP})!"
echo "============================================================"
echo "  Launch WoW:  double-click 'WoW - Hearthguard' on Desktop, or run: ~/wow.sh"
if [[ "${USE_DXVK}" -eq 0 ]]; then
  echo "  Graphics tip: if FPS feels sluggish on older Intel graphics, try: ~/wow.sh -opengl"
fi
echo "  Dashboard:   http://${SERVER_IP}:8787"
echo "  AI CLIs:     open a new terminal, then run 'agy' and 'claude' to sign in!"
echo ""
