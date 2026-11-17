# PIRO-LC

Z homing, paper offset and nozzle growth for Klipper's own `[load_cell_probe]`. Klipper does the probing (tare,
MCU trigger, pullback fit); PIRO-LC only decides how `G28 Z` touches, where Z=0 ends up, and how much the
nozzle grows. Nothing in Klipper is patched.

| Part | How |
|---|---|
| Homing | Klipper's own `G28 Z` (`endstop_pin: probe:z_virtual_endstop`): touch 1 at 10 mm/s, touch 2 at 2 mm/s, 2 taps each. Every tap is Klipper's tap with its pullback fit. The taps of a touch must agree within 0.01 mm (with retries); the median sets Z. Z is marked homed and the homing events are Klipper's own, so `homing_override`, Z tilt, bed mesh and KAMP work unchanged |
| Paper offset | `PIRO_LC_CALIBRATE`: home Z, then lower with TESTZ until the paper drags, ACCEPT. Offset = paper touch - probed Z - paper (0.08 mm, changeable live). Added at every homing, so `SET_GCODE_OFFSET Z=0` can't remove it. `z_offset` stays 0 |
| Nozzle growth | Every Z homing saves the nozzle temperature (only the latest). `NOZZLE_GROWTH_APPLY TEMP=` in PRINT_START: offset = `nozzle_growth` × (first layer temp - homing temp), set before heating. A new homing clears it |
| Tests | Started from the page only (`PIRO_LC_TEST`): accuracy, homing repeat, bed mesh, Z tilt. Every tap is saved with time, temperatures and peak force. Prints and macros are never recorded |
| Safety | Errors stop the command, never Klipper. A paper offset over 1 mm or a growth offset over 0.3 mm is refused. Files are written on a worker thread |

Setup and the PRINT_START order: [`piro_lc.cfg`](piro_lc.cfg). Wiki: [PIRO-LC load cell homing](https://github.com/VcikeyTheCoder/piro-air-pa-calibrator/wiki/PIRO-LC-load-cell-homing)

| Folder | |
|---|---|
| `klipper/piro_lc/` | The Klipper module (linked to `klippy/extras/piro_lc`) |
| `piro_lc.cfg` | Config template (copied to your config folder) |
| `tests/` | `python3 loadcell/tests/test_piro_lc.py`, with `KLIPPER_DIR=~/klipper` also a batch print through real Klipper |
