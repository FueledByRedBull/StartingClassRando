# Elden Ring Starting Class Stat Randomizer

A GUI tool that randomizes starting class stats in Elden Ring. Also includes a Grace Unlocker feature for unlocking Sites of Grace while the game is running.

## Features

- **Stat Randomizer**: Randomizes the starting stats for all 10 classes while keeping the total points balanced (88 points, Level 9)
- **Grace Unlocker**: Unlock any Site of Grace in-game (requires game to be running)
- **Seed-based**: Use seeds to share randomized stat distributions with friends
- **Starting Equipment Check**: Shows if your randomized class can wield its starting weapons

## Requirements

- [WitchyBND](https://github.com/ividyon/WitchyBND/releases) - Required for modifying game files
- Windows 10/11
- Elden Ring (Steam version)

## Installation

1. Download [WitchyBND](https://github.com/ividyon/WitchyBND/releases) and extract it
2. Download `EldenRingStatRandomizer.exe` from [Releases](https://github.com/FueledByRedBull/StartingClassRando/releases)
3. **Place the .exe inside the WitchyBND folder** (same folder as `WitchyBND.exe`)
4. Run `EldenRingStatRandomizer.exe`

## Usage

### Stat Randomizer Tab
1. Browse to your `regulation.bin` file (usually in your mod folder)
2. Enter a seed or click "Random" for a new one
3. Click "Preview" to see the randomized stats
4. Click "Randomize Stats!" to apply changes

### Grace Unlocker Tab
1. Launch Elden Ring and load a character
2. Click "Connect to Game"
3. Select the graces you want to unlock
4. Click "Unlock Selected" or "Unlock ALL"

## Notes

- A backup of your `regulation.bin` is created automatically
- The Grace Unlocker requires the game to be running with a character loaded
- You may need to run as Administrator for the Grace Unlocker to work

## Building from Source

```bash
pip install pyinstaller pymem pillow
pyinstaller --onefile --windowed --name EldenRingStatRandomizer stat_randomizer_gui.py
```

## License

MIT
