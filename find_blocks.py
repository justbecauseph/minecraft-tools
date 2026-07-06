#!/usr/bin/env python3
"""
find_blocks.py — locate every block whose registry id matches a pattern, across
all saved dimensions, OFFLINE (never loads/ticks a chunk).

Unlike scan_connectors.py (which reads block_entities), this decodes the actual
block-section palettes + packed indices, so it finds *placed blocks* by id even
when they have no block entity. Reports world coordinates.

Originally written to answer "where are the non-excluded sharestones?" — hence the
sharestone defaults — but it takes arbitrary substring filters.

Usage:
    # default: every waystones sharestone that ISN'T one of the excluded colors
    python3 find_blocks.py

    # any block id containing "sharestone"
    python3 find_blocks.py --match sharestone

    # find diamond ore, exclude nothing
    python3 find_blocks.py --match diamond_ore --no-default-exclude

    --match SUBSTR        block-id substring to look for (repeatable). Default: sharestone
    --exclude SUBSTR      drop matches whose id contains this (repeatable).
    --no-default-exclude  don't apply the built-in sharestone color exclusions.
    --world DIR           world save dir (default: /home/minecraft/sintara).
    --air-prefilter       also keep counting; by default we skip the cheap byte
                          prefilter only when --match is empty.

Exit code: 0 = nothing found, 1 = one or more blocks found (for cron/alerts).

Notes / deps:
  - Reads the SAVED region files only. Run `save-all flush` via RCON first if you
    need in-memory edits reflected (the live world here autosaves periodically;
    freshly-placed blocks may not be on disk yet).
  - Region chunks here use LZ4 (Anvil compression type 4 = Java
    LZ4BlockOutputStream framing): per-block
        MAGIC "LZ4Block"(8) token(1) compLen(4 LE) origLen(4 LE) checksum(4 LE) data
    The magic repeats per block; end marker has origLen==0. A naive zlib/anvil
    reader silently skips every chunk here and reports a FALSE "nothing found".
  - Block positions come from section block_states: bits = max(4, ceil(log2(n))),
    indices packed into longs without spanning long boundaries (1.16+ format),
    block order y*256 + z*16 + x within each 16^3 section.
  - Requires: lz4 (C ext) and nbtlib.
        pip install --user --break-system-packages lz4 nbtlib
"""
import struct, io, glob, time, sys, gzip, zlib, argparse, os
from collections import Counter

try:
    import lz4.block
    import nbtlib
except ImportError as e:
    sys.exit("missing dependency: %s\n  pip install --user --break-system-packages lz4 nbtlib" % e)

# waystones sharestone colors deliberately excluded by default
DEFAULT_EXCLUDE = ["cyan_sharestone", "magenta_sharestone", "lime_sharestone",
                   "red_sharestone", "orange_sharestone", "purple_sharestone",
                   "black_sharestone"]


def lz4_stream(data):
    """Decode Java LZ4BlockOutputStream framing (per-block magic header)."""
    i = 0
    out = bytearray()
    while i + 21 <= len(data):
        if data[i:i + 8] != b"LZ4Block":
            break
        token = data[i + 8]
        comp_len = int.from_bytes(data[i + 9:i + 13], "little")
        orig_len = int.from_bytes(data[i + 13:i + 17], "little")
        i += 21
        if orig_len == 0:               # end marker
            break
        block = data[i:i + comp_len]
        i += comp_len
        if (token & 0xF0) == 0x10:       # stored / uncompressed
            out += block
        else:
            out += lz4.block.decompress(block, uncompressed_size=orig_len)
    return bytes(out)


def decompress(comp, data):
    comp &= 0x7f  # strip external-file (.mcc) flag; we skip those (see caller)
    if comp == 1: return gzip.decompress(data)
    if comp == 2: return zlib.decompress(data)
    if comp == 3: return data
    if comp == 4: return lz4_stream(data)
    raise ValueError("unknown compression type %d" % comp)


def matches(name, includes, excludes):
    if not any(s in name for s in includes):
        return False
    if any(s in name for s in excludes):
        return False
    return True


def decode_section(sec, want_indices):
    """Yield (palette_index, x, y, z) for blocks in `want_indices` (local coords)."""
    bs = sec.get("block_states")
    if bs is None:
        return
    pal = bs.get("palette")
    data = bs.get("data")
    n = len(pal)
    if n <= 1 or data is None:           # single-block section: whole 16^3 is pal[0]
        if n == 1 and 0 in want_indices:
            for idx in range(4096):
                yield 0, idx & 15, (idx >> 8) & 15, (idx >> 4) & 15
        return
    bits = max(4, (n - 1).bit_length())
    per_long = 64 // bits
    mask = (1 << bits) - 1
    idx = 0
    for L in data:
        L &= 0xFFFFFFFFFFFFFFFF
        for k in range(per_long):
            if idx >= 4096:
                break
            pi = (L >> (k * bits)) & mask
            if pi in want_indices:
                yield pi, idx & 15, (idx >> 8) & 15, (idx >> 4) & 15
            idx += 1
        if idx >= 4096:
            break


def scan_region(path, dim, includes, excludes, found):
    needles = [s.encode() for s in includes] or [b""]
    with open(path, "rb") as f:
        hdr = f.read(4096)
        if len(hdr) < 4096:
            return
        for slot in range(1024):
            off = struct.unpack(">I", b"\x00" + hdr[slot * 4:slot * 4 + 3])[0]
            if off == 0:
                continue
            f.seek(off * 4096)
            ln = struct.unpack(">I", f.read(4))[0]
            if ln <= 1:
                continue
            comp = f.read(1)[0]
            if comp & 0x80:              # data in external r.x.z.cX.cZ.mcc file
                continue
            data = f.read(ln - 1)
            try:
                raw = decompress(comp, data)
            except Exception as e:
                print("  DECOMP FAIL %s slot%d: %s" % (path, slot, e), flush=True)
                continue
            if not any(nd in raw for nd in needles):   # cheap skip
                continue
            try:
                ch = nbtlib.File.parse(io.BytesIO(raw), byteorder="big")
            except Exception as e:
                print("  NBT FAIL %s slot%d: %s" % (path, slot, e), flush=True)
                continue
            cx, cz = int(ch.get("xPos")), int(ch.get("zPos"))
            for sec in (ch.get("sections") or []):
                bs = sec.get("block_states")
                if bs is None:
                    continue
                pal = bs.get("palette")
                if pal is None:
                    continue
                want = {i: str(e.get("Name")) for i, e in enumerate(pal)
                        if matches(str(e.get("Name")), includes, excludes)}
                if not want:
                    continue
                secY = int(sec.get("Y"))
                for pi, lx, ly, lz in decode_section(sec, set(want)):
                    found.append((dim, want[pi], cx * 16 + lx, secY * 16 + ly, cz * 16 + lz))


def discover_dims(world):
    dims = [("overworld", world + "/region"),
            ("the_nether", world + "/DIM-1/region"),
            ("the_end", world + "/DIM1/region")]
    for rdir in sorted(glob.glob(world + "/dimensions/*/*/region")):
        p = rdir.split("/")
        dims.append(("%s:%s" % (p[-3], p[-2]), rdir))
    return [(d, r) for d, r in dims if os.path.isdir(r)]


def main():
    ap = argparse.ArgumentParser(description="Find placed blocks by id across all dimensions (offline).")
    ap.add_argument("--match", action="append", default=[], help="block-id substring (repeatable). default: sharestone")
    ap.add_argument("--exclude", action="append", default=[], help="exclude block-id substring (repeatable)")
    ap.add_argument("--no-default-exclude", action="store_true", help="don't apply built-in sharestone color exclusions")
    ap.add_argument("--world", default="/home/minecraft/sintara", help="world save dir")
    args = ap.parse_args()

    includes = args.match or ["sharestone"]
    excludes = list(args.exclude)
    if not args.no_default_exclude and includes == ["sharestone"]:
        excludes += DEFAULT_EXCLUDE

    print("match=%s  exclude=%s" % (includes, excludes or "(none)"), flush=True)
    found = []
    t0 = time.time()
    for dim, rdir in discover_dims(args.world):
        files = sorted(glob.glob(rdir + "/*.mca"))
        print("=== %s: %d region files ===" % (dim, len(files)), flush=True)
        for n, path in enumerate(files, 1):
            scan_region(path, dim, includes, excludes, found)
            if n % 200 == 0:
                print("  %s %d/%d, %d blocks, %.0fs" % (dim, n, len(files), len(found), time.time() - t0), flush=True)

    print("\nTOTAL matching blocks: %d in %.0fs" % (len(found), time.time() - t0), flush=True)
    for dim, name, x, y, z in sorted(found):
        print("    [%s] %s (%d,%d,%d)" % (dim, name, x, y, z), flush=True)
    if found:
        print("\nby id:", flush=True)
        for name, c in Counter(n for _, n, _, _, _ in found).most_common():
            print("    %-40s %d" % (name, c), flush=True)
    print("DONE", flush=True)
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
