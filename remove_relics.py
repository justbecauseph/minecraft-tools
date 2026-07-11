#!/usr/bin/env python3
"""
remove_relics.py — audit and remove specific sskirillss Relics from the Sintara world.

WHAT IT DOES
  Scans every place an item can hide in the world and removes the targeted relic
  item stacks. By default it targets the 9 OP/laggy relics that were pulled from
  chest loot (see config/relics/relics/*.yaml entries cleared + the Trash Bag
  blacklist datapack). Acquisition is already blocked for NEW copies; this strips
  the copies players/containers ALREADY hold.

LOCATIONS SCANNED
  - sintara/playerdata/*.dat   : Inventory, EnderItems, and curios:inventory
                                 (neoforge:attachments -> curios:inventory -> Curios[].StacksHandler.Stacks.Items)
  - sintara/data/*.dat         : world saved-data (e.g. Sophisticated Backpack contents live here, not in the item)
  - <dim>/region/*.mca         : block entities (chests, barrels, hoppers, ...). Also catches shulker-box
                                 'minecraft:container' components and any nested Items lists.
  - <dim>/entities/*.mca       : dropped item entities (removed whole) and item frames (Item cleared).
    dims = sintara (overworld), sintara/DIM-1 (nether), sintara/DIM1 (end)

MODES
  (default)      DRY RUN. Scans everything, prints a per-location report, writes a JSON
                 report next to this script. Touches nothing.
  --apply        Actually remove. REQUIRES THE SERVER TO BE STOPPED. Backs up every file
                 it modifies (just-in-time copy) under sintara/relic-removal-backup-<ts>/.
  --all-relics   Target all 20 relics (read from config/relics/relics/*.yaml) instead of the 9.
  --force        Skip the "is the server running?" guard (NOT recommended).

USAGE
  python3 tools/remove_relics.py                # dry-run audit of the 9
  python3 tools/remove_relics.py --apply        # remove the 9 (server must be stopped)
  python3 tools/remove_relics.py --all-relics   # dry-run audit of all 20

Relies on nbtlib for all NBT payloads (per the project's NBT tooling convention); the .mca
sector container is handled with a minimal reader/writer that hands raw chunk bytes to nbtlib.
"""

import sys, os, io, gzip, zlib, glob, json, time, shutil, struct, fcntl, subprocess
from collections import Counter, defaultdict

try:
    import lz4.block
    _HAVE_LZ4 = True
except ImportError:
    _HAVE_LZ4 = False

try:
    import nbtlib
    from nbtlib import Compound, List
except ImportError:
    sys.exit("nbtlib is required: pip install nbtlib")

WORLD = "/home/minecraft/sintara"
HERE = os.path.dirname(os.path.abspath(__file__))
RELICS_CONFIG_DIR = "/home/minecraft/config/relics/relics"

# The 9 flagged relics (OP / laggy) — default target set.
NINE = [
    "springy_boot", "kinetic_belt", "roller_skate", "chorus_staff", "clot_of_time",
    "glitchy_mantle", "ghostly_mantle", "reflective_necklace", "midnight_mantle",
]
FRAME_IDS = {"minecraft:item_frame", "minecraft:glow_item_frame"}

DIMS = [WORLD, os.path.join(WORLD, "DIM-1"), os.path.join(WORLD, "DIM1")]


def target_ids(all_relics: bool):
    if all_relics:
        names = sorted(
            os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(RELICS_CONFIG_DIR, "*.yaml"))
        )
        if not names:
            sys.exit(f"no relic configs found in {RELICS_CONFIG_DIR}")
        return {f"relics:{n}" for n in names}
    return {f"relics:{n}" for n in NINE}


# ---------------------------------------------------------------------------
# Core recursive cleaner. Mutates `node` in place. Returns Counter of removed ids.
# Handles three shapes of relic occurrence:
#   1. plain item stack as a list element:        Items[i] = {id: relics:...}
#   2. container-component element {slot, item}:  components."minecraft:container"[i] = {slot, item:{id: relics:...}}
#   3. dropped-item entity:                       Entities[i] = {id: minecraft:item, Item:{id: relics:...}}
#   4. item frame (compound, not in a list):      {id: minecraft:item_frame, Item:{id: relics:...}}  -> clear Item
# ---------------------------------------------------------------------------
def is_relic_stack(o, targets):
    return isinstance(o, dict) and isinstance(o.get("id"), str) and str(o["id"]) in targets


def element_is_removable(e, targets):
    """True if list element `e` should be dropped entirely, plus the relic id removed."""
    if is_relic_stack(e, targets):
        return str(e["id"])
    if isinstance(e, dict):
        # shulker/bundle container component: {slot, item:{...}}
        if is_relic_stack(e.get("item"), targets):
            return str(e["item"]["id"])
        # dropped item entity: {id: minecraft:item, Item:{...}}
        if str(e.get("id", "")) == "minecraft:item" and is_relic_stack(e.get("Item"), targets):
            return str(e["Item"]["id"])
    return None


def clean(node, targets, removed):
    if isinstance(node, dict):
        # item frame holding a relic -> empty the frame, keep the frame entity
        if str(node.get("id", "")) in FRAME_IDS and is_relic_stack(node.get("Item"), targets):
            removed[str(node["Item"]["id"])] += 1
            del node["Item"]
        for v in node.values():
            clean(v, targets, removed)
    elif isinstance(node, list):
        # delete matching elements in reverse to keep indices valid and preserve list type
        for i in range(len(node) - 1, -1, -1):
            rid = element_is_removable(node[i], targets)
            if rid:
                removed[rid] += 1
                del node[i]
        for v in node:
            clean(v, targets, removed)


# ---------------------------------------------------------------------------
# .dat (gzip NBT) handling
# ---------------------------------------------------------------------------
def process_dat(path, targets, apply, backup_dir, report):
    try:
        nbt = nbtlib.load(path)  # auto-detects gzip
    except Exception as e:
        report["skipped"].append((path, str(e)))
        return Counter()
    removed = Counter()
    clean(nbt, targets, removed)
    if removed and apply:
        backup_file(path, backup_dir)
        nbt.save(path)  # preserves gzip + byteorder
    return removed


# ---------------------------------------------------------------------------
# .mca region/entities handling (minimal Anvil container; NBT via nbtlib)
# ---------------------------------------------------------------------------
SECTOR = 4096
_LZ4_MAGIC = b"LZ4Block"


def _lz4java_decompress(data):
    """Decode the lz4-java LZ4BlockOutputStream stream format (Minecraft chunk compression type 4)."""
    if not _HAVE_LZ4:
        raise RuntimeError("python-lz4 not installed (needed for type-4 LZ4 chunks): pip install lz4")
    out = bytearray()
    pos = 0
    while pos < len(data):
        if data[pos:pos + 8] != _LZ4_MAGIC:
            break
        token = data[pos + 8]
        comp_len = struct.unpack("<I", data[pos + 9:pos + 13])[0]
        orig_len = struct.unpack("<I", data[pos + 13:pos + 17])[0]
        body = data[pos + 21:pos + 21 + comp_len]  # skip 4-byte checksum at +17
        if orig_len == 0:  # end mark
            break
        method = token & 0xF0
        if method == 0x10:        # uncompressed block
            out += body
        elif method == 0x20:      # LZ4-compressed block
            out += lz4.block.decompress(body, uncompressed_size=orig_len)
        else:
            raise ValueError(f"bad lz4 block method {method:#x}")
        pos += 21 + comp_len
    return bytes(out)


def _decompress(comp_type, data):
    if comp_type == 1:
        return gzip.decompress(data)
    if comp_type == 2:
        return zlib.decompress(data)
    if comp_type == 3:
        return data
    if comp_type == 4:
        return _lz4java_decompress(data)
    raise ValueError(f"unknown chunk compression {comp_type}")


def _parse_chunk_nbt(raw):
    # raw is the decompressed chunk NBT (root = unnamed TAG_Compound)
    return nbtlib.File.parse(io.BytesIO(raw), byteorder="big")


def _serialize_chunk_nbt(nbtfile):
    buf = io.BytesIO()
    nbtfile.write(buf, byteorder="big")
    return buf.getvalue()


def process_region(path, targets, apply, backup_dir, report):
    removed = Counter()
    with open(path, "rb") as f:
        header = f.read(SECTOR * 2)
        if len(header) < SECTOR * 2:
            return removed  # empty region
        body = f.read()
    locations = header[:SECTOR]
    timestamps = header[SECTOR:SECTOR * 2]

    # chunk index -> (offset_sectors, sector_count)
    chunks = {}
    for i in range(1024):
        offset = struct.unpack(">I", b"\x00" + locations[i * 4:i * 4 + 3])[0]
        count = locations[i * 4 + 3]
        if offset and count:
            chunks[i] = (offset, count)

    target_bytes = [t.encode("utf-8") for t in targets]
    dirty = False
    # store rebuilt chunk payloads: idx -> raw compressed (5-byte header + data), padded to sectors
    new_blobs = {}
    file_start = SECTOR * 2
    for idx, (offset, count) in chunks.items():
        start = offset * SECTOR - file_start
        orig_blob = body[start:start + count * SECTOR]
        if start < 0 or start + 5 > len(body):
            continue
        length = struct.unpack(">I", body[start:start + 4])[0]
        comp_type = body[start + 4]
        cdata = body[start + 5:start + 4 + length]
        try:
            raw = _decompress(comp_type, cdata)
        except Exception as e:
            report["skipped"].append((f"{path}#chunk{idx}", str(e)))
            new_blobs[idx] = orig_blob  # keep original bytes verbatim
            continue
        # Fast path: if no target relic id appears in the decompressed bytes, skip the
        # (expensive) NBT parse entirely and keep the chunk's original bytes.
        if not any(tb in raw for tb in target_bytes):
            new_blobs[idx] = orig_blob
            continue
        try:
            nbtfile = _parse_chunk_nbt(raw)
        except Exception as e:
            report["skipped"].append((f"{path}#chunk{idx}", str(e)))
            new_blobs[idx] = orig_blob
            continue
        c_removed = Counter()
        clean(nbtfile, targets, c_removed)
        if c_removed:
            removed.update(c_removed)
            dirty = True
            payload = zlib.compress(_serialize_chunk_nbt(nbtfile))
            blob = struct.pack(">I", len(payload) + 1) + bytes([2]) + payload
        else:
            blob = body[start:start + 4 + length]  # original (untouched) chunk payload
        # pad to sector boundary
        pad = (-len(blob)) % SECTOR
        new_blobs[idx] = blob + b"\x00" * pad

    if dirty and apply:
        backup_file(path, backup_dir)
        _write_region(path, new_blobs, timestamps)
    return removed


def _write_region(path, new_blobs, timestamps):
    # Re-pack: assign sequential sectors starting at sector 2.
    new_locations = bytearray(SECTOR)
    out = io.BytesIO()
    out.write(b"\x00" * (SECTOR * 2))  # placeholder header
    sector_cursor = 2
    for idx in sorted(new_blobs):
        blob = new_blobs[idx]
        sect_count = len(blob) // SECTOR
        out.write(blob)
        off_bytes = struct.pack(">I", sector_cursor)[1:]  # 3 bytes
        new_locations[idx * 4:idx * 4 + 3] = off_bytes
        new_locations[idx * 4 + 3] = sect_count & 0xFF
        sector_cursor += sect_count
    data = out.getvalue()
    full = bytearray(data)
    full[:SECTOR] = new_locations
    full[SECTOR:SECTOR * 2] = timestamps
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(full)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def backup_file(path, backup_dir):
    rel = os.path.relpath(path, WORLD)
    dst = os.path.join(backup_dir, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst):
        shutil.copy2(path, dst)


def server_running():
    # 1) try to take the world session lock; if it's held, the server is up.
    lock = os.path.join(WORLD, "session.lock")
    if os.path.exists(lock):
        try:
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.lockf(fd, fcntl.LOCK_UN)
            except OSError:
                os.close(fd)
                return "session.lock is held"
            os.close(fd)
        except OSError:
            pass
    # 2) look for a java process referencing this world dir
    try:
        out = subprocess.run(["pgrep", "-fa", "java"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "neoforge" in line.lower() or "nogui" in line.lower() or "server" in line.lower():
                return f"java process: {line.strip()[:80]}"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
def main():
    apply = "--apply" in sys.argv
    force = "--force" in sys.argv
    all_relics = "--all-relics" in sys.argv
    targets = target_ids(all_relics)
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(WORLD, f"relic-removal-backup-{ts}") if apply else None

    print(f"{'APPLY (writing)' if apply else 'DRY RUN (no changes)'} — targeting {len(targets)} relics:")
    for t in sorted(targets):
        print(f"    {t}")
    print()

    if apply:
        why = None if force else server_running()
        if why:
            sys.exit(f"REFUSING: server appears to be running ({why}).\n"
                     f"Stop the server, then re-run. Use --force only if you are certain it is down.")
        os.makedirs(backup_dir, exist_ok=True)
        print(f"Backups of modified files -> {backup_dir}\n")

    report = {"skipped": [], "per_location": {}}
    grand = Counter()

    def tally(label, counter):
        if counter:
            report["per_location"][label] = dict(counter)
            grand.update(counter)
            print(f"  {label}: {sum(counter.values())}  ({dict(counter)})")

    # 1) playerdata
    print("== playerdata ==")
    pd = Counter()
    for p in sorted(glob.glob(os.path.join(WORLD, "playerdata", "*.dat"))):
        pd.update(process_dat(p, targets, apply, backup_dir, report))
    tally("playerdata", pd)

    # 2) world saved-data
    print("== data (saved-data) ==")
    dd = Counter()
    for p in sorted(glob.glob(os.path.join(WORLD, "data", "*.dat"))):
        dd.update(process_dat(p, targets, apply, backup_dir, report))
    tally("data", dd)

    # 3) region (block entities) + 4) entities, per dimension
    for dim in DIMS:
        dname = os.path.basename(dim) or "overworld"
        for sub in ("region", "entities"):
            d = os.path.join(dim, sub)
            files = sorted(glob.glob(os.path.join(d, "*.mca")))
            if not files:
                continue
            print(f"== {dname}/{sub} ({len(files)} files) ==", flush=True)
            c = Counter()
            for n, p in enumerate(files, 1):
                c.update(process_region(p, targets, apply, backup_dir, report))
                if n % 50 == 0 or n == len(files):
                    print(f"    ...{n}/{len(files)} files, {sum(c.values())} found so far", flush=True)
            tally(f"{dname}/{sub}", c)

    print("\n==================== SUMMARY ====================")
    print(f"{'REMOVED' if apply else 'WOULD REMOVE'}: {sum(grand.values())} relic item(s)")
    for k in sorted(grand):
        print(f"    {k}: {grand[k]}")
    if report["skipped"]:
        print(f"\nSkipped {len(report['skipped'])} unreadable file(s)/chunk(s) "
              f"(empty or corrupt — left untouched). See report.")
    report["mode"] = "apply" if apply else "dry-run"
    report["targets"] = sorted(targets)
    report["total"] = sum(grand.values())
    report["totals_by_id"] = dict(grand)
    out = os.path.join(HERE, f"relic_removal_report_{ts}.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport: {out}")
    if not apply and sum(grand.values()):
        print("Re-run with --apply (server stopped) to actually remove these.")


if __name__ == "__main__":
    main()
