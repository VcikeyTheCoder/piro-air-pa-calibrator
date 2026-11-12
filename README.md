# PIRO-EDGE

Pressure advance calibration for Klipper using the load cell on the **PIRO-AIR** toolhead (HX717, 320 Hz).
The nozzle extrudes fast and slow pulses into the air while the load cell (or a BTT Eddy) measures the nozzle
pressure. The result, **PIRO-EDGE**, is the highest PA before the force dips after the slow-down, with an
uncertainty and a grade. With **dual PA** (on by default) the same pulses also give the PA for the speed-up, so one
run gives a push and a pull value for [dual_pa](https://github.com/VcikeyTheCoder/dual_pa). **PIRO-FALL** (slow-down area 0) and **PIRO-AREA** (rise
and fall together) are computed on the same data and shown beside it.

Tested on printers with more than 200 runs across several filaments.

![PIRO-EDGE](docs/images/theme-piro.png)

**[Documentation (wiki)](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki)**: [Installation](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/Installation) ·
[Configuration](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/Configuration) · [Commands](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/Commands) · [Using the page](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/Using-the-page) ·
[Reading a result](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/Reading-a-result) · [How PIRO-EDGE works](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/How-PIRO-EDGE-works) · [Themes](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/Themes) ·
[Troubleshooting](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/Troubleshooting)

## Features

- Adaptive search: up to 7 PA values (`max_values`, every mode; repeats of a value are not counted), about 2 minutes per filament
- PIRO-EDGE result with ± uncertainty, a *good / fair / poor* grade and the graphs it came from; PIRO-FALL and PIRO-AREA shown beside it
- Dual PA: push (speed-up) and pull (slow-down) PA from one run, no extra PA values. Edge takes push from the overshoot after the speed-up, fall from the speed-up area. Auto PA sets both with `SET_DUAL_PA` when [dual_pa](https://github.com/VcikeyTheCoder/dual_pa) is installed
- Mode and method, chosen separately: **mode** = the rule judging the curve (`edge` PIRO-EDGE, default, `fall` PIRO-FALL or `area` PIRO-AREA); **method** = how each PA value is applied (`push`, default: exactly Klipper's own PA with no XY motion, or `klipper`: Klipper's own PA on a short XY line). No conversion either way
- Load cell, BTT Eddy (raw Hz over I2C, `[piro_tap]` or `[pa_eddy]`) or both at once to compare them on the same pulses
- **PIRO-TAP** (its own part, [`piro_tap/`](piro_tap)): the same Eddy as a nozzle-contact Z probe: bed mesh, Z tilt / QGL, 4-touch Z homing, MCU-side trigger in Hz, Kalico-style pullback fit for Z, ooze checks with retries, nozzle thermal expansion calibration
- Mainsail mini window: read-only test progress, probing progress and a live 3 / 5 / 10 s graph with display-only smoothing on the Mainsail dashboard (HTML Iframe webcam)
- Auto PA before every print at the slicer's outer wall flow (OrcaSlicer), set as Klipper's PA, no copying
- Graphs that show how the value was chosen, from the raw pulse to the final line
- Live load cell graph, manual recordings, compare across runs, export and import of runs
- Settings saved to `pa_loadcell.cfg`, six themes (including a tmux + Neovim style Terminal theme) plus custom theme files
- Works with `[load_cell]`, `[load_cell_probe]`, `[piro_tap]`, `[pa_eddy]` and `[probe_eddy_current]`
- Hard to break: endpoint errors never shut Klipper down, crash-safe saves, heavy analysis in a background process, checked imports

## Install

```bash
cd ~
git clone https://github.com/VcikeyTheCoder/piro-air-pa-calibrator.git
cd piro-air-pa-calibrator && bash install.sh     # asks: PA calibrator, PIRO-TAP probe, or both
```

`bash install.sh --pa`, `--probe` or `--both` chooses without asking. Later runs keep the choice.

Set `test_x/test_y` and `park_x/park_y` in `pa_loadcell.cfg`, then refresh Mainsail (Ctrl+F5). **Load Cell**
appears in the sidebar. Details: [Installation](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/Installation).

| | |
|---|---|
| Update | `cd ~/piro-air-pa-calibrator && git pull && bash install.sh`, or **Update** on Mainsail's Machine page (run `bash install.sh` once after updates that add files). Your `pa_loadcell.cfg` and `pa_auto.cfg` values are kept |
| Uninstall | `bash install.sh --uninstall` (config and runs are kept) |

## Commands

| Command | Description |
|---|---|
| `PA_LOADCELL_TEST` | Run the test. Parameters override `[pa_loadcell]`, e.g. `PA_LOADCELL_TEST FILAMENT="PETG black" FAST_FLOW=10` |
| `PA_LOADCELL_TEST MODE=fall` | Judge the curve with PIRO-FALL for this run (default `mode: edge`) |
| `PA_LOADCELL_TEST DUAL=0` | One PA only for this run (default `dual: 1`: push and pull) |
| `PA_LOADCELL_TEST METHOD=klipper` | Apply PA on a short XY line for this run (default `method: push`) |
| `PA_LOADCELL_DIAG` | Check the load cell stream: expect about 320 samples/s |
| `PA_AUTO {rawparams}` | In PRINT_START: auto PA before every print (`pa_auto.cfg`) |
| `PIRO_TAP_CALIBRATE MODE=THRESHOLD` | PIRO-TAP: `trigger_hz` = 3 × Eddy noise (peak to peak), then `SAVE_CONFIG` |
| `PIRO_TAP_THERMAL_CALIBRATE START=0 END=200 STEP=50` | PIRO-TAP: nozzle growth per °C |
| `PIRO_TAP_THERMAL_APPLY TEMP=` | PIRO-TAP: thermal Z offset in PRINT_START, before the first layer |
| `NOZZLE_CLEAN_CHECK` | PIRO-TAP: tap until 3 clean taps agree |
| `PIRO_TAP_STATUS` | PIRO-TAP: rate, trigger, last tap. More: [PIRO-TAP probe](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/PIRO-TAP-probe) |

The test needs the nozzle at `min_temp`, all axes homed and Z at `required_z` (20 mm). Apply the result with
`SET_PRESSURE_ADVANCE ADVANCE=<value>`, or with dual PA `SET_DUAL_PA PUSH=<push> PULL=<pull>` (the page shows the
command to copy). All parameters: [Commands](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/Commands).

## Auto PA before every print

When pressure advance is off in the OrcaSlicer filament profile, PA is measured at the outer wall flow and wall
temperature, and set: push and pull with `SET_DUAL_PA` when [dual_pa](https://github.com/VcikeyTheCoder/dual_pa) is installed, else one PA (the pull
value). A recent result of the same filament is reused; no usable result sets 0.02.

OrcaSlicer, Machine start G-code (one line):

```
print_start EXTRUDER=[first_layer_temperature] BED=[first_layer_bed_temperature] CHAMBER=[chamber_temperature] PRINT_TEMP={nozzle_temperature[initial_extruder]} PA_ON={if enable_pressure_advance[initial_extruder]}1{else}0{endif} WALL_FLOW={outer_wall_volumetric_speed} FILAMENT="{filament_settings_id[initial_extruder]}" OUTER_ACCEL={outer_wall_acceleration[initial_extruder]}
```

PRINT_START, after the bed mesh:

```
PA_AUTO {rawparams}
```

`pa_auto.cfg` holds four macros, each usable on its own: `PA_AUTO` (called by PRINT_START), `PA_AUTO_POSITION`,
`PA_AUTO_RUN` (Klipper's PA on a line from the PA location at the slicer's outer wall acceleration, or push) and
`PA_AUTO_FLICK` (retract and flick, `flick: 1`, and an optional push-off of the purge for printers without a
bucket). Settings are variables at the top of each macro; `install.sh` carries your values over when the file
changes. Details:
[Auto PA before every print](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/Auto-PA).

## Example

Run 42 at 240 °C, analysed as PIRO-AREA: 0.0659 ± 0.0010, good. The no-dip edge is 0.0596.

![Result](docs/images/answer.png)

![Analysis](docs/images/how.png)

How each value was chosen: [How PIRO-EDGE works](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/How-PIRO-EDGE-works).

## Credits

Pulse timing and parts of the analysis are based on [OznLab sensor](https://github.com/ozancs/oznlab-sensor) and
[KAPAT](https://github.com/vzagranichnyy/KAPAT), both GPL-3.0.
