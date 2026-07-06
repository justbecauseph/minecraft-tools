#!/bin/bash
# Mother's Grove teleport helper (RCON).
#   ./grove_tp.sh send     -> players within radius 10 of overworld 780 64 -458
#                             go to lampas:mothers_grove 159 192 -65
#   ./grove_tp.sh return   -> players within radius 30 of grove 159 192 -65
#                             go back to overworld 780 64 -458
set -euo pipefail

PROPS="/home/minecraft/server.properties"
PORT=$(grep -E "^rcon.port=" "$PROPS" | cut -d= -f2)
PASS=$(grep -E "^rcon.password=" "$PROPS" | cut -d= -f2)
RCON=(mcrcon -H localhost -P "$PORT" -p "$PASS")

case "${1:-}" in
  send)
    "${RCON[@]}" "execute in minecraft:overworld as @a[x=780,y=64,z=-458,distance=..20] in lampas:mothers_grove run tp @s 159 194 -68"
    ;;
  return)
    "${RCON[@]}" "execute in lampas:mothers_grove as @a[x=159,y=192,z=-65,distance=..30] in minecraft:overworld run tp @s 780 65 -458"
    ;;
  *)
    echo "usage: $0 {send|return}" >&2
    exit 1
    ;;
esac
