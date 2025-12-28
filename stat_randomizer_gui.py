#!/usr/bin/env python3
"""
Elden Ring Starting Class Stat Randomizer - GUI Version
With Grace Unlock feature (requires game running)
"""

import os
import random
import shutil
import subprocess
import sys
import re
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
import threading
import struct
from typing import Optional, Callable

# Try to import pymem, fall back to manual implementation
PYMEM_AVAILABLE = False
try:
    import pymem
    import pymem.process
    import pymem.pattern
    PYMEM_AVAILABLE = True
except ImportError:
    print("pymem not installed. Run: pip install pymem")
    pass

# ============================================================
# Memory Manager using pymem (preferred) or ctypes fallback
# ============================================================

class MemoryManager:
    """Manages reading/writing to game process memory using pymem."""

    # Known Elden Ring process names
    ELDEN_RING_PROCESSES = [
        "eldenring.exe",
        "start_protected_game.exe",
    ]

    def __init__(self):
        self.pm = None  # pymem instance
        self.module_base = 0
        self.module_size = 0
        self.process_id = 0
        self.process_name = ""
        self.last_error = ""

    @property
    def is_attached(self):
        return self.pm is not None

    def attach(self, process_name: str = None) -> bool:
        """Attach to Elden Ring process using pymem."""
        if not PYMEM_AVAILABLE:
            self.last_error = "pymem not installed. Run: pip install pymem"
            return False

        # Detach first if already attached
        if self.is_attached:
            self.detach()

        # Build list of process names to try
        if process_name:
            process_names = [process_name] + self.ELDEN_RING_PROCESSES
        else:
            process_names = self.ELDEN_RING_PROCESSES

        # Try each process name
        for name in process_names:
            try:
                self.pm = pymem.Pymem(name)
                self.process_name = name
                self.process_id = self.pm.process_id

                # Get module info
                module = pymem.process.module_from_name(self.pm.process_handle, name)
                if module:
                    self.module_base = module.lpBaseOfDll
                    self.module_size = module.SizeOfImage
                    print(f"Attached to {name} (PID: {self.process_id})")
                    print(f"Module base: 0x{self.module_base:X}, size: {self.module_size} ({self.module_size / (1024*1024):.1f} MB)")
                    return True
                else:
                    self.last_error = f"Could not get module info for {name}"
            except pymem.exception.ProcessNotFound:
                continue
            except pymem.exception.CouldNotOpenProcess as e:
                self.last_error = f"Could not open process (run as Administrator): {e}"
                return False
            except Exception as e:
                self.last_error = f"Error attaching to {name}: {e}"
                continue

        self.last_error = f"Process not found. Tried: {', '.join(process_names)}"
        return False

    def detach(self) -> None:
        """Detach from the process."""
        if self.pm:
            try:
                self.pm.close_process()
            except Exception:
                pass  # Process may already be closed
            finally:
                self.pm = None
        self.module_base = 0
        self.module_size = 0
        self.process_id = 0
        self.process_name = ""

    def read_bytes(self, address: int, size: int) -> bytes:
        """Read bytes from process memory."""
        if not self.pm:
            return b''
        try:
            return self.pm.read_bytes(address, size)
        except Exception as e:
            self.last_error = f"Read failed at 0x{address:X}: {e}"
            return b''

    def read_int64(self, address: int) -> int:
        """Read 64-bit integer from address."""
        if not self.pm:
            return 0
        try:
            return self.pm.read_longlong(address)
        except Exception:
            return 0

    def read_int32(self, address: int) -> int:
        """Read 32-bit integer from address."""
        if not self.pm:
            return 0
        try:
            return self.pm.read_int(address)
        except Exception:
            return 0

    def read_byte(self, address: int) -> int:
        """Read single byte from address."""
        if not self.pm:
            return 0
        try:
            return self.pm.read_uchar(address)
        except Exception:
            return 0

    def write_bytes(self, address: int, data: bytes) -> bool:
        """Write bytes to process memory."""
        if not self.pm:
            return False
        try:
            self.pm.write_bytes(address, data, len(data))
            return True
        except Exception as e:
            self.last_error = f"Write failed at 0x{address:X}: {e}"
            return False

    def write_byte(self, address: int, value: int) -> bool:
        """Write single byte to address."""
        if not self.pm:
            return False
        try:
            self.pm.write_uchar(address, value & 0xFF)
            return True
        except Exception as e:
            self.last_error = f"Write failed at 0x{address:X}: {e}"
            return False

    def scan_aob(self, pattern: str, rel_offset: int, additional: int) -> int:
        """Scan for AOB pattern and resolve pointer using pymem."""
        if not self.pm or self.module_size == 0:
            self.last_error = "Cannot scan: not attached or module size is 0"
            return 0

        try:
            # Convert pattern to pymem format (replace ?? with ..)
            pymem_pattern = pattern.replace('??', '..')

            # Use pymem's pattern scan
            result = pymem.pattern.pattern_scan_module(
                self.pm.process_handle,
                pymem.process.module_from_name(self.pm.process_handle, self.process_name),
                pymem_pattern
            )

            if result:
                # Read relative offset and calculate absolute address
                relative_offset = self.pm.read_int(result + rel_offset)
                absolute_address = result + additional + relative_offset
                print(f"Pattern found at 0x{result:X}, resolved to 0x{absolute_address:X}")
                return absolute_address
            else:
                self.last_error = "Pattern not found in module memory"
                return 0

        except Exception as e:
            self.last_error = f"Pattern scan failed: {e}"
            # Fallback to manual scan
            return self._manual_scan_aob(pattern, rel_offset, additional)

    def _manual_scan_aob(self, pattern: str, rel_offset: int, additional: int) -> int:
        """Manual AOB scan fallback with chunked reading for performance."""
        try:
            # Parse pattern
            parts = pattern.split(' ')
            pattern_bytes: list[int] = []
            mask: list[bool] = []

            for part in parts:
                if part == '??':
                    pattern_bytes.append(0)
                    mask.append(False)
                else:
                    pattern_bytes.append(int(part, 16))
                    mask.append(True)

            pattern_len = len(pattern_bytes)

            # Chunked scanning - read 4MB at a time with overlap for pattern matching
            CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks
            overlap = pattern_len - 1  # Overlap to catch patterns at chunk boundaries

            offset = 0
            while offset < self.module_size:
                # Calculate chunk size (may be smaller for last chunk)
                remaining = self.module_size - offset
                current_chunk_size = min(CHUNK_SIZE, remaining)

                # Read chunk
                chunk_bytes = self.read_bytes(self.module_base + offset, current_chunk_size)

                if not chunk_bytes or len(chunk_bytes) < pattern_len:
                    offset += current_chunk_size - overlap
                    continue

                # Search for pattern in this chunk
                search_end = len(chunk_bytes) - pattern_len + 1
                for i in range(search_end):
                    found = True
                    for j in range(pattern_len):
                        if mask[j] and chunk_bytes[i + j] != pattern_bytes[j]:
                            found = False
                            break

                    if found:
                        pattern_address = self.module_base + offset + i
                        relative_offset_bytes = chunk_bytes[i + rel_offset:i + rel_offset + 4]
                        relative_offset_val = struct.unpack('<i', relative_offset_bytes)[0]
                        absolute_address = pattern_address + additional + relative_offset_val
                        print(f"[Manual/Chunked] Pattern found at 0x{pattern_address:X}, resolved to 0x{absolute_address:X}")
                        return absolute_address

                # Move to next chunk with overlap
                offset += current_chunk_size - overlap

            self.last_error = "Pattern not found in module memory"
            return 0
        except Exception as e:
            self.last_error = f"Manual scan failed: {e}"
            return 0


# ============================================================
# Elden Ring Specifics
# ============================================================

# AOB Patterns for finding pointers (from Hexinton CE table)
EVENT_FLAG_MAN_AOB = "48 8B 3D ?? ?? ?? ?? 48 85 FF ?? ?? 32 C0 E9"

# Grace data organized by region: region -> {name: (offset, bit)}
# Offsets from Hexinton CE table - pointer chain: [[EventFlagMan]+0x28]+offset
GRACES_BY_REGION = {
    # ============ MAIN GAME ============
    "Roundtable Hold": {
        "Table of Lost Grace": (0xA58, 1),
    },
    "Stranded Graveyard": {
        "Cave of Knowledge": (0xAA5, 7),
        "Stranded Graveyard": (0xAA5, 6),
    },
    "Limgrave": {
        "The First Step": (0xCBE, 2),
        "Church of Elleh": (0xCBE, 3),
        "Artist's Shack (Limgrave)": (0xCBE, 0),
        "Gatefront": (0xCBF, 0),
        "Agheel Lake South": (0xCBF, 5),
        "Agheel Lake North": (0xCBF, 3),
        "Church of Dragon Communion": (0xCBF, 1),
        "Fort Haight West": (0xCBF, 6),
        "Third Church of Marika": (0xCBF, 7),
        "Seaside Ruins": (0xCC0, 6),
        "Mistwood Outskirts": (0xCC0, 5),
        "Murkwater Coast": (0xCC0, 3),
        "Summonwater Village Outskirts": (0xCC0, 0),
        "Waypoint Ruins Cellar": (0xCC1, 7),
        "Stormfoot Catacombs": (0xB3B, 5),
        "Murkwater Catacombs": (0xB3B, 3),
        "Groveside Cave": (0xB47, 0),
        "Coastal Cave": (0xB49, 4),
        "Murkwater Cave": (0xB47, 3),
        "Highroad Cave": (0xB49, 2),
        "Limgrave Tunnels": (0xB54, 6),
    },
    "Stormhill": {
        "Stormhill Shack": (0xCBE, 1),
        "Castleward Tunnel": (0xA41, 5),
        "Margit, the Fell Omen": (0xA41, 6),
        "Warmaster's Shack": (0xCC0, 1),
        "Saintsbridge": (0xCC0, 2),
        "Deathtouched Catacombs": (0xB3C, 4),
        "Limgrave Tower Bridge": (0xB6E, 5),
        "Divine Tower of Limgrave": (0xB6E, 3),
    },
    "Stormveil Castle": {
        "Stormveil Main Gate": (0xA42, 7),
        "Gateside Chamber": (0xA41, 4),
        "Stormveil Cliffside": (0xA41, 3),
        "Rampart Tower": (0xA41, 2),
        "Liftside Chamber": (0xA41, 1),
        "Secluded Cell": (0xA41, 0),
        "Godrick the Grafted": (0xA41, 7),
    },
    "Weeping Peninsula": {
        "Church of Pilgrimage": (0xCC4, 1),
        "Castle Morne Rampart": (0xCC4, 0),
        "Tombsward": (0xCC5, 7),
        "South of the Lookout Tower": (0xCC5, 6),
        "Ailing Village Outskirts": (0xCC5, 5),
        "Beside the Crater-Pocked Glade": (0xCC5, 4),
        "Isolated Merchant's Shack (Limgrave)": (0xCC5, 3),
        "Fourth Church of Marika": (0xCC6, 5),
        "Bridge of Sacrifice": (0xCC5, 2),
        "Castle Morne Lift": (0xCC5, 1),
        "Behind the Castle": (0xCC5, 0),
        "Beside the Rampart Gaol": (0xCC6, 7),
        "Morne Moangrave": (0xCC6, 6),
        "Impaler's Catacombs": (0xB3B, 6),
        "Tombsward Catacombs": (0xB3B, 7),
        "Earthbore Cave": (0xB47, 2),
        "Tombsward Cave": (0xB47, 1),
        "Morne Tunnel": (0xB54, 7),
    },
    "Liurnia of the Lakes": {
        "Lake-Facing Cliffs": (0xCCB, 7),
        "Laskyar Ruins": (0xCCB, 5),
        "Liurnia Lake Shore": (0xCCB, 6),
        "Academy Gate Town": (0xCCB, 3),
        "Artist's Shack (Liurnia)": (0xCCD, 6),
        "Eastern Liurnia Lake Shore": (0xCCD, 0),
        "Gate Town Bridge": (0xCCD, 1),
        "Liurnia Highway North": (0xCCD, 2),
        "Liurnia Highway South": (0xCD0, 3),
        "Main Academy Gate": (0xCCB, 1),
        "Scenic Isle": (0xCCB, 4),
        "South Raya Lucaria Gate": (0xCCB, 2),
        "Ruined Labyrinth": (0xCCE, 6),
        "Boilprawn Shack": (0xCCD, 7),
        "Church of Vows": (0xCCE, 7),
        "Converted Tower": (0xCCF, 2),
        "Eastern Tableland": (0xCCF, 5),
        "Fallen Ruins of the Lake": (0xCCF, 3),
        "Folly on the Lake": (0xCCD, 4),
        "Jarburg": (0xCD0, 2),
        "Mausoleum Compound": (0xCCE, 5),
        "Ranni's Chamber": (0xCD0, 0),
        "Revenger's Shack": (0xCCD, 5),
        "Slumbering Wolf's Shack": (0xCCC, 0),
        "Village of the Albinaurics": (0xCCD, 3),
        "Crystalline Woods": (0xCD0, 4),
        "East Gate Bridge Trestle": (0xCD0, 5),
        "Foot of the Four Belfries": (0xCCC, 5),
        "Gate Town North": (0xCCF, 6),
        "Main Caria Manor Gate": (0xCCC, 1),
        "Manor Lower Level": (0xCCE, 0),
        "Manor Upper Level": (0xCCE, 1),
        "Northern Liurnia Lake Shore": (0xCCC, 3),
        "Road to the Manor": (0xCCC, 2),
        "Royal Moongazing Grounds": (0xCCF, 7),
        "Sorcerer's Isle": (0xCCC, 4),
        "Temple Quarter": (0xCD0, 6),
        "The Four Belfries": (0xCCE, 4),
        "Academy Crystal Cave": (0xB48, 5),
        "Behind Caria Manor": (0xCCF, 1),
        "Black Knife Catacombs": (0xB3B, 2),
        "Cliffbottom Catacombs": (0xB3B, 1),
        "Lakeside Crystal Cave": (0xB48, 6),
        "Liurnia Tower Bridge": (0xB6F, 2),
        "Ranni's Rise": (0xCCE, 3),
        "Ravine-Veiled Village": (0xCCE, 2),
        "Raya Lucaria Crystal Tunnel": (0xB54, 5),
        "Road's End Catacombs": (0xB3B, 4),
        "Stillwater Cave": (0xB48, 7),
        "Study Hall Entrance": (0xB6F, 3),
        "The Ravine": (0xCCF, 4),
        "Divine Tower of Liurnia": (0xB6F, 1),
    },
    "Bellum Highway": {
        "Bellum Church": (0xCCC, 7),
        "Church of Inhibition": (0xCD0, 7),
        "East Raya Lucaria Gate": (0xCCB, 0),
        "Frenzied Flame Village Outskirts": (0xCCF, 0),
        "Grand Lift of Dectus": (0xCCC, 6),
    },
    "Ruin-Strewn Precipice": {
        "Magma Wyrm": (0xBAB, 3),
        "Ruin-Strewn Precipice": (0xBAB, 2),
        "Ruin-Strewn Precipice Overlook": (0xBAB, 1),
    },
    "Moonlight Altar": {
        "Altar South": (0xCD1, 3),
        "Cathedral of Manus Celes": (0xCD1, 4),
        "Moonlight Altar": (0xCD1, 5),
    },
    "Raya Lucaria Academy": {
        "Church of the Cuckoo": (0xA73, 5),
        "Debate Parlor": (0xA73, 6),
        "Raya Lucaria Grand Library": (0xA73, 7),
        "Schoolhouse Classroom": (0xA73, 4),
    },
    "Altus Plateau": {
        "Abandoned Coffin": (0xCD7, 3),
        "Altus Highway Junction": (0xCD7, 0),
        "Altus Plateau": (0xCD7, 2),
        "Bower of Bounty": (0xCD8, 5),
        "Erdtree-Gazing Hill": (0xCD7, 1),
        "Forest-Spanning Greatbridge": (0xCD8, 7),
        "Rampartside Path": (0xCD8, 6),
        "Road of Iniquity Side Path": (0xCD8, 4),
        "Shaded Castle Inner Gate": (0xCDA, 6),
        "Shaded Castle Ramparts": (0xCDA, 7),
        "Windmill Heights": (0xCD9, 6),
        "Windmill Village": (0xCD8, 3),
        "Altus Tunnel": (0xB54, 2),
        "Castellan's Hall": (0xCDA, 5),
        "Old Altus Tunnel": (0xB54, 3),
        "Perfumer's Grotto": (0xB49, 1),
        "Sage's Cave": (0xB49, 0),
        "Sainted Hero's Grave": (0xB3C, 7),
        "Unsightly Catacombs": (0xB3C, 3),
    },
    "Mt. Gelmir": {
        "Bridge of Iniquity": (0xCDD, 1),
        "Craftsman's Shack": (0xCDE, 3),
        "First Mt. Gelmir Campsite": (0xCDD, 0),
        "Gelmir Hero's Grave": (0xB3C, 6),
        "Ninth Mt. Gelmir Campsite": (0xCDE, 7),
        "Primeval Sorcerer Azur": (0xCDE, 2),
        "Road of Iniquity": (0xCDE, 6),
        "Seethewater Cave": (0xB48, 4),
        "Seethewater River": (0xCDE, 5),
        "Seethewater Terminus": (0xCDE, 4),
        "Volcano Cave": (0xB48, 2),
        "Wyndham Catacombs": (0xB3B, 0),
    },
    "Leyndell Royal Capital (Pre-Ash)": {
        "Auriza Side Tomb": (0xB3C, 2),
        "Auriza Hero's Grave": (0xB3C, 5),
        "Capital Rampart": (0xCD9, 5),
        "Divine Tower of West Altus": (0xB70, 1),
        "Divine Tower of West Altus: Gate": (0xB71, 7),
        "Hermit Merchant's Shack": (0xCD8, 0),
        "Minor Erdtree Church": (0xCD8, 1),
        "Outer Wall Battleground": (0xCD9, 7),
        "Outer Wall Phantom Tree": (0xCD8, 2),
        "Sealed Tunnel": (0xB70, 0),
    },
    "Volcano Manor": {
        "Abductor Virgin": (0xA8C, 1),
        "Audience Pathway": (0xA8C, 2),
        "Guest Hall": (0xA8C, 3),
        "Prison Town Church": (0xA8C, 4),
        "Rykard, Lord of Blasphemy": (0xA8C, 7),
        "Subterranean Inquisition Chamber": (0xA8C, 0),
        "Temple of Eiglay": (0xA8C, 6),
        "Volcano Manor": (0xA8C, 5),
    },
    "Leyndell Royal Capital": {
        "East Capital Rampart": (0xA4D, 1),
        "Avenue Balcony": (0xA4E, 7),
        "Lower Capital Church": (0xA4D, 0),
        "West Capital Rampart": (0xA4E, 6),
        "Fortified Manor, First Floor": (0xA4E, 3),
        "Divine Bridge": (0xA4E, 2),
        "Erdtree Sanctuary": (0xA4E, 6),
        "Elden Throne": (0xA4D, 3),
        "Queen's Bedchamber": (0xA4E, 4),
    },
    "Caelid": {
        "Caelem Ruins": (0xCE4, 4),
        "Caelid Highway South": (0xCE4, 2),
        "Cathedral of Dragon Communion": (0xCE4, 3),
        "Chair-Crypt of Sellia": (0xCE5, 0),
        "Church of the Plague": (0xCE6, 5),
        "Fort Gael North": (0xCE4, 5),
        "Rotview Balcony": (0xCE4, 6),
        "Sellia Backstreets": (0xCE5, 1),
        "Sellia Under-Stair": (0xCE6, 7),
        "Smoldering Church": (0xCE4, 7),
        "Smoldering Wall": (0xCE5, 6),
        "Southern Aeonia Swamp Bank": (0xCE5, 4),
        "Abandoned Cave": (0xB4A, 7),
        "Caelid Catacombs": (0xB3C, 0),
        "Chamber Outside the Plaza": (0xCE6, 3),
        "Deep Siofra Well": (0xCE5, 5),
        "Gael Tunnel": (0xB54, 0),
        "Gaol Cave": (0xB4A, 6),
        "Impassable Greatbridge": (0xCE6, 6),
        "Minor Erdtree Catacombs": (0xB3C, 1),
        "Rear Gael Tunnel Entrance": (0xB5B, 6),
        "Redmane Castle Plaza": (0xCE6, 4),
        "Sellia Crystal Tunnel": (0xB55, 7),
        "Starscourge Radahn": (0xCE6, 1),
        "War-Dead Catacombs": (0xB3D, 7),
    },
    "Swamp of Aeonia": {
        "Aeonia Swamp Shore": (0xCE4, 1),
        "Astray from Caelid Highway North": (0xCE4, 0),
        "Heart of Aeonia": (0xCE5, 3),
        "Inner Aeonia": (0xCE5, 2),
    },
    "Greyoll's Dragonbarrow": {
        "Bestial Sanctum": (0xCEA, 1),
        "Divine Tower of Caelid: Basement": (0xB72, 7),
        "Divine Tower of Caelid: Center": (0xB72, 6),
        "Dragonbarrow Cave": (0xB48, 1),
        "Dragonbarrow Fork": (0xCEA, 3),
        "Dragonbarrow West": (0xCEA, 5),
        "Farum Greatbridge": (0xCEB, 7),
        "Fort Faroth": (0xCEA, 2),
        "Isolated Divine Tower": (0xB74, 3),
        "Isolated Merchant's Shack (Dragonbarrow)": (0xCEA, 4),
        "Lenne's Rise": (0xCEA, 0),
        "Sellia Hideaway": (0xB48, 0),
    },
    "Forbidden Lands": {
        "Divine Tower of East Altus": (0xB73, 4),
        "Divine Tower of East Altus: Gate": (0xB73, 5),
        "Forbidden Lands": (0xCF0, 3),
        "Grand Lift of Rold": (0xCF0, 1),
        "Hidden Path to the Haligtree": (0xB3D, 3),
    },
    "Mountaintops of the Giants": {
        "Ancient Snow Valley Ruins": (0xCF0, 0),
        "Castle Sol Main Gate": (0xCF3, 5),
        "Castle Sol Rooftop": (0xCF3, 3),
        "Church of the Eclipse": (0xCF3, 4),
        "First Church of Marika": (0xCF1, 6),
        "Freezing Lake": (0xCF1, 7),
        "Snow Valley Ruins Overlook": (0xCF3, 6),
        "Spiritcaller's Cave": (0xB4A, 5),
        "Whiteridge Road": (0xCF3, 7),
        "Zamor Ruins": (0xCF0, 2),
    },
    "Flame Peak": {
        "Church of Repose": (0xCF1, 4),
        "Fire Giant": (0xCF1, 2),
        "Foot of the Forge": (0xCF1, 3),
        "Forge of the Giants": (0xCF1, 1),
        "Giant's Gravepost": (0xCF1, 5),
        "Giant's Mountaintop Catacombs": (0xB3D, 5),
        "Giant-Conquering Hero's Grave": (0xB3D, 6),
    },
    "Consecrated Snowfield": {
        "Apostate Derelict": (0xD03, 2),
        "Cave of the Forlorn": (0xB49, 7),
        "Consecrated Snowfield": (0xCF6, 1),
        "Consecrated Snowfield Catacombs": (0xB3D, 4),
        "Inner Consecrated Snowfield": (0xCF6, 0),
        "Ordina, Liturgical Town": (0xD03, 3),
        "Yelough Anix Tunnel": (0xB55, 4),
    },
    "Miquella's Haligtree": {
        "Haligtree Canopy": (0xA80, 5),
        "Haligtree Promenade": (0xA80, 6),
        "Haligtree Town": (0xA80, 4),
        "Haligtree Town Plaza": (0xA80, 3),
    },
    "Elphael, Brace of the Haligtree": {
        "Drainage Channel": (0xA7F, 0),
        "Elphael Inner Wall": (0xA7F, 1),
        "Haligtree Roots": (0xA80, 7),
        "Malenia, Goddess of Rot": (0xA7F, 3),
        "Prayer Room": (0xA7F, 2),
    },
    "Crumbling Farum Azula": {
        "Beside the Great Bridge": (0xA67, 1),
        "Crumbling Beast Grave": (0xA66, 0),
        "Crumbling Beast Grave Depths": (0xA67, 7),
        "Dragon Temple": (0xA67, 5),
        "Dragon Temple Altar": (0xA66, 1),
        "Dragon Temple Lift": (0xA67, 3),
        "Dragon Temple Rooftop": (0xA67, 2),
        "Dragon Temple Transept": (0xA67, 4),
        "Dragonlord Placidusax": (0xA66, 2),
        "Maliketh, the Black Blade": (0xA66, 3),
        "Tempest-Facing Balcony": (0xA67, 6),
    },
    "Ainsel River": {
        "Ainsel River Downstream": (0xA5B, 2),
        "Ainsel River Sluice Gate": (0xA5B, 3),
        "Ainsel River Well Depths": (0xA5B, 4),
        "Astel, Naturalborn of the Void": (0xA5F, 7),
        "Dragonkin Soldier of Nokstella": (0xA5B, 5),
    },
    "Ainsel River Main": {
        "Ainsel River Main": (0xA5B, 1),
        "Nokstella, Eternal City": (0xA5B, 0),
        "Nokstella Waterfall Basin": (0xA5C, 4),
    },
    "Lake of Rot": {
        "Grand Cloister": (0xA5C, 5),
        "Lake of Rot Shoreside": (0xA5C, 7),
    },
    "Nokron, Eternal City": {
        "Nokron, Eternal City": (0xA62, 0),
        "Ancestral Woods": (0xA5D, 7),
        "Aqueduct-Facing Cliffs": (0xA5D, 6),
        "Great Waterfall Basin": (0xA5C, 3),
        "Mimic Tear": (0xA5C, 2),
        "Night's Sacred Ground": (0xA5D, 5),
    },
    "Siofra River": {
        "Below the Well": (0xA5D, 4),
        "Siofra River Bank": (0xA5C, 1),
        "Siofra River Well Depths": (0xA62, 1),
        "Worshippers' Woods": (0xA5C, 0),
    },
    "Mohgwyn Palace": {
        "Cocoon of the Empyrean": (0xA60, 5),
        "Dynasty Mausoleum Entrance": (0xA60, 3),
        "Dynasty Mausoleum Midpoint": (0xA60, 2),
        "Palace Approach Ledge-Road": (0xA60, 4),
    },
    "Deeproot Depths": {
        "Across the Roots": (0xA5E, 4),
        "Deeproot Depths": (0xA5E, 6),
        "Great Waterfall Crest": (0xA5E, 7),
        "Prince of Death's Throne": (0xA5D, 1),
        "Root-Facing Cliffs": (0xA5D, 0),
        "The Nameless Eternal City": (0xA5E, 5),
    },
    # ============ SUBTERRANEAN SHUNNING-GROUNDS ============
    "Subterranean Shunning-Grounds": {
        "Cathedral of the Forsaken": (0xB79, 3),
        "Forsaken Depths": (0xB79, 1),
        "Frenzied Flame Proscription": (0xB7A, 7),
        "Leyndell Catacombs": (0xB79, 0),
        "Underground Roadside": (0xB79, 2),
    },
    # ============ LEYNDELL ASHEN CAPITAL ============
    "Leyndell, Ashen Capital": {
        "Divine Bridge (Ash)": (0xA50, 2),
        "East Capital Rampart (Ash)": (0xA50, 5),
        "Elden Throne (Ash)": (0xA50, 7),
        "Erdtree Sanctuary (Ash)": (0xA50, 6),
        "Leyndell, Capital of Ash": (0xA50, 4),
        "Queen's Bedchamber (Ash)": (0xA50, 3),
    },
    "Stone Platform": {
        "Fractured Marika": (0xAB1, 3),
    },
    # ============ DLC - GRAVESITE PLAIN ============
    "Gravesite Plain": {
        "Gravesite Plain": (0xD16, 7),
        "Scorched Ruins": (0xD16, 6),
        "Three-Path Cross": (0xD16, 5),
        "Greatbridge, North": (0xD16, 2),
        "Main Gate Cross": (0xD16, 4),
        "Cliffroad Terminus": (0xD16, 3),
        "Castle Front": (0xD17, 2),
        "Pillar Path Cross": (0xD17, 5),
        "Pillar Path Waypoint": (0xD17, 4),
        "Ellac River Cave": (0xD17, 3),
        "Ellac River Downstream": (0xD19, 1),
        "Fog Rift Catacombs": (0xBB8, 7),
        "Belurat Gaol": (0xBC4, 3),
        "Ruined Forge Lava Intake": (0xBD1, 7),
        "Dragon's Pit": (0xBDD, 2),
        "Dragon's Pit Terminus": (0xBE3, 0),
    },
    "Castle Ensis": {
        "Castle Ensis Checkpoint": (0xD18, 2),
        "Castle Lord's Chamber": (0xD18, 1),
        "Ensis Moongazing Grounds": (0xD18, 0),
    },
    "Cerulean Coast": {
        "Cerulean Coast": (0xD19, 0),
        "Cerulean Coast West": (0xD1A, 7),
        "Cerulean Coast Cross": (0xD1A, 4),
        "The Fissure": (0xD1A, 6),
        "Finger Ruins of Rhia": (0xD1A, 5),
    },
    "Charo's Hidden Grave": {
        "Charo's Hidden Grave": (0xD1B, 6),
        "Lamenter's Gaol": (0xBC4, 1),
    },
    "Stone Coffin Fissure": {
        "Stone Coffin Fissure": (0xAD7, 6),
        "Fissure Cross": (0xAD7, 5),
        "Fissure Waypoint": (0xAD7, 4),
        "Fissure Depths": (0xAD7, 3),
        "Garden of Deep Purple": (0xAD7, 7),
    },
    "Foot of the Jagged Peak": {
        "Grand Altar of Dragon Communion": (0xD1B, 7),
        "Foot of the Jagged Peak": (0xD1C, 5),
    },
    "Jagged Peak": {
        "Jagged Peak Mountainside": (0xD1C, 4),
        "Jagged Peak Summit": (0xD1C, 3),
        "Rest of the Dead Dragon": (0xD1C, 2),
    },
    # ============ DLC - BELURAT / ENIR-ILIM ============
    "Belurat, Tower Settlement": {
        "Belurat, Tower Settlement": (0xABE, 6),
        "Small Private Altar": (0xABE, 5),
        "Stagefront": (0xABE, 4),
        "Theatre of the Divine Beast": (0xABE, 7),
    },
    "Enir-Ilim": {
        "Enir-Ilim Outer Wall": (0xABF, 3),
        "First Rise": (0xABF, 2),
        "Spiral Rise": (0xABF, 1),
        "Cleansing Chamber Anteroom": (0xABF, 0),
        "Divine Gate Front Staircase": (0xAC0, 7),
        "Gate of Divinity": (0xABF, 5),
    },
    "Ancient Ruins of Rauh": {
        "Viaduct Minor Tower": (0xD27, 3),
        "Rauh Ancient Ruins, East": (0xD27, 2),
        "Rauh Ancient Ruins, West": (0xD27, 1),
        "Ancient Ruins Grand Stairway": (0xD28, 7),
        "Church of the Bud, Main Entrance": (0xD27, 0),
        "Church of the Bud": (0xD28, 6),
        "Rivermouth Cave": (0xBDD, 3),
    },
    "Rauh Base": {
        "Ancient Ruins Base": (0xD24, 7),
        "Temple Town Ruins": (0xD24, 6),
        "Ravine North": (0xD24, 5),
        "Scorpion River Catacombs": (0xBB8, 6),
        "Taylew's Ruined Forge": (0xBD1, 4),
    },
    # ============ DLC - SCADU ALTUS ============
    "Scadu Altus": {
        "Highroad Cross": (0xD22, 3),
        "Scadu Altus, West": (0xD23, 4),
        "Moorth Ruins": (0xD22, 1),
        "Moorth Highway, South": (0xD23, 3),
        "Fort of Reprimand": (0xD23, 2),
        "Behind the Fort of Reprimand": (0xD23, 1),
        "Scaduview Cross": (0xD23, 0),
        "Bonny Village": (0xD22, 0),
        "Bridge Leading to the Village": (0xD23, 7),
        "Church District Highroad": (0xD23, 6),
        "Cathedral of Manus Metyr": (0xD23, 5),
        "Finger Birthing Grounds": (0xAFC, 3),
        "Castle Watering Hole": (0xD24, 3),
        "Recluses' River Upstream": (0xD24, 2),
        "Recluses' River Downstream": (0xD24, 1),
        "Darklight Catacombs": (0xBB8, 5),
        "Bonny Gaol": (0xBC4, 2),
        "Ruined Forge of Starfall Past": (0xBD1, 5),
    },
    "Abyssal Woods": {
        "Abyssal Woods": (0xD1D, 3),
        "Forsaken Graveyard": (0xD1D, 1),
        "Woodland Trail": (0xD1D, 0),
        "Church Ruins": (0xD1E, 7),
        "Divided Falls": (0xD1D, 2),
    },
    "Midra's Manse": {
        "Manse Hall": (0xB22, 6),
        "Midra's Library": (0xB22, 5),
        "Second Floor Chamber": (0xB22, 4),
        "Discussion Chamber": (0xB22, 7),
    },
    # ============ DLC - SHADOW KEEP ============
    "Shadow Keep": {
        "Shadow Keep Main Gate": (0xACA, 1),
        "Main Gate Plaza": (0xACA, 2),
    },
    "Shadow Keep, Church District": {
        "Church District Entrance": (0xACB, 5),
        "Sunken Chapel": (0xACB, 4),
        "Tree-Worship Passage": (0xACB, 3),
        "Tree-Worship Sanctum": (0xACB, 2),
    },
    "Specimen Storehouse": {
        "Storehouse, First Floor": (0xACB, 0),
        "Storehouse, Fourth Floor": (0xACC, 7),
        "Storehouse, Seventh Floor": (0xACC, 6),
        "Dark Chamber Entrance": (0xACC, 5),
        "Storehouse, Back Section": (0xACC, 3),
        "Storehouse, Loft": (0xACC, 2),
        "West Rampart": (0xACD, 7),
        "Messmer's Dark Chamber": (0xACB, 1),
    },
    "Scaduview": {
        "Scaduview": (0xD26, 5),
        "Shadow Keep, Back Gate": (0xD26, 4),
        "Scadutree Base": (0xD2A, 7),
        "Hinterland": (0xD26, 0),
        "Hinterland Bridge": (0xD27, 6),
        "Fingerstone Hill": (0xD27, 7),
    },
}

# Flatten for compatibility
KNOWN_GRACES: dict[str, tuple[int, int]] = {}
for region, graces in GRACES_BY_REGION.items():
    KNOWN_GRACES.update(graces)

# Grace presets for quick selection
GRACE_PRESETS: dict[str, list[str]] = {
    "Bingo": [
        "Scenic Isle",
        "Ruined Labyrinth",
        "Altus Highway Junction",
        "Road of Iniquity",
        "Inner Consecrated Snowfield",
        "Snow Valley Ruins Overlook",
        "Haligtree Roots",
    ],
    "Early Game": [
        "The First Step",
        "Church of Elleh",
        "Gatefront",
        "Stormhill Shack",
        "Agheel Lake South",
        "Agheel Lake North",
        "Waypoint Ruins Cellar",
    ],
    "All Roundtables": [
        "Table of Lost Grace",
    ],
    "Divine Towers": [
        "Divine Tower of Limgrave",
        "Divine Tower of Liurnia",
        "Divine Tower of West Altus",
        "Divine Tower of East Altus",
        "Divine Tower of Caelid: Center",
        "Isolated Divine Tower",
    ],
    "Legacy Dungeons": [
        "Stormveil Main Gate",
        "Godrick the Grafted",
        "Raya Lucaria Grand Library",
        "Volcano Manor",
        "Rykard, Lord of Blasphemy",
        "East Capital Rampart",
        "Elden Throne",
        "Haligtree Roots",
        "Malenia, Goddess of Rot",
        "Crumbling Beast Grave",
        "Maliketh, the Black Blade",
    ],
}


# ============================================================
# Core Stat Randomizer Logic
# ============================================================

PLAYER_CLASSES = {
    3000: 'Vagabond', 3001: 'Warrior', 3002: 'Hero', 3003: 'Bandit',
    3004: 'Astrologer', 3005: 'Prisoner', 3006: 'Confessor',
    3007: 'Samurai', 3008: 'Prophet', 3009: 'Wretch',
}

STAT_FIELDS = {
    'level': 'soulLv', 'vigor': 'baseVit', 'mind': 'baseWil',
    'endurance': 'baseEnd', 'strength': 'baseStr', 'dexterity': 'baseDex',
    'intelligence': 'baseMag', 'faith': 'baseFai', 'arcane': 'baseLuc',
}

TARGET_LEVEL = 9
TOTAL_STATS = 88
MIN_STAT = 6
NUM_STATS = 8

DEFAULT_REGULATION_PATH = r"D:\ER MODS\ModEngine-2.1.0.0-win64\randomizer\regulation.bin"
SCRIPT_DIR = Path(__file__).parent
OUTPUT_DIR = SCRIPT_DIR / "output"
CONFIG_FILE = SCRIPT_DIR / "config.json"


def load_config() -> dict:
    """Load saved configuration."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, ValueError):
            pass  # Return empty config if file is corrupted
    return {}


def save_config(config: dict) -> None:
    """Save configuration to file."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
    except (OSError, TypeError) as e:
        print(f"Warning: Could not save config: {e}")


def find_witchybnd() -> Optional[str]:
    """Find WitchyBND executable."""
    search_paths = [
        SCRIPT_DIR / "WitchyBND.exe",
        SCRIPT_DIR / "WitchyBND" / "WitchyBND.exe",
        SCRIPT_DIR / "WitchyBND-v2.16.0.5" / "WitchyBND.exe",
        SCRIPT_DIR / "tools" / "WitchyBND.exe",
    ]
    for path in search_paths:
        if path.exists():
            return str(path)
    return shutil.which("WitchyBND")


def randomize_stats(seed: int) -> dict[int, dict]:
    """Generate randomized stats for all classes using seed."""
    random.seed(seed)
    remaining_pool = TOTAL_STATS - (MIN_STAT * NUM_STATS)
    all_stats: dict[int, dict] = {}
    for row_id, class_name in PLAYER_CLASSES.items():
        stats: dict[str, int] = {k: MIN_STAT for k in ['vigor', 'mind', 'endurance', 'strength',
                                        'dexterity', 'intelligence', 'faith', 'arcane']}
        for _ in range(remaining_pool):
            stats[random.choice(list(stats.keys()))] += 1
        stats['level'] = TARGET_LEVEL
        all_stats[row_id] = {'name': class_name, 'stats': stats}
    return all_stats


def format_stats_text(all_stats: dict[int, dict]) -> str:
    """Format stats as text for display."""
    lines = ["-" * 65]
    for row_id in sorted(all_stats.keys()):
        d = all_stats[row_id]
        s = d['stats']
        total = sum(v for k, v in s.items() if k != 'level')
        lines.append(f"[{d['name']:10s}] Lv:{s['level']:2d} | "
                    f"Vig:{s['vigor']:2d} Min:{s['mind']:2d} End:{s['endurance']:2d} "
                    f"Str:{s['strength']:2d} Dex:{s['dexterity']:2d} Int:{s['intelligence']:2d} "
                    f"Fai:{s['faith']:2d} Arc:{s['arcane']:2d} | Total: {total}")
    lines.append("-" * 65)
    return "\n".join(lines)


def export_csv(all_stats: dict[int, dict], seed: int, output_dir: Path) -> Path:
    """Export stats to CSV file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"modified_{seed}.csv"
    with open(csv_path, 'w') as f:
        f.write("Row ID,Class,Level,Vigor,Mind,Endurance,Strength,Dexterity,Intelligence,Faith,Arcane\n")
        for row_id in sorted(all_stats.keys()):
            d = all_stats[row_id]
            s = d['stats']
            f.write(f"{row_id},{d['name']},{s['level']},{s['vigor']},{s['mind']},"
                   f"{s['endurance']},{s['strength']},{s['dexterity']},{s['intelligence']},"
                   f"{s['faith']},{s['arcane']}\n")
    return csv_path


def run_witchybnd(witchybnd_path: str, target_path: str) -> tuple[bool, str]:
    """Run WitchyBND on a file/folder."""
    result = subprocess.run([witchybnd_path, "-s", target_path], capture_output=True, text=True)
    if result.stderr and result.stderr.strip():
        return False, result.stderr
    return True, ""


def find_charainitparam(folder: Path) -> Optional[Path]:
    """Find CharaInitParam.param file in unpacked folder."""
    for root, dirs, files in os.walk(folder):
        for f in files:
            if "CharaInitParam" in f and f.endswith(".param"):
                return Path(root) / f
    return None


def find_equip_param(folder: Path, param_name: str) -> Optional[Path]:
    """Find a specific param file (e.g., EquipParamWeapon) in unpacked folder."""
    for root, dirs, files in os.walk(folder):
        for f in files:
            if param_name in f and f.endswith(".param"):
                return Path(root) / f
    return None


def load_weapon_names(witchybnd_path: Optional[str] = None) -> dict[int, str]:
    """Load weapon ID to name mapping from Paramdex Names file."""
    # Try multiple locations for the names file
    possible_paths = [
        SCRIPT_DIR / "WitchyBND-v2.16.0.5" / "Assets" / "Paramdex" / "ER" / "Names" / "EquipParamWeapon.txt",
        Path(sys.executable).parent / "WitchyBND-v2.16.0.5" / "Assets" / "Paramdex" / "ER" / "Names" / "EquipParamWeapon.txt",
        Path.cwd() / "WitchyBND-v2.16.0.5" / "Assets" / "Paramdex" / "ER" / "Names" / "EquipParamWeapon.txt",
    ]

    # If WitchyBND path is provided, derive the names path from it
    if witchybnd_path:
        witchy_dir = Path(witchybnd_path).parent
        possible_paths.insert(0, witchy_dir / "Assets" / "Paramdex" / "ER" / "Names" / "EquipParamWeapon.txt")

    names_file = None
    for path in possible_paths:
        if path.exists():
            names_file = path
            break

    names = {}
    if not names_file:
        return names

    with open(names_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(' ', 1)
            if len(parts) >= 2 and parts[1].strip():  # Must have a non-empty name
                try:
                    weapon_id = int(parts[0])
                    weapon_name = parts[1].strip()
                    names[weapon_id] = weapon_name
                except ValueError:
                    continue
    return names


def parse_weapon_requirements(unpacked_folder: Path, witchybnd_path: str) -> dict[int, dict[str, int]]:
    """Parse EquipParamWeapon.param.xml to get weapon stat requirements.

    Returns: {weapon_id: {'str': N, 'dex': N, 'int': N, 'fth': N, 'arc': N}, ...}

    Note: EquipParamWeapon uses 7-digit IDs, CharaInitParam uses 8-digit IDs (10x larger).
    We store both formats for lookup compatibility.
    """
    weapon_param = find_equip_param(unpacked_folder, "EquipParamWeapon")
    if not weapon_param:
        return {}

    # Serialize to XML
    weapon_xml = Path(str(weapon_param) + ".xml")
    if not weapon_xml.exists():
        run_witchybnd(witchybnd_path, str(weapon_param))

    if not weapon_xml.exists():
        return {}

    requirements = {}
    with open(weapon_xml, 'r', encoding='utf-8') as f:
        content = f.read()

    # Find all weapon rows - match the entire row element
    row_pattern = r'<row\s+id="(\d+)"([^/]*)/>'

    for match in re.finditer(row_pattern, content):
        weapon_id = int(match.group(1))
        # Only include base weapons (not upgrade variants)
        if weapon_id % 10000 != 0:
            continue

        row_attrs = match.group(2)

        # Extract each stat requirement independently (handles any attribute order)
        def get_attr(attr_name):
            attr_match = re.search(rf'{attr_name}="(\d+)"', row_attrs)
            return int(attr_match.group(1)) if attr_match else 0

        str_req = get_attr('properStrength')
        dex_req = get_attr('properAgility')
        int_req = get_attr('properMagic')
        fth_req = get_attr('properFaith')
        arc_req = get_attr('properLuck')

        # Only include weapons with actual requirements
        if str_req > 0 or dex_req > 0 or int_req > 0 or fth_req > 0 or arc_req > 0:
            requirements[weapon_id] = {
                'str': str_req,
                'dex': dex_req,
                'int': int_req,
                'fth': fth_req,
                'arc': arc_req
            }

    # Clean up XML file
    if weapon_xml.exists():
        weapon_xml.unlink()

    return requirements


def calculate_class_offset(weapon_reqs: dict[str, int], class_stats: dict[str, int]) -> int:
    """Calculate stat points needed for a class to wield a weapon (two-handed).

    Assumes two-handed wielding, which gives 1.5x effective strength.
    Returns: Number of stat points needed (0 if class can already wield)
    """
    cost = 0
    stat_mapping = {
        'str': 'strength',
        'dex': 'dexterity',
        'int': 'intelligence',
        'fth': 'faith',
        'arc': 'arcane'
    }
    for req_stat, class_stat in stat_mapping.items():
        req_val = weapon_reqs.get(req_stat, 0)
        class_val = class_stats.get(class_stat, 0)
        # Two-handed wielding gives 1.5x effective strength
        if req_stat == 'str':
            class_val = int(class_val * 1.5)
        cost += max(0, req_val - class_val)
    return cost


def parse_starting_equipment(param_xml_path: Path) -> dict[int, dict]:
    """Parse CharaInitParam XML to get starting equipment for each class.

    Returns: {class_id: {'name': str, 'weapons': [weapon_id, ...]}, ...}
    """
    if not param_xml_path.exists():
        return {}

    with open(param_xml_path, 'r', encoding='utf-8') as f:
        content = f.read()

    equipment = {}
    # Equipment slot fields in CharaInitParam
    equip_fields = ['equip_Wep_Right', 'equip_Subwep_Right', 'equip_Wep_Left', 'equip_Subwep_Left']

    # Match each class row (3000-3009)
    row_pattern = r'<row\s+id="(300\d)"([^/]*)/>'

    for match in re.finditer(row_pattern, content):
        class_id = int(match.group(1))
        row_attrs = match.group(2)

        weapons = []
        for field in equip_fields:
            field_match = re.search(rf'{field}="(\d+)"', row_attrs)
            if field_match:
                weapon_id = int(field_match.group(1))
                # Only include valid weapon IDs (not -1 or 0)
                if weapon_id > 0:
                    # Get base weapon ID (remove upgrade suffix)
                    base_id = (weapon_id // 10000) * 10000
                    if base_id not in weapons:
                        weapons.append(base_id)

        equipment[class_id] = {
            'name': PLAYER_CLASSES.get(class_id, f"Class_{class_id}"),
            'weapons': weapons
        }

    return equipment


def format_starting_equipment_offsets(starting_equip: dict[int, dict],
                                       weapon_reqs: dict[int, dict[str, int]],
                                       all_stats: dict[int, dict],
                                       weapon_names: dict[int, str]) -> str:
    """Format starting equipment offsets for each class."""
    if not starting_equip or not weapon_reqs:
        return ""

    lines = ["\n[Starting Equipment Stat Offsets]"]
    lines.append("(Negative = stat points needed to wield)\n")

    for class_id in sorted(starting_equip.keys()):
        equip_data = starting_equip[class_id]
        class_name = equip_data['name']
        class_stats = all_stats[class_id]['stats']

        weapon_offsets = []
        for weapon_id in equip_data['weapons']:
            weapon_name = weapon_names.get(weapon_id, f"Weapon_{weapon_id}")

            if weapon_id in weapon_reqs:
                reqs = weapon_reqs[weapon_id]
                offset = calculate_class_offset(reqs, class_stats)
                if offset > 0:
                    weapon_offsets.append(f"{weapon_name} ({-offset})")
                else:
                    weapon_offsets.append(f"{weapon_name} (OK)")
            else:
                weapon_offsets.append(f"{weapon_name} (OK)")

        if weapon_offsets:
            lines.append(f"{class_name}: {', '.join(weapon_offsets)}")

    return "\n".join(lines)


def modify_param_xml(param_path: Path, all_stats: dict[int, dict]) -> bool:
    """Modify the CharaInitParam XML with randomized stats."""
    with open(param_path, 'r', encoding='utf-8') as f:
        content = f.read()
    modified_count = 0
    for row_id, data in all_stats.items():
        stats = data['stats']
        def replace_stat_in_row(match):
            row = match.group(0)
            for stat_name, field_name in STAT_FIELDS.items():
                row = re.sub(rf'{field_name}="[^"]*"', f'{field_name}="{stats[stat_name]}"', row)
            return row
        content, count = re.subn(rf'<row\s+id="{row_id}"[^>]*/>', replace_stat_in_row, content)
        modified_count += count
    with open(param_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return modified_count > 0


def process_regulation(regulation_path: Path, seed: int, witchybnd_path: str,
                       log_callback: Optional[Callable[[str], None]] = None) -> tuple:
    """Process regulation.bin with randomized stats."""
    def log(msg):
        if log_callback:
            log_callback(msg)
    try:
        all_stats = randomize_stats(seed)
        work_dir = OUTPUT_DIR / f"work_{seed}"
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        work_regulation = work_dir / "regulation.bin"
        shutil.copy2(regulation_path, work_regulation)
        log("Copied regulation.bin to work directory")

        log("\n[Step 1] Unpacking regulation.bin...")
        success, err = run_witchybnd(witchybnd_path, str(work_regulation))
        if not success:
            return False, f"Unpack failed: {err}", None, None, None

        unpacked_folder = next((item for item in work_dir.iterdir()
                               if item.is_dir() and "regulation" in item.name.lower()), None)
        if not unpacked_folder:
            return False, "Could not find unpacked folder", None, None, None
        log(f"  Unpacked to: {unpacked_folder.name}")

        log("\n[Step 2] Serializing CharaInitParam...")
        param_file = find_charainitparam(unpacked_folder)
        if not param_file:
            return False, "Could not find CharaInitParam.param", None, None, None
        run_witchybnd(witchybnd_path, str(param_file))

        param_xml = Path(str(param_file) + ".xml")
        if not param_xml.exists():
            return False, "XML file not created", None, None, None

        log("\n[Step 2.5] Reading starting equipment (from ER Randomizer)...")
        starting_equip = parse_starting_equipment(param_xml)
        total_weapons = sum(len(e['weapons']) for e in starting_equip.values())
        log(f"  Found {total_weapons} starting weapons across {len(starting_equip)} classes")

        log("\n[Step 3] Applying randomized stats...")
        modify_param_xml(param_xml, all_stats)
        log("  Updated 10 class entries")

        log("\n[Step 4] Converting XML back to param...")
        run_witchybnd(witchybnd_path, str(param_xml))
        if param_file.exists():
            param_xml.unlink()

        log("\n[Step 4.5] Parsing weapon requirements...")
        weapon_reqs = parse_weapon_requirements(unpacked_folder, witchybnd_path)
        log(f"  Found {len(weapon_reqs)} weapons with stat requirements")

        log("\n[Step 5] Repacking regulation.bin...")
        run_witchybnd(witchybnd_path, str(unpacked_folder))
        if not work_regulation.exists():
            return False, "Repacked file not found", None, None, None

        log("\n[Step 6] Injecting into original location...")
        backup_path = regulation_path.with_suffix('.bin.backup')
        if not backup_path.exists():
            shutil.copy2(regulation_path, backup_path)
            log(f"  Created backup: {backup_path.name}")
        shutil.copy2(work_regulation, regulation_path)
        log(f"  Copied to: {regulation_path}")

        csv_path = export_csv(all_stats, seed, OUTPUT_DIR)
        log(f"\nExported CSV: {csv_path}")
        shutil.rmtree(work_dir)
        log("\nWork files cleaned up.")
        return True, "Success!", all_stats, weapon_reqs, starting_equip
    except Exception as e:
        return False, str(e), None, None, None


# ============================================================
# GUI Application
# ============================================================

class EldenRingModTool:
    def __init__(self, root):
        self.root = root
        self.root.title("Elden Ring Mod Tool - Stats & Graces")
        self.root.geometry("750x700")
        self.root.resizable(True, True)

        self.witchybnd_path = find_witchybnd()
        self.memory = MemoryManager()
        self.event_flag_man = 0
        self.grace_checkboxes = {}
        self.config = load_config()

        self.create_widgets()

    def create_widgets(self):
        # Notebook for tabs
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Tab 1: Stat Randomizer
        stats_frame = ttk.Frame(notebook, padding="10")
        notebook.add(stats_frame, text="Stat Randomizer")
        self.create_stats_tab(stats_frame)

        # Tab 2: Grace Unlocker
        grace_frame = ttk.Frame(notebook, padding="10")
        notebook.add(grace_frame, text="Grace Unlocker")
        self.create_grace_tab(grace_frame)

    def create_stats_tab(self, parent):
        # Regulation.bin selection
        reg_frame = ttk.LabelFrame(parent, text="Regulation.bin File", padding="10")
        reg_frame.pack(fill=tk.X, pady=(0, 10))

        # Use saved path from config, or default
        saved_path = self.config.get('regulation_path', DEFAULT_REGULATION_PATH)
        self.reg_path_var = tk.StringVar(value=saved_path)
        ttk.Entry(reg_frame, textvariable=self.reg_path_var, width=65).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        ttk.Button(reg_frame, text="Browse...", command=self.browse_regulation).pack(side=tk.RIGHT)

        # Seed input
        seed_frame = ttk.LabelFrame(parent, text="Seed", padding="10")
        seed_frame.pack(fill=tk.X, pady=(0, 10))

        seed_inner = ttk.Frame(seed_frame)
        seed_inner.pack(fill=tk.X)

        ttk.Label(seed_inner, text="Seed:").pack(side=tk.LEFT)
        self.seed_var = tk.StringVar(value=str(random.randint(100000, 999999)))
        ttk.Entry(seed_inner, textvariable=self.seed_var, width=15).pack(side=tk.LEFT, padx=(5, 15))
        ttk.Button(seed_inner, text="Random", command=lambda: self.seed_var.set(str(random.randint(100000, 999999)))).pack(side=tk.LEFT, padx=(0, 15))
        ttk.Button(seed_inner, text="Preview", command=self.preview_stats).pack(side=tk.LEFT)

        # Preview
        preview_frame = ttk.LabelFrame(parent, text="Stats Preview", padding="10")
        preview_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        self.preview_text = scrolledtext.ScrolledText(preview_frame, height=10, font=('Consolas', 9))
        self.preview_text.pack(fill=tk.BOTH, expand=True)

        # Log
        log_frame = ttk.LabelFrame(parent, text="Log", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=6, font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Buttons
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X)

        self.randomize_btn = ttk.Button(btn_frame, text="Randomize Stats!", command=self.randomize_stats)
        self.randomize_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.stats_status_var = tk.StringVar(value="Ready")
        ttk.Label(btn_frame, textvariable=self.stats_status_var).pack(side=tk.LEFT)

        witchy_color = 'green' if self.witchybnd_path else 'red'
        witchy_text = "WitchyBND: Found" if self.witchybnd_path else "WitchyBND: NOT FOUND"
        ttk.Label(btn_frame, text=witchy_text, foreground=witchy_color).pack(side=tk.RIGHT)

    def create_grace_tab(self, parent):
        # Info label
        info_text = "Unlock Sites of Grace while the game is running.\nRequires: Game running + Character loaded"
        ttk.Label(parent, text=info_text, foreground='gray').pack(pady=(0, 10))

        # Connection frame
        conn_frame = ttk.LabelFrame(parent, text="Game Connection", padding="10")
        conn_frame.pack(fill=tk.X, pady=(0, 10))

        conn_inner = ttk.Frame(conn_frame)
        conn_inner.pack(fill=tk.X)

        self.conn_status_var = tk.StringVar(value="Not connected")
        ttk.Label(conn_inner, textvariable=self.conn_status_var, foreground='red').pack(side=tk.LEFT)
        ttk.Button(conn_inner, text="Connect to Game", command=self.connect_to_game).pack(side=tk.RIGHT)

        self.efm_status_var = tk.StringVar(value="EventFlagMan: Not found")
        ttk.Label(conn_frame, textvariable=self.efm_status_var, foreground='gray').pack(anchor=tk.W)

        # Presets frame
        presets_frame = ttk.LabelFrame(parent, text="Presets", padding="5")
        presets_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(presets_frame, text="Quick Select:").pack(side=tk.LEFT, padx=(0, 5))
        self.preset_var = tk.StringVar(value="")
        preset_combo = ttk.Combobox(presets_frame, textvariable=self.preset_var,
                                     values=list(GRACE_PRESETS.keys()), state="readonly", width=20)
        preset_combo.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(presets_frame, text="Apply Preset", command=self.apply_grace_preset).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(presets_frame, text="Unlock Preset", command=self.unlock_preset_graces).pack(side=tk.LEFT)

        # Show preset contents
        self.preset_info_var = tk.StringVar(value="")
        ttk.Label(presets_frame, textvariable=self.preset_info_var, foreground='gray').pack(side=tk.RIGHT)
        preset_combo.bind("<<ComboboxSelected>>", self.on_preset_selected)

        # Search frame
        search_frame = ttk.Frame(parent)
        search_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(search_frame, text="Search:").pack(side=tk.LEFT)
        self.grace_search_var = tk.StringVar()
        self.grace_search_var.trace_add("write", lambda *args: self.filter_graces())
        search_entry = ttk.Entry(search_frame, textvariable=self.grace_search_var, width=40)
        search_entry.pack(side=tk.LEFT, padx=(5, 10), fill=tk.X, expand=True)
        ttk.Button(search_frame, text="Clear", width=6, command=lambda: self.grace_search_var.set("")).pack(side=tk.LEFT)

        self.search_results_var = tk.StringVar(value="")
        ttk.Label(search_frame, textvariable=self.search_results_var, foreground='gray').pack(side=tk.RIGHT)

        # Grace selection frame with canvas for scrolling
        grace_outer = ttk.LabelFrame(parent, text="Select Graces to Unlock (by Region)", padding="10")
        grace_outer.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        # Create canvas with scrollbar
        self.grace_canvas = tk.Canvas(grace_outer, height=350)
        scrollbar = ttk.Scrollbar(grace_outer, orient="vertical", command=self.grace_canvas.yview)
        self.grace_inner = ttk.Frame(self.grace_canvas)

        self.grace_inner.bind("<Configure>", lambda e: self.grace_canvas.configure(scrollregion=self.grace_canvas.bbox("all")))
        self.grace_canvas.create_window((0, 0), window=self.grace_inner, anchor="nw")
        self.grace_canvas.configure(yscrollcommand=scrollbar.set)

        self.grace_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Enable mousewheel scrolling
        def on_mousewheel(event):
            self.grace_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        self.grace_canvas.bind_all("<MouseWheel>", on_mousewheel)

        # Store region frames and checkbox widgets for filtering
        self.region_vars = {}
        self.region_frames = {}
        self.grace_widgets = {}  # grace_name -> (checkbox_widget, region_name)

        # Add graces organized by region
        row_idx = 0
        for region, graces in GRACES_BY_REGION.items():
            # Region header with select all checkbox
            region_var = tk.BooleanVar(value=False)
            self.region_vars[region] = region_var

            region_frame = ttk.LabelFrame(self.grace_inner, text=f" {region} ({len(graces)} graces) ", padding="5")
            region_frame.grid(row=row_idx, column=0, columnspan=2, sticky='ew', padx=5, pady=5)
            self.region_frames[region] = region_frame
            row_idx += 1

            # Select all for this region button
            def make_select_region(r, rv):
                def select_region():
                    new_val = not rv.get()
                    rv.set(new_val)
                    for gname in GRACES_BY_REGION[r].keys():
                        if gname in self.grace_checkboxes:
                            self.grace_checkboxes[gname].set(new_val)
                return select_region

            toggle_btn = ttk.Button(region_frame, text="Toggle All", width=10,
                      command=make_select_region(region, region_var))
            toggle_btn.grid(row=0, column=0, sticky='w', padx=2)

            # Add grace checkboxes in grid within region
            grace_list = sorted(graces.keys())
            col_count = 2
            for i, grace_name in enumerate(grace_list):
                var = tk.BooleanVar(value=False)
                cb = ttk.Checkbutton(region_frame, text=grace_name, variable=var)
                cb.grid(row=1 + i // col_count, column=i % col_count, sticky='w', padx=5, pady=1)
                self.grace_checkboxes[grace_name] = var
                self.grace_widgets[grace_name] = (cb, region)

        # Buttons
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X)

        ttk.Button(btn_frame, text="Select All", command=self.select_all_graces).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="Deselect All", command=self.deselect_all_graces).pack(side=tk.LEFT, padx=(0, 15))
        ttk.Button(btn_frame, text="Unlock Selected", command=self.unlock_selected_graces).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="Unlock ALL", command=self.unlock_all_graces).pack(side=tk.LEFT)

        self.grace_status_var = tk.StringVar(value="")
        ttk.Label(btn_frame, textvariable=self.grace_status_var).pack(side=tk.RIGHT)

    def filter_graces(self):
        """Filter displayed graces based on search text."""
        search_text = self.grace_search_var.get().lower().strip()

        if not search_text:
            # Show all regions and graces
            for region, frame in self.region_frames.items():
                frame.grid()
                for grace_name in GRACES_BY_REGION[region].keys():
                    if grace_name in self.grace_widgets:
                        self.grace_widgets[grace_name][0].grid()
            self.search_results_var.set("")
            return

        # Track matches
        matching_graces = 0
        visible_regions = set()

        # First pass: find matching graces and their regions
        for grace_name, (widget, region) in self.grace_widgets.items():
            if search_text in grace_name.lower() or search_text in region.lower():
                visible_regions.add(region)
                matching_graces += 1

        # Hide all regions first
        for region, frame in self.region_frames.items():
            if region in visible_regions:
                frame.grid()
            else:
                frame.grid_remove()

        # Show/hide individual graces within visible regions
        for grace_name, (widget, region) in self.grace_widgets.items():
            if region in visible_regions:
                # In visible region, show if matches grace name or if region matched
                if search_text in grace_name.lower() or search_text in region.lower():
                    widget.grid()
                else:
                    # If region matched but not this specific grace, still show it
                    if search_text in region.lower():
                        widget.grid()
                    else:
                        widget.grid_remove()

        # Update results label
        if matching_graces > 0:
            self.search_results_var.set(f"{matching_graces} matches in {len(visible_regions)} regions")
        else:
            self.search_results_var.set("No matches found")

        # Reset scroll position
        self.grace_canvas.yview_moveto(0)

    # ==================== Stats Tab Methods ====================

    def browse_regulation(self):
        initial_dir = Path(self.reg_path_var.get()).parent
        if not initial_dir.exists():
            initial_dir = Path.home()
        filepath = filedialog.askopenfilename(
            title="Select regulation.bin", initialdir=initial_dir,
            filetypes=[("BIN files", "*.bin"), ("All files", "*.*")]
        )
        if filepath:
            self.reg_path_var.set(filepath)
            # Save to config
            self.config['regulation_path'] = filepath
            save_config(self.config)

    def preview_stats(self):
        try:
            seed = int(self.seed_var.get())
        except ValueError:
            messagebox.showerror("Error", "Please enter a valid numeric seed")
            return
        all_stats = randomize_stats(seed)
        self.preview_text.delete(1.0, tk.END)
        self.preview_text.insert(tk.END, f"Seed: {seed}\n")
        self.preview_text.insert(tk.END, f"Target Level: {TARGET_LEVEL} ({TOTAL_STATS} total points)\n\n")
        self.preview_text.insert(tk.END, format_stats_text(all_stats))

    def log(self, message):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def randomize_stats(self):
        try:
            seed = int(self.seed_var.get())
        except ValueError:
            messagebox.showerror("Error", "Please enter a valid numeric seed")
            return

        reg_path = Path(self.reg_path_var.get())
        if not reg_path.exists():
            messagebox.showerror("Error", f"regulation.bin not found:\n{reg_path}")
            return

        # Save the regulation path to config
        self.config['regulation_path'] = str(reg_path)
        save_config(self.config)

        if not self.witchybnd_path:
            messagebox.showerror("Error", "WitchyBND not found!\n\nDownload from:\nhttps://github.com/ividyon/WitchyBND/releases")
            return

        self.log_text.delete(1.0, tk.END)
        self.preview_stats()
        self.randomize_btn.config(state='disabled')
        self.stats_status_var.set("Processing...")

        def run_process():
            success, message, all_stats, weapon_reqs, starting_equip = process_regulation(
                reg_path, seed, self.witchybnd_path,
                log_callback=lambda msg: self.root.after(0, lambda: self.log(msg))
            )
            def finish():
                self.randomize_btn.config(state='normal')
                if success:
                    self.stats_status_var.set("Success!")
                    self.log(f"\n{'='*50}\nSUCCESS! Seed: {seed}")

                    # Display starting equipment offsets
                    if all_stats and weapon_reqs and starting_equip:
                        weapon_names = load_weapon_names(self.witchybnd_path)
                        self.log(f"\n(Loaded {len(weapon_names)} weapon names)")
                        offset_table = format_starting_equipment_offsets(
                            starting_equip, weapon_reqs, all_stats, weapon_names
                        )
                        if offset_table:
                            self.log(offset_table)

                    messagebox.showinfo("Success", f"Randomization complete!\n\nSeed: {seed}")
                else:
                    self.stats_status_var.set("Failed")
                    self.log(f"\nERROR: {message}")
                    messagebox.showerror("Error", f"Failed:\n{message}")
            self.root.after(0, finish)

        threading.Thread(target=run_process).start()

    # ==================== Grace Tab Methods ====================

    def connect_to_game(self):
        self.conn_status_var.set("Searching for Elden Ring...")
        self.root.update_idletasks()

        # Try to attach to the game
        if not self.memory.attach():
            self.conn_status_var.set("Game not found")
            error_msg = self.memory.last_error or "Elden Ring is not running"
            messagebox.showerror("Error", f"{error_msg}\n\nMake sure:\n1. Elden Ring is running\n2. A character is loaded\n3. Run this tool as Administrator if needed")
            return

        # Show which process was found
        process_info = f"Found: {self.memory.process_name} (PID: {self.memory.process_id})"
        self.conn_status_var.set(process_info)
        self.root.update_idletasks()

        # Check if module info was obtained
        if self.memory.module_base == 0:
            self.conn_status_var.set("Attached but module not ready")
            self.efm_status_var.set("Wait for game to fully load...")
            messagebox.showwarning("Warning", "Connected to game but module info not available.\n\nWait for the game to fully load and try again.")
            return

        # Show module info for debugging
        module_info = f"Module: 0x{self.memory.module_base:X}, Size: {self.memory.module_size / (1024*1024):.1f} MB"
        print(module_info)

        self.conn_status_var.set("Scanning memory...")
        self.root.update_idletasks()

        # Scan for EventFlagMan
        self.event_flag_man = self.memory.scan_aob(EVENT_FLAG_MAN_AOB, 3, 7)

        if self.event_flag_man:
            self.efm_status_var.set(f"EventFlagMan: 0x{self.event_flag_man:X}")
            self.conn_status_var.set(f"Connected! ({self.memory.process_name})")
            # Update label colors
            for widget in self.root.winfo_children():
                self._update_label_color(widget, "EventFlagMan:", 'green')
                self._update_label_color(widget, "Connected!", 'green')
        else:
            scan_error = self.memory.last_error or "Unknown error"
            self.efm_status_var.set(f"EventFlagMan: Not found ({scan_error})")
            self.conn_status_var.set("Partial connection")
            messagebox.showwarning("Warning", f"Connected to game but EventFlagMan not found.\n\nError: {scan_error}\n\nMake sure:\n1. You're past the title screen\n2. A character is loaded into the game world")

    def _update_label_color(self, widget, text_contains, color):
        """Recursively update label colors."""
        if isinstance(widget, ttk.Label):
            try:
                if text_contains in str(widget.cget('text')) or text_contains in widget.cget('textvariable'):
                    widget.config(foreground=color)
            except:
                pass
        for child in widget.winfo_children():
            self._update_label_color(child, text_contains, color)

    def get_event_flag_base(self) -> int:
        """Get the base address for event flags."""
        if not self.event_flag_man:
            return 0
        ptr = self.memory.read_int64(self.event_flag_man)
        if ptr == 0:
            return 0
        base_addr = self.memory.read_int64(ptr + 0x28)
        return base_addr

    def select_all_graces(self) -> None:
        for var in self.grace_checkboxes.values():
            var.set(True)

    def deselect_all_graces(self) -> None:
        for var in self.grace_checkboxes.values():
            var.set(False)

    def unlock_selected_graces(self) -> None:
        if not self.memory.is_attached:
            messagebox.showerror("Error", "Not connected to game!\n\nClick 'Connect to Game' first.")
            return

        flag_base = self.get_event_flag_base()
        if flag_base == 0:
            messagebox.showerror("Error", "Could not access event flags.\n\nMake sure a character is loaded.")
            return

        unlocked = []
        for grace_name, var in self.grace_checkboxes.items():
            if var.get() and grace_name in KNOWN_GRACES:
                offset, bit = KNOWN_GRACES[grace_name]
                addr = flag_base + offset
                current = self.memory.read_byte(addr)
                new_value = current | (1 << bit)
                if self.memory.write_byte(addr, new_value):
                    unlocked.append(grace_name)

        if unlocked:
            self.grace_status_var.set(f"Unlocked {len(unlocked)} graces!")
            messagebox.showinfo("Success", f"Unlocked {len(unlocked)} graces:\n\n" + "\n".join(unlocked[:10]) +
                              (f"\n...and {len(unlocked)-10} more" if len(unlocked) > 10 else ""))
        else:
            messagebox.showinfo("Info", "No graces selected")

    def unlock_all_graces(self) -> None:
        self.select_all_graces()
        self.unlock_selected_graces()

    def on_preset_selected(self, event=None) -> None:
        """Handle preset selection - show info about the preset."""
        preset_name = self.preset_var.get()
        if preset_name and preset_name in GRACE_PRESETS:
            grace_count = len(GRACE_PRESETS[preset_name])
            self.preset_info_var.set(f"{grace_count} graces")
        else:
            self.preset_info_var.set("")

    def apply_grace_preset(self) -> None:
        """Apply selected preset - check the corresponding grace checkboxes."""
        preset_name = self.preset_var.get()
        if not preset_name:
            messagebox.showinfo("Info", "Please select a preset first.")
            return

        if preset_name not in GRACE_PRESETS:
            messagebox.showerror("Error", f"Unknown preset: {preset_name}")
            return

        # First deselect all
        self.deselect_all_graces()

        # Then select only the preset graces
        preset_graces = GRACE_PRESETS[preset_name]
        selected_count = 0
        not_found = []

        for grace_name in preset_graces:
            if grace_name in self.grace_checkboxes:
                self.grace_checkboxes[grace_name].set(True)
                selected_count += 1
            else:
                not_found.append(grace_name)

        # Clear search to show all graces
        self.grace_search_var.set("")

        if not_found:
            messagebox.showwarning("Warning",
                f"Selected {selected_count} graces.\n\n"
                f"Not found ({len(not_found)}):\n" + "\n".join(not_found))
        else:
            self.grace_status_var.set(f"Preset '{preset_name}' applied ({selected_count} graces)")

    def unlock_preset_graces(self) -> None:
        """Apply preset and immediately unlock those graces."""
        preset_name = self.preset_var.get()
        if not preset_name:
            messagebox.showinfo("Info", "Please select a preset first.")
            return

        self.apply_grace_preset()
        self.unlock_selected_graces()

    def on_closing(self) -> None:
        """Clean up resources before closing the application."""
        if self.memory.is_attached:
            self.memory.detach()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = EldenRingModTool(root)
    # Register cleanup handler for proper resource release
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
