#!/usr/bin/env bash
# package_singleplayer.sh — build a distributable singleplayer edition of the Sintara world.
#
# Produces dist/sintara-singleplayer-<date>.zip containing:
#   sintara-singleplayer/
#     README.txt
#     neoforge-21.1.233-installer.jar   (downloaded if absent)
#     minecraft/                        (drop into a NeoForge 21.1.233 instance dir)
#       mods/  config/  defaultconfigs/  moonlight-global-datapacks/  saves/sintara/
#
# Safe against the live server: RCON save-off + save-all flush around the world copy,
# save-on restored by trap. No restart involved. Staged copies are real (not hardlinks)
# so later edits never touch live files.
#
# Usage: package_singleplayer.sh [--no-photos] [--out DIR] [--world-dir DIR] [--save-name NAME]
#   --no-photos      exclude camerapture/ + data/exposures (~1.6 GB of player photos)
#   --out DIR        output/staging dir (default /home/minecraft/dist)
#   --world-dir DIR  source world (default /home/minecraft/sintara, the live world;
#                    anything else is treated as a static snapshot — no RCON save-off)
#   --save-name NAME folder name under saves/ + world display name + zip prefix
#                    (default: sintara)
#   --mods-src DIR   mods folder to bundle. Default: download the curated client pack
#                    from s3://lampas-assets/singleplayer-mods.zip (jars at zip root).
#                    Deliberately NOT the server's mods/ — the curated pack is
#                    maintained independently (owned by the user; never overwrite it).
#   --config-src DIR config folder to bundle. Default: download
#                    s3://lampas-assets/singleplayer-config.zip (config/ at zip root).

set -euo pipefail

SRV=/home/minecraft
WORLD_DIR=$SRV/sintara
SAVE_NAME=sintara
MODS_SRC=""
CONFIG_SRC=""
PACKS_SRC=""
OUT=$SRV/dist
ASSETS_URL=https://lampas-assets.sfo3.digitaloceanspaces.com
INCLUDE_PHOTOS=1
NEOFORGE_VER=21.1.233
NEOFORGE_URL="https://maven.neoforged.net/releases/net/neoforged/neoforge/${NEOFORGE_VER}/neoforge-${NEOFORGE_VER}-installer.jar"

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-photos) INCLUDE_PHOTOS=0; shift ;;
        --out) OUT=$2; shift 2 ;;
        --world-dir) WORLD_DIR=${2%/}; shift 2 ;;
        --save-name) SAVE_NAME=$2; shift 2 ;;
        --mods-src) MODS_SRC=${2%/}; shift 2 ;;
        --config-src) CONFIG_SRC=${2%/}; shift 2 ;;
        --packs-src) PACKS_SRC=${2%/}; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

[[ -f $WORLD_DIR/level.dat ]] || { echo "no level.dat in $WORLD_DIR" >&2; exit 1; }
LIVE=0; [[ $WORLD_DIR == "$SRV/sintara" ]] && LIVE=1

# default mods/config: the user-curated zips in the lampas-assets bucket (NOT server dirs)
fetch_src() { # $1=zip name  $2=dest dir
    rm -rf "$2"; mkdir -p "$2"
    curl -fsSL --retry 3 -o "$2.zip" "$ASSETS_URL/$1"
    unzip -q "$2.zip" -d "$2" && rm "$2.zip"
}
if [[ -z $MODS_SRC ]]; then
    echo "fetching curated mods from $ASSETS_URL/singleplayer-mods.zip"
    fetch_src singleplayer-mods.zip "$OUT/src-mods"
    MODS_SRC=$OUT/src-mods            # jars at zip root
fi
if [[ -z $CONFIG_SRC ]]; then
    echo "fetching curated config from $ASSETS_URL/singleplayer-config.zip"
    fetch_src singleplayer-config.zip "$OUT/src-config"
    CONFIG_SRC=$OUT/src-config/config # zip contains config/ at root
fi
[[ -d $CONFIG_SRC ]] || { echo "config source $CONFIG_SRC missing" >&2; exit 1; }
if [[ -z $PACKS_SRC ]]; then
    echo "fetching resource/shader packs from $ASSETS_URL/singleplayer-resourcepacks-shaderpacks.zip"
    fetch_src singleplayer-resourcepacks-shaderpacks.zip "$OUT/src-packs"
    PACKS_SRC=$OUT/src-packs          # resourcepacks/ + shaderpacks/ at zip root
fi

STAGE=$OUT/$SAVE_NAME-singleplayer
MC=$STAGE/minecraft
DATE=$(date +%Y%m%d)
ZIP=$OUT/$SAVE_NAME-singleplayer-$DATE.zip

log() { echo "[$(date +%H:%M:%S)] $*"; }

# --- RCON helpers -----------------------------------------------------------
RCON_PORT=$(awk -F= '/^rcon.port=/{print $2}' $SRV/server.properties)
RCON_PASS=$(awk -F= '/^rcon.password=/{print $2}' $SRV/server.properties)
SAVES_OFF=0

rcon() { mcrcon -H 127.0.0.1 -P "$RCON_PORT" -p "$RCON_PASS" "$@"; }

restore_saves() {
    if [[ $SAVES_OFF -eq 1 ]]; then
        log "restoring automatic saves (save-on)"
        rcon save-on || echo "WARNING: save-on failed — run it manually!" >&2
        SAVES_OFF=0
    fi
}
trap restore_saves EXIT

# --- staging ----------------------------------------------------------------
log "staging into $STAGE"
rm -rf "$STAGE"
mkdir -p "$MC/saves"

# --- world copy under save-off ---------------------------------------------
WORLD_EXCLUDES=(
    --exclude 'session.lock'
    --exclude 'lampas_audit.db*'          # server-side audit log, ~500M
    --exclude 'level[0-9]*.dat'           # orphaned atomic-write temp files
    --exclude '/playerdata_backup*'       # admin playerdata backups
    --exclude '/relic-removal-backup-*'   # admin world-surgery backups
)
if [[ $INCLUDE_PHOTOS -eq 0 ]]; then
    WORLD_EXCLUDES+=(--exclude '/camerapture/' --exclude '/data/exposures/')
fi

if [[ $LIVE -eq 1 ]] && rcon "save-off" >/dev/null 2>&1; then
    SAVES_OFF=1
    log "server live: save-off issued, flushing world to disk"
    rcon "save-all flush" >/dev/null
    sleep 3
elif [[ $LIVE -eq 1 ]]; then
    log "RCON unreachable — assuming server offline, copying world directly"
else
    log "static snapshot source ($WORLD_DIR) — no save-off needed"
fi

log "copying world (~19 GB)"
rsync -a "${WORLD_EXCLUDES[@]}" "$WORLD_DIR/" "$MC/saves/$SAVE_NAME/"
restore_saves

# --- mods / configs (static, no save-off needed) ----------------------------
log "copying mods (minus automodpack) and configs"
rsync -a --exclude 'automodpack-*.jar' --exclude '.connector' --exclude '.index' "$MODS_SRC/" "$MC/mods/"
rsync -a "$CONFIG_SRC/" "$MC/config/"
[[ -d $SRV/defaultconfigs ]] && rsync -a "$SRV/defaultconfigs/" "$MC/defaultconfigs/"
[[ -d $SRV/moonlight-global-datapacks ]] && rsync -a "$SRV/moonlight-global-datapacks/" "$MC/moonlight-global-datapacks/"
log "copying resource/shader packs"
rsync -a "$PACKS_SRC/resourcepacks/" "$MC/resourcepacks/"
rsync -a "$PACKS_SRC/shaderpacks/" "$MC/shaderpacks/"
# the server's own resource pack (server.properties resource-pack=), served to
# players automatically in MP so the SP bundle must carry it too
curl -fsSL --retry 3 -o "$MC/resourcepacks/lampas-resource-pack.zip" \
    "https://github.com/justbecauseph/lampas-resource-pack/releases/latest/download/lampas-resource-pack.zip"

# --- duplicate-mod guard (two versions of one modid = instant boot crash) ---
log "checking for duplicate mod ids"
python3 - "$MC/mods" <<'EOF'
import sys, zipfile, re, os
from collections import defaultdict
seen = defaultdict(list)
d = sys.argv[1]
for j in sorted(os.listdir(d)):
    if not j.endswith('.jar'):
        continue
    try:
        with zipfile.ZipFile(os.path.join(d, j)) as z:
            names = set(z.namelist())
            toml = next((n for n in ('META-INF/neoforge.mods.toml', 'META-INF/mods.toml') if n in names), None)
            if toml:
                section = None
                for line in z.read(toml).decode('utf8', 'replace').splitlines():
                    s = line.strip()
                    if s.startswith('[['):
                        section = s
                    elif section == '[[mods]]':
                        m = re.match(r'modId\s*=\s*"([^"]+)"', s)
                        if m:
                            seen[m.group(1)].append(j)
    except Exception as e:
        print(f"  WARNING: unreadable jar {j}: {e}")
dupes = {k: v for k, v in seen.items() if len(set(v)) > 1}
if dupes:
    for k, v in dupes.items():
        print(f"  DUPLICATE modid '{k}': {sorted(set(v))}")
    sys.exit("duplicate mod ids in mods folder — remove the older jars and re-run")
print(f"  ok ({len(seen)} mod ids, no duplicates)")
EOF

# --- config scrubbing / singleplayer tweaks ---------------------------------
log "scrubbing secrets + applying singleplayer config tweaks"
LAMPAS_TOML=$MC/config/lampas_overrides-common.toml
sed -i 's/^\(\s*apiKey = \).*/\1""/' "$LAMPAS_TOML"
if grep -q '^\s*offlineMode' "$LAMPAS_TOML"; then
    sed -i 's/^\(\s*offlineMode = \).*/\1true/' "$LAMPAS_TOML"
else
    sed -i '/^\["General Settings"\]/a\\t#Singleplayer edition: skip every portal HTTP call (bank sync, bounties, socials, Discord relay).\n\tofflineMode = true' "$LAMPAS_TOML"
fi

# OPAC: disable claim enforcement so other players' claims don't block you in SP
python3 - "$MC/config/openpartiesandclaims-server.toml" <<'EOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
s2, n = re.subn(r'(\[serverConfig\.claims\]\s*\n(?:\s*#[^\n]*\n)*\s*)enabled = true',
                r'\1enabled = false', s, count=1)
if n != 1:
    sys.exit("FAILED to disable OPAC claims — pattern not found")
open(p, 'w').write(s2)
print("OPAC claims disabled")
EOF

# report anything that still looks like a secret (manual review, non-fatal)
log "secret scan of staged config/ (review any hits):"
grep -rniE '(api[-_]?key|secret|token|webhook) *= *"[^" ]{8,}"' "$MC/config" || echo "  (clean)"

# --- level.dat: enable cheats, set display name ------------------------------
log "patching level.dat (allowCommands=1, LevelName=$SAVE_NAME)"
python3 - "$MC/saves/$SAVE_NAME/level.dat" "$SAVE_NAME" <<'EOF'
import nbtlib, sys
p, name = sys.argv[1], sys.argv[2]
f = nbtlib.load(p)
root = f['Data'] if 'Data' in f else f['']['Data']
root['allowCommands'] = nbtlib.Byte(1)
root['LevelName'] = nbtlib.String(name)
f.save(p)
print(f"allowCommands set to 1, LevelName set to {name}")
EOF

# --- NeoForge installer -------------------------------------------------------
INSTALLER=$OUT/neoforge-${NEOFORGE_VER}-installer.jar
if [[ ! -s $INSTALLER ]]; then
    log "downloading NeoForge installer $NEOFORGE_VER"
    curl -fL --retry 3 -o "$INSTALLER" "$NEOFORGE_URL" \
        || echo "WARNING: NeoForge installer download failed — zip will not include it" >&2
fi
[[ -s $INSTALLER ]] && cp "$INSTALLER" "$STAGE/"

# --- README -------------------------------------------------------------------
SNAPSHOT_NOTE=""
if [[ $SAVE_NAME != sintara ]]; then
    SNAPSHOT_NOTE="
NOTE: this build is packaged from a specific world snapshot
($(basename "$WORLD_DIR")), not the final live world.
"
fi

cat > "$STAGE/README.txt" <<EOF
LAMPAS — SINGLEPLAYER EDITION ($(date +%Y-%m-%d))
==================================================
$SNAPSHOT_NOTE

The full Lampas server world, playable offline in singleplayer.
Minecraft 1.21.1 + NeoForge ${NEOFORGE_VER}, all 194 server mods included.

WHAT YOU NEED
  - Minecraft Java Edition (1.21.1)
  - 8-12 GB of RAM to spare for the game
  - ~25 GB of free disk space

INSTALL — OPTION A: Prism Launcher / MultiMC (recommended)
  1. Create a new instance: Minecraft 1.21.1, then under "Mod loader" pick
     NeoForge ${NEOFORGE_VER}.
  2. Open the instance's folder ("Folder" / ".minecraft" button).
  3. Copy EVERYTHING inside this package's "minecraft/" folder into it
     (mods, config, defaultconfigs, moonlight-global-datapacks, saves).
  4. Instance Settings -> Java -> set maximum memory to 8192-12288 MB.
  5. Launch, then open the world "${SAVE_NAME}".

INSTALL — OPTION B: official Minecraft Launcher
  1. Run neoforge-${NEOFORGE_VER}-installer.jar (needs Java 21) and
     choose "Install client".
  2. Copy everything inside "minecraft/" into your .minecraft folder
     (Windows: %APPDATA%\\.minecraft). NOTE: this mixes ~200 mods into your
     main game folder — a separate launcher profile with its own game
     directory (or Option A) is strongly recommended.
  3. In the launcher, edit the NeoForge profile's JVM arguments:
     change -Xmx2G to -Xmx10G.
  4. Launch, then open the world "${SAVE_NAME}".

GOOD TO KNOW
  - First launch takes several minutes (mod init + recipe indexing).
  - Cheats are enabled in the world, so you can /gamemode, /tp, etc.
  - If you play with the SAME Minecraft account you used on the server,
    you continue exactly where you last logged out — inventory and all.
    A different account starts fresh at spawn.
  - Land claims (Open Parties and Claims) are DISABLED in the bundled
    config so old claims don't lock you out of builds. To re-enable:
    config/openpartiesandclaims-server.toml -> [serverConfig.claims] ->
    enabled = true.
  - Portal features (bank sync, bounty board, chat icons, Discord relay)
    are switched off via lampas-overrides "offlineMode". Everything else
    about the currency/bank system still works locally.
  - Voice chat does nothing in singleplayer; ignore it.
  - Waystones, trains, airships, shops, photos — it is all in there.
  - IMPORTANT: enable the "lampas-resource-pack" under Options -> Resource
    Packs -- the server applied it automatically, singleplayer will not.
    (Custom coin/item looks and other Lampas touches live there.)
  - Bundled extras: Fresh Animations resource packs (same menu) and
    Complementary/Photon shaders (Iris is included -- Options -> Video
    Settings -> Shader Packs).

Enjoy, and thanks for playing on Lampas: The Last Resort!
EOF

# --- zip ----------------------------------------------------------------------
log "zipping (this is the slow part)"
rm -f "$ZIP"
( cd "$OUT" && zip -1 -r -q "$ZIP" "$(basename "$STAGE")" )

log "done"
du -sh "$STAGE" "$ZIP"
sha256sum "$ZIP"
