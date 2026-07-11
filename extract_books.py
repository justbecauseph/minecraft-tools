#!/usr/bin/env python3
"""
extract_books.py — extract the text of every player-written book world-wide, OFFLINE
(reads saved files only; never loads/ticks a chunk).

Covers:
  - minecraft:written_book / minecraft:writable_book (book & quill, signed or not)
  - candlelight:note_paper_written / note_paper_writeable (text in minecraft:custom_data)

Scanned sources (all dimensions incl. dimensions/*/*):
  - region/*.mca      block entities: chests, barrels, lecterns, chiseled bookshelves,
                      shulkers-in-chests, mod containers — anything holding an item NBT
  - entities/*.mca    item frames, dropped items, chest boats/minecarts, corpses
  - playerdata/*.dat  inventories, ender chests, curios, equipped backpacks
  - sublevels/*.slvls Create Aeronautics ships (gzip NBT)

The walk is recursive over the whole NBT tree, so nested containers (book in shulker
in chest, backpack contents stored as components) are found without special cases.
Handles both 1.20.5+ component format and legacy tag.pages chunks that predate it.

Usage:
    python3 extract_books.py [--world DIR] [--out DIR]

Output:
    <out>/books.json   every instance with dimension/coords/holder
    <out>/books.txt    human-readable, deduplicated by content

Deps: lz4 (C ext) + nbtlib  (same as scan_connectors.py / find_blocks.py).
Region compression here is LZ4Block (Anvil type 4, Java LZ4BlockOutputStream framing).
Run `save-all flush` via RCON first if you need the last few minutes reflected.
"""
import struct, io, glob, sys, gzip, zlib, json, argparse, os, time, hashlib
from collections import defaultdict

try:
    import lz4.block
    import nbtlib
except ImportError as e:
    sys.exit("missing dependency: %s\n  pip install --user --break-system-packages lz4 nbtlib" % e)

TARGET_IDS = {
    "minecraft:written_book",
    "minecraft:writable_book",
    "candlelight:note_paper_written",
    "candlelight:note_paper_writeable",
}
# cheap byte prefilter on decompressed chunk data before paying for an NBT parse
NEEDLES = [b"written_book", b"writable_book", b"note_paper_writ"]


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
        if orig_len == 0:
            break
        block = data[i:i + comp_len]
        i += comp_len
        if (token & 0xF0) == 0x10:
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


def json_text_to_plain(s):
    """Flatten a serialized JSON text component to plain text."""
    def flatten(c):
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(flatten(x) for x in c)
        if isinstance(c, dict):
            out = c.get("text", "")
            if "translate" in c:
                out += c["translate"]
            out += "".join(flatten(x) for x in c.get("extra", []))
            return out
        return str(c)
    try:
        return flatten(json.loads(s))
    except Exception:
        return s


def unwrap(v):
    """nbtlib tags -> plain python."""
    if isinstance(v, dict):
        return {str(k): unwrap(x) for k, x in v.items()}
    if isinstance(v, (list, nbtlib.tag.List)):
        return [unwrap(x) for x in v]
    if isinstance(v, (nbtlib.tag.String,)):
        return str(v)
    if isinstance(v, (nbtlib.tag.Byte, nbtlib.tag.Short, nbtlib.tag.Int, nbtlib.tag.Long)):
        return int(v)
    if isinstance(v, (nbtlib.tag.Float, nbtlib.tag.Double)):
        return float(v)
    return v


def filterable(page):
    """1.20.5+ pages are Filterable: either raw value or {raw: ..., filtered: ...}."""
    if isinstance(page, dict):
        return page.get("raw", "")
    return page


def extract_item(node, where):
    """node: plain-python dict for an item compound with id in TARGET_IDS."""
    iid = node["id"]
    rec = {"item": iid, "count": int(node.get("count", node.get("Count", 1)) or 1), **where}
    comp = node.get("components") or {}
    legacy = node.get("tag") or {}

    if iid.endswith("written_book"):
        c = comp.get("minecraft:written_book_content") or {}
        if c:
            rec["title"] = filterable(c.get("title", ""))
            rec["author"] = c.get("author", "")
            rec["pages"] = [json_text_to_plain(str(filterable(p))) for p in c.get("pages", [])]
        else:  # legacy pre-component chunk
            rec["title"] = legacy.get("title", "")
            rec["author"] = legacy.get("author", "")
            rec["pages"] = [json_text_to_plain(str(p)) for p in legacy.get("pages", [])]
    elif iid.endswith("writable_book"):
        c = comp.get("minecraft:writable_book_content") or {}
        pages = c.get("pages") if c else legacy.get("pages", [])
        rec["pages"] = [str(filterable(p)) for p in (pages or [])]
    else:  # candlelight note paper: free-form compound in custom_data
        c = comp.get("minecraft:custom_data") or legacy or {}
        rec["note_data"] = c
        rec["title"] = c.get("title", "")
        rec["author"] = c.get("author", "")
        pages = c.get("text", c.get("pages", []))
        if isinstance(pages, str):
            pages = [pages]
        rec["pages"] = [json_text_to_plain(str(filterable(p))) for p in pages]

    # skip books with no text at all (blank book & quill)
    if not rec.get("pages") and not rec.get("title") and not rec.get("note_data"):
        return None
    return rec


def walk(node, where, out):
    """Recursively find target item compounds anywhere in the tree."""
    if isinstance(node, dict):
        nid = node.get("id")
        if isinstance(nid, str) and nid in TARGET_IDS:
            r = extract_item(node, where)
            if r:
                out.append(r)
        # refine location context as we descend
        if all(k in node for k in ("x", "y", "z")) and isinstance(node.get("x"), int):
            where = {**where, "pos": [node["x"], node["y"], node["z"]],
                     "holder": str(node.get("id", where.get("holder", "?")))}
        elif "Pos" in node and isinstance(node["Pos"], list) and len(node["Pos"]) == 3:
            try:
                where = {**where, "pos": [round(float(p), 1) for p in node["Pos"]],
                         "holder": str(node.get("id", where.get("holder", "?")))}
            except Exception:
                pass
        for v in node.values():
            walk(v, where, out)
    elif isinstance(node, list):
        for v in node:
            walk(v, where, out)


def scan_mca(path, dim, kind, out, stats):
    with open(path, "rb") as f:
        hdr = f.read(4096)
        if len(hdr) < 4096:
            return
        for slot in range(1024):
            off = struct.unpack(">I", b"\x00" + hdr[slot * 4:slot * 4 + 3])[0]
            if off == 0:
                continue
            f.seek(off * 4096)
            lb = f.read(4)
            if len(lb) < 4:
                continue
            ln = struct.unpack(">I", lb)[0]
            if ln <= 1:
                continue
            comp = f.read(1)[0]
            data = f.read(ln - 1)
            try:
                raw = decompress(comp, data)
            except Exception as e:
                stats["decomp_fail"] += 1
                continue
            stats["chunks"] += 1
            if not any(n in raw for n in NEEDLES):
                continue
            try:
                ch = unwrap(nbtlib.File.parse(io.BytesIO(raw), byteorder="big"))
            except Exception:
                stats["nbt_fail"] += 1
                continue
            walk(ch, {"dim": dim, "source": "%s %s slot%d" % (kind, os.path.basename(path), slot)}, out)


def player_names(world):
    names = {}
    for uc in (os.path.join(os.path.dirname(world), "usercache.json"), os.path.join(world, "..", "usercache.json")):
        try:
            for e in json.load(open(uc)):
                names[e["uuid"]] = e["name"]
            break
        except Exception:
            pass
    return names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="/home/minecraft/sintara")
    ap.add_argument("--out", default="/home/minecraft/book-extract")
    args = ap.parse_args()
    world = args.world
    os.makedirs(args.out, exist_ok=True)

    dims = [("overworld", world)]
    for d, sub in (("nether", "DIM-1"), ("end", "DIM1")):
        if os.path.isdir(os.path.join(world, sub)):
            dims.append((d, os.path.join(world, sub)))
    for p in sorted(glob.glob(os.path.join(world, "dimensions", "*", "*"))):
        dims.append(("%s:%s" % (os.path.basename(os.path.dirname(p)), os.path.basename(p)), p))

    out, stats = [], defaultdict(int)
    t0 = time.time()

    # 1) playerdata
    names = player_names(world)
    for pd in sorted(glob.glob(os.path.join(world, "playerdata", "*.dat"))):
        uuid = os.path.basename(pd)[:-4]
        if len(uuid) != 36:   # skip <uuid>-<number>.dat leftovers with empty roots
            continue
        try:
            nbt = unwrap(nbtlib.load(pd))
        except Exception:
            stats["nbt_fail"] += 1
            continue
        walk(nbt, {"dim": "player", "source": "playerdata",
                   "holder": names.get(uuid, uuid)}, out)
    print("[%5.1fs] playerdata done: %d hits so far" % (time.time() - t0, len(out)), flush=True)

    # 2) sublevels (Create Aeronautics ships). *.slvls are Anvil-layout region
    # files whose chunks use compression byte 0 followed by a plain gzip stream
    # (*.slvlr are tiny plot-index records with no item NBT).
    for sl in sorted(glob.glob(os.path.join(world, "sublevels", "*.slvls"))):
        try:
            with open(sl, "rb") as f:
                hdr = f.read(4096)
                if len(hdr) < 4096:
                    continue
                for slot in range(1024):
                    off = struct.unpack(">I", b"\x00" + hdr[slot * 4:slot * 4 + 3])[0]
                    if not off:
                        continue
                    f.seek(off * 4096)
                    lb = f.read(4)
                    if len(lb) < 4:
                        continue
                    ln = struct.unpack(">I", lb)[0]
                    if ln <= 1:
                        continue
                    comp = f.read(1)[0]
                    data = f.read(ln - 1)
                    try:
                        raw = gzip.decompress(data) if (comp == 0 and data[:2] == b"\x1f\x8b") \
                            else decompress(comp, data)
                    except Exception:
                        stats["decomp_fail"] += 1
                        continue
                    stats["chunks"] += 1
                    if not any(n in raw for n in NEEDLES):
                        continue
                    try:
                        nbt = unwrap(nbtlib.File.parse(io.BytesIO(raw), byteorder="big"))
                    except Exception:
                        stats["nbt_fail"] += 1
                        continue
                    walk(nbt, {"dim": "sublevel",
                               "source": "%s slot%d" % (os.path.basename(sl), slot)}, out)
        except Exception:
            stats["nbt_fail"] += 1
    print("[%5.1fs] sublevels done: %d hits so far" % (time.time() - t0, len(out)), flush=True)

    # 3) region + entities per dimension
    for dim, base in dims:
        for kind in ("region", "entities"):
            files = sorted(glob.glob(os.path.join(base, kind, "*.mca")))
            for i, p in enumerate(files):
                scan_mca(p, dim, kind, out, stats)
                if i % 200 == 0:
                    print("[%5.1fs] %s/%s %d/%d files, %d hits" %
                          (time.time() - t0, dim, kind, i, len(files), len(out)), flush=True)
        print("[%5.1fs] %s done: %d hits so far" % (time.time() - t0, dim, len(out)), flush=True)

    with open(os.path.join(args.out, "books.json"), "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, default=str)

    # dedupe by content for the readable dump
    groups = {}
    for r in out:
        key = hashlib.sha1(json.dumps(
            [r["item"], r.get("title", ""), r.get("author", ""), r.get("pages", []),
             str(r.get("note_data", ""))], ensure_ascii=False).encode()).hexdigest()
        groups.setdefault(key, {"rec": r, "locs": []})["locs"].append(
            "%s %s %s" % (r["dim"], r.get("pos", ""), r.get("holder", r.get("source", ""))))

    with open(os.path.join(args.out, "books.txt"), "w") as f:
        for g in sorted(groups.values(), key=lambda g: (g["rec"]["item"], str(g["rec"].get("title", "")))):
            r = g["rec"]
            f.write("=" * 70 + "\n")
            f.write("%s  title=%r  author=%r  (%d cop%s)\n" %
                    (r["item"], r.get("title", ""), r.get("author", ""),
                     len(g["locs"]), "y" if len(g["locs"]) == 1 else "ies"))
            for l in sorted(set(g["locs"])):
                f.write("  @ %s\n" % l)
            if r.get("note_data") and not r.get("pages"):
                f.write("-- raw note data --\n%s\n" % json.dumps(r["note_data"], ensure_ascii=False, indent=1))
            for i, pg in enumerate(r.get("pages", [])):
                f.write("-- page %d --\n%s\n" % (i + 1, pg))
            f.write("\n")

    print("DONE in %.1fs: %d instances, %d unique; chunks=%d decomp_fail=%d nbt_fail=%d" %
          (time.time() - t0, len(out), len(groups), stats["chunks"],
           stats["decomp_fail"], stats["nbt_fail"]), flush=True)
    print("wrote %s/books.json and %s/books.txt" % (args.out, args.out))


if __name__ == "__main__":
    main()
