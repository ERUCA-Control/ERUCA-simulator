"""Independent behavioral checks for the educational RocketPy flight viewer.

Run with the same Python environment that contains RocketPy::

    python -m unittest discover -s tests -v

Set ERUCA_SIMULATOR_DIR to test a different simulator directory.  The tests use
the supplied hypothetical vehicle; they do not represent measured flight data.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
import unittest
from functools import lru_cache


SIMULATOR_DIR = Path(
    os.environ.get(
        "ERUCA_SIMULATOR_DIR",
        Path(__file__).resolve().parents[1],
    )
)
sys.path.insert(0, str(SIMULATOR_DIR))

from simulation import default_config, simulate  # noqa: E402


@lru_cache(maxsize=20)
def cached_flight(config_json: str, dt: float):
    return simulate(json.loads(config_json), dt=dt)


class FlightBehaviorTests(unittest.TestCase):
    def flight(self, *, dt=0.1, **overrides):
        config = default_config()
        config.update(
            wind_east_mps=0.0,
            wind_north_mps=0.0,
            launch_elevation_deg=90.0,
            heading_deg=0.0,
            trigger_mode="apogee",
            deployment_lag_s=1.2,
            max_time_s=300.0,
        )
        config.update(overrides)
        return cached_flight(json.dumps(config, sort_keys=True), float(dt))

    def event(self, result, kind):
        matching = [event for event in result.events if event["type"] == kind]
        self.assertEqual(len(matching), 1, f"Expected one {kind!r}: {result.events}")
        return matching[0]

    def test_command_full_deployment_and_apogee_are_distinct_events(self):
        result = self.flight()
        apogee = self.event(result, "apogee")
        command = self.event(result, "parachute_command")
        opened = self.event(result, "full_deployment")
        landing = self.event(result, "landing")
        self.assertGreater(apogee["t_s"], self.event(result, "burnout")["t_s"])
        self.assertGreaterEqual(command["t_s"], apogee["t_s"] - 1e-6)
        self.assertLessEqual(command["t_s"] - apogee["t_s"], 0.03)
        self.assertAlmostEqual(opened["t_s"] - command["t_s"], 1.2, delta=0.025)
        self.assertLess(opened["altitude_agl_m"], command["altitude_agl_m"])
        self.assertGreater(landing["t_s"], opened["t_s"])
        self.assertEqual(result.summary["deployment_status"], "deployed")
        for event in (apogee, command, opened, landing):
            for field in (
                "east_m", "north_m", "altitude_agl_m", "altitude_asl_m",
                "latitude_deg", "longitude_deg",
            ):
                self.assertTrue(math.isfinite(event[field]), (event["type"], field))

    def test_vertical_no_wind_flight_returns_to_ground_after_launch(self):
        result = self.flight()
        apogee = self.event(result, "apogee")
        landing = self.event(result, "landing")
        self.assertGreater(apogee["altitude_agl_m"], 10.0)
        self.assertGreater(landing["t_s"], apogee["t_s"])
        self.assertAlmostEqual(landing["altitude_agl_m"], 0.0, delta=0.03)
        self.assertTrue(result.summary["landed"])
        self.assertAlmostEqual(result.samples[-1]["t_s"], landing["t_s"], delta=1e-5)
        self.assertGreater(len(result.samples), 10)
        # RocketPy retains Earth's rotation: a vertical flight can drift through
        # the Coriolis term.  The first-order displacement scale is
        # 2 * Omega * integral(height dt), rather than identically zero.
        height_integral = sum(
            0.5 * (a["altitude_agl_m"] + b["altitude_agl_m"]) * (b["t_s"] - a["t_s"])
            for a, b in zip(result.samples, result.samples[1:])
        )
        coriolis_scale = 2 * 7.292115e-5 * height_integral
        for sample in result.samples:
            self.assertLessEqual(
                math.hypot(sample["east_m"], sample["north_m"]),
                1.1 * coriolis_scale + 0.05,
            )
            self.assertGreaterEqual(sample["altitude_agl_m"], -0.03)

    def test_agl_asl_and_geographic_origin_have_explicit_reference(self):
        result = self.flight(
            site_elevation_m=725.0, latitude_deg=37.6, longitude_deg=126.9,
        )
        for point in [*result.samples, *result.events]:
            self.assertAlmostEqual(
                point["altitude_asl_m"] - point["altitude_agl_m"], 725.0,
                delta=1e-6,
            )
            # Check geographic directions and distance against an independent
            # local spherical approximation; the UI does not claim geodesy accuracy.
            north_from_lat = math.radians(point["latitude_deg"] - 37.6) * 6_378_137
            east_from_lon = (
                math.radians(point["longitude_deg"] - 126.9)
                * 6_378_137 * math.cos(math.radians(37.6))
            )
            self.assertAlmostEqual(
                north_from_lat, point["north_m"],
                delta=max(0.01, abs(point["north_m"]) * 0.005),
            )
            self.assertAlmostEqual(
                east_from_lon, point["east_m"],
                delta=max(0.01, abs(point["east_m"]) * 0.005),
            )
        launch = self.event(result, "launch")
        self.assertAlmostEqual(launch["t_s"], 0.0, delta=1e-9)
        self.assertAlmostEqual(launch["altitude_agl_m"], 0.0, delta=1e-6)
        self.assertAlmostEqual(launch["altitude_asl_m"], 725.0, delta=1e-6)
        self.assertAlmostEqual(launch["latitude_deg"], 37.6, delta=1e-9)
        self.assertAlmostEqual(launch["longitude_deg"], 126.9, delta=1e-9)

    def test_north_zero_east_ninety_heading_rotates_the_trajectory(self):
        north = self.flight(launch_elevation_deg=80.0, heading_deg=0.0)
        east = self.flight(launch_elevation_deg=80.0, heading_deg=90.0)
        north_apogee = self.event(north, "apogee")
        east_apogee = self.event(east, "apogee")
        self.assertGreater(north_apogee["north_m"], 1.0)
        self.assertGreater(east_apogee["east_m"], 1.0)
        # Earth's rotation breaks exact rotational symmetry. Bound its expected
        # transverse scale using a <= 2*Omega*v Coriolis acceleration estimate.
        transverse_scale = max(
            2 * 7.292115e-5 * apogee["t_s"] ** 2
            * max(sample["speed_mps"] for sample in result.samples if sample["t_s"] <= apogee["t_s"])
            for result, apogee in ((north, north_apogee), (east, east_apogee))
        )
        self.assertAlmostEqual(north_apogee["east_m"], 0.0, delta=transverse_scale)
        self.assertAlmostEqual(east_apogee["north_m"], 0.0, delta=transverse_scale)
        self.assertAlmostEqual(
            north_apogee["north_m"], east_apogee["east_m"],
            delta=max(0.1, transverse_scale),
        )
        self.assertGreater(north_apogee["latitude_deg"], north.config["latitude_deg"])
        self.assertGreater(east_apogee["longitude_deg"], east.config["longitude_deg"])

    def test_eastward_wind_produces_eastward_landing_drift(self):
        still = self.event(self.flight(), "landing")
        windy_result = self.flight(wind_east_mps=8.0)
        windy = self.event(windy_result, "landing")
        self.assertGreater(windy["east_m"], still["east_m"] + 1.0)
        self.assertGreater(windy["longitude_deg"], windy_result.config["longitude_deg"])

    def test_unreached_altitude_does_not_create_a_parachute_event(self):
        result = self.flight(
            trigger_mode="descending_altitude", trigger_altitude_m=100000.0,
        )
        self.assertLess(self.event(result, "apogee")["altitude_agl_m"], 100000.0)
        self.assertFalse(any(
            event["type"] in {"parachute_command", "full_deployment"}
            for event in result.events
        ))
        self.assertTrue(result.summary["landed"])
        self.assertEqual(result.summary["deployment_status"], "not_triggered")
        self.assertIsNone(result.summary["parachute_command_time_s"])
        self.assertIsNone(result.summary["full_deployment_time_s"])

    def test_time_limit_does_not_invent_landing_or_late_trigger(self):
        result = self.flight(
            max_time_s=0.5, trigger_mode="time", trigger_time_s=60.0,
        )
        self.assertFalse(result.summary["landed"])
        self.assertEqual(result.summary["termination"], "max_time")
        self.assertEqual(result.summary["deployment_status"], "not_triggered")
        self.assertFalse(any(
            event["type"] in {"landing", "parachute_command", "full_deployment"}
            for event in result.events
        ))
        self.assertAlmostEqual(result.samples[-1]["t_s"], 0.5, delta=1e-6)
        self.assertGreater(result.samples[-1]["altitude_agl_m"], 0.0)

        pending = self.flight(
            max_time_s=4.0, trigger_mode="time", trigger_time_s=3.0,
            deployment_lag_s=10.0,
        )
        self.assertAlmostEqual(self.event(pending, "parachute_command")["t_s"], 3.0, delta=0.011)
        self.assertEqual(pending.summary["deployment_status"], "pending_at_end")
        self.assertIsNone(pending.summary["full_deployment_time_s"])
        self.assertFalse(pending.summary["landed"])
        self.assertFalse(any(event["type"] == "full_deployment" for event in pending.events))
        self.assertAlmostEqual(pending.samples[-1]["t_s"], 4.0, delta=1e-6)

    def test_output_sampling_dt_does_not_change_flight_physics(self):
        fine = self.flight(dt=0.1, wind_east_mps=3.0)
        coarse = self.flight(dt=0.4, wind_east_mps=3.0)
        self.assertGreater(len(fine.samples), len(coarse.samples))
        self.assertEqual(
            [event["type"] for event in fine.events],
            [event["type"] for event in coarse.events],
        )
        for kind in ("apogee", "parachute_command", "full_deployment", "landing"):
            a, b = self.event(fine, kind), self.event(coarse, kind)
            self.assertAlmostEqual(a["t_s"], b["t_s"], delta=1e-6)
            for field in ("east_m", "north_m", "altitude_agl_m"):
                self.assertAlmostEqual(a[field], b[field], delta=1e-4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
