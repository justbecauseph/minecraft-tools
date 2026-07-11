#!/usr/bin/env python3
"""
spark_report.py - Read a spark profiler report directly from its share URL.

spark's web viewer (https://spark.lucko.me/<key>) is a client-side app; the real
data is gzip-free protobuf stored in bytebin (https://bytebin.lucko.me/<key>).
This script downloads that payload and decodes it WITHOUT the .proto schema, by
walking the protobuf wire format and using the field numbers spark emits.

Usage:
    python3 spark_report.py <spark-url-or-key> [--top N]

Examples:
    python3 spark_report.py https://spark.lucko.me/nGwbVoqNRq
    python3 spark_report.py nGwbVoqNRq --top 40

Output:
    1. Metadata summary  - platform, players online, TPS, MSPT, loaded entities
    2. Hot-path summary  - true SELF-time per method (children subtracted),
                           and self-time grouped by mod/package.

Notes / caveats:
  * Field numbers (below) were verified against spark on NeoForge 1.21.1.
    If a future spark version renumbers fields, adjust the constants.
  * The big `native .../libc.so.6` leaf is usually the main thread SLEEPING
    between ticks (parked waiting for the next tick) - i.e. spare headroom,
    not real work. Compare it against MSPT: idle% ~= 1 - mspt/50.
"""
import sys
import struct
import collections
import urllib.request

BYTEBIN = "https://bytebin.lucko.me/"

# ---- protobuf field numbers spark uses (verified on spark / NeoForge 1.21.1) ----
# top-level SamplerData
F_METADATA = 1          # SamplerMetadata
F_THREADS = 2           # repeated ThreadNode
# ThreadNode
TN_NAME = 1
TN_CHILDREN = 3         # repeated StackTraceNode (flattened node list)
# StackTraceNode
SN_CLASS = 3            # class_name (string)
SN_METHOD = 4           # method_name (string)
SN_DESC = 7             # method_desc (string)
SN_TIMES = 8            # packed double - sample time (ms) per window
SN_CHILD_REFS = 9       # packed varint - indices into the node list
# SamplerMetadata
M_PLATFORM = 7          # PlatformMetadata { .2 name, .3 version, .4 mc-version }
M_STATS = 8             # PlatformStatistics
S_TPS = 4               # TpsRollingAverage [recent windows]
S_MSPT = 5              # repeated DoubleAverageInfo { mean,max,min,median,p95 }
S_WORLD = 8             # WorldStatistics { .1 total_entities, .2 repeat{type,count} }


def read_varint(b, i):
    shift = 0
    res = 0
    while True:
        if i >= len(b):
            raise IndexError
        x = b[i]
        i += 1
        res |= (x & 0x7F) << shift
        if not (x & 0x80):
            break
        shift += 7
    return res, i


def parse(b):
    """Parse one protobuf message into {field: [(wiretype, raw), ...]}.

    Tolerant: stops at the first malformed byte rather than raising, so it is
    safe to speculatively parse a length-delimited value that may be a string.
    """
    i = 0
    out = collections.defaultdict(list)
    n = len(b)
    while i < n:
        try:
            key, i = read_varint(b, i)
        except IndexError:
            break
        field = key >> 3
        wt = key & 7
        if wt == 0:
            try:
                v, i = read_varint(b, i)
            except IndexError:
                break
            out[field].append((0, v))
        elif wt == 1:
            if i + 8 > n:
                break
            out[field].append((1, b[i:i + 8]))
            i += 8
        elif wt == 2:
            ln, i = read_varint(b, i)
            if i + ln > n:
                break
            out[field].append((2, b[i:i + ln]))
            i += ln
        elif wt == 5:
            if i + 4 > n:
                break
            out[field].append((5, b[i:i + 4]))
            i += 4
        else:
            break
    return out


def unpack_doubles(b):
    return [struct.unpack('<d', b[i:i + 8])[0] for i in range(0, len(b) - 7, 8)]


def unpack_varints(b):
    out = []
    i = 0
    while i < len(b):
        v, i = read_varint(b, i)
        out.append(v)
    return out


def as_str(field_list):
    if field_list and field_list[0][0] == 2:
        try:
            return field_list[0][1].decode('utf8', 'replace')
        except Exception:
            return None
    return None


def fetch(url_or_key):
    key = url_or_key.rstrip('/').split('/')[-1]
    req = urllib.request.Request(BYTEBIN + key, headers={'User-Agent': 'spark-report-cli'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


# ---------------------------------------------------------------------------
def summarize_metadata(top):
    if F_METADATA not in top:
        print("(no metadata block)")
        return
    meta = parse(top[F_METADATA][0][1])

    # players: SamplerMetadata.user-style entries live at field 1 (repeat)
    players = []
    for wt, raw in meta.get(1, []):
        if wt == 2:
            name = as_str(parse(raw).get(2, []))
            if name:
                players.append(name)

    print("=== SERVER ===")
    if M_PLATFORM in meta:
        pf = parse(meta[M_PLATFORM][0][1])
        print(f"  platform : {as_str(pf.get(2, []))} {as_str(pf.get(3, []))}  (MC {as_str(pf.get(4, []))})")
    print(f"  players  : {len(players)} online" + (f"  {players}" if players else ""))

    if M_STATS in meta:
        st = parse(meta[M_STATS][0][1])

        # TPS
        if S_TPS in st:
            tps = parse(st[S_TPS][0][1])
            vals = [round(struct.unpack('<d', v)[0], 1) for f in sorted(tps) for w, v in tps[f] if w == 1]
            if vals:
                print(f"  TPS      : {vals}")

        # MSPT: st[5] is a message whose sub-fields are per-window DoubleAverageInfo
        # ({1:mean, 2:max, 3:min, 4:median, 5:p95} in ms).
        for w, raw in st.get(S_MSPT, []):
            if w != 2:
                continue
            container = parse(raw)
            for win in sorted(container):
                for ww, wraw in container[win]:
                    if ww != 2:
                        continue
                    d = parse(wraw)
                    g = lambda f: round(struct.unpack('<d', d[f][0][1])[0], 1) if f in d and d[f][0][0] == 1 else None
                    if g(1) is not None:
                        print(f"  MSPT[{win}] : mean={g(1)}  median={g(4)}  p95={g(5)}  min={g(3)}  max={g(2)}  ms")

        # entities
        if S_WORLD in st:
            w = parse(st[S_WORLD][0][1])
            total = w[1][0][1] if 1 in w and w[1][0][0] == 0 else None
            ents = []
            for wt, raw in w.get(2, []):
                if wt == 2:
                    e = parse(raw)
                    name = as_str(e.get(1, []))
                    cnt = e[2][0][1] if 2 in e and e[2][0][0] == 0 else 0
                    if name:
                        ents.append((cnt, name))
            print(f"  entities : {total} loaded")
            ents.sort(reverse=True)
            for cnt, name in ents[:12]:
                print(f"             {cnt:6}  {name}")
    print()


# ---------------------------------------------------------------------------
def summarize_hotpath(top, topn):
    for wt, raw in top.get(F_THREADS, []):
        if wt != 2:
            continue
        tn = parse(raw)
        tname = as_str(tn.get(TN_NAME, [])) or "?"
        nodes = [r for w, r in tn.get(TN_CHILDREN, []) if w == 2]
        N = len(nodes)
        if N == 0:
            continue

        cls = [None] * N
        meth = [None] * N
        total = [0.0] * N
        crefs = [()] * N
        for idx, nraw in enumerate(nodes):
            sn = parse(nraw)
            cls[idx] = as_str(sn.get(SN_CLASS, [])) or '?'
            meth[idx] = as_str(sn.get(SN_METHOD, [])) or '?'
            if SN_TIMES in sn:
                total[idx] = sum(unpack_doubles(sn[SN_TIMES][0][1]))
            if SN_CHILD_REFS in sn:
                crefs[idx] = unpack_varints(sn[SN_CHILD_REFS][0][1])

        self_t = [0.0] * N
        referenced = set()
        for idx in range(N):
            child = sum(total[c] for c in crefs[idx] if c < N)
            self_t[idx] = total[idx] - child
            referenced.update(crefs[idx])
        wall = sum(total[i] for i in range(N) if i not in referenced) or 1.0

        by_method = collections.Counter()
        by_mod = collections.Counter()
        for i in range(N):
            if self_t[i] <= 0:
                continue
            by_method[f"{cls[i]}.{meth[i]}"] += self_t[i]
            c = cls[i]
            if c.startswith('net.minecraft'):
                key = '[vanilla] ' + '.'.join(c.split('.')[:5])
            else:
                key = '.'.join(c.split('.')[:3])
            by_mod[key] += self_t[i]

        print(f"=== THREAD: {tname}   ({N} nodes, wall={wall:.0f}ms) ===")
        print(f"  -- top {topn} methods by SELF time --")
        for m, t in by_method.most_common(topn):
            print(f"  {t:9.0f}ms {100 * t / wall:5.1f}%  {m}")
        print(f"  -- self time grouped by mod/package --")
        for m, t in by_mod.most_common(20):
            print(f"  {t:9.0f}ms {100 * t / wall:5.1f}%  {m}")
        print()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    topn = 30
    if '--top' in sys.argv:
        topn = int(sys.argv[sys.argv.index('--top') + 1])
    if not args:
        print(__doc__)
        sys.exit(1)

    src = args[0]
    if src.startswith('http') or not src.endswith('.bin'):
        data = fetch(src)
    else:
        data = open(src, 'rb').read()

    top = parse(data)
    summarize_metadata(top)
    summarize_hotpath(top, topn)


if __name__ == '__main__':
    main()
