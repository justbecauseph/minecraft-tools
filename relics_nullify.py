#!/usr/bin/env python3
"""
Lore-nullify all Relics (sskirillss) accessories: lock every ability so none
ever unlocks, while leaving the items fully equippable.

Route: set requiredLevel + requiredPoints absurdly high on every ability in
every generated per-relic config. Chosen over zeroing stat values because some
relics use *multiplier* stats (e.g. incoming_damage_multiplier) where 0 inverts
into invincibility. Locking is uniform and has no such trap.

Prereq: run the server ONCE with `enabledExtendedConfigs: true` in
config/relics.yaml so the mod generates config/relics/*. Then run this.

Usage:
    python3 relics_nullify.py            # dry-run, prints planned changes
    python3 relics_nullify.py --apply    # back up, then rewrite in place
    python3 relics_nullify.py --selftest # validate logic on a synthetic sample
"""
import sys, os, shutil, json, datetime

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

RELICS_DIR = "/home/minecraft/config/relics"
LOCK = 1_000_000          # requiredLevel/requiredPoints far beyond any maxLevel
GATE_KEYS = ("requiredLevel", "requiredPoints")


def lock_abilities(node, counter):
    """Recurse any nested structure; any mapping that looks like an ability
    (has a requiredLevel/requiredPoints gate) gets its gate maxed out."""
    if isinstance(node, dict):
        if any(k in node for k in GATE_KEYS):
            for k in GATE_KEYS:
                if k in node and node[k] != LOCK:
                    counter.append((k, node[k]))
                    node[k] = LOCK
        for v in node.values():
            lock_abilities(v, counter)
    elif isinstance(node, list):
        for v in node:
            lock_abilities(v, counter)
    return node


# --- Stat-value nullify for the 6 "leaking" relics (always-on passives that the
# requiredLevel lock does NOT cover, because their effects gate only on the
# hardcoded-true isEnabled()). For these we zero the benefit-magnitude stats so
# the effect computes to 0. We deliberately DO NOT touch cost/cooldown/ratio
# stats (cooldown, revival_cost, player_xp_ratio) — zeroing those inverts into a
# stronger/broken effect, and they're moot once the magnitude is 0 anyway.
# All other relics are canPlayerUse/isUnlocked-gated and handled by LOCK.
STAT_ZERO = {
    "leafy_mantle":         ["heal", "absorption", "radius", "damage", "paralysis"],
    "hunting_belt":         ["amount", "damage_modifier", "pet_radius", "resistance_per_pet"],
    "experience_disperser": ["distribution_ratio", "same_item_bonus"],
    "roller_skate":         ["speed", "step_height", "resistance", "damage", "ignite"],
    "springy_boot":         ["power", "damage_modifier", "radius", "damage", "stun"],
    "reflective_necklace":  ["chance", "damage", "lifetime", "piercings", "stun", "bounces"],
}
ZERO_FIELDS = ("minInitialValue", "maxInitialValue", "targetValue")


def zero_stats(data, stat_names, counter):
    """Recurse to the stats map; for each named stat set its initial/target
    values to 0 so the effect magnitude is 0 (thresholds left as-is — 0 is
    within their range)."""
    if isinstance(data, dict):
        stats = data.get("stats")
        if isinstance(stats, dict):
            for sname, sval in stats.items():
                if sname in stat_names and isinstance(sval, dict):
                    for fld in ZERO_FIELDS:
                        if fld in sval and sval[fld] != 0:
                            counter.append((sname, fld, sval[fld]))
                            sval[fld] = 0.0
        for v in data.values():
            zero_stats(v, stat_names, counter)
    elif isinstance(data, list):
        for v in data:
            zero_stats(v, stat_names, counter)


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json"):
        return json.loads(text), "json"
    return yaml.safe_load(text), "yaml"


def dump(data, kind):
    if kind == "json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)


def process_tree(apply):
    if not os.path.isdir(RELICS_DIR):
        sys.exit(f"Not found: {RELICS_DIR}\n"
                 "Generate it first: set enabledExtendedConfigs: true and "
                 "restart the server once.")
    files = []
    for root, _, names in os.walk(RELICS_DIR):
        for n in names:
            if n.endswith((".yaml", ".yml", ".json")):
                files.append(os.path.join(root, n))
    files.sort()
    if not files:
        sys.exit(f"No config files under {RELICS_DIR}")

    if apply:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{RELICS_DIR}.bak.{stamp}"
        shutil.copytree(RELICS_DIR, backup)
        print(f"Backup: {backup}")

    total = 0
    zeroed = 0
    for path in files:
        try:
            data, kind = load(path)
        except Exception as e:
            print(f"  SKIP (parse error) {path}: {e}")
            continue
        changes = []
        lock_abilities(data, changes)
        # Stat-zero pass for the always-on leaking relics (matched by basename).
        base = os.path.splitext(os.path.basename(path))[0]
        stat_changes = []
        if base in STAT_ZERO:
            zero_stats(data, set(STAT_ZERO[base]), stat_changes)
        rel = os.path.relpath(path, RELICS_DIR)
        if changes or stat_changes:
            total += len(changes)
            zeroed += len(stat_changes)
            msg = f"  {rel}: "
            if changes:
                msg += f"{len(changes)} gate(s)->{LOCK} "
            if stat_changes:
                stats_hit = sorted({s for s, _, _ in stat_changes})
                msg += f"| {len(stat_changes)} stat-field(s)->0 [{', '.join(stats_hit)}]"
            print(msg)
            if apply:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(dump(data, kind))
        else:
            print(f"  {rel}: no ability gates found (already locked or none)")
    verb = "APPLIED" if apply else "DRY-RUN (no files written)"
    print(f"\n{verb}: {total} gate value(s) locked, {zeroed} stat-field(s) "
          f"zeroed across {len(files)} file(s).")
    if not apply:
        print("Re-run with --apply to write, then restart the server.")


def selftest():
    sample = {
        "relic": "relics:ring_of_the_seven_deadly_sins",
        "abilitiesData": {"abilities": {
            "wrath": {"requiredPoints": 1, "requiredLevel": 0, "maxLevel": 10,
                      "stats": {"bonus_damage": {"minInitialValue": 0.1,
                                                 "maxInitialValue": 0.5,
                                                 "targetValue": 1.0,
                                                 "scalingModel": "to"}}},
            "gluttony": {"requiredPoints": 2, "requiredLevel": 3, "maxLevel": 5,
                         "stats": {"incoming_damage_multiplier":
                                   {"minInitialValue": 0.8, "targetValue": 0.2}}},
        }},
        "levelingData": {"step": 5, "initialCost": 10},
    }
    changes = []
    lock_abilities(sample, changes)
    a = sample["abilitiesData"]["abilities"]
    assert a["wrath"]["requiredLevel"] == LOCK
    assert a["wrath"]["requiredPoints"] == LOCK
    assert a["gluttony"]["requiredLevel"] == LOCK
    assert a["gluttony"]["requiredPoints"] == LOCK
    # stats and multipliers are untouched (no invincibility inversion)
    assert a["wrath"]["maxLevel"] == 10
    assert a["gluttony"]["stats"]["incoming_damage_multiplier"]["targetValue"] == 0.2
    assert len(changes) == 4
    # round-trips through YAML cleanly
    yaml.safe_load(dump(sample, "yaml"))

    # stat-zero pass: only listed stats zeroed, cost/ratio stats left intact
    leaker = {"abilitiesData": {"abilities": {
        "skating": {"requiredLevel": 0, "stats": {
            "speed":      {"minInitialValue": 0.2, "maxInitialValue": 0.5, "targetValue": 1.0},
            "resistance": {"minInitialValue": 0.1, "maxInitialValue": 0.3, "targetValue": 0.5},
        }},
    }}}
    sc = []
    zero_stats(leaker, set(STAT_ZERO["roller_skate"]), sc)
    rs = leaker["abilitiesData"]["abilities"]["skating"]["stats"]
    assert rs["speed"]["minInitialValue"] == 0 and rs["speed"]["targetValue"] == 0
    assert rs["resistance"]["targetValue"] == 0          # resistance IS in roller_skate's list
    assert len(sc) == 6                                  # 3 fields x 2 stats
    # a stat NOT in the list is untouched
    leaker2 = {"stats": {"player_xp_ratio": {"targetValue": 1.0}}}
    sc2 = []
    zero_stats(leaker2, set(STAT_ZERO["experience_disperser"]), sc2)
    assert leaker2["stats"]["player_xp_ratio"]["targetValue"] == 1.0 and not sc2
    print("selftest OK: 4 gates locked, leaker stats zeroed, cost/ratio stats preserved, YAML valid")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        process_tree(apply="--apply" in sys.argv)
