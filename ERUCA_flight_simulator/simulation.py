"""Local RocketPy trajectory prototype; all default vehicle data are fictional.

Coordinates: east/north/up in metres relative to the launch site's flat ground.
Altitude ASL is RocketPy z; AGL is z - site_elevation_m. Quaternions are scalar
first (q0, q1, q2, q3), body to ENU. Accelerations are kinematic ENU acceleration,
NOT accelerometer measurements. No sensor fusion, telemetry or firing hardware.

Parachute commands use prescribed true-state conditions, not a tested flight
computer algorithm. The lag is an ideal delay followed by full parachute drag;
inflation loads, canopy entanglement, ejection mechanics and terrain are absent.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
import warnings
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class SimulationConfig:
    label: str = "Fictional demonstration vehicle — replace with measured data"
    rocket_mass_kg: float = 4.0  # vehicle excluding the entire motor
    rocket_radius_m: float = 0.045
    rocket_length_m: float = 1.5
    rocket_inertia_kg_m2: list[float] = field(default_factory=lambda: [0.65, 0.65, 0.007])
    cg_from_tail_m: float = 0.82
    drag_coefficient: float = 0.55
    motor_dry_mass_kg: float = 0.4
    propellant_mass_kg: float = 0.3
    # Original synthetic example, not a certified or measured motor curve.
    thrust_curve: list[list[float]] = field(default_factory=lambda: [
        [0.0, 0.0], [0.08, 300.0], [0.3, 270.0], [1.6, 250.0], [1.8, 0.0]
    ])
    thrust_scale: float = 1.0
    wind_east_mps: float = 2.0
    wind_north_mps: float = 0.0
    launch_elevation_deg: float = 85.0  # angle above horizontal
    heading_deg: float = 0.0  # clockwise from north
    rail_length_m: float = 3.0
    latitude_deg: float = 37.0  # arbitrary example; not a recommended launch site
    longitude_deg: float = 127.0
    site_elevation_m: float = 100.0
    parachute_cd_s_m2: float = 1.2
    trigger_mode: str = "apogee"
    trigger_altitude_m: float = 100.0  # AGL, downward crossing after reaching it
    trigger_time_s: float = 8.0  # elapsed simulation time from ignition at t=0
    deployment_lag_s: float = 0.5
    trigger_sampling_hz: float = 100.0
    max_time_s: float = 300.0
    max_time_step_s: float = 0.05

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "SimulationConfig":
        if not isinstance(values, dict):
            raise ValueError("config must be a JSON object")
        names = {item.name for item in fields(cls)}
        unknown = set(values) - names
        if unknown:
            raise ValueError("Unknown configuration key(s): " + ", ".join(sorted(unknown)))
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        if not isinstance(self.label, str):
            raise ValueError("label must be text")
        if not isinstance(self.trigger_mode, str) or self.trigger_mode not in {"apogee", "descending_altitude", "time"}:
            raise ValueError("trigger_mode must be apogee, descending_altitude, or time")
        special = {"label", "trigger_mode", "rocket_inertia_kg_m2", "thrust_curve"}
        for item in fields(self):
            if item.name in special:
                continue
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{item.name} must be a finite number")
        positive = [
            "rocket_mass_kg", "rocket_radius_m", "rocket_length_m", "drag_coefficient",
            "motor_dry_mass_kg", "propellant_mass_kg", "thrust_scale", "rail_length_m",
            "parachute_cd_s_m2", "trigger_altitude_m", "trigger_time_s", "trigger_sampling_hz",
            "max_time_s", "max_time_step_s",
        ]
        for name in positive:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if not 0 < self.launch_elevation_deg <= 90:
            raise ValueError("launch_elevation_deg must be in (0, 90]")
        if not 0 <= self.heading_deg < 360:
            raise ValueError("heading_deg must be in [0, 360)")
        if not -85 <= self.latitude_deg <= 85 or not -180 <= self.longitude_deg <= 180:
            raise ValueError("latitude must be in [-85, 85], longitude in [-180, 180]")
        if not -400 <= self.site_elevation_m <= 5000:
            raise ValueError("site_elevation_m must be in [-400, 5000] for this prototype")
        if not 0 < self.cg_from_tail_m < self.rocket_length_m:
            raise ValueError("cg_from_tail_m must lie inside the rocket")
        if self.rocket_length_m <= 8 * self.rocket_radius_m:
            raise ValueError("The example geometry requires length > 8 × body radius")
        if self.rail_length_m <= self.rocket_length_m * 0.55:
            raise ValueError("rail_length_m must exceed the upper rail-button position (0.55 × rocket length)")
        if not 0 <= self.deployment_lag_s <= 120:
            raise ValueError("deployment_lag_s must be in [0, 120]")
        if self.trigger_sampling_hz > 500 or self.max_time_s > 1800:
            raise ValueError("Prototype limits: trigger_sampling_hz <= 500, max_time_s <= 1800")
        if not 0.001 <= self.max_time_step_s <= 0.2:
            raise ValueError("max_time_step_s must be in [0.001, 0.2]")
        if not isinstance(self.rocket_inertia_kg_m2, (list, tuple)) or len(self.rocket_inertia_kg_m2) != 3:
            raise ValueError("rocket_inertia_kg_m2 must contain three positive diagonal moments")
        for moment in self.rocket_inertia_kg_m2:
            if isinstance(moment, bool) or not isinstance(moment, (int, float)) or not math.isfinite(moment) or moment <= 0:
                raise ValueError("Inertia moments must be finite and positive")
        if not isinstance(self.thrust_curve, (list, tuple)) or len(self.thrust_curve) < 3:
            raise ValueError("thrust_curve requires at least three [time_s, thrust_N] points")
        previous = -1.0
        for point in self.thrust_curve:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("Each thrust_curve point must be [time_s, thrust_N]")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point):
                raise ValueError("Thrust curve points must be finite numbers")
            time, thrust = point
            if time <= previous or thrust < 0:
                raise ValueError("Curve times must strictly increase, thrust must be nonnegative")
            previous = time
        if self.thrust_curve[0] != [0.0, 0.0] and tuple(self.thrust_curve[0]) != (0.0, 0.0):
            raise ValueError("The thrust curve must start at [0, 0]")
        if self.thrust_curve[-1][1] != 0 or max(p[1] for p in self.thrust_curve) <= 0:
            raise ValueError("The thrust curve must end at zero and contain positive thrust")
        if self.thrust_curve[-1][0] > 60:
            raise ValueError("Prototype burn duration must be <= 60 s")
        initial_mass = self.rocket_mass_kg + self.motor_dry_mass_kg + self.propellant_mass_kg
        if max(p[1] for p in self.thrust_curve) * self.thrust_scale / initial_mass > 1000:
            raise ValueError("Peak thrust / initial mass exceeds this prototype's 1000 m/s² input limit")


@dataclass
class SimulationResult:
    samples: list[dict[str, Any]]
    events: list[dict[str, Any]]
    summary: dict[str, Any]
    config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_config() -> dict[str, Any]:
    """Return a fresh, editable JSON-compatible configuration."""
    return asdict(SimulationConfig())


def _latlon(east: float, north: float, config: SimulationConfig) -> tuple[float, float]:
    """Local tangent-plane approximation; not navigation or mapping accuracy."""
    radius = 6_371_000.0
    lat = config.latitude_deg + math.degrees(north / radius)
    lon = config.longitude_deg + math.degrees(east / (radius * math.cos(math.radians(config.latitude_deg))))
    return lat, (lon + 180) % 360 - 180


def simulate(config: dict[str, Any], dt: float = 0.1) -> SimulationResult:
    """Run one deterministic, offline RocketPy 6DOF / parachute-descent case.

    dt only sets export/replay spacing. ODE tolerances and trigger sampling are
    independent. Events are inserted at their exact reported times in samples.
    Landing/full-deployment events are omitted when not reached before cutoff.
    """
    cfg = SimulationConfig.from_dict(config)
    if isinstance(dt, bool) or not isinstance(dt, (int, float)) or not math.isfinite(dt) or not 0.01 <= dt <= 5:
        raise ValueError("dt must be a finite number in [0.01, 5] seconds")
    if cfg.max_time_s / dt > 100_000:
        raise ValueError("Too many export samples; increase dt or reduce max_time_s")
    # RocketPy imports plotting dependencies even for numerical runs. A writable
    # temporary cache also works in restricted accounts; honor user overrides.
    cache = Path(tempfile.gettempdir()) / "eruca-flight-simulator-cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache / "matplotlib"))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache))
    os.environ.setdefault("MPLBACKEND", "Agg")
    try:
        from rocketpy import Environment, Flight, GenericMotor, Rocket
    except ImportError as error:
        raise RuntimeError("Install requirements first: python -m pip install rocketpy==1.13.0") from error

    env = Environment(latitude=cfg.latitude_deg, longitude=cfg.longitude_deg, elevation=cfg.site_elevation_m)
    # Compare against the lowest possible mass, after all propellant is gone.
    # This conservative bound never rejects a case solely because its initial
    # thrust-to-weight ratio is low: liftoff later in the burn remains possible.
    dry_weight_along_rail = (cfg.rocket_mass_kg + cfg.motor_dry_mass_kg) * float(env.gravity(cfg.site_elevation_m)) * math.sin(math.radians(cfg.launch_elevation_deg))
    if max(point[1] for point in cfg.thrust_curve) * cfg.thrust_scale <= dry_weight_along_rail:
        raise ValueError("Liftoff is impossible: even peak thrust cannot overcome dry-vehicle weight along the rail")
    # Standard pressure/temperature plus explicit constant wind: no forecast API.
    env.set_atmospheric_model(type="custom_atmosphere", wind_u=cfg.wind_east_mps, wind_v=cfg.wind_north_mps)
    length = cfg.rocket_length_m
    radius = cfg.rocket_radius_m
    motor = GenericMotor(
        thrust_source=[[float(t), float(f * cfg.thrust_scale)] for t, f in cfg.thrust_curve],
        burn_time=float(cfg.thrust_curve[-1][0]),
        chamber_radius=radius * 0.65,
        chamber_height=length * 0.21,
        chamber_position=length * 0.115,
        propellant_initial_mass=cfg.propellant_mass_kg,
        nozzle_radius=radius * 0.30,
        dry_mass=cfg.motor_dry_mass_kg,
        center_of_dry_mass_position=length * 0.115,
        dry_inertia=(0.004, 0.004, 0.0003),
        nozzle_position=0,
        coordinate_system_orientation="nozzle_to_combustion_chamber",
        interpolation_method="linear",
    )
    rocket = Rocket(
        radius=radius, mass=cfg.rocket_mass_kg,
        inertia=tuple(cfg.rocket_inertia_kg_m2),
        power_off_drag=cfg.drag_coefficient, power_on_drag=cfg.drag_coefficient,
        center_of_mass_without_motor=cfg.cg_from_tail_m,
        coordinate_system_orientation="tail_to_nose",
    )
    rocket.add_motor(motor, position=0)
    rocket.add_nose(length=length * 0.20, kind="vonKarman", position=length)
    rocket.add_trapezoidal_fins(
        n=4, root_chord=length * 0.16, tip_chord=length * 0.07,
        span=radius * 2.0, position=length * 0.24,
        sweep_length=length * 0.08, cant_angle=0,
    )
    rocket.set_rail_buttons(upper_button_position=length * 0.55, lower_button_position=length * 0.12)

    holder: dict[str, Any] = {}
    has_reached_altitude = False
    recorded_command_time: float | None = None

    def prescribed_trigger(pressure: float, height: float, state: list[float]) -> bool:
        nonlocal has_reached_altitude, recorded_command_time
        # Flight appends this public signal before evaluating the trigger. It
        # supplies an actual simulation clock, avoiding sample-count estimates.
        chute = holder["parachute"]
        time = float(chute.clean_pressure_signal[-1][0])
        if recorded_command_time is not None or time > cfg.max_time_s:
            return False
        altitude = float(state[2]) - cfg.site_elevation_m
        if cfg.trigger_mode == "time":
            condition = time + 1e-10 >= cfg.trigger_time_s
        elif cfg.trigger_mode == "apogee":
            condition = altitude > 0 and state[5] < 0
        else:
            has_reached_altitude = has_reached_altitude or altitude >= cfg.trigger_altitude_m
            condition = has_reached_altitude and state[5] < 0 and altitude <= cfg.trigger_altitude_m
        if not condition:
            return False
        recorded_command_time = time
        # RocketPy 1.13 can fail when asked to insert an inflation phase beyond
        # max_time. Record that command but do not schedule a future force change
        # outside this run. Up to the cutoff the undeployed dynamics are identical.
        return time + cfg.deployment_lag_s < cfg.max_time_s

    rocket.add_parachute(
        name="Example recovery parachute", cd_s=cfg.parachute_cd_s_m2,
        trigger=prescribed_trigger, sampling_rate=cfg.trigger_sampling_hz,
        lag=cfg.deployment_lag_s, noise=(0, 0, 0),
    )
    holder["parachute"] = rocket.parachutes[-1]
    # Use the documented RK45 solver: LSODA can take extremely small steps near
    # axial/reverse flow around apogee with Barrowman surfaces in this example.
    # RocketPy passes min_step to all solvers; only its irrelevant-option warning
    # is filtered. Physics and numerical warnings remain visible.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="The following arguments have no effect for a chosen solver: `min_step`.",
            category=UserWarning, module="rocketpy.simulation.flight",
        )
        flight = Flight(
            rocket=rocket, environment=env, rail_length=cfg.rail_length_m,
            inclination=cfg.launch_elevation_deg, heading=cfg.heading_deg,
            max_time=cfg.max_time_s, max_time_step=cfg.max_time_step_s,
            rtol=1e-7, atol=1e-6, time_overshoot=True, verbose=False,
            ode_solver="RK45",
        )
    final_time = float(flight.t_final)
    command_time = float(flight.parachute_events[0][0]) if flight.parachute_events else recorded_command_time
    full_time = command_time + cfg.deployment_lag_s if command_time is not None else None
    deployed = full_time is not None and full_time <= final_time + 1e-8
    landed = len(flight.impact_state) > 1
    reached_apogee = len(flight.apogee_state) > 1

    def sample(time: float) -> dict[str, Any]:
        east, north, asl = float(flight.x(time)), float(flight.y(time)), float(flight.z(time))
        latitude, longitude = _latlon(east, north, cfg)
        ve, vn, vu = float(flight.vx(time)), float(flight.vy(time)), float(flight.vz(time))
        quat = [float(getattr(flight, f"e{i}")(time)) for i in range(4)]
        norm = math.sqrt(sum(q * q for q in quat))
        if norm > 0:
            quat = [q / norm for q in quat]
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, 1 - 2 * (quat[1]**2 + quat[2]**2)))))
        has_deployed = full_time is not None and time >= full_time - 1e-9
        phase = "parachute_descent" if has_deployed else (
            "deployment_delay" if command_time is not None and time >= command_time - 1e-9 else (
                "powered_ascent" if time <= cfg.thrust_curve[-1][0] else (
                    "coast_ascent" if vu >= 0 else "ballistic_descent"
                )
            )
        )
        return {
            "t_s": float(time), "east_m": east, "north_m": north,
            "altitude_agl_m": max(0.0, asl - cfg.site_elevation_m), "altitude_asl_m": asl,
            "latitude_deg": latitude, "longitude_deg": longitude,
            "v_east_mps": ve, "v_north_mps": vn, "v_up_mps": vu,
            "speed_mps": math.sqrt(ve * ve + vn * vn + vu * vu),
            **{f"q{i}": value for i, value in enumerate(quat)},
            "tilt_deg": tilt, "attitude_valid": not has_deployed,
            "a_east_mps2": float(flight.ax(time)), "a_north_mps2": float(flight.ay(time)),
            "a_up_mps2": float(flight.az(time)),
            "pressure_pa": float(env.pressure(asl)), "phase": phase,
        }

    events: list[dict[str, Any]] = []

    def event(kind: str, time: float, note: str) -> None:
        if 0 <= time <= final_time + 1e-8:
            events.append({"type": kind, "note": note, **sample(min(time, final_time))})

    event("launch", 0.0, "Simulation ignition/start at launch point; not measured liftoff")
    if flight.out_of_rail_time > 0:
        event("rail_exit", float(flight.out_of_rail_time), "RocketPy rail exit")
    event("burnout", float(cfg.thrust_curve[-1][0]), "End of prescribed synthetic thrust curve")
    if reached_apogee:
        event("apogee", float(flight.apogee_time), "True simulated vertical-velocity zero crossing")
    if command_time is not None:
        event("parachute_command", command_time, "Prescribed true-state trigger; not sensor algorithm output")
    if deployed:
        event("full_deployment", float(full_time), "Full drag begins after ideal delay; no inflation dynamics")
    if landed:
        event("landing", final_time, "Flat ground intersection at site elevation")
    events.sort(key=lambda row: row["t_s"])
    time_values = {0.0, final_time, *(row["t_s"] for row in events)}
    time_values.update(i * dt for i in range(int(final_time / dt) + 1))
    samples = [sample(time) for time in sorted(time_values) if time <= final_time]
    model_warnings = [
        "All default geometry, masses, drag and thrust are fictional example inputs.",
        "Uses ideal true-state triggers, not MS5611/WT901 processing or validated firmware.",
        "Constant horizontal wind, flat terrain, approximate lat/lon; no landing uncertainty estimate.",
        "GenericMotor is a rough lumped motor model; no measured ERUCA motor or aerodynamic calibration.",
        "Recovery descent does not preserve a validated 6DOF body-attitude model; attitude_valid becomes false.",
        "RocketPy includes Earth rotation/Coriolis; vertical launches can have small lateral drift in zero wind.",
        "Barrowman aerodynamic surfaces are not validated for reversed flow or tumbling near/after apogee.",
    ]
    if not landed:
        model_warnings.append("Ground was not reached before max_time_s; endpoint is not a landing prediction.")
    if command_time is None:
        model_warnings.append("Parachute trigger was not reached during this run.")
    elif not deployed:
        model_warnings.append("Parachute commanded, but full deployment was not reached before the run ended.")
    if command_time is not None and command_time < cfg.thrust_curve[-1][0]:
        model_warnings.append("Command occurs during thrust; powered recovery behavior is outside the validated scope.")
    last = samples[-1]
    summary = {
        "engine": "RocketPy", "engine_version": "1.13.0", "solver": "RK45", "model": "6DOF ascent / simplified parachute descent",
        "trigger_basis": "prescribed simulation truth; sensor algorithm not evaluated",
        "coordinate_system": "ENU; altitude_agl_m relative to flat launch-site ground",
        "duration_s": final_time, "landed": landed,
        "termination": "ground_impact" if landed else "max_time",
        "apogee_reached": reached_apogee,
        "apogee_agl_m": float(flight.apogee) - cfg.site_elevation_m if reached_apogee else None,
        "apogee_time_s": float(flight.apogee_time) if reached_apogee else None,
        "maximum_sampled_altitude_agl_m": max(row["altitude_agl_m"] for row in samples),
        "maximum_sampled_speed_mps": max(row["speed_mps"] for row in samples),
        "parachute_command_time_s": command_time,
        "full_deployment_time_s": float(full_time) if deployed else None,
        "deployment_status": "deployed" if deployed else ("pending_at_end" if command_time is not None else "not_triggered"),
        "landing_range_m": math.hypot(last["east_m"], last["north_m"]) if landed else None,
        "landing_speed_mps": last["speed_mps"] if landed else None,
        "initial_total_mass_kg": cfg.rocket_mass_kg + cfg.motor_dry_mass_kg + cfg.propellant_mass_kg,
        "output_step_s": float(dt), "trigger_interval_s": 1 / cfg.trigger_sampling_hz,
        "warnings": model_warnings,
    }
    return SimulationResult(samples=samples, events=events, summary=summary, config=asdict(cfg))


def save_json(result: SimulationResult, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return target


def save_csv(result: SimulationResult, path: str | Path, *, events: bool = False) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = result.events if events else result.samples
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        if rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="JSON configuration; uses fictional defaults if absent")
    parser.add_argument("--output", type=Path, default=Path("result.json"))
    parser.add_argument("--dt", type=float, default=0.1, help="Export/replay time spacing in seconds")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else default_config()
        result = simulate(config, args.dt)
        save_json(result, args.output)
        save_csv(result, args.output.with_suffix(".csv"))
        save_csv(result, args.output.with_name(args.output.stem + "_events.csv"), events=True)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2, f"Simulation failed: {error}\n")
    print(f"Saved {args.output} ({len(result.samples)} samples, {result.summary['termination']})")


if __name__ == "__main__":
    main()
