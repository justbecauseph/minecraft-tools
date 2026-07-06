#!/usr/bin/env python3
"""
set_player_spawn.py — one-time relocate of every existing player to a fixed point.

Run this OFFLINE (server stopped). It rewrites each saved player .dat in the
world's playerdata/ so that the next time the player logs in they appear at the
target coordinates in the overworld. This is a one-shot edit of current players,
NOT an on-every-login mechanism.

For each player it sets:
    Pos       -> [x+0.5, y, z+0.5]   (block-centered)
    Motion    -> [0, 0, 0]           (drop any carried velocity)
    Dimension -> minecraft:overworld (so nobody loads in the nether/end)
With --set-respawn it also sets the bed/respawn point (SpawnX/Y/Z + dimension)
so deaths return them to the same spot.

Safety: refuses to run while the server is holding the world's session.lock, and
makes a timestamped backup of playerdata/ before touching anything (unless
--no-backup). Use --dry-run to preview.

Usage:
    python3 set_player_spawn.py                       # all players -> 1244 89 -1245
    python3 set_player_spawn.py --x 1244 --y 89 --z -1245
    python3 set_player_spawn.py --dry-run
    python3 set_player_spawn.py --set-respawn         # also set respawn point
    python3 set_player_spawn.py --players Steve Alex   # only these (name or uuid)

Options:
    --x/--y/--z     Target block coords (default 1244 89 -1245).
    --world DIR     World save dir (default /home/minecraft/sintara).
    --players ...   Limit to these usernames/UUIDs (default: everyone).
    --set-respawn   Also set the player's respawn (spawn) point to the target.
    --no-backup     Skip the playerdata backup (not recommended).
    --dry-run       Report what would change; write nothing.

Deps: nbtlib  (pip install --user --break-system-packages nbtlib)
"""
import os
import re
import sys
import json
import shutil
import fcntl
import random
import uuid as uuidlib
import datetime
import argparse

try:
    import nbtlib
    from nbtlib import Double, Float, String, List
except ImportError as e:
    sys.exit("missing dependency: %s\n  pip install --user --break-system-packages nbtlib" % e)

UUID_FILE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.dat$', re.IGNORECASE)
OVERWORLD = "minecraft:overworld"


def is_server_running(world_dir):
    """True if a process holds the world's session.lock (server is up)."""
    lock_file = os.path.join(world_dir, "session.lock")
    if not os.path.exists(lock_file):
        return False
    try:
        with open(lock_file, "r+b") as f:
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False  # we got the lock -> server not running
    except BlockingIOError:
        return True
    except Exception:
        return False


def load_username_cache(server_root):
    path = os.path.join(server_root, "usernamecache.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def resolve_filter(players, playerdata_dir, cache):
    """Map requested names/UUIDs to a set of lowercase uuid strings."""
    wanted = set()
    cache_lower = {u.lower(): name for u, name in cache.items()}
    name_to_uuid = {name.lower(): u.lower() for u, name in cache.items()}
    for p in players:
        pl = p.lower()
        if pl.endswith(".dat"):
            pl = pl[:-4]
        if pl in cache_lower:
            wanted.add(pl)
        elif pl in name_to_uuid:
            wanted.add(name_to_uuid[pl])
        elif os.path.exists(os.path.join(playerdata_dir, pl + ".dat")):
            wanted.add(pl)
        else:
            print("WARNING: could not resolve player '%s' -- skipping" % p)
    return wanted


def make_backup(playerdata_dir):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(os.path.dirname(playerdata_dir), "playerdata_backup_%s" % stamp)
    shutil.copytree(playerdata_dir, dest)
    return dest


def main():
    ap = argparse.ArgumentParser(description="One-time relocate all players to fixed coords (offline).")
    ap.add_argument("--x", type=float, default=1244)
    ap.add_argument("--y", type=float, default=89)
    ap.add_argument("--z", type=float, default=-1245)
    ap.add_argument("--world", default="/home/minecraft/sintara")
    ap.add_argument("--players", nargs="+", default=None)
    ap.add_argument("--spread", type=float, default=0,
                    help="scatter each player randomly within this radius (blocks) on X/Z "
                         "so they don't land on the exact same point (default 0 = exact)")
    ap.add_argument("--set-respawn", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    playerdata_dir = os.path.join(args.world, "playerdata")
    if not os.path.isdir(playerdata_dir):
        sys.exit("playerdata dir not found: %s" % playerdata_dir)

    if is_server_running(args.world):
        sys.exit("REFUSING: the server is running (session.lock held). Stop it first.")

    server_root = os.path.dirname(args.world)
    cache = load_username_cache(server_root)

    files = sorted(f for f in os.listdir(playerdata_dir) if UUID_FILE.match(f))
    if args.players:
        wanted = resolve_filter(args.players, playerdata_dir, cache)
        files = [f for f in files if f[:-4].lower() in wanted]
        if not files:
            sys.exit("No matching player files for: %s" % " ".join(args.players))

    # block-centered horizontal placement
    cx, py, cz = args.x + 0.5, float(args.y), args.z + 0.5
    if args.spread > 0:
        print("Target: Pos within %.1f blocks of [%.1f, %.1f, %.1f] in %s%s" %
              (args.spread, cx, py, cz, OVERWORLD, "  (+respawn point)" if args.set_respawn else ""))
    else:
        print("Target: Pos [%.1f, %.1f, %.1f] in %s%s" %
              (cx, py, cz, OVERWORLD, "  (+respawn point)" if args.set_respawn else ""))
    print("Players to update: %d%s" % (len(files), "  [DRY RUN]" if args.dry_run else ""))

    if not args.dry_run and not args.no_backup:
        print("Backup -> %s" % make_backup(playerdata_dir))

    changed = 0
    for fn in files:
        path = os.path.join(playerdata_dir, fn)
        uid = fn[:-4]
        name = cache.get(uid, cache.get(uid.lower(), "?"))
        try:
            nbt = nbtlib.load(path)
        except Exception as e:
            print("  SKIP %s (%s): load failed: %s" % (uid, name, e))
            continue

        if args.spread > 0:
            px = cx + random.uniform(-args.spread, args.spread)
            pz = cz + random.uniform(-args.spread, args.spread)
        else:
            px, pz = cx, cz

        old = list(nbt.get("Pos", []))
        nbt["Pos"] = List[Double]([Double(px), Double(py), Double(pz)])
        nbt["Motion"] = List[Double]([Double(0.0), Double(0.0), Double(0.0)])
        nbt["Dimension"] = String(OVERWORLD)
        if args.set_respawn:
            nbt["SpawnX"] = nbtlib.Int(int(args.x))
            nbt["SpawnY"] = nbtlib.Int(int(args.y))
            nbt["SpawnZ"] = nbtlib.Int(int(args.z))
            nbt["SpawnDimension"] = String(OVERWORLD)
            nbt["SpawnForced"] = nbtlib.Byte(1)

        oldstr = "[%.1f, %.1f, %.1f]" % tuple(float(v) for v in old) if len(old) == 3 else str(old)
        print("  %s (%s): %s -> [%.1f, %.1f, %.1f]" % (uid, name, oldstr, px, py, pz))

        if not args.dry_run:
            nbt.save(path)
        changed += 1

    print("\n%s %d player file(s)." % ("Would update" if args.dry_run else "Updated", changed))


if __name__ == "__main__":
    main()
