#!/usr/bin/env python3
"""
rollback_chunks.py — surgically roll back one chunk (or a rectangular chunk
selection) from a backup into the LIVE world, OFFLINE, without disturbing any
other chunk.

For each selected chunk it lifts that chunk's raw NBT from the BACKUP region
file, re-encodes it as zlib (type 2 — Minecraft reads any compression type and
rewrites as LZ4 on the next save), and splices it into the current LIVE region
file. Every chunk NOT in the selection is preserved byte-for-byte from live, so
this is a true partial rollback, not a whole-region revert.

Works on region/ (blocks + tile entities), entities/, and poi/ .mca files. See
memory [[mca-region-format]] / [[entity-region-recovery]] for the container +
compression details this implements (NeoForge 1.21.1 writes LZ4Block-framed
type-4; we decode that and any of gzip/zlib/raw).

  *** RUN WITH THE SERVER STOPPED ***
A running server has the world loaded and rewrites chunks on unload/save, which
would clobber the swap. Stop the service, confirm no java PID (auto-restart),
run this, then start it again.

Backup source may be a directory (an unpacked world root, containing
region/entities/poi) OR a .tar.gz whose members are <prefix>/<world>/region/...
(the daily /tmp/minecraft_*.tar.gz backups store paths as
"minecraft/sintara/region/r.X.Z.mca"). For a tarball, the needed region files
are extracted to a temp dir first.

Semantics per selected chunk:
  present in backup                 -> REPLACE live chunk with the backup copy
  absent in backup, present in live -> DELETE from live (it was ungenerated at
                                       backup time; removing lets it regenerate)
  absent in both                    -> no-op

Dry run by default (reads only, writes nothing). Pass --apply to overwrite the
live files; each touched live file is copied to <name>.bak-<ts> first.

Usage:
    # single chunk, blocks+entities+poi, from a tarball — DRY RUN
    python3 rollback_chunks.py --chunk 41 -42 \
        --backup /tmp/minecraft_20260706_020009.tar.gz

    # same, actually write it (server must be stopped)
    python3 rollback_chunks.py --chunk 41 -42 \
        --backup /tmp/minecraft_20260706_020009.tar.gz --apply

    # a rectangle of chunks, blocks only, from an unpacked backup world
    python3 rollback_chunks.py --rect 103 -32 115 -30 --only region \
        --backup /home/minecraft/sintara.pre-restore-20260704_065652

    # restrict which layers to touch
    python3 rollback_chunks.py --chunk 41 -42 --only region entities \
        --backup /tmp/minecraft_20260706_020009.tar.gz
"""
import argparse, io, os, struct, sys, tarfile, tempfile, time, zlib, shutil
import nbtlib

WORLD = "/home/minecraft/sintara"      # live world root (level-name=sintara)
WORLD_NAME = "sintara"
SUBDIRS_ALL = ("region", "entities", "poi")
SECTOR = 4096
LZ4_MAGIC = bytes.fromhex("4c5a34426c6f636b")  # "LZ4Block"

# ---------------------------------------------------------------- anvil mca I/O
def read_region(path):
    """{(local_cx,local_cz): (comp_type, raw_payload, timestamp)}; payload stays
    compressed and comp_type is preserved so untouched chunks round-trip exactly."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 2 * SECTOR:
        return {}
    loc, ts = data[:SECTOR], data[SECTOR:2*SECTOR]
    out = {}
    for idx in range(1024):
        e = struct.unpack(">I", b"\x00" + loc[idx*4:idx*4+3])[0]
        cnt = loc[idx*4+3]
        if e == 0 or cnt == 0:
            continue
        off = e * SECTOR
        length = struct.unpack(">I", data[off:off+4])[0]
        ctype = data[off+4]
        payload = data[off+5:off+4+length]
        timestamp = struct.unpack(">I", ts[idx*4:idx*4+4])[0]
        out[(idx % 32, idx // 32)] = (ctype, payload, timestamp)
    return out

def lz4block_decompress(buf):
    import lz4.block
    o = io.BytesIO(); p = 0
    while True:
        assert buf[p:p+8] == LZ4_MAGIC, "bad LZ4Block magic"
        token = buf[p+8]
        clen = struct.unpack("<I", buf[p+9:p+13])[0]
        olen = struct.unpack("<I", buf[p+13:p+17])[0]
        p += 21  # 8 magic + 1 token + 4 clen + 4 olen + 4 checksum
        if olen == 0:
            break
        block = buf[p:p+clen]; p += clen
        method = token & 0xF0
        if method == 0x10:      # stored
            o.write(block)
        elif method == 0x20:    # lz4
            o.write(lz4.block.decompress(block, uncompressed_size=olen))
        else:
            raise ValueError(f"unknown LZ4Block method {method:#x}")
    return o.getvalue()

def decompress_chunk(ctype, payload):
    if ctype == 1: return __import__("gzip").decompress(payload)
    if ctype == 2: return zlib.decompress(payload)
    if ctype == 3: return payload
    if ctype == 4: return lz4block_decompress(payload)
    raise ValueError(f"unknown compression type {ctype}")

def write_region(path, chunks):
    """Rebuild the anvil container from {(lcx,lcz): (ctype, payload, ts)}."""
    loc = bytearray(SECTOR); ts = bytearray(SECTOR); body = bytearray()
    next_sector = 2
    for (lcx, lcz), (ctype, payload, timestamp) in sorted(chunks.items()):
        idx = lcx + lcz * 32
        rec = struct.pack(">I", len(payload) + 1) + bytes([ctype]) + payload
        rec += b"\x00" * ((-len(rec)) % SECTOR)
        nsec = len(rec) // SECTOR
        assert nsec <= 255, f"chunk {(lcx,lcz)} too big: {nsec} sectors"
        loc[idx*4:idx*4+3] = struct.pack(">I", next_sector)[1:]
        loc[idx*4+3] = nsec
        ts[idx*4:idx*4+4] = struct.pack(">I", timestamp)
        body += rec
        next_sector += nsec
    with open(path, "wb") as f:
        f.write(loc); f.write(ts); f.write(body)

# ---------------------------------------------------------------- backup source
def resolve_backup(backup, world_name, region_files, subdirs):
    """Return a dir that holds <subdir>/<fname> for the needed files. If `backup`
    is a tarball, extract just those members into a temp dir and return it."""
    if os.path.isdir(backup):
        return backup, None
    tmp = tempfile.mkdtemp(prefix="rollback_bak_")
    wanted = {f"{sd}/{fn}" for sd in subdirs for fn in region_files}
    with tarfile.open(backup, "r:gz") as tf:
        for m in tf:
            # strip the leading "<prefix>/<world>/" to match "<subdir>/<fname>"
            parts = m.name.split("/")
            if world_name in parts:
                rel = "/".join(parts[parts.index(world_name)+1:])
            else:
                rel = m.name
            if rel in wanted:
                m.name = rel
                tf.extract(m, tmp)
    return tmp, tmp

# ---------------------------------------------------------------- verification
def chunk_info(subdir, raw, cx, cz):
    nf = nbtlib.File.from_fileobj(io.BytesIO(raw), byteorder="big")
    root = nf[""] if "" in nf else nf
    if subdir == "region":
        ok = (root.get("xPos") == cx and root.get("zPos") == cz)
        return ok, f"xPos/zPos={root.get('xPos')},{root.get('zPos')} status={root.get('Status')}"
    if subdir == "entities":
        p = root.get("Position")
        ok = p is not None and int(p[0]) == cx and int(p[1]) == cz
        ents = root.get("Entities") or []
        return ok, f"Position={list(p) if p is not None else None} entities={len(ents)}"
    return True, "poi parsed ok"   # poi has no embedded position; index-derived

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Surgically roll back chunks from a backup.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--chunk", nargs=2, type=int, metavar=("CX", "CZ"),
                   help="single chunk coordinate")
    g.add_argument("--rect", nargs=4, type=int, metavar=("CX0", "CZ0", "CX1", "CZ1"),
                   help="inclusive rectangle of chunk coords")
    ap.add_argument("--backup", required=True, help="backup world dir OR .tar.gz")
    ap.add_argument("--world", default=WORLD, help=f"live world root (default {WORLD})")
    ap.add_argument("--world-name", default=WORLD_NAME,
                    help="world/level name as stored inside a tarball path")
    ap.add_argument("--only", nargs="+", choices=SUBDIRS_ALL, default=list(SUBDIRS_ALL),
                    help="which layers to touch (default: all three)")
    ap.add_argument("--apply", action="store_true",
                    help="write live files (default is dry run). SERVER MUST BE STOPPED.")
    a = ap.parse_args()

    if a.chunk:
        cx0 = cx1 = a.chunk[0]; cz0 = cz1 = a.chunk[1]
    else:
        cx0, cz0, cx1, cz1 = a.rect
        if cx0 > cx1: cx0, cx1 = cx1, cx0
        if cz0 > cz1: cz0, cz1 = cz1, cz0
    coords = [(cx, cz) for cx in range(cx0, cx1+1) for cz in range(cz0, cz1+1)]

    # group by region file
    regions = {}
    for cx, cz in coords:
        regions.setdefault((cx >> 5, cz >> 5), []).append((cx, cz))
    region_files = {f"r.{rx}.{rz}.mca" for rx, rz in regions}

    bak_root, cleanup = resolve_backup(a.backup, a.world_name, region_files, a.only)
    now = int(time.time())
    print(f"=== rollback {len(coords)} chunk(s) {coords if len(coords)<=6 else f'{(cx0,cz0)}..{(cx1,cz1)}'} ===")
    print(f"backup: {a.backup}")
    print(f"live  : {a.world}")
    print(f"layers: {a.only}    mode: {'APPLY (writing)' if a.apply else 'DRY RUN'}\n")

    total = {"replace": 0, "delete": 0, "add": 0, "noop": 0, "bad": 0}
    try:
        for subdir in a.only:
            for (rx, rz), sel in sorted(regions.items()):
                fname = f"r.{rx}.{rz}.mca"
                live_path = os.path.join(a.world, subdir, fname)
                bak_path  = os.path.join(bak_root, subdir, fname)
                if not os.path.exists(live_path):
                    print(f"[{subdir}/{fname}] SKIP: live missing"); continue
                live = read_region(live_path)
                bak  = read_region(bak_path) if os.path.exists(bak_path) else {}
                merged = dict(live)
                for cx, cz in sel:
                    key = (cx & 31, cz & 31)
                    if key in bak:
                        ct, pl, bts = bak[key]
                        raw = decompress_chunk(ct, pl)
                        ok, info = chunk_info(subdir, raw, cx, cz)
                        if not ok:
                            print(f"   ! {subdir} ({cx},{cz}) position mismatch: {info} — skipped")
                            total["bad"] += 1; continue
                        act = "REPLACE" if key in live else "ADD"
                        total["replace" if key in live else "add"] += 1
                        print(f"[{subdir}] {act} ({cx},{cz}): {info}")
                        merged[key] = (2, zlib.compress(raw, 6), bts or now)
                    elif key in live:
                        print(f"[{subdir}] DELETE ({cx},{cz}): absent in backup (was ungenerated)")
                        total["delete"] += 1
                        del merged[key]
                    else:
                        total["noop"] += 1
                if not a.apply:
                    continue
                bkp = f"{live_path}.bak-{now}"
                shutil.copy2(live_path, bkp)
                write_region(live_path, merged)
                # verify swapped chunks re-read + parse
                ver = read_region(live_path); bad = 0
                for cx, cz in sel:
                    key = (cx & 31, cz & 31)
                    if key not in ver: continue
                    try:
                        decompress_chunk(*ver[key][:2])
                    except Exception as ex:
                        bad += 1; print(f"   VERIFY FAIL ({cx},{cz}): {ex}")
                print(f"[{subdir}/{fname}] wrote (live-backup {bkp}); "
                      f"chunks {len(live)}->{len(ver)}; verify-bad={bad}\n")
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)

    print(f"\nsummary: {total}")
    if not a.apply:
        print("DRY RUN — nothing written. Re-run with --apply (server stopped) to commit.")

if __name__ == "__main__":
    main()
