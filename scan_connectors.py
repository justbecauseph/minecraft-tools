#!/usr/bin/env python3
"""
scan_connectors.py — find Tom's Storage inventory connectors world-wide and flag
clusters of multiple connectors (the misconfiguration that crashes the server
with "Do not call getCapability on an invalid cache").

Background: Tom's Storage wants EXACTLY ONE inventory connector per network.
Multiple connectors indexing overlapping inventories trigger a NeoForge
capability-cache invalidation race -> server-thread crash (ticking block entity).
This scans the saved region files OFFLINE (safe; never loads/ticks the chunk) and
reports any group of >=2 connectors close together.

Usage:
    python3 scan_connectors.py [--radius N] [--world DIR]

    --radius N   Chebyshev distance (blocks) to treat connectors as one cluster
                 (default 8). Two connectors within N on any axis are grouped.
    --world DIR  World save dir (default: /home/minecraft/sintara). Scans the
                 overworld (region/) and nether (DIM-1/region/). Add dims below.

Exit code: 0 = no clusters found, 1 = one or more clusters found (for cron/alerts).

Notes / deps:
  - Reads the SAVED region files only. Chunks edited in-memory but not yet
    autosaved won't be reflected until the next save.
  - Region chunks here use LZ4 (Anvil compression type 4 = Java
    LZ4BlockOutputStream framing): each block is
        MAGIC "LZ4Block"(8) token(1) compLen(4 LE) origLen(4 LE) checksum(4 LE) data
    The magic repeats per block; end marker has origLen==0.
  - Requires: lz4 (C ext) and nbtlib.
        pip install --user --break-system-packages lz4 nbtlib
"""
import struct, io, glob, time, sys, gzip, zlib, argparse
from collections import defaultdict

try:
    import lz4.block
    import nbtlib
except ImportError as e:
    sys.exit("missing dependency: %s\n  pip install --user --break-system-packages lz4 nbtlib" % e)

NEEDLE = b"inventory_connector"


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
    if comp == 1: return gzip.decompress(data)
    if comp == 2: return zlib.decompress(data)
    if comp == 3: return data
    if comp == 4: return lz4_stream(data)
    raise ValueError("unknown compression type %d" % comp)


def scan_region(path, dim, connectors):
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
            data = f.read(ln - 1)
            try:
                raw = decompress(comp, data)
            except Exception as e:
                print("  DECOMP FAIL %s slot%d: %s" % (path, slot, e), flush=True)
                continue
            if NEEDLE not in raw:        # cheap skip for the ~99% with no connector
                continue
            try:
                ch = nbtlib.File.parse(io.BytesIO(raw), byteorder="big")
                for be in (ch.get("block_entities") or []):
                    if "inventory_connector" in str(be.get("id", "")):
                        connectors.append((dim, int(be["x"]), int(be["y"]), int(be["z"])))
            except Exception as e:
                print("  NBT FAIL %s slot%d: %s" % (path, slot, e), flush=True)


def cluster(connectors, radius):
    """Union-find connectors within `radius` (Chebyshev) in the same dimension."""
    parent = list(range(len(connectors)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(len(connectors)):
        di, xi, yi, zi = connectors[i]
        for j in range(i + 1, len(connectors)):
            dj, xj, yj, zj = connectors[j]
            if di == dj and max(abs(xi - xj), abs(yi - yj), abs(zi - zj)) <= radius:
                parent[find(i)] = find(j)

    groups = defaultdict(list)
    for i in range(len(connectors)):
        groups[find(i)].append(connectors[i])
    return list(groups.values())


def main():
    ap = argparse.ArgumentParser(description="Scan world for Tom's Storage connector clusters.")
    ap.add_argument("--radius", type=int, default=8, help="cluster distance in blocks (Chebyshev, default 8)")
    ap.add_argument("--world", default="/home/minecraft/sintara", help="world save dir")
    args = ap.parse_args()

    dims = [("overworld", args.world + "/region"),
            ("nether",    args.world + "/DIM-1/region")]
    # add custom/end dims here, e.g. ("end", args.world + "/DIM1/region")

    connectors = []
    t0 = time.time()
    for dim, rdir in dims:
        files = sorted(glob.glob(rdir + "/*.mca"))
        print("=== %s: %d region files ===" % (dim, len(files)), flush=True)
        for n, path in enumerate(files, 1):
            scan_region(path, dim, connectors)
            if n % 100 == 0:
                print("  %s %d/%d files, %d connectors, %.0fs"
                      % (dim, n, len(files), len(connectors), time.time() - t0), flush=True)

    print("\nTOTAL connectors found: %d in %.0fs" % (len(connectors), time.time() - t0), flush=True)
    for d, x, y, z in sorted(connectors):
        print("    [%s] (%d,%d,%d)" % (d, x, y, z), flush=True)

    clusters = sorted([g for g in cluster(connectors, args.radius) if len(g) >= 2],
                      key=len, reverse=True)
    print("\n=== CLUSTERS of >=2 connectors within %d blocks (Chebyshev): %d ==="
          % (args.radius, len(clusters)), flush=True)
    for g in clusters:
        dim = g[0][0]
        xs = [c[1] for c in g]; ys = [c[2] for c in g]; zs = [c[3] for c in g]
        print("\n[%s] %d connectors near (%d,%d,%d):"
              % (dim, len(g), sum(xs) // len(xs), sum(ys) // len(ys), sum(zs) // len(zs)), flush=True)
        for d, x, y, z in sorted(g, key=lambda c: (c[1], c[2], c[3])):
            print("    (%d,%d,%d)" % (x, y, z), flush=True)

    singles = sum(1 for g in cluster(connectors, args.radius) if len(g) == 1)
    print("\nsingle (healthy) connectors: %d" % singles, flush=True)
    print("DONE", flush=True)
    sys.exit(1 if clusters else 0)


if __name__ == "__main__":
    main()
