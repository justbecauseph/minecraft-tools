#!/usr/bin/env python3
"""
camerapture_owners.py — reconstruct who took each Camerapture picture.

The server picture store (sintara/camerapture/<uuid>.webp) is anonymous. But every
picture ITEM carries a `camerapture:picture_data` component = {id, creator, timestamp}.
We recover ownership by scanning all item NBT in the world for that component:
  - playerdata/*.dat            (inventories, ender chests)
  - <dim>/entities/*.mca        (placed picture frames, item frames, dropped items…)
  - <dim>/region/*.mca          (block entities: chests/shulkers/barrels/albums…)

Coverage is best-effort: an image whose every item copy was destroyed/never crafted
has no surviving creator and stays unattributed.

Output: CSV (image_uuid, creator, timestamp_utc, n_copies, sources) + a per-creator
summary to stdout, and coverage vs the .webp files on disk.

Region MCA reader (LZ4Block framing) reused from scan_connectors.py.
Deps: lz4, nbtlib.
"""
import struct, io, glob, gzip, zlib, sys, time, csv, os, argparse
from collections import defaultdict
from datetime import datetime, timezone

import lz4.block
import nbtlib

WORLD = "/home/minecraft/sintara"
DIMS = ["", "/DIM-1", "/DIM1"]          # overworld, nether, end
COMPONENT = "camerapture:picture_data"
NEEDLE = b"camerapture:picture_data"     # cheap byte pre-filter before NBT parse


# --- region decompression (from scan_connectors.py) ---
def lz4_stream(data):
    i, out = 0, bytearray()
    while i + 21 <= len(data):
        if data[i:i + 8] != b"LZ4Block":
            break
        token = data[i + 8]
        comp_len = int.from_bytes(data[i + 9:i + 13], "little")
        orig_len = int.from_bytes(data[i + 13:i + 17], "little")
        i += 21
        if orig_len == 0:
            break
        block = data[i:i + comp_len]; i += comp_len
        out += block if (token & 0xF0) == 0x10 else lz4.block.decompress(block, uncompressed_size=orig_len)
    return bytes(out)


def decompress(comp, data):
    if comp == 1: return gzip.decompress(data)
    if comp == 2: return zlib.decompress(data)
    if comp == 3: return data
    if comp == 4: return lz4_stream(data)
    raise ValueError("unknown compression %d" % comp)


# --- generic NBT walk collecting every picture_data component ---
def dash_uuid(s):
    """Camerapture stores the image id as a 32-char undashed hex string;
    the .webp files on disk use the canonical dashed form. Normalize."""
    s = str(s).replace("-", "").lower()
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}" if len(s) == 32 else str(s)


def collect(tag, source, hits):
    if hasattr(tag, "items"):
        sub = tag.get(COMPONENT) if COMPONENT in tag else None
        if sub is not None:
            try:
                hits.append((dash_uuid(sub["id"]), str(sub["creator"]),
                             int(sub.get("timestamp", 0)), source))
            except Exception:
                pass
        for v in tag.values():
            collect(v, source, hits)
    elif isinstance(tag, (list, tuple)) or (hasattr(tag, "__iter__") and not isinstance(tag, (str, bytes, bytearray))):
        for v in tag:
            collect(v, source, hits)


def scan_playerdata(hits):
    files = glob.glob(WORLD + "/playerdata/*.dat")
    for f in files:
        try:
            collect(nbtlib.load(f), "playerdata:" + os.path.basename(f), hits)
        except Exception:
            pass
    return len(files)


def scan_region_dir(rdir, kind, hits):
    files = sorted(glob.glob(rdir + "/*.mca"))
    for path in files:
        try:
            with open(path, "rb") as f:
                hdr = f.read(4096)
                if len(hdr) < 4096:
                    continue
                for slot in range(1024):
                    off = struct.unpack(">I", b"\x00" + hdr[slot*4:slot*4+3])[0]
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
                    except Exception:
                        continue
                    if NEEDLE not in raw:
                        continue
                    try:
                        ch = nbtlib.File.parse(io.BytesIO(raw), byteorder="big")
                    except Exception:
                        continue
                    collect(ch, "%s:%s" % (kind, os.path.basename(path)), hits)
        except Exception:
            pass
    return len(files)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.expanduser("~/camerapture_owners.csv"))
    args = ap.parse_args()

    hits = []
    t0 = time.time()
    n = scan_playerdata(hits)
    print(f"playerdata: {n} files, running hits={len(hits)} ({time.time()-t0:.0f}s)", flush=True)
    for d in DIMS:
        c = scan_region_dir(WORLD + d + "/entities", "entities" + (d or "/ow"), hits)
        print(f"entities{d or ' (overworld)'}: {c} files, hits={len(hits)} ({time.time()-t0:.0f}s)", flush=True)
    for d in DIMS:
        c = scan_region_dir(WORLD + d + "/region", "region" + (d or "/ow"), hits)
        print(f"region{d or ' (overworld)'}: {c} files, hits={len(hits)} ({time.time()-t0:.0f}s)", flush=True)

    # dedupe by image uuid: one row per picture, count copies, keep source kinds
    by_img = {}
    for uid, creator, ts, source in hits:
        r = by_img.setdefault(uid, {"creator": creator, "ts": ts, "n": 0, "src": set()})
        r["n"] += 1
        r["src"].add(source.split(":")[0])
        if creator and creator != "None":
            r["creator"] = creator
        if ts:
            r["ts"] = ts

    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["image_uuid", "creator", "timestamp_utc", "n_copies", "sources"])
        for uid, r in sorted(by_img.items(), key=lambda kv: kv[1]["creator"].lower()):
            ts = datetime.fromtimestamp(r["ts"]/1000, timezone.utc).isoformat() if r["ts"] else ""
            w.writerow([uid, r["creator"], ts, r["n"], "|".join(sorted(r["src"]))])

    # coverage
    disk = {os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(WORLD + "/camerapture/*.webp")}
    mapped = set(by_img)
    per_creator = defaultdict(int)
    for uid, r in by_img.items():
        per_creator[r["creator"]] += 1

    print(f"\n=== RESULTS ({time.time()-t0:.0f}s) ===")
    print(f"total picture items found (all copies): {len(hits)}")
    print(f"unique images attributed:               {len(mapped)}")
    print(f"images on disk:                         {len(disk)}")
    print(f"attributed & on disk:                   {len(mapped & disk)}")
    print(f"unattributed (no surviving item):       {len(disk - mapped)}")
    print(f"mapped but file missing (frame w/o webp):{len(mapped - disk)}")
    print(f"\n=== pictures per creator ({len(per_creator)} creators) ===")
    for c, k in sorted(per_creator.items(), key=lambda kv: -kv[1]):
        print(f"  {k:5d}  {c}")
    print(f"\nCSV -> {args.out}")


if __name__ == "__main__":
    main()
