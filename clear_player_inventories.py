#!/usr/bin/env python3
import os
import sys
import argparse
import datetime
import shutil
import json
import re
import fcntl
import uuid
import nbtlib

# Pattern to match UUID v4 player data files
UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.dat$', re.IGNORECASE)

ARMOR_SLOTS = {
    103: "Head",
    102: "Chest",
    101: "Legs",
    100: "Feet"
}

def uuid_to_ints(uuid_str):
    """Converts a standard UUID hex string into Minecraft's NBT IntArray (4 signed 32-bit integers) representation."""
    try:
        u = uuid.UUID(uuid_str)
        val = u.int
        ints = []
        for i in range(4):
            part = (val >> (96 - i * 32)) & 0xFFFFFFFF
            if part >= 0x80000000:
                part -= 0x100000000
            ints.append(part)
        return ints
    except Exception:
        return None

def load_username_cache(server_root):
    """Loads the player UUID-to-Username cache."""
    cache_path = os.path.join(server_root, 'usernamecache.json')
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load usernamecache.json: {e}")
    return {}

def is_server_running(world_dir):
    """
    Checks if the Minecraft server is currently running and locking the world.
    Uses POSIX fcntl lockf checking which aligns with Java's FileChannel.lock().
    """
    lock_file = os.path.join(world_dir, 'session.lock')
    if not os.path.exists(lock_file):
        return False
    try:
        with open(lock_file, 'r+b') as f:
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Lock acquired successfully, which means the server process is NOT running
        return False
    except BlockingIOError:
        # Lock failed, server is running
        return True
    except Exception as e:
        # Be conservative: if we can't check, assume it might be running or return False
        return False

def make_backup(playerdata_dir, backup_root):
    """Creates a full timestamped copy of the playerdata directory and global bank data."""
    try:
        os.makedirs(backup_root, exist_ok=True)
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        dest_dir = os.path.join(backup_root, f"playerdata_backup_{timestamp}")
        # Copy playerdata files
        shutil.copytree(playerdata_dir, dest_dir)
        
        # Also copy Lightman's Currency global bank data if it exists
        world_dir = os.path.dirname(playerdata_dir)
        bank_data_file = os.path.join(world_dir, 'data', 'lightmanscurrency_bank_data.dat')
        if os.path.exists(bank_data_file):
            shutil.copy2(bank_data_file, os.path.join(dest_dir, 'lightmanscurrency_bank_data.dat'))
            
        return dest_dir
    except Exception as e:
        print(f"CRITICAL ERROR: Failed to create inventory backup: {e}")
        sys.exit(1)

def find_player_files(player_str, playerdata_dir, username_cache):
    """Resolves a username or UUID string to the correct player data file path."""
    # 1. Try treating player_str as exact UUID
    uuid_pattern_exact = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)
    if uuid_pattern_exact.match(player_str):
        filepath = os.path.join(playerdata_dir, f"{player_str.lower()}.dat")
        if os.path.exists(filepath):
            return [(player_str.lower(), filepath)]

    # 2. Scan username cache
    matches = []
    player_str_lower = player_str.lower()
    for uuid, username in username_cache.items():
        if username.lower() == player_str_lower or uuid.lower() == player_str_lower:
            filepath = os.path.join(playerdata_dir, f"{uuid.lower()}.dat")
            if os.path.exists(filepath):
                matches.append((uuid.lower(), filepath))

    return matches

def get_bank_balance(uuid_str, bank_data_path):
    """Extracts the player's bank account balances from Lightman's Currency bank data file."""
    if not os.path.exists(bank_data_path):
        return None
    ints = uuid_to_ints(uuid_str)
    if not ints:
        return None
    try:
        nbt = nbtlib.load(bank_data_path)
        for entry in nbt.get('data', {}).get('PlayerBankData', []):
            player_ints = list(entry.get('Player', []))
            if player_ints == ints:
                bank = entry.get('BankAccount', {})
                coin_storage = bank.get('CoinStorage', [])
                balances = []
                for storage in coin_storage:
                    for coin_val in storage.get('Value', []):
                        amount = int(coin_val.get('Amount', 0))
                        coin_id = str(coin_val.get('Coin', 'unknown'))
                        coin_name = coin_id.split(':')[-1].replace('coin_', '').replace('_', ' ')
                        balances.append(f"{amount} {coin_name}")
                return balances if balances else ["0 coins"]
    except Exception:
        pass
    return None

def clear_player_bank_account(uuid_str, bank_data_path):
    """Clears the player's personal bank account balance in the Lightman's Currency bank file."""
    if not os.path.exists(bank_data_path):
        return False, "Bank data file not found"
    ints = uuid_to_ints(uuid_str)
    if not ints:
        return False, "Invalid UUID"
    try:
        nbt = nbtlib.load(bank_data_path)
        mutated = False
        for entry in nbt.get('data', {}).get('PlayerBankData', []):
            player_ints = list(entry.get('Player', []))
            if player_ints == ints:
                bank = entry.get('BankAccount', {})
                if 'CoinStorage' in bank and len(bank['CoinStorage']) > 0:
                    bank['CoinStorage'].clear()
                    mutated = True
        if mutated:
            nbt.save()
            return True, "Bank account cleared successfully"
        return False, "Bank balance already 0"
    except Exception as e:
        return False, f"Failed to modify bank file: {str(e)}"

def restore_player_bank_account(uuid_str, backup_bank_path, active_bank_path):
    """Copies a single player's bank account entry from a backup bank file to the active bank file."""
    if not os.path.exists(backup_bank_path) or not os.path.exists(active_bank_path):
        return False
    ints = uuid_to_ints(uuid_str)
    if not ints:
        return False
    try:
        nbt_backup = nbtlib.load(backup_bank_path)
        backup_entry = None
        for entry in nbt_backup.get('data', {}).get('PlayerBankData', []):
            if list(entry.get('Player', [])) == ints:
                backup_entry = entry
                break
        if not backup_entry:
            return False
            
        nbt_active = nbtlib.load(active_bank_path)
        active_entries = nbt_active.get('data', {}).get('PlayerBankData', [])
        replaced = False
        for idx, entry in enumerate(active_entries):
            if list(entry.get('Player', [])) == ints:
                active_entries[idx] = backup_entry
                replaced = True
                break
        if not replaced:
            active_entries.append(backup_entry)
            
        nbt_active.save()
        return True
    except Exception:
        return False

def analyze_player_data(filepath, include_enderchest=False, bank_data_path=None):
    """
    Parses player NBT and extracts a summary of all items in:
    - Main Inventory & Hotbar
    - Armor slots
    - Offhand
    - Carried/cursor slot
    - Curios slots (accessories)
    - Ender Chest (optional)
    - Lightman's Currency Bank Account (optional)
    """
    try:
        nbt = nbtlib.load(filepath)
    except Exception as e:
        return {"error": f"Failed to load/parse NBT: {str(e)}"}
    
    summary = {
        "vanilla_inventory_count": 0,
        "vanilla_items": [],
        "armor_count": 0,
        "armor_items": [],
        "offhand_count": 0,
        "offhand_items": [],
        "curios_count": 0,
        "curios_items": [],
        "enderchest_count": 0,
        "enderchest_items": [],
        "carried_item": None,
        "bank_balance": None,
    }
    
    # 1. Standard Inventory (includes hotbar, main inv, armor, offhand)
    inventory = nbt.get('Inventory', [])
    for item in inventory:
        slot = item.get('Slot', 0)
        # Convert byte to signed int (-128 to 127)
        if isinstance(slot, int) and slot > 127:
            slot -= 256
        
        item_id = str(item.get('id', 'unknown'))
        count = int(item.get('count', 1))
        item_info = {"id": item_id, "count": count, "slot": slot}
        
        if 100 <= slot <= 103:
            summary["armor_count"] += count
            summary["armor_items"].append(item_info)
        elif slot == -106:
            summary["offhand_count"] += count
            summary["offhand_items"].append(item_info)
        else:
            summary["vanilla_inventory_count"] += count
            summary["vanilla_items"].append(item_info)
            
    # 2. Carried (Cursor item in GUI)
    if 'carried' in nbt:
        carried = nbt['carried']
        item_id = str(carried.get('id', ''))
        if item_id:
            count = int(carried.get('count', 1))
            summary["carried_item"] = {"id": item_id, "count": count}
            
    # 3. Curios API Accessories (NeoForge attachment)
    attachments = nbt.get('neoforge:attachments', {})
    curios_inv = attachments.get('curios:inventory', {})
    for curio in curios_inv.get('Curios', []):
        identifier = str(curio.get('Identifier', 'unknown'))
        sh = curio.get('StacksHandler', {})
        for key in ['Stacks', 'Cosmetics']:
            if key in sh and 'Items' in sh[key]:
                for item in sh[key]['Items']:
                    item_id = str(item.get('id', 'unknown'))
                    count = int(item.get('count', 1))
                    slot = int(item.get('Slot', 0))
                    summary["curios_count"] += count
                    summary["curios_items"].append({
                        "identifier": identifier,
                        "type": key,
                        "id": item_id,
                        "count": count,
                        "slot": slot
                    })
                    
    # 4. Ender Chest
    if include_enderchest:
        ender_items = nbt.get('EnderItems', [])
        for item in ender_items:
            item_id = str(item.get('id', 'unknown'))
            count = int(item.get('count', 1))
            slot = int(item.get('Slot', 0))
            summary["enderchest_count"] += count
            summary["enderchest_items"].append({"id": item_id, "count": count, "slot": slot})

    # 5. Lightman's Currency Bank Account Balance
    if bank_data_path:
        uuid_str = os.path.splitext(os.path.basename(filepath))[0]
        summary["bank_balance"] = get_bank_balance(uuid_str, bank_data_path)
            
    return summary

def format_player_report(uuid, username, summary, include_enderchest, include_bank):
    """Formats a detailed report for a single player's items to delete."""
    if "error" in summary:
        return f"Player: {username} ({uuid})\n  ERROR: {summary['error']}"

    lines = []
    lines.append(f"Player: {username} ({uuid})")
    
    # Vanilla Inventory
    inv_count = summary["vanilla_inventory_count"]
    lines.append(f"  Vanilla Inventory: {inv_count} items")
    if summary["vanilla_items"]:
        samples = [f"{it['id']} x{it['count']}" for it in summary["vanilla_items"][:5]]
        if len(summary["vanilla_items"]) > 5:
            samples.append("...")
        lines.append(f"    Sample: {', '.join(samples)}")
        
    # Armor
    armor_count = summary["armor_count"]
    lines.append(f"  Equipped Armor: {armor_count} items")
    for it in summary["armor_items"]:
        slot_name = ARMOR_SLOTS.get(it["slot"], f"Slot {it['slot']}")
        lines.append(f"    - {slot_name}: {it['id']} x{it['count']}")
        
    # Off-hand
    offhand_count = summary["offhand_count"]
    lines.append(f"  Off-hand: {offhand_count} items")
    for it in summary["offhand_items"]:
        lines.append(f"    - {it['id']} x{it['count']}")
        
    # Carried
    carried = summary["carried_item"]
    if carried:
        lines.append(f"  Carried (Mouse Cursor): {carried['id']} x{carried['count']}")
    else:
        lines.append(f"  Carried (Mouse Cursor): None")
        
    # Curios
    curios_count = summary["curios_count"]
    lines.append(f"  Curios Accessories: {curios_count} items")
    for it in summary["curios_items"]:
        type_str = "cosmetic" if it["type"] == "Cosmetics" else "active"
        lines.append(f"    - [{it['identifier']} ({type_str})]: {it['id']} x{it['count']}")
        
    # Ender Chest
    if include_enderchest:
        ec_count = summary["enderchest_count"]
        lines.append(f"  Ender Chest: {ec_count} items")
        if summary["enderchest_items"]:
            samples = [f"{it['id']} x{it['count']}" for it in summary["enderchest_items"][:5]]
            if len(summary["enderchest_items"]) > 5:
                samples.append("...")
            lines.append(f"    Sample: {', '.join(samples)}")

    # Lightman's Currency Bank Account Balance
    bank_bal = summary["bank_balance"]
    if bank_bal is not None:
        bal_str = ", ".join(bank_bal) if bank_bal else "0 coins"
        flag_note = "WIPE ENABLED" if include_bank else "NOT CLEARED UNLESS --include-bank SPECIFIED"
        lines.append(f"  Lightman's Currency Bank Balance: {bal_str} ({flag_note})")
            
    return "\n".join(lines)

def clear_player_data(filepath, include_enderchest=False):
    """
    Safely mutates and saves the player NBT file to delete:
    - Vanilla inventory list
    - Carried/cursor item
    - Curios slots items
    - Optionally Ender Chest items
    """
    try:
        nbt = nbtlib.load(filepath)
    except Exception as e:
        return False, f"Failed to load NBT: {str(e)}"
        
    mutated = False
    
    # 1. Clear vanilla inventory
    if 'Inventory' in nbt and len(nbt['Inventory']) > 0:
        nbt['Inventory'].clear()
        mutated = True
        
    # 2. Clear carried item
    if 'carried' in nbt:
        nbt.pop('carried')
        mutated = True
        
    # 3. Clear curios accessories
    attachments = nbt.get('neoforge:attachments', {})
    curios_inv = attachments.get('curios:inventory', {})
    for curio in curios_inv.get('Curios', []):
        sh = curio.get('StacksHandler', {})
        for key in ['Stacks', 'Cosmetics']:
            if key in sh and 'Items' in sh[key]:
                if len(sh[key]['Items']) > 0:
                    sh[key]['Items'].clear()
                    mutated = True
                    
    # 4. Clear ender chest
    if include_enderchest:
        if 'EnderItems' in nbt and len(nbt['EnderItems']) > 0:
            nbt['EnderItems'].clear()
            mutated = True
            
    if mutated:
        try:
            nbt.save()
            return True, "Successfully cleared"
        except Exception as e:
            return False, f"Failed to write changes to disk: {str(e)}"
            
    return False, "No items found to clear"

def restore_from_backup(backup_dir, playerdata_dir, target_player=None, username_cache=None, force=False, no_backup=False, world_dir=None, include_bank=False):
    """
    Restores playerdata file(s) from a backup directory.
    If target_player is specified, only that player's data is restored.
    Otherwise, all playerdata files in the backup are restored.
    """
    backup_dir = os.path.abspath(backup_dir)
    if not os.path.isdir(backup_dir):
        print(f"Error: Specified backup directory does not exist or is not a directory: {backup_dir}")
        sys.exit(1)
        
    # Run-state safety check
    server_running = is_server_running(world_dir)
    if server_running:
        print("=" * 80)
        print("WARNING: THE MINECRAFT SERVER APPEARS TO BE RUNNING!")
        print("Restoring player data while the server is active can result in data loss or")
        print("changes being overwritten when players log out or the server autosaves.")
        print("It is HIGHLY recommended to stop the server before proceeding.")
        print("=" * 80)
        if not force:
            print("Aborting restore. Please stop the server or run with --force to override.")
            sys.exit(1)

    # Determine files to restore
    files_to_restore = [] # List of tuples: (filename, src_path, dest_path)
    
    if target_player:
        resolved = find_player_files(target_player, backup_dir, username_cache)
        if not resolved:
            print(f"Error: Could not find player data for: '{target_player}' inside backup folder: {backup_dir}")
            sys.exit(1)
        for uuid_str, src_path in resolved:
            filename = f"{uuid_str}.dat"
            dest_path = os.path.join(playerdata_dir, filename)
            files_to_restore.append((filename, src_path, dest_path))
            
            # Check for .dat_old
            src_old = src_path + "_old"
            if os.path.exists(src_old):
                files_to_restore.append((filename + "_old", src_old, os.path.join(playerdata_dir, filename + "_old")))
    else:
        # Restore all files in the backup directory
        for f in os.listdir(backup_dir):
            if f.endswith('.dat') or f.endswith('.dat_old'):
                files_to_restore.append((f, os.path.join(backup_dir, f), os.path.join(playerdata_dir, f)))
                
    if not files_to_restore:
        print(f"No player data files (.dat or .dat_old) found in backup: {backup_dir}")
        sys.exit(0)

    # Check for backup bank data file
    backup_bank_path = os.path.join(backup_dir, 'lightmanscurrency_bank_data.dat')
    active_bank_path = os.path.join(world_dir, 'data', 'lightmanscurrency_bank_data.dat')
    has_backup_bank = os.path.exists(backup_bank_path)

    print("=" * 80)
    print(f"RESTORE ACTION SUMMARY")
    print(f"Source Backup: {backup_dir}")
    print(f"Target Directory: {playerdata_dir}")
    if target_player:
        print(f"Restoring single player: {target_player}")
    else:
        print(f"Restoring ALL players (replaces current files)")
    print(f"Files to copy: {len(files_to_restore)}")
    if has_backup_bank:
        if include_bank:
            print("Lightman's Currency Bank Data restore: ENABLED")
        else:
            print("Lightman's Currency Bank Data restore: SKIPPED (use --include-bank to restore)")
    print("=" * 80)
    
    for fname, src, dest in files_to_restore:
        print(f"  - {fname} -> {dest}")
        
    if not force:
        confirm = input("\nAre you sure you want to PERMANENTLY restore and overwrite current player files? (y/N): ")
        if confirm.lower() not in ['y', 'yes']:
            print("Restore cancelled.")
            sys.exit(0)

    # Safety backup of current playerdata before overwriting
    if not no_backup:
        safety_backup_root = os.path.join(world_dir, 'playerdata_backups')
        safety_path = make_backup(playerdata_dir, safety_backup_root)
        print(f"\n[Safety Backup] Created safety backup of current playerdata before overwrite:")
        print(f"  {safety_path}")

    # Perform copying files
    print("\nRestoring files...")
    success_count = 0
    for fname, src, dest in files_to_restore:
        try:
            shutil.copy2(src, dest)
            print(f"  [Restored] {fname}")
            success_count += 1
        except Exception as e:
            print(f"  [ERROR] Failed to restore {fname}: {e}")

    # Restore bank accounts
    if has_backup_bank and include_bank:
        print("\nRestoring Lightman's Currency bank account balances...")
        if target_player:
            # Selective restore for specific player UUIDs
            for uuid_str, _ in resolved:
                username = username_cache.get(uuid_str, "Unknown")
                if os.path.exists(active_bank_path):
                    ok = restore_player_bank_account(uuid_str, backup_bank_path, active_bank_path)
                    if ok:
                        print(f"  [Restored Bank] {username} ({uuid_str})")
                    else:
                        print(f"  [Bank Skip/Failed] {username} ({uuid_str}) - Player not found in backup bank file")
                else:
                    # If active bank data file does not exist, copy entire backup file
                    try:
                        shutil.copy2(backup_bank_path, active_bank_path)
                        print(f"  [Restored Entire Bank File] Active bank file did not exist.")
                    except Exception as e:
                        print(f"  [ERROR] Failed to write bank file: {e}")
        else:
            # Overwrite entire bank data file for bulk restore
            try:
                shutil.copy2(backup_bank_path, active_bank_path)
                print("  [Restored Bank] Overwrote global bank data file.")
            except Exception as e:
                print(f"  [ERROR] Failed to overwrite active bank data file: {e}")
            
    print("\n" + "=" * 80)
    print("RESTORE COMPLETE SUMMARY:")
    print(f"  Successfully restored: {success_count} / {len(files_to_restore)} files.")
    print("=" * 80)

def main():
    parser = argparse.ArgumentParser(
        description="Safely deletes player inventory, equipped gear, Curios accessories, and Lightman's Currency bank accounts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run scan all players (SAFE, default)
  python3 clear_player_inventories.py
  
  # Dry-run scan a specific player by username or UUID
  python3 clear_player_inventories.py --player LucaLullaby
  
  # Apply inventory wipe to all players
  python3 clear_player_inventories.py --apply
  
  # Wipe inventory AND ender chests for a specific player (by UUID)
  python3 clear_player_inventories.py --apply --player 8cd8ddea-9067-4bf8-b41b-5e06858bda6e --include-enderchest

  # Wipe inventory AND Lightman's Currency bank account balances for all players
  python3 clear_player_inventories.py --apply --include-bank

  # Restore all playerdata from a backup directory (including bank accounts)
  python3 clear_player_inventories.py --restore sintara/playerdata_backups/playerdata_backup_20260618_120000 --include-bank
"""
    )
    parser.add_argument('--apply', action='store_true', help='Execute the inventory deletions (modifies files). Default is dry-run.')
    parser.add_argument('--player', type=str, help='Only wipe/restore inventory for a specific player (username or UUID).')
    parser.add_argument('--include-enderchest', action='store_true', help='Also clear the player\'s Ender Chest inventory (only applies to clearing).')
    parser.add_argument('--include-bank', action='store_true', help='Also clear/restore player\'s Lightman\'s Currency bank accounts.')
    parser.add_argument('--restore', type=str, metavar='BACKUP_DIR', help='Restore player data from a specified backup folder.')
    parser.add_argument('--server-dir', type=str, default='/home/minecraft', help='Server root directory path.')
    parser.add_argument('--backup-dir', type=str, help='Custom directory for safety backups (defaults to <world>/playerdata_backups).')
    parser.add_argument('--no-backup', action='store_true', help='Skip the automatic playerdata backup (NOT RECOMMENDED).')
    parser.add_argument('--force', action='store_true', help='Bypass server-running checks and confirmations.')

    args = parser.parse_args()

    server_dir = os.path.abspath(args.server_dir)
    world_dir = os.path.join(server_dir, 'sintara')
    playerdata_dir = os.path.join(world_dir, 'playerdata')
    bank_data_path = os.path.join(world_dir, 'data', 'lightmanscurrency_bank_data.dat')
    
    if not os.path.exists(playerdata_dir):
        print(f"Error: Playerdata directory not found at: {playerdata_dir}")
        sys.exit(1)

    # Load cache mappings (UUID -> Username)
    username_cache = load_username_cache(server_dir)

    # If restore is specified, handle restore and exit early
    if args.restore:
        restore_from_backup(
            backup_dir=args.restore,
            playerdata_dir=playerdata_dir,
            target_player=args.player,
            username_cache=username_cache,
            force=args.force,
            no_backup=args.no_backup,
            world_dir=world_dir,
            include_bank=args.include_bank
        )
        sys.exit(0)

    # Determine files to process
    target_files = [] # List of tuples: (uuid_str, filepath)
    if args.player:
        resolved = find_player_files(args.player, playerdata_dir, username_cache)
        if not resolved:
            print(f"Error: Could not find player data for: '{args.player}' (no file matches UUID or cached name)")
            sys.exit(1)
        target_files = resolved
    else:
        # Load all UUID files in playerdata directory
        for f in os.listdir(playerdata_dir):
            if UUID_PATTERN.match(f):
                uuid_str = f[:-4].lower()
                target_files.append((uuid_str, os.path.join(playerdata_dir, f)))

    if not target_files:
        print("No player data files found to process.")
        sys.exit(0)

    # 1. Run-state safety check
    server_running = is_server_running(world_dir)
    if server_running:
        print("=" * 80)
        print("WARNING: THE MINECRAFT SERVER APPEARS TO BE RUNNING!")
        print("Modifying player data while the server is active can result in data loss or")
        print("changes being overwritten when players log out or the server autosaves.")
        print("It is HIGHLY recommended to stop the server before proceeding.")
        print("=" * 80)
        if args.apply and not args.force:
            print("Aborting. Please stop the server or run with --force to override.")
            sys.exit(1)

    # 2. Perform Scan & Summary (Dry Run Report)
    print("=" * 80)
    print(f"INVENTORY SCAN REPORT ({'APPLY WIPE' if args.apply else 'DRY RUN'})")
    print(f"Total player files selected: {len(target_files)}")
    print(f"Ender Chest clearing: {'ENABLED' if args.include_enderchest else 'DISABLED'}")
    print(f"Lightman's Currency Bank clearing: {'ENABLED' if args.include_bank else 'DISABLED'}")
    print("=" * 80)

    total_vanilla = 0
    total_armor = 0
    total_offhand = 0
    total_curios = 0
    total_ender = 0
    total_carried = 0
    
    summaries = [] # List of tuples: (uuid, name, summary_dict)
    for uuid_str, filepath in target_files:
        username = username_cache.get(uuid_str, "Unknown")
        summary = analyze_player_data(filepath, include_enderchest=args.include_enderchest, bank_data_path=bank_data_path)
        summaries.append((uuid_str, username, summary))
        
        if "error" not in summary:
            total_vanilla += summary["vanilla_inventory_count"]
            total_armor += summary["armor_count"]
            total_offhand += summary["offhand_count"]
            total_curios += summary["curios_count"]
            total_ender += summary["enderchest_count"]
            if summary["carried_item"]:
                total_carried += summary["carried_item"]["count"]

        # Print each player detailed info
        report_text = format_player_report(uuid_str, username, summary, args.include_enderchest, args.include_bank)
        print(report_text)
        print("-" * 40)

    # Print aggregated summary
    print("\n" + "=" * 80)
    print("AGGREGATED TOTALS TO BE DELETED:")
    print(f"  Vanilla Inventory Items: {total_vanilla}")
    print(f"  Equipped Armor Items:    {total_armor}")
    print(f"  Off-hand Items:          {total_offhand}")
    print(f"  Carried Cursor Items:    {total_carried}")
    print(f"  Curios Accessory Items:  {total_curios}")
    if args.include_enderchest:
        print(f"  Ender Chest Items:       {total_ender}")
    print("=" * 80)

    # If this is just dry run, exit now
    if not args.apply:
        print("\nDry run completed. No files were modified.")
        print("To execute the changes, run the script with the '--apply' flag.")
        sys.exit(0)

    # 3. Execution (Apply Mode)
    # Confirm with user
    if not args.force:
        confirm = input(f"\nAre you sure you want to PERMANENTLY delete these items/balances for {len(target_files)} players? (y/N): ")
        if confirm.lower() not in ['y', 'yes']:
            print("Operation cancelled. No files modified.")
            sys.exit(0)

    # Create safety backup
    if not args.no_backup:
        backup_root = args.backup_dir if args.backup_dir else os.path.join(world_dir, 'playerdata_backups')
        backup_path = make_backup(playerdata_dir, backup_root)
        print(f"\n[Backup] Safely backed up 'playerdata' and bank files to:")
        print(f"  {backup_path}")

    # Wipe inventories & bank accounts
    print("\nWiping player data...")
    modified_count = 0
    skipped_count = 0
    failed_count = 0
    
    for uuid_str, filepath in target_files:
        username = username_cache.get(uuid_str, "Unknown")
        
        # Clear files
        success_data, message_data = clear_player_data(filepath, include_enderchest=args.include_enderchest)
        success_bank = False
        message_bank = "N/A"
        
        if args.include_bank and os.path.exists(bank_data_path):
            success_bank, message_bank = clear_player_bank_account(uuid_str, bank_data_path)
            
        if success_data or success_bank:
            actions = []
            if success_data:
                actions.append("Inventory cleared")
            if success_bank:
                actions.append("Bank account cleared")
            print(f"  [Wiped] {username} ({uuid_str}) - {', '.join(actions)}.")
            modified_count += 1
        elif message_data == "No items found to clear" and (not args.include_bank or message_bank in ["N/A", "Bank balance already 0"]):
            print(f"  [Skipped] {username} ({uuid_str}) - No items or bank balance found to clear.")
            skipped_count += 1
        else:
            errs = []
            if not success_data and message_data != "No items found to clear":
                errs.append(f"Inv: {message_data}")
            if args.include_bank and not success_bank and message_bank != "Bank balance already 0":
                errs.append(f"Bank: {message_bank}")
            print(f"  [ERROR] {username} ({uuid_str}) - {'; '.join(errs)}")
            failed_count += 1

    print("\n" + "=" * 80)
    print("WIPE COMPLETE SUMMARY:")
    print(f"  Wiped successfully: {modified_count} players")
    print(f"  Skipped (already clean): {skipped_count} players")
    print(f"  Failed (errors): {failed_count} players")
    print("=" * 80)

if __name__ == "__main__":
    main()
