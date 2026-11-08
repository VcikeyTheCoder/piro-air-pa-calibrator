# PIRO-TAP

A Klipper Z probe that touches the bed with the nozzle. A BTT Eddy senses the nozzle pressure (the coil watches
the hotend mount flex), and everything is in **Hz**: the change of the Eddy frequency from a tare taken before
every touch.

It is a normal Klipper probe: `PROBE`, `PROBE_ACCURACY`, `G28` with `probe:z_virtual_endstop`,
`BED_MESH_CALIBRATE`, `Z_TILT_ADJUST`, `QUAD_GANTRY_LEVEL`, `SCREWS_TILT_CALCULATE`.

| Part | How |
|---|---|
| Contact | The Eddy's MCU filters every sample and stops Z when the pressure passes `trigger_hz` (no Pi delay). Past `force_limit_hz` (2000) the move stops with an error |
| Z | Slow pullback (4 samples per micron). Z = where the release line meets the free-air line. Kalico's break-contact checks: a touch whose contact breaks in the last quarter of the pullback is refused (`TAP_PULLBACK_TOO_SHORT`) |
| Trigger | `PIRO_TAP_CALIBRATE MODE=THRESHOLD`: tare, noise peak to peak, `trigger_hz` = 3 × noise |
| Homing | Klipper's own homing: 2 touches at `homing_speed` (5), then 2 at `second_homing_speed` (2). Z = average of the 2 slow touches |
| Ooze | Every touch is scored; bad touches are handled by Kalico's `bad_probe_strategy` (retry, circle, ignore, fail). `NOZZLE_CLEAN_CHECK` |
| Thermal | `PIRO_TAP_THERMAL_CALIBRATE START=0 END=200 STEP=50` fits nozzle growth per °C; `PIRO_TAP_THERMAL_APPLY TEMP=` sets the Z offset once before a print |
| Page | Probe tab: every touch with its curve, and the nozzle growth chart |

Install from the repo folder: `bash install.sh --probe` (probe only) or `--both` (with the PA calibrator).
Then read `piro_tap.cfg` in your config folder.

Needs Klipper v0.13.0-496 or newer on the Pi and on the board the Eddy is wired to.

`klipper/piro_tap/kalico_retry.py` is copied from [Kalico](https://github.com/KalicoCrew/kalico) (GPLv3); only its
imports are changed.

Wiki: [PIRO-TAP probe](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/PIRO-TAP-probe)

| Folder | |
|---|---|
| `klipper/piro_tap/` | The Klipper module (linked to `klippy/extras/piro_tap`) |
| `piro_tap.cfg` | Config template (copied to your config folder) |
| `tests/` | `KLIPPER_DIR=~/klipper python3 piro_tap/tests/test_piro_tap.py` |
| `../shared/piro_eddy.py` | The Eddy sample hub, shared with the PA calibrator |
