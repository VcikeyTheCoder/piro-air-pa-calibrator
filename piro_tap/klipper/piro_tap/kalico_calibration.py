# ---------------------------------------------------------------------------
# Copied 1:1 from Kalico (github.com/KalicoCrew/kalico, commit 648194e,
# klippy/extras/load_cell/load_cell_probe.py: PullbackDistanceCalibration,
# the first half of LOAD_CELL_PROBE_CALIBRATE CALIBRATION=TAP_PERFORMANCE)
# for PIRO-TAP. Changed: the imports, and mainline Klipper's bed_mesh has no
# generate_points(gcmd, name) and its ProbePointsHelper takes no use_offsets /
# enable_horizontal_z_clearance arguments, so the probe points come from
# generate_points() below (the mesh points of [bed_mesh]) and
# use_xy_offsets(True) is called; its finalize callback gets (results) only.
# Forces are in Hz, not grams.
# ---------------------------------------------------------------------------
# Load Cell Probe
#
# Copyright (C) 2025  Gareth Farrington <gareth@waves.ky>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import math
from typing import Optional

import numpy as np

try:
    from ..probe import ProbePointsHelper
except (ImportError, ValueError):       # tests outside Klipper
    ProbePointsHelper = None
from .kalico_compat import ConfigWrapper, GCodeCommand
from .kalico_tap_analysis import TapAnalysis

BedMesh = LoadCellProbeConfigHelper = object
Callable = object


def generate_points(bed_mesh, gcmd):
    """The probe points of [bed_mesh] (mainline Klipper)."""
    try:
        points = bed_mesh.bmc.probe_mgr.get_std_path()
    except AttributeError:
        points = None
    if not points:
        raise gcmd.error("bed_mesh has no probe points")
    return [tuple(p[:2]) for p in points]


class PullbackDistanceCalibration:
    def __init__(
        self,
        config: ConfigWrapper,
        config_helper: LoadCellProbeConfigHelper,
        register_tap_callback: Callable,
    ):
        self._config = config
        self._config_helper = config_helper
        self._register_tap_callback = register_tap_callback
        self._printer = config.get_printer()
        self._bed_mesh_config = None
        if config.has_section("bed_mesh"):
            self._bed_mesh_config = config.getsection("bed_mesh")
        self._calibrating = False
        self._gcmd: Optional[GCodeCommand] = None
        self._taps: list[TapAnalysis] = []
        self._distances: list[float] = []
        self._forces: list[float] = []

    @staticmethod
    def _decompression_distance(tap: TapAnalysis) -> tuple[float, float]:
        if not tap.is_valid():
            return math.nan, math.nan

        tap_points = tap.get_tap_points()
        decompression_start_z = tap.get_toolhead_position(tap_points[3].time)[2]
        decompression_start_force = tap_points[3].force
        break_contact_z = tap.get_toolhead_position(tap_points[4].time)[2]
        break_contact_force = tap_points[4].force
        return (
            abs(decompression_start_z - break_contact_z),
            abs(decompression_start_force - break_contact_force),
        )

    def _tap_callback(self, tap: TapAnalysis):
        if not self._calibrating or isinstance(tap, dict):
            return self._calibrating
        self._taps.append(tap)
        decomp_dist, decomp_force = self._decompression_distance(tap)
        if math.isnan(decomp_dist):
            raise self._gcmd.error(
                "Tap pullback distance could not be measured"
            )
        self._distances.append(decomp_dist)
        self._forces.append(decomp_force)
        self._gcmd.respond_info(
            f"Decompression distance: {self._distances[-1]:.3f}mm, force: "
            f"{self._forces[-1]:.0f}g"
        )
        return self._calibrating

    def _finalize_callback(self, probe_offsets, results):
        pass

    def calibrate(self, gcmd: GCodeCommand):
        self._gcmd = gcmd
        self._taps = []
        self._distances = []
        self._forces = []
        gcmd.respond_info("Optimizing pullback_distance...")
        bed_mesh: BedMesh = self._printer.lookup_object(
            "bed_mesh", default=None
        )
        if bed_mesh is None or self._bed_mesh_config is None:
            raise gcmd.error("bed_mesh not configured")

        original_pullback_distance = self._config_helper.get_pullback_distance()
        pullback_distance = gcmd.get_float(
            "PULLBACK_DISTANCE", default=1.0, minval=0.5, maxval=2.0
        )
        self._config_helper.set_pullback_distance(pullback_distance)

        try:
            self._calibrating: bool = True
            self._register_tap_callback(self._tap_callback)
            points = generate_points(bed_mesh, gcmd)
            points_helper = ProbePointsHelper(
                self._bed_mesh_config,
                lambda results: self._finalize_callback(None, results),
                points,
            )
            points_helper.use_xy_offsets(True)
            points_helper.start_probe(gcmd)
        finally:
            self._calibrating = False
            self._register_tap_callback(None)
            # restore state in case of an error
            self._config_helper.set_pullback_distance(
                original_pullback_distance
            )

        if not self._distances:
            raise gcmd.error("No taps were collected!")
        min_distance = min(self._distances)
        max_distance = max(self._distances)
        mean_distance = float(np.mean(self._distances))
        mean_force = float(np.mean(self._forces))
        distance_std = float(np.std(self._distances))
        pullback_distance = (mean_distance + (3.0 * distance_std)) * 2.0
        pullback_distance = round(pullback_distance, 2)
        deflection_per_kg = round((mean_distance / mean_force) * 1000.0, 3)

        self._config_helper.save_pullback_distance(pullback_distance)
        gcmd.respond_info(
            f"Decompression Distance: mean={mean_distance:.4f}mm, "
            f"min={min_distance:.4f}mm, max={max_distance:.4f}mm, "
            f"std={distance_std:.4f}mm\n"
            f"pullback_distance={pullback_distance:.2f}\n"
            f"deflection per kilogram={deflection_per_kg:.3f}mm\n"
            "This has been saved for the current session.\n"
            "The SAVE_CONFIG command will update the printer config file "
            "with the above and restart the printer."
        )
        return self._taps
