# ---------------------------------------------------------------------------
# Copied 1:1 from Kalico (github.com/KalicoCrew/kalico, commit 648194e,
# klippy/extras/probe.py: the bad-probe retry policy, the nozzle scrubber and
# the retry state per point) for PIRO-TAP. Only the imports are changed.
# ---------------------------------------------------------------------------
# Kalico: Z-Probe support
#
# Copyright (C) 2017-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

GCodeCommand = ConfigWrapper = Printer = Template = object


class RetryStrategy(IntEnum):
    # Probe strategy constants
    FAIL = 0  # bad probes fail with a command error
    IGNORE = 1  # bad probes are ignored
    RETRY = 2  # bad probes are retried in the same location, assumes
    # no fouling
    CIRCLE = 3  # bad probes are retried in a circular pattern to avoid


MAX_CIRCLE_RETRIES: int = 6 * 3

@dataclass(frozen=True)
class RetryPolicy:
    retry_speed: float
    bad_probe_retries: int
    bad_probe_strategy: RetryStrategy
    pattern_spacing: float
    scrubber: GcodeNozzleScrubber


class RetryPolicyFactory:
    def __init__(self, config: ConfigWrapper):
        speed = config.getfloat("speed", 5.0, above=0.0)
        retry_speed = config.getfloat("retry_speed", speed, above=0.0)
        strategy = config.getchoice(
            "bad_probe_strategy",
            {strat.name.lower(): strat for strat in RetryStrategy},
            RetryStrategy.RETRY.name.lower(),
        )
        max_retries = None
        if strategy is RetryStrategy.CIRCLE:
            max_retries = MAX_CIRCLE_RETRIES
        self._defaults = RetryPolicy(
            retry_speed=retry_speed,
            bad_probe_retries=config.getint(
                "bad_probe_retries", 6, minval=0, maxval=max_retries
            ),
            bad_probe_strategy=strategy,
            pattern_spacing=config.getfloat("pattern_spacing", 2.0, above=0.0),
            scrubber=GcodeNozzleScrubber(config),
        )

    def customize(
        self,
        gcmd: GCodeCommand,
        strategy: Optional[RetryStrategy] = None,
    ) -> RetryPolicy:
        """Create a policy from the config values updated by a GCode command.
        A strategy override takes precedence over BAD_PROBE_STRATEGY."""
        defaults = self._defaults
        if strategy is None:
            strategy_name = gcmd.get(
                "BAD_PROBE_STRATEGY", defaults.bad_probe_strategy.name
            )
            strategy = RetryStrategy[strategy_name.upper()]
        max_retries = None
        if strategy is RetryStrategy.CIRCLE:
            max_retries = MAX_CIRCLE_RETRIES
        return RetryPolicy(
            retry_speed=gcmd.get_float(
                "RETRY_SPEED", defaults.retry_speed, above=0.0
            ),
            bad_probe_retries=gcmd.get_int(
                "BAD_PROBE_RETRIES",
                defaults.bad_probe_retries,
                minval=0,
                maxval=max_retries,
            ),
            bad_probe_strategy=strategy,
            pattern_spacing=gcmd.get_float(
                "PATTERN_SPACING", defaults.pattern_spacing, above=0.0
            ),
            scrubber=defaults.scrubber,
        )


class GcodeNozzleScrubberConfig:
    def __init__(self, config: ConfigWrapper):
        self.template: Template
        gcode_macro = config.get_printer().load_object(config, "gcode_macro")
        self.template = gcode_macro.load_template(
            config, "nozzle_scrubber_gcode", ""
        )
        self._cfg_scrubbing_frequency = config.getint(
            "scrubbing_frequency", 0, minval=0
        )

    def get_scrubbing_frequency(self, gcmd: GCodeCommand) -> int:
        return gcmd.get_int(
            "SCRUBBING_FREQUENCY", self._cfg_scrubbing_frequency, minval=0
        )


class GcodeNozzleScrubber:
    is_scrubbing: bool = False

    def __init__(
        self,
        config: ConfigWrapper,
    ):
        self._printer: Printer = config.get_printer()
        self.config = GcodeNozzleScrubberConfig(config)

    def clean_nozzle(self, gcmd: GCodeCommand, attempt, retries):
        if not self.config.template:
            return
        scrubbing_frequency = self.config.get_scrubbing_frequency(gcmd)
        if attempt == 0 or scrubbing_frequency <= 0:
            return
        if attempt % scrubbing_frequency != 0:
            return
        if GcodeNozzleScrubber.is_scrubbing:
            raise gcmd.error(
                "Nozzle Scrubber GCode called itself."
                " (Hint: Use BAD_PROBE_STRATEGY=IGNORE or FAIL)"
            )
        try:
            GcodeNozzleScrubber.is_scrubbing = True
            toolhead = self._printer.lookup_object("toolhead")
            start_pos = toolhead.get_position()
            context = self.config.template.create_template_context()
            context["params"] = {
                "ATTEMPT": attempt,
                "RETRIES": retries,
            }
            self.config.template.run_gcode_from_command(context)
            end_pos = toolhead.get_position()
            if not start_pos[:3] == end_pos[:3]:
                raise gcmd.error(
                    "Nozzle Scrubber GCode did not return to the start position. "
                    "(Hint: Use RESTORE_GCODE_STATE MOVE=1)"
                )
        finally:
            GcodeNozzleScrubber.is_scrubbing = False


class ProbeRetryState:
    @staticmethod
    def _build_hexagonal_offsets(
        distance_step: float,
    ) -> list[tuple[float, float]]:
        lookup = [(0.0, 0.0)]
        max_rings = 2
        size = distance_step / math.sqrt(3)
        # Hexagonal direction vectors in axial coordinates
        directions = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]
        for ring in range(1, max_rings + 1):
            # Start at ring steps in direction 4 (up-left)
            q, r = -ring, ring
            # Walk around the hexagon ring
            for direction in range(6):
                for step in range(ring):
                    # Convert axial to Cartesian
                    x = size * (math.sqrt(3) * q + math.sqrt(3) / 2 * r)
                    y = size * (3.0 / 2.0 * r)
                    lookup.append((x, y))
                    # Move to next hexagonal cell
                    dq, dr = directions[direction]
                    q, r = q + dq, r + dr
        return lookup

    def __init__(self, pos: tuple[float, float], retry_policy: RetryPolicy):
        self.retry_policy = retry_policy
        self._bad_probe_count = 0
        self._start_pos = pos
        self._fouled_count = 0
        # Build circular lookup based on pattern spacing
        self._circle_lookup = []
        logging.info(f"retry policy={self.retry_policy.bad_probe_strategy}")
        if self.retry_policy.bad_probe_strategy is RetryStrategy.CIRCLE:
            self._circle_lookup = self._build_hexagonal_offsets(
                retry_policy.pattern_spacing
            )

    def has_retries_remaining(self) -> bool:
        return self._bad_probe_count <= self.retry_policy.bad_probe_retries

    def get_attempt(self):
        return self._bad_probe_count

    def evaluate_probe(self, is_good: Optional[bool], gcmd) -> bool:
        """
        Evaluate probe result based on strategy.
        Returns True if probe should be accepted.
        Returns False if probe should be retried.
        Raises error if strategy is FAIL or retries exhausted.
        """
        if (
            is_good
            or self.retry_policy.bad_probe_strategy is RetryStrategy.IGNORE
        ):
            return True
        if self.retry_policy.bad_probe_strategy is RetryStrategy.FAIL:
            raise gcmd.error("Probe failed because it was deemed bad quality")
        self._bad_probe_count += 1
        if self.retry_policy.bad_probe_strategy is RetryStrategy.CIRCLE:
            self._fouled_count += 1
        if not self.has_retries_remaining():
            raise gcmd.error(
                f"Probing failed after {self._bad_probe_count} bad probes"
            )
        # Probe rejected, inform user and retry
        gcmd.respond_info(
            "Bad probe detected. Retrying "
            f"({self._bad_probe_count}/{self.retry_policy.bad_probe_retries})..."
        )
        return False

    def get_position(self) -> tuple[float, float, None]:
        if self.retry_policy.bad_probe_strategy is not RetryStrategy.CIRCLE:
            return self._start_pos[0], self._start_pos[1], None
        x, y = self._circle_lookup[self._fouled_count]
        return self._start_pos[0] + x, self._start_pos[1] + y, None

    def get_bad_probe_count(self):
        return self._bad_probe_count


class RetrySession:
    def __init__(self, retry_policy: RetryPolicy, gcmd: GCodeCommand):
        self.retry_policy = retry_policy
        self._gcmd: GCodeCommand = gcmd
        self._point_lookup: dict[tuple[float, float], ProbeRetryState] = {}
        self._pos: Optional[tuple[float, float]] = None
        self._quantized_pos: Optional[tuple[float, float]] = None
        self._retry_state: Optional[ProbeRetryState] = None

    @staticmethod
    def _quantize_position(pos: tuple[float, float]) -> tuple[float, float]:
        return round(pos[0], 1), round(pos[1], 1)

    def set_position(self, pos: list[float]) -> None:
        """Set the current ideal position being probed"""
        self._pos = (pos[0], pos[1])
        self._quantized_pos = self._quantize_position((pos[0], pos[1]))
        if self._quantized_pos not in self._point_lookup:
            self._point_lookup[self._quantized_pos] = ProbeRetryState(
                self._pos, self.retry_policy
            )
        self._retry_state = self._point_lookup[self._quantized_pos]

    def get_position(self) -> tuple[float, float]:
        return self._pos

    def get_probe_position(self) -> tuple[float, float, None]:
        """Get the actual probe position for the current 'ideal' position"""
        return self._retry_state.get_position()

    def can_retry(self) -> bool:
        return self._retry_state.has_retries_remaining()

    def get_bad_probe_count(self):
        return self._retry_state.get_bad_probe_count()

    def evaluate_probe(self, is_good: bool) -> bool:
        return self._retry_state.evaluate_probe(is_good, self._gcmd)

    def scrub_nozzle(self):
        """Scrub nozzle based on attempts at the current position"""
        self.retry_policy.scrubber.clean_nozzle(
            self._gcmd,
            self._retry_state.get_attempt(),
            self.retry_policy.bad_probe_retries,
        )
