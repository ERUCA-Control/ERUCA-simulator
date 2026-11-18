"""Local RocketPy trajectory prototype with a requested geometric envelope.

Length, inner diameter and nozzle-section length are user-supplied dimensions.
Wall thickness, mass, mass distribution, aerodynamics and thrust remain examples.

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
    label: str = "ERUCA requested envelope — illustrative mass, wall, aerodynamics and thrust"
    rocket_mass_kg: float = 4.0  # vehicle excluding the entire motor
    rocket_length_m: float = 0.70  # overall envelope, including nozzle section
    rocket_inner_diameter_m: float = 0.08
    body_wall_thickness_m: float = 0.002  # illustrative, not a measured wall
    nozzle_length_m: float = 0.15  # geometric section; does not determine thrust
    rocket_inertia_kg_m2: list[float] | None = None
    cg_from_tail_m: float | None = None
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
    altitude_limit_m: float = 80.0  # AGL, ascending threshold for virtual command
    altitude_limit_enabled: bool = True
    deployment_lag_s: float = 0.5
    trigger_sampling_hz: float = 100.0
    max_time_s: float = 300.0
    max_time_step_s: float = 0.05

    @property
    def rocket_radius_m(self) -> float:
        """Outer aerodynamic radius, derived from inner diameter and wall."""
        return self.rocket_inner_diameter_m / 2 + self.body_wall_thickness_m

    @property
    def effective_cg_from_tail_m(self) -> float:
        return self.rocket_length_m / 2 if self.cg_from_tail_m is None else self.cg_from_tail_m

    @property
    def effective_inertia_kg_m2(self) -> tuple[float, float, float]:
        if self.rocket_inertia_kg_m2 is not None:
            return tuple(self.rocket_inertia_kg_m2)
        # Lumped vehicle-only mass, approximated as a uniform hollow cylinder
        # over the overall envelope. This is not a measured mass distribution.
        radial_sum = self.rocket_radius_m**2 + (self.rocket_inner_diameter_m / 2)**2
        transverse = self.rocket_mass_kg * (3 * radial_sum + self.rocket_length_m**2) / 12
        axial = self.rocket_mass_kg * radial_sum / 2
        return transverse, transverse, axial

    @property
    def geometry(self) -> dict[str, float]:
        forward_length = self.rocket_length_m - self.nozzle_length_m
        nose_length = forward_length * 0.20
        return {
            "total_length_m": self.rocket_length_m,
            "nozzle_length_m": self.nozzle_length_m,
            "body_including_nose_length_m": forward_length,
            "nose_length_m": nose_length,
            "cylindrical_body_length_m": forward_length - nose_length,
            "inner_diameter_m": self.rocket_inner_diameter_m,
            "wall_thickness_m": self.body_wall_thickness_m,
            "outer_diameter_m": 2 * self.rocket_radius_m,
            "outer_radius_m": self.rocket_radius_m,
        }

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "SimulationConfig":
        if not isinstance(values, dict):
            raise ValueError("config must be a JSON object")
        values = dict(values)
        if "initial_total_mass_kg" in values:
            total = values.pop("initial_total_mass_kg")
            vehicle = vehicle_mass_from_total(
                total, values.get("motor_dry_mass_kg", cls.motor_dry_mass_kg),
                values.get("propellant_mass_kg", cls.propellant_mass_kg),
            )
            if "rocket_mass_kg" in values:
                supplied = values["rocket_mass_kg"]
                if isinstance(supplied, bool) or not isinstance(supplied, (int, float)) or not math.isfinite(supplied) or not math.isclose(supplied, vehicle, rel_tol=1e-9, abs_tol=1e-9):
                    raise ValueError("전체 질량과 모터 제외 기체 질량 입력이 서로 일치하지 않습니다.")
            values["rocket_mass_kg"] = vehicle
        # Legacy rocket_radius_m was an OUTER radius. Preserve that dimension
        # when importing old files rather than silently interpreting it as ID.
        if "rocket_radius_m" in values:
            legacy_radius = values.pop("rocket_radius_m")
            wall = values.get("body_wall_thickness_m", cls.body_wall_thickness_m)
            for key, value in (("rocket_radius_m", legacy_radius), ("body_wall_thickness_m", wall)):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{key} must be a finite number")
            migrated_inner = 2 * (legacy_radius - wall)
            if "rocket_inner_diameter_m" in values:
                supplied_inner = values["rocket_inner_diameter_m"]
                if isinstance(supplied_inner, bool) or not isinstance(supplied_inner, (int, float)) or not math.isfinite(supplied_inner):
                    raise ValueError("rocket_inner_diameter_m must be a finite number")
                if not math.isclose(supplied_inner, migrated_inner, rel_tol=1e-9, abs_tol=1e-9):
                    raise ValueError("Legacy rocket_radius_m conflicts with inner diameter plus wall thickness")
            else:
                values["rocket_inner_diameter_m"] = migrated_inner
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
        if not isinstance(self.altitude_limit_enabled, bool):
            raise ValueError("altitude_limit_enabled must be true or false")
        special = {"label", "trigger_mode", "rocket_inertia_kg_m2", "cg_from_tail_m", "thrust_curve", "altitude_limit_enabled"}
        for item in fields(self):
            if item.name in special:
                continue
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{item.name} must be a finite number")
        positive = [
            "rocket_mass_kg", "rocket_inner_diameter_m", "rocket_length_m", "nozzle_length_m", "drag_coefficient",
            "motor_dry_mass_kg", "propellant_mass_kg", "thrust_scale", "rail_length_m",
            "parachute_cd_s_m2", "trigger_altitude_m", "trigger_time_s", "trigger_sampling_hz",
            "max_time_s", "max_time_step_s", "altitude_limit_m",
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
        if self.body_wall_thickness_m < 0:
            raise ValueError("body_wall_thickness_m must be nonnegative")
        if self.cg_from_tail_m is not None and (isinstance(self.cg_from_tail_m, bool) or not isinstance(self.cg_from_tail_m, (int, float)) or not math.isfinite(self.cg_from_tail_m)):
            raise ValueError("cg_from_tail_m must be null or a finite number")
        if not 0 < self.effective_cg_from_tail_m < self.rocket_length_m:
            raise ValueError("cg_from_tail_m must lie inside the rocket")
        if self.rocket_length_m <= 8 * self.rocket_radius_m:
            raise ValueError("The example geometry requires length > 8 × body radius")
        forward_length = self.rocket_length_m - self.nozzle_length_m
        if forward_length <= 4 * self.rocket_radius_m:
            raise ValueError("nozzle_length_m must leave body and nose length greater than 4 × outer radius")
        upper_button = self.nozzle_length_m + 0.55 * forward_length
        if self.rail_length_m <= upper_button:
            raise ValueError("rail_length_m must exceed the upper rail-button position")
        if not 0 <= self.deployment_lag_s <= 120:
            raise ValueError("deployment_lag_s must be in [0, 120]")
        if self.trigger_sampling_hz > 500 or self.max_time_s > 1800:
            raise ValueError("Prototype limits: trigger_sampling_hz <= 500, max_time_s <= 1800")
        if not 0.001 <= self.max_time_step_s <= 0.2:
            raise ValueError("max_time_step_s must be in [0.001, 0.2]")
        if self.rocket_inertia_kg_m2 is not None and (not isinstance(self.rocket_inertia_kg_m2, (list, tuple)) or len(self.rocket_inertia_kg_m2) != 3):
            raise ValueError("rocket_inertia_kg_m2 must be null or three positive diagonal moments")
        for moment in self.effective_inertia_kg_m2:
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


def vehicle_mass_from_total(total: float, motor_dry: float, propellant: float) -> float:
    """Convert the GUI's initial total mass to the engine's vehicle-only mass."""
    for label, value in (("전체 질량", total), ("모터 건조 질량", motor_dry), ("추진제 질량", propellant)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label}은 0보다 큰 유한한 kg 값이어야 합니다.")
    vehicle = total - motor_dry - propellant
    if vehicle <= 0:
        raise ValueError(
            f"전체 질량 {total:g} kg보다 모터({motor_dry:g} kg)와 추진제({propellant:g} kg)의 "
            f"합계 {motor_dry + propellant:g} kg이 크거나 같습니다. 실제 모터·추진제 질량을 입력하세요."
        )
    return vehicle


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
    forward_length = length - cfg.nozzle_length_m
    motor_center = cfg.nozzle_length_m + forward_length * 0.115
    motor = GenericMotor(
        thrust_source=[[float(t), float(f * cfg.thrust_scale)] for t, f in cfg.thrust_curve],
        burn_time=float(cfg.thrust_curve[-1][0]),
        chamber_radius=radius * 0.65,
        chamber_height=forward_length * 0.21,
        chamber_position=motor_center,
        propellant_initial_mass=cfg.propellant_mass_kg,
        nozzle_radius=radius * 0.30,
        dry_mass=cfg.motor_dry_mass_kg,
        center_of_dry_mass_position=motor_center,
        dry_inertia=(0.004, 0.004, 0.0003),
        nozzle_position=0,
        coordinate_system_orientation="nozzle_to_combustion_chamber",
        interpolation_method="linear",
    )
    rocket = Rocket(
        radius=radius, mass=cfg.rocket_mass_kg,
        inertia=cfg.effective_inertia_kg_m2,
        power_off_drag=cfg.drag_coefficient, power_on_drag=cfg.drag_coefficient,
        center_of_mass_without_motor=cfg.effective_cg_from_tail_m,
        coordinate_system_orientation="tail_to_nose",
    )
    rocket.add_motor(motor, position=0)
    rocket.add_nose(length=cfg.geometry["nose_length_m"], kind="vonKarman", position=length)
    rocket.add_trapezoidal_fins(
        n=4, root_chord=forward_length * 0.16, tip_chord=forward_length * 0.07,
        span=radius * 2.0, position=cfg.nozzle_length_m + forward_length * 0.24,
        sweep_length=forward_length * 0.08, cant_angle=0,
    )
    rocket.set_rail_buttons(
        upper_button_position=cfg.nozzle_length_m + forward_length * 0.55,
        lower_button_position=cfg.nozzle_length_m + forward_length * 0.12,
    )
    initial_static_margin = float(rocket.static_margin(0))
    if not math.isfinite(initial_static_margin) or initial_static_margin < 0:
        raise ValueError(
            f"설정한 질량 분포에서 시작 정적 안정 여유가 {initial_static_margin:.2f} cal입니다. "
            "현재 모델은 이 불안정·텀블링 조건을 지원하지 않습니다. "
            "전체/기체 질량 구분, 모터·추진제 질량과 무게중심·관성 제원을 확인하세요."
        )

    holder: dict[str, Any] = {}
    has_reached_altitude = False
    recorded_command_time: float | None = None
    command_reason: str | None = None

    def prescribed_trigger(pressure: float, height: float, state: list[float]) -> bool:
        nonlocal has_reached_altitude, recorded_command_time, command_reason
        # Flight appends this public signal before evaluating the trigger. It
        # supplies an actual simulation clock, avoiding sample-count estimates.
        chute = holder["parachute"]
        time = float(chute.clean_pressure_signal[-1][0])
        if recorded_command_time is not None or time > cfg.max_time_s:
            return False
        altitude = float(state[2]) - cfg.site_elevation_m
        if cfg.altitude_limit_enabled and altitude >= cfg.altitude_limit_m:
            condition = True
            reason = "altitude_limit"
        elif cfg.trigger_mode == "time":
            condition = time + 1e-10 >= cfg.trigger_time_s
            reason = cfg.trigger_mode
        elif cfg.trigger_mode == "apogee":
            condition = altitude > 0 and state[5] < 0
            reason = cfg.trigger_mode
        else:
            has_reached_altitude = has_reached_altitude or altitude >= cfg.trigger_altitude_m
            condition = has_reached_altitude and state[5] < 0 and altitude <= cfg.trigger_altitude_m
            reason = cfg.trigger_mode
        if not condition:
            return False
        recorded_command_time = time
        command_reason = reason
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
    powered_deployment_unsupported = bool(deployed and full_time < cfg.thrust_curve[-1][0])
    landed = len(flight.impact_state) > 1
    reached_apogee = len(flight.apogee_state) > 1
    # The parachute trigger is removed by RocketPy after it commands deployment.
    # Recover the first ascending limit crossing from the completed trajectory
    # too, so a prior time/altitude command cannot hide a later limit crossing.
    trajectory_times = {float(row[0]) for row in flight.solution if 0 <= float(row[0]) <= final_time}
    trajectory_times.update((0.0, final_time))
    if reached_apogee:
        trajectory_times.add(float(flight.apogee_time))
    trajectory_points = [(t, float(flight.z(t)) - cfg.site_elevation_m) for t in sorted(trajectory_times)]
    maximum_altitude_agl = max(0.0, *(height for _, height in trajectory_points))
    limit_crossing_time: float | None = None
    if cfg.altitude_limit_enabled:
        for (left, h_left), (right, h_right) in zip(trajectory_points, trajectory_points[1:]):
            if h_left < cfg.altitude_limit_m <= h_right:
                # Root on the exported RocketPy trajectory interpolation, not an
                # altitude clamp and not a claim of physical centimetre accuracy.
                for _ in range(45):
                    middle = (left + right) / 2
                    if float(flight.z(middle)) - cfg.site_elevation_m >= cfg.altitude_limit_m:
                        right = middle
                    else:
                        left = middle
                limit_crossing_time = (left + right) / 2
                break

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

    def event(kind: str, time: float, note: str, reason: str | None = None) -> None:
        if 0 <= time <= final_time + 1e-8:
            events.append({"type": kind, "note": note, "reason": reason, **sample(min(time, final_time))})

    event("launch", 0.0, "Simulation ignition/start at launch point; not measured liftoff")
    if flight.out_of_rail_time > 0:
        event("rail_exit", float(flight.out_of_rail_time), "RocketPy rail exit")
    event("burnout", float(cfg.thrust_curve[-1][0]), "End of prescribed synthetic thrust curve")
    if reached_apogee:
        event("apogee", float(flight.apogee_time), "True simulated vertical-velocity zero crossing")
    if limit_crossing_time is not None:
        event("altitude_limit_reached", limit_crossing_time, "First ascending AGL threshold crossing; simulation continues", "altitude_limit")
    if command_time is not None:
        event("parachute_command", command_time, "Prescribed true-state trigger; not sensor algorithm output", command_reason)
    if deployed:
        event("full_deployment", float(full_time), "Full drag begins after ideal delay; no inflation dynamics")
    if landed:
        event("landing", final_time, "Flat ground intersection at site elevation")
    events.sort(key=lambda row: row["t_s"])
    time_values = {0.0, final_time, *(row["t_s"] for row in events)}
    time_values.update(i * dt for i in range(int(final_time / dt) + 1))
    samples = [sample(time) for time in sorted(time_values) if time <= final_time]
    model_warnings = [
        "Overall length, inner diameter and nozzle-section length follow the requested envelope; wall, mass, aerodynamic and thrust values remain illustrative.",
        "The 2 mm default wall is an example used to convert inner diameter to outer aerodynamic diameter.",
        "Automatic CG/inertia use a uniform hollow-cylinder vehicle-envelope approximation; they are not measured mass properties.",
        "Nozzle-section length does not determine thrust, exit radius or an optimized nozzle; the existing motor parameters remain examples.",
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
    if powered_deployment_unsupported:
        model_warnings.append("Full deployment occurs before burnout: RocketPy's recovery phase is a 3DOF dry-mass model without continuing thrust or remaining propellant dynamics.")
    overshoot = max(0.0, maximum_altitude_agl - cfg.altitude_limit_m) if cfg.altitude_limit_enabled else 0.0
    if cfg.altitude_limit_enabled:
        model_warnings.append("Altitude limit is a sampled virtual parachute-command threshold, not a hard maximum; inertia and deployment delay can produce overshoot.")
    last = samples[-1]
    summary = {
        "engine": "RocketPy", "engine_version": "1.13.0", "solver": "RK45", "model": "6DOF ascent / simplified parachute descent",
        "trigger_basis": "prescribed simulation truth; sensor algorithm not evaluated",
        "coordinate_system": "ENU; altitude_agl_m relative to flat launch-site ground",
        "geometry": cfg.geometry,
        "effective_cg_from_tail_m": cfg.effective_cg_from_tail_m,
        "effective_inertia_kg_m2": list(cfg.effective_inertia_kg_m2),
        "cg_model": "uniform_envelope_approximation" if cfg.cg_from_tail_m is None else "explicit_input",
        "inertia_model": "uniform_hollow_cylinder_approximation" if cfg.rocket_inertia_kg_m2 is None else "explicit_input",
        "duration_s": final_time, "landed": landed,
        "termination": "ground_impact" if landed else "max_time",
        "apogee_reached": reached_apogee,
        "apogee_agl_m": float(flight.apogee) - cfg.site_elevation_m if reached_apogee else None,
        "apogee_time_s": float(flight.apogee_time) if reached_apogee else None,
        "maximum_sampled_altitude_agl_m": max(row["altitude_agl_m"] for row in samples),
        "maximum_altitude_agl_m": maximum_altitude_agl,
        "maximum_sampled_speed_mps": max(row["speed_mps"] for row in samples),
        "parachute_command_time_s": command_time,
        "command_reason": command_reason,
        "configured_trigger_mode": cfg.trigger_mode,
        "altitude_limit_enabled": cfg.altitude_limit_enabled,
        "altitude_limit_m": cfg.altitude_limit_m,
        "altitude_limit_reached": limit_crossing_time is not None,
        "altitude_limit_reached_time_s": limit_crossing_time,
        "altitude_limit_exceeded": overshoot > 1e-6,
        "altitude_limit_overshoot_m": overshoot,
        "full_deployment_time_s": float(full_time) if deployed else None,
        "deployment_status": "deployed" if deployed else ("pending_at_end" if command_time is not None else "not_triggered"),
        "powered_deployment_unsupported": powered_deployment_unsupported,
        "landing_range_m": math.hypot(last["east_m"], last["north_m"]) if landed else None,
        "landing_speed_mps": last["speed_mps"] if landed else None,
        "initial_total_mass_kg": cfg.rocket_mass_kg + cfg.motor_dry_mass_kg + cfg.propellant_mass_kg,
        "initial_static_margin_cal": initial_static_margin,
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
    parser.add_argument("--timeout", type=float, default=45, help="Wall-clock calculation limit in seconds (not flight time)")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else default_config()
        import time
        from calculation import CalculationJob
        job = CalculationJob(timeout_s=args.timeout)
        try:
            job.start(config, args.dt)
            response = None
            while response is None:
                response = job.poll()
                if response is None:
                    time.sleep(0.05)
            kind, result = response
            if kind == "error":
                raise RuntimeError(result)
        finally:
            job.cancel()
        save_json(result, args.output)
        save_csv(result, args.output.with_suffix(".csv"))
        save_csv(result, args.output.with_name(args.output.stem + "_events.csv"), events=True)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2, f"Simulation failed: {error}\n")
    print(f"Saved {args.output} ({len(result.samples)} samples, {result.summary['termination']})")


if __name__ == "__main__":
    main()
