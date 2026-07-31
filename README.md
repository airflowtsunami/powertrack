# PowerTrack

A two-rider, 1990s-style velodrome racing game built with Python and Pygame. Rider speed is controlled entirely by live ANT+ power data, with optional ANT+ FE-C trainer resistance control.

## Requirements

- Windows 10 or 11
- Python 3.13
- An ANT+ USB stick
- Compatible ANT+ power meters or smart trainers

## Installation and launch

1. Download or clone this repository.
2. Connect the ANT+ USB stick and rider devices.
3. Run `run_game.bat`.

On the first launch, `run_game.bat` automatically runs `install.bat` if the local Python environment or required packages are missing. It then starts the game.

You can also run `install.bat` directly to create or repair the local environment.

## Power-based racing

Each rider selects an ANT+ power source before the race. Rider speed is calculated from live power output relative to the rider's configured FTP.

Compatible ANT+ FE-C trainers can optionally be placed into resistance mode when selected.

## User data

The game creates local files that are intentionally excluded from Git:

- `rider_preferences.json` — rider names and FTP settings
- `race_logs/` — completed race logs

`rider_preferences.example.json` shows the expected preferences format.

## Project files

- `powertrack.py` — game, interface and race logic
- `ant_power.py` — ANT+ power and optional FE-C integration
- `requirements.txt` — Python dependencies
- `install.bat` — Windows setup and dependency installation
- `run_game.bat` — automatic setup check and game launcher

## Troubleshooting

If ANT+ is not detected:

1. Confirm the ANT+ USB stick is connected.
2. Close other applications that may be using the stick.
3. Run `install.bat` to repair or refresh the Python environment.
4. Start the game with `run_game.bat` and review any error shown in the Command Prompt window.

## Development

Run a basic syntax check with:

```powershell
.venv\Scripts\python.exe -m compileall powertrack.py ant_power.py
```

The code is currently kept in two modules deliberately. Further splitting may make the architecture cleaner, but is not necessary for running or sharing the project.
