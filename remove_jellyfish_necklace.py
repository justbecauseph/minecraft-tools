#!/usr/bin/env python3
"""
remove_jellyfish_necklace.py

Eradicate the Relics mod item `relics:jellyfish_necklace` from the Sintara world.

It closes both the acquisition paths AND scrubs every existing copy out of
offline player data:

  1. Existing copies  -- recursively deletes any `relics:jellyfish_necklace`
     item stack from every player .dat: main inventory, hotbar, armor/offhand,
     the GUI cursor (`carried`), Curios accessory slots (Stacks + Cosmetics),
     the Ender Chest, and any NESTED container (shulker boxes, bundles,
     backpacks, etc.) reachable inside those stacks' NBT/components.
  2. Chest-loot injection -- blanks `lootData.entries` in
     config/relics/relics/jellyfish_necklace.yaml so Relics stops seeding the
     necklace into chest loot tables. (Needs a FULL server restart to take
     effect -- /reload does NOT reload Relics extended configs.)

The OTHER acquisition path, Let's Do Furniture's Trash Bag, is already closed
for ALL 20 relics via the `furniture:trash_bag_blacklist` datapack tag, so the
jellyfish necklace is already covered there -- nothing to do for that path.
See the relics-loot-removal / relics-nullify-accessories notes.

SAFETY:
  * Dry-run by default. Nothing is written unless you pass --apply.
  * Refuses to touch player data while the server is running (session.lock),
    unless --force is given. Player .dat edits require the server OFFLINE, or
    autosave/logout will clobber them.
  * On --apply it first makes a full timestamped backup of the playerdata dir
    and a .loot-backup-* copy of the relics config it edits.

Usage:
  ./remove_jellyfish_necklace.py                 # dry-run report over all players + config
  ./remove_jellyfish_necklace.py --apply         # actually remove (server must be stopped)
  ./remove_jellyfish_necklace.py --player Steve   # limit the data scrub to one player
  ./remove_jellyfish_necklace.py --no-config      # only scrub player data, leave loot config alone
  ./remove_jellyfish_necklace.py --apply --force  # override the server-running guard (dangerous)
"""

import os
import re
import sys
import json
import fcntl
import shutil
import argparse
import datetime

import nbtlib

TARGET_ID = "relics:jellyfish_necklace"

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WORLD_DIR = os.path.join(SERVER_ROOT, "sintara")
PLAYERDATA_DIR = os.path.join(WORLD_DIR, "playerdata")
BACKUP_ROOT = os.path.join(SERVER_ROOT, "admin-tools", "backups")
RELIC_CONFIG = os.path.join(SERVER_ROOT, "config", "relics", "relics", "jellyfish_necklace.yaml")

UUID_DAT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.dat$", re.IGNORECASE
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def is_server_running(world_dir):
    """True if the Minecraft server holds the world's session.lock (Java FileChannel.lock)."""
    lock_file = os.path.join(world_dir, "session.lock")
    if not os.path.exists(lock_file):
        return False
    try:
        with open(lock_file, "r+b") as f:
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False  # we got the lock -> server is NOT running
    except BlockingIOError:
        return True
    except Exception:
        return False


def load_username_cache():
    path = os.path.join(SERVER_ROOT, "usernamecache.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {k.lower(): v for k, v in json.load(f).items()}
    except Exception:
        return {}


def is_target_stack(node):
    """A dict-like NBT node that is itself a jellyfish-necklace item stack."""
    return isinstance(node, dict) and str(node.get("id", "")) == TARGET_ID


def scrub(node, path="", hits=None):
    """
    Recursively delete every `relics:jellyfish_necklace` item stack anywhere
    under `node`, in place. Returns a list of human-readable location labels,
    one per removed stack (count folded in).

    Removal rules:
      * A LIST element that is a target stack is dropped from the list.
      * A COMPOUND value that is a target stack is dropped by key (e.g. `carried`).
      * Everything else is recursed into, so a necklace nested inside a shulker
        box / bundle / backpack (stored under `components`, `Items`, `tag`, ...)
        is still found and removed, leaving the container itself intact.
    """
    if hits is None:
        hits = []

    if isinstance(node, list):
        # walk backwards so index deletion is stable
        for i in range(len(node) - 1, -1, -1):
            child = node[i]
            if is_target_stack(child):
                cnt = int(child.get("count", child.get("Count", 1)) or 1)
                hits.append(f"{path}[{i}] x{cnt}")
                del node[i]
            else:
                scrub(child, f"{path}[{i}]", hits)
    elif isinstance(node, dict):
        for key in list(node.keys()):
            child = node[key]
            if is_target_stack(child):
                cnt = int(child.get("count", child.get("Count", 1)) or 1)
                hits.append(f"{path}/{key} x{cnt}")
                del node[key]
            else:
                scrub(child, f"{path}/{key}", hits)

    return hits


def process_player(filepath, apply):
    """Scan (and, if apply, rewrite) one player .dat. Returns (removed_count, labels, error)."""
    try:
        nbt = nbtlib.load(filepath)
    except Exception as e:
        return 0, [], f"parse error: {e}"

    labels = scrub(nbt)
    if labels and apply:
        try:
            nbt.save()
        except Exception as e:
            return len(labels), labels, f"WRITE FAILED: {e}"
    return len(labels), labels, None


# --------------------------------------------------------------------------- #
# loot config
# --------------------------------------------------------------------------- #
def config_has_entries():
    """True if the relic config still injects the necklace into chest loot."""
    try:
        with open(RELIC_CONFIG, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return False
    m = re.search(r"^lootData:\n(?:.*\n?)*", text, re.MULTILINE)
    if not m:
        return False
    block = m.group(0)
    # already blanked?
    if re.search(r"^\s*entries:\s*\[\s*\]\s*$", block, re.MULTILINE):
        return False
    return "entries:" in block and "tables:" in block


def blank_loot_entries(apply):
    """
    Set `lootData.entries: []` in the relic config. lootData is the last
    top-level key and entries is its last child, so we truncate from the
    `entries:` line to EOF. Returns a status string.
    """
    with open(RELIC_CONFIG, "r", encoding="utf-8") as f:
        text = f.read()

    lines = text.splitlines(keepends=True)
    # find the `entries:` line that belongs to lootData (4-space indented)
    entries_idx = None
    in_lootdata = False
    for i, line in enumerate(lines):
        if re.match(r"^lootData:\s*$", line):
            in_lootdata = True
            continue
        if in_lootdata and re.match(r"^\S", line):  # a new top-level key -> lootData ended
            in_lootdata = False
        if in_lootdata and re.match(r"^\s{4}entries:\s*", line):
            entries_idx = i
            break

    if entries_idx is None:
        return "SKIP: could not locate lootData.entries"

    new_text = "".join(lines[:entries_idx]) + "    entries: []\n"
    if new_text == text:
        return "SKIP: entries already empty"

    if apply:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(
            os.path.dirname(RELIC_CONFIG), f".loot-backup-jellyfish-{ts}.yaml"
        )
        shutil.copy2(RELIC_CONFIG, backup)
        with open(RELIC_CONFIG, "w", encoding="utf-8") as f:
            f.write(new_text)
        return f"APPLIED: entries blanked (backup: {os.path.basename(backup)})"
    return "WOULD APPLY: blank lootData.entries (chest-loot injection off; needs restart)"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=f"Remove {TARGET_ID} from the world.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--player", help="limit the player-data scrub to one username or UUID")
    ap.add_argument("--no-config", action="store_true", help="do NOT edit the relics loot config")
    ap.add_argument("--force", action="store_true", help="override the server-running guard")
    args = ap.parse_args()

    print("=" * 78)
    print(f"  Remove '{TARGET_ID}' from the game")
    print(f"  Mode: {'APPLY (writing changes)' if args.apply else 'DRY-RUN (no writes)'}")
    print("=" * 78)

    # ---- server-running guard (only matters when we will write) --------------
    running = is_server_running(WORLD_DIR)
    if running:
        print("\n!! WARNING: the Minecraft server appears to be RUNNING.")
        print("   Editing player .dat files now risks being clobbered by autosave/logout.")
        if args.apply and not args.force:
            print("   Aborting. Stop the server, or re-run with --force to override.")
            sys.exit(1)

    # ---- resolve player file list -------------------------------------------
    cache = load_username_cache()
    if args.player:
        p = args.player.lower()
        uuid = None
        if UUID_DAT_RE.match(p + ".dat"):
            uuid = p
        else:
            for u, name in cache.items():
                if name.lower() == p:
                    uuid = u
                    break
        if not uuid:
            print(f"\nERROR: could not resolve player '{args.player}'.")
            sys.exit(1)
        fp = os.path.join(PLAYERDATA_DIR, f"{uuid}.dat")
        if not os.path.exists(fp):
            print(f"\nERROR: no .dat for {uuid}")
            sys.exit(1)
        files = [f"{uuid}.dat"]
    else:
        files = sorted(f for f in os.listdir(PLAYERDATA_DIR) if UUID_DAT_RE.match(f))

    print(f"\nScanning {len(files)} player file(s) in {PLAYERDATA_DIR} ...\n")

    # ---- backup before any write --------------------------------------------
    if args.apply:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(BACKUP_ROOT, exist_ok=True)
        dest = os.path.join(BACKUP_ROOT, f"playerdata_backup_{ts}")
        shutil.copytree(PLAYERDATA_DIR, dest)
        print(f"Backup of playerdata -> {dest}\n")

    # ---- scrub players -------------------------------------------------------
    total_removed = 0
    affected = 0
    errors = []
    for fn in files:
        uuid = fn[:-4]
        name = cache.get(uuid, "?")
        removed, labels, err = process_player(os.path.join(PLAYERDATA_DIR, fn), args.apply)
        if err:
            errors.append((name, uuid, err))
            print(f"  [ERR] {name} ({uuid}): {err}")
            continue
        if removed:
            affected += 1
            total_removed += removed
            verb = "removed" if args.apply else "would remove"
            print(f"  {name} ({uuid}): {verb} {removed} necklace(s)")
            for lbl in labels:
                print(f"        - {lbl}")

    print("\n" + "-" * 78)
    print(f"Player data: {total_removed} necklace stack(s) across {affected} player(s) "
          f"{'REMOVED' if args.apply else 'to remove'}.")
    if errors:
        print(f"  {len(errors)} file(s) had errors -- see [ERR] lines above.")

    # ---- loot config ---------------------------------------------------------
    if not args.no_config:
        print("\nChest-loot injection (config/relics/relics/jellyfish_necklace.yaml):")
        print("  " + blank_loot_entries(args.apply))
    print("Trash Bag path: already blocked for all relics via "
          "furniture:trash_bag_blacklist datapack -- no action needed.")

    print("-" * 78)
    if not args.apply:
        print("Dry-run only. Re-run with --apply (server STOPPED) to make changes.")
    else:
        print("Done. NOTE: the loot-config change needs a FULL server restart "
              "(not /reload) to take effect.")
    print("=" * 78)


if __name__ == "__main__":
    main()
