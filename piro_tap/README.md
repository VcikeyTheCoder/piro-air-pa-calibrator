# PIRO-TAP

A Klipper Z probe that touches the bed with the nozzle. A BTT Eddy senses the nozzle pressure (the coil watches
the hotend mount flex), and everything is in **Hz**: the change of the Eddy frequency from a tare taken before
every touch.

It is a normal Klipper probe: `PROBE`, `PROBE_ACCURACY`, `G28` with `probe:z_virtual_endstop`,
`BED_MESH_CALIBRATE`, `Z_TILT_ADJUST`, `QUAD_GANTRY_LEVEL`, `SCREWS_TILT_CALCULATE`.

| Part | How |
|---|---|
| Contact | The Eddy's MCU filters every sample and stops Z when the pressure passes `trigger_hz` (no Pi delay). Past `force_limit_hz` (2000) the move stops with an error |
| Z | Kalico's tap analysis, copied 1:1: pullback of `pullback_distance` at `pullback_speed`, five lines fitted in time (approach, press, dwell, release, free air), Z = toolhead position when the release line meets the free-air line |
| Trigger | `PIRO_TAP_CALIBRATE MODE=THRESHOLD`: tare, noise peak to peak, `trigger_hz` = 3 × noise |
| Pullback | `PIRO_TAP_CALIBRATE MODE=TAP_PERFORMANCE` (Kalico): `pullback_distance` and `decompression_angle` |
| Homing | Klipper's own homing: 2 touches at `homing_speed` (5), then 2 at `second_homing_speed` (2). Z = average of the 2 slow touches |
| Ooze | Kalico's tap quality classifier and `bad_probe_strategy` (retry, circle, ignore, fail). `NOZZLE_CLEAN_CHECK` |
| Thermal | `PIRO_TAP_THERMAL_CALIBRATE START=0 END=200 STEP=50` fits nozzle growth per °C; `PIRO_TAP_THERMAL_APPLY TEMP=` sets the Z offset once before a print |
| Page | Probe tab: every touch with its curve, and the nozzle growth chart |

Install from the repo folder: `bash install.sh --probe` (probe only) or `--both` (with the PA calibrator).
Then read `piro_tap.cfg` in your config folder.

Needs Klipper v0.13.0-496 or newer on the Pi and on the board the Eddy is wired to, and numpy in Klipper's
Python (the installer adds it).

The files `klipper/piro_tap/kalico_*.py` are copied from [Kalico](https://github.com/KalicoCrew/kalico)
(GPLv3, © Gareth Farrington and the Klipper authors); only their imports are changed. Each file says what it was
copied from.

Wiki: [PIRO-TAP probe](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/PIRO-TAP-probe)

| Folder | |
|---|---|
| `klipper/piro_tap/` | The Klipper module (linked to `klippy/extras/piro_tap`) |
| `piro_tap.cfg` | Config template (copied to your config folder) |
| `tests/` | `KLIPPER_DIR=~/klipper python3 piro_tap/tests/test_piro_tap.py` |
| `../shared/piro_eddy.py` | The Eddy sample hub, shared with the PA calibrator |
