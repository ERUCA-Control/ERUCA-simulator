"""Offline desktop viewer for a hypothetical RocketPy flight."""
from __future__ import annotations

import argparse
import bisect
import json
import math
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from simulation import SimulationConfig, default_config, save_csv, save_json, simulate


BG = "#0d1521"
PANEL = "#162233"
FG = "#e4edf6"
MUTED = "#95a9bf"
CYAN = "#53d5d2"
ORANGE = "#ffb45e"
GRID = "#304155"
TRIGGERS = {"최고점 통과": "apogee", "하강 중 지정 고도": "descending_altitude", "발사 후 지정 시간": "time"}
EVENTS = {"launch": "발사 시작", "rail_exit": "레일 이탈", "burnout": "연소 종료", "apogee": "최고점", "parachute_command": "사출 명령", "full_deployment": "전개 반영(모델)", "landing": "착지"}
PHASES = {"powered_ascent": "추력 비행", "coast_ascent": "관성 상승", "ballistic_descent": "하강", "deployment_delay": "전개 지연", "parachute_descent": "낙하산 강하"}


class FlightApp(tk.Tk):
    def __init__(self, *, autorun=True):
        super().__init__()
        self.title("ERUCA · 로컬 비행 시뮬레이터")
        self.geometry("1440x940")
        self.minsize(1120, 800)
        self.configure(bg=BG)
        self.font = "Malgun Gothic" if sys.platform == "win32" else "Apple SD Gothic Neo" if sys.platform == "darwin" else "DejaVu Sans"
        self.option_add("*Font", (self.font, 11))
        self.base_config = default_config()
        self.result = None
        self.busy = False
        self.playing = False
        self.loading = True
        self.vars = {}
        self.entries = {}
        self.messages = queue.Queue()
        self.play_clock = 0.0
        self.last_tick = time.monotonic()
        self._style()
        self._build()
        self._set_config(self.base_config)
        self.loading = False
        self.after(100, self._poll)
        self.after(40, self._tick)
        if autorun:
            self.after(350, self.calculate)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=PANEL, foreground=FG, font=(self.font, 11))
        style.configure("TLabel", background=PANEL, foreground=FG)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("TFrame", background=PANEL)
        style.configure("TButton", padding=(9, 6), background="#263a50", foreground=FG)
        style.map("TButton", background=[("active", "#34526b")])
        style.configure("Primary.TButton", background="#247b7b", foreground="#ffffff")
        style.configure("TEntry", fieldbackground="#0f1a29", foreground=FG, insertcolor=FG, padding=4)
        style.configure("TCombobox", fieldbackground="#0f1a29", foreground=FG, arrowcolor=FG, padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", "#0f1a29")], foreground=[("readonly", FG)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(12, 7))
        style.map("TNotebook.Tab", background=[("selected", "#263a50")], foreground=[("selected", FG)])
        style.configure("Treeview", background="#111e2c", fieldbackground="#111e2c", foreground=FG, rowheight=27)
        style.configure("Treeview.Heading", background="#263a50", foreground=FG)
        style.map("Treeview", background=[("selected", "#245c67")])
        style.configure("Horizontal.TScale", background=PANEL, troughcolor="#263a50")

    def _build(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=22, pady=(16, 10))
        tk.Label(header, text="ERUCA", bg=BG, fg=CYAN, font=(self.font, 25, "bold")).pack(side="left")
        tk.Label(header, text="  비행 궤적 · 낙하산 사출 위치", bg=BG, fg=FG, font=(self.font, 17)).pack(side="left", padx=12)
        tk.Label(header, text="LOCAL  /  ROCKETPY", bg=BG, fg=MUTED, font=(self.font, 10)).pack(side="right")
        self.notice = tk.Label(self, text="예제 기체 · 질량, 형상, 추력곡선은 가상 입력값입니다.", bg="#3b2d1f", fg="#ffd69a", anchor="w", padx=14, pady=7)
        self.notice.pack(fill="x", padx=22, pady=(0, 12))
        body = tk.PanedWindow(self, orient="horizontal", bg=BG, sashwidth=10, borderwidth=0)
        body.pack(fill="both", expand=True, padx=22, pady=(0, 14))
        left = ttk.Frame(body, width=330)
        body.add(left, minsize=300, width=330)
        right = ttk.Frame(body)
        body.add(right, minsize=760, stretch="always")

        tabs = ttk.Notebook(left)
        tabs.pack(fill="both", expand=True, padx=10, pady=10)
        form = ttk.Frame(tabs)
        advanced = ttk.Frame(tabs)
        tabs.add(form, text="비행 설정")
        tabs.add(advanced, text="전체 제원 JSON")
        form.columnconfigure(1, weight=1)
        row = 0
        sections = [
            ("발사 · 기체", [("rocket_mass_kg", "기체 질량, 모터 제외 (kg)"), ("thrust_scale", "추력곡선 배율"), ("launch_elevation_deg", "발사 고각, 수직 90°"), ("heading_deg", "방위각, 북 0° / 동 90°")]),
            ("바람", [("wind_east_mps", "동쪽으로 부는 바람 (m/s)"), ("wind_north_mps", "북쪽으로 부는 바람 (m/s)")]),
        ]
        for title, rows in sections:
            ttk.Label(form, text=title, foreground=CYAN, font=(self.font, 11, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(9, 4))
            row += 1
            for key, label in rows:
                self._field(form, row, key, label)
                row += 1
        ttk.Label(form, text="낙하산", foreground=ORANGE, font=(self.font, 11, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 5))
        row += 1
        ttk.Label(form, text="사출 조건").grid(row=row, column=0, sticky="w", pady=3)
        self.trigger = tk.StringVar()
        selector = ttk.Combobox(form, textvariable=self.trigger, values=list(TRIGGERS), state="readonly", width=17)
        selector.grid(row=row, column=1, sticky="ew", padx=(6, 0), pady=3)
        selector.bind("<<ComboboxSelected>>", self._trigger_changed)
        row += 1
        for key, label in [("trigger_altitude_m", "하강 사출 고도 AGL (m)"), ("trigger_time_s", "발사 후 사출 시간 (s)"), ("deployment_lag_s", "명령 → 전개 반영 지연 (s)"), ("parachute_cd_s_m2", "낙하산 Cd × 면적 (m²)"), ("max_time_s", "계산 종료 시간 (s)")]:
            self._field(form, row, key, label)
            row += 1
        ttk.Label(form, text="발사점 위경도·형상·관성·추력곡선은\n전체 제원 JSON에서 수정할 수 있습니다.", style="Muted.TLabel", wraplength=285, justify="left").grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 5))
        row += 1
        ttk.Label(form, text="명령 좌표와 전개 반영 좌표를 구분합니다.\n사출 조건은 모델의 고도·시간을 사용합니다.", style="Muted.TLabel", wraplength=285, justify="left").grid(row=row, column=0, columnspan=2, sticky="w", pady=5)

        self.config_text = tk.Text(advanced, bg="#0f1a29", fg=FG, insertbackground=FG, font=("Menlo" if sys.platform == "darwin" else "Consolas", 10), wrap="none", undo=True, borderwidth=0)
        self.config_text.pack(fill="both", expand=True, padx=3, pady=8)
        ttk.Button(advanced, text="JSON 내용을 설정에 적용", command=self.apply_json).pack(fill="x", pady=(0, 5))
        ttk.Label(advanced, text="편집 후 적용 버튼을 누르세요.", style="Muted.TLabel").pack(anchor="w")

        controls = ttk.Frame(left)
        controls.pack(fill="x", padx=10, pady=(2, 10))
        self.run_button = ttk.Button(controls, text="비행 계산", style="Primary.TButton", command=self.calculate)
        self.run_button.pack(fill="x", pady=4)
        files = ttk.Frame(controls)
        files.pack(fill="x", pady=4)
        ttk.Button(files, text="제원 열기", command=self.open_config).pack(side="left", fill="x", expand=True)
        ttk.Button(files, text="제원 저장", command=self.save_config).pack(side="left", fill="x", expand=True, padx=(5, 0))
        files2 = ttk.Frame(controls)
        files2.pack(fill="x", pady=4)
        ttk.Button(files2, text="예제 초기화", command=lambda: self._set_config(default_config())).pack(side="left", fill="x", expand=True)
        self.export_button = ttk.Button(files2, text="결과 내보내기", command=self.export_result, state="disabled")
        self.export_button.pack(side="left", fill="x", expand=True, padx=(5, 0))

        metrics = ttk.Frame(right)
        metrics.pack(fill="x", padx=12, pady=(12, 4))
        self.metrics = []
        for title in ["최고 고도 AGL", "사출 명령", "전개 반영(모델)", "착지 수평 거리"]:
            cell = tk.Frame(metrics, bg="#1c2d41", padx=12, pady=8)
            cell.pack(side="left", expand=True, fill="both", padx=3)
            tk.Label(cell, text=title, bg="#1c2d41", fg=MUTED, font=(self.font, 10)).pack(anchor="w")
            value = tk.Label(cell, text="—", bg="#1c2d41", fg=FG, font=(self.font, 17, "bold"))
            value.pack(anchor="w", pady=(4, 0))
            self.metrics.append(value)
        self.status = tk.StringVar(value="비행 계산을 준비합니다.")
        ttk.Label(right, textvariable=self.status, style="Muted.TLabel").pack(anchor="w", padx=16, pady=(5, 0))

        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        self.figure = Figure(figsize=(10, 5.5), dpi=100, facecolor=PANEL)
        self.chart = FigureCanvasTkAgg(self.figure, master=right)
        self.chart.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=4)
        self.figure.text(0.5, 0.5, "Calculate a flight to see the trajectory", ha="center", color=MUTED, fontsize=13)
        self.chart.draw()
        self.ax3 = None
        self.cursor_artists = []
        self.index = 0

        playback = ttk.Frame(right)
        playback.pack(fill="x", padx=14, pady=(0, 4))
        self.play_button = ttk.Button(playback, text="▶ 재생", command=self.toggle_play)
        self.play_button.pack(side="left")
        self.replay_speed = tk.StringVar(value="1")
        ttk.Combobox(playback, textvariable=self.replay_speed, values=["0.25", "1", "2", "5", "10"], state="readonly", width=5).pack(side="left", padx=(8, 1))
        ttk.Label(playback, text="배속").pack(side="left", padx=(0, 8))
        self.slider_value = tk.DoubleVar(value=0)
        self.slider = ttk.Scale(playback, from_=0, to=1, variable=self.slider_value, command=self.seek)
        self.slider.pack(side="left", fill="x", expand=True, padx=8)
        self.clock_label = ttk.Label(playback, text="T+0.00 s", width=12)
        self.clock_label.pack(side="right")
        self.live = tk.StringVar(value="발사점 기준 동(E)·북(N)·고도(AGL) · 단위 m")
        ttk.Label(right, textvariable=self.live, style="Muted.TLabel").pack(anchor="w", padx=16, pady=(0, 7))

        table_frame = ttk.Frame(right)
        table_frame.pack(fill="x", padx=14, pady=(0, 8))
        columns = ("event", "time", "east", "north", "agl", "latitude", "longitude")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=7, selectmode="browse")
        for key, title, width in zip(columns, ["이벤트", "시간 s", "동 m", "북 m", "AGL m", "위도 °", "경도 °"], [145, 70, 85, 85, 85, 105, 105]):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=55, anchor="w" if key == "event" else "e", stretch=True)
        self.table.pack(fill="x")
        self.table.tag_configure("parachute", foreground=ORANGE)
        self.table.bind("<<TreeviewSelect>>", self._select_event)
        ttk.Label(right, text="AGL: 발사점 지면 기준 · 위경도: 국소 근사 · 낙하산 전개 후 자세 표시는 제공하지 않습니다.", style="Muted.TLabel").pack(anchor="w", padx=16, pady=(0, 10))

    def _field(self, parent, row, key, label):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar()
        entry = ttk.Entry(parent, textvariable=var, width=10)
        entry.grid(row=row, column=1, sticky="ew", padx=(6, 0), pady=3)
        self.vars[key] = var
        self.entries[key] = entry
        var.trace_add("write", self._dirty)

    def _dirty(self, *_):
        if not self.loading and not self.busy:
            self.status.set("입력이 변경되었습니다. ‘비행 계산’을 눌러 반영하세요.")

    def _trigger_changed(self, *_):
        mode = TRIGGERS.get(self.trigger.get(), "apogee")
        self.entries["trigger_altitude_m"].configure(state="normal" if mode == "descending_altitude" else "disabled")
        self.entries["trigger_time_s"].configure(state="normal" if mode == "time" else "disabled")
        self._dirty()

    def _set_config(self, config):
        cfg = SimulationConfig.from_dict(config)
        from dataclasses import asdict
        self.base_config = asdict(cfg)
        self.loading = True
        for key, var in self.vars.items():
            var.set(str(self.base_config[key]))
        self.trigger.set(next(label for label, mode in TRIGGERS.items() if mode == cfg.trigger_mode))
        self._write_json(self.base_config)
        self.loading = False
        self._trigger_changed()
        self.status.set("설정을 불러왔습니다. 비행 계산으로 결과를 갱신하세요.")

    def _write_json(self, config):
        self.config_text.delete("1.0", "end")
        self.config_text.insert("1.0", json.dumps(config, ensure_ascii=False, indent=2))

    def read_config(self):
        config = dict(self.base_config)
        for key, var in self.vars.items():
            try:
                config[key] = float(var.get())
            except ValueError as error:
                raise ValueError(f"{key}: 숫자를 입력하세요.") from error
        config["trigger_mode"] = TRIGGERS[self.trigger.get()]
        SimulationConfig.from_dict(config)
        return config

    def apply_json(self):
        try:
            self._set_config(json.loads(self.config_text.get("1.0", "end")))
        except (ValueError, TypeError) as error:
            messagebox.showerror("제원 JSON 오류", str(error), parent=self)

    def open_config(self):
        path = filedialog.askopenfilename(parent=self, title="기체 제원 JSON 열기", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            self._set_config(json.loads(Path(path).read_text(encoding="utf-8-sig")))
        except (OSError, ValueError, TypeError) as error:
            messagebox.showerror("제원 열기 오류", str(error), parent=self)

    def save_config(self):
        try:
            config = self.read_config()
            path = filedialog.asksaveasfilename(parent=self, title="기체 제원 저장", defaultextension=".json", initialfile="my_rocket.json", filetypes=[("JSON", "*.json")])
            if path:
                Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.set("기체 제원을 저장했습니다.")
        except (ValueError, OSError) as error:
            messagebox.showerror("저장 오류", str(error), parent=self)

    def calculate(self):
        if self.busy:
            return
        try:
            config = self.read_config()
        except ValueError as error:
            messagebox.showerror("입력 오류", str(error), parent=self)
            return
        self.playing = False
        self.busy = True
        self.run_button.configure(text="계산 중…", state="disabled")
        self.status.set("비행 궤적과 낙하산 사출 좌표를 계산하고 있습니다…")
        self._write_json(config)
        def worker():
            try:
                self.messages.put(("result", simulate(config)))
            except Exception as error:
                self.messages.put(("error", f"{type(error).__name__}: {error}"))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            kind, value = self.messages.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.run_button.configure(text="비행 계산", state="normal")
            if kind == "result":
                self.accept_result(value)
            else:
                self.status.set("계산을 완료하지 못했습니다. 입력과 실행 환경을 확인하세요.")
                messagebox.showerror("계산 오류", value, parent=self)
        self.after(100, self._poll)

    def accept_result(self, result):
        self.result = result
        self.times = [p["t_s"] for p in result.samples]
        by_event = {p["type"]: p for p in result.events}
        def altitude(kind):
            return f"{by_event[kind]['altitude_agl_m']:,.1f} m" if kind in by_event else "미도달"
        top = result.summary["apogee_agl_m"]
        self.metrics[0].configure(text=f"{top:,.1f} m" if top is not None else "미도달")
        self.metrics[1].configure(text=altitude("parachute_command"))
        self.metrics[2].configure(text=altitude("full_deployment"))
        landing_range = result.summary["landing_range_m"]
        self.metrics[3].configure(text=f"{landing_range:,.1f} m" if landing_range is not None else "미도달")
        self.table.delete(*self.table.get_children())
        for i, event in enumerate(result.events):
            self.table.insert("", "end", iid=str(i), values=(EVENTS.get(event["type"], event["type"]), f"{event['t_s']:.2f}", f"{event['east_m']:.1f}", f"{event['north_m']:.1f}", f"{event['altitude_agl_m']:.1f}", f"{event['latitude_deg']:.6f}", f"{event['longitude_deg']:.6f}"), tags=("parachute",) if event["type"] in {"parachute_command", "full_deployment"} else ())
        self.slider.configure(to=result.summary["duration_s"])
        self.export_button.configure(state="normal")
        self._draw_flight()
        self.index = -1
        self.play_button.configure(text="▶ 재생")
        self.seek(0)
        end = "착지까지 계산 완료" if result.summary["landed"] else "설정된 종료 시간 도달 · 착지 미도달"
        deployment = {"deployed": "낙하산 전개 반영", "pending_at_end": "전개 지연 중 종료", "not_triggered": "사출 조건 미도달"}[result.summary["deployment_status"]]
        self.status.set(f"{end} / {deployment} / 계산 구간 {result.summary['duration_s']:.1f} s")
        self.notice.configure(text=f"제원: {result.config['label']}  ·  실제 기체의 제원·실측자료 적용 전에는 예측 성능을 보장하지 않습니다.")

    def _axes_style(self, ax):
        ax.set_facecolor(PANEL)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.title.set_color(FG)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        for label in [ax.xaxis.label, ax.yaxis.label]:
            label.set_color(MUTED)
        ax.grid(True, color=GRID, linewidth=0.6, alpha=0.6)

    def _draw_flight(self):
        self.figure.clear()
        grid = self.figure.add_gridspec(2, 2, width_ratios=[1.5, 1], hspace=0.52, wspace=0.30)
        self.ax3 = self.figure.add_subplot(grid[:, 0], projection="3d")
        self.axh = self.figure.add_subplot(grid[0, 1])
        self.axmap = self.figure.add_subplot(grid[1, 1])
        for ax in [self.ax3, self.axh, self.axmap]:
            self._axes_style(ax)
        self.figure.subplots_adjust(left=0.06, right=0.94, top=0.92, bottom=0.12)
        points = self.result.samples
        self.x = [p["east_m"] for p in points]
        self.y = [p["north_m"] for p in points]
        self.z = [p["altitude_agl_m"] for p in points]
        self.ax3.plot(self.x, self.y, self.z, color="#627b94", lw=1.3, alpha=0.6)
        self.traveled, = self.ax3.plot([], [], [], color=CYAN, lw=2)
        self.rocket_marker, = self.ax3.plot([], [], [], marker="o", color=CYAN, ms=5)
        self.body_axis, = self.ax3.plot([], [], [], color="#ffffff", lw=2.5)
        self.ax3.set_xlabel("East (m)", labelpad=7)
        self.ax3.set_ylabel("North (m)", labelpad=7)
        self.ax3.set_zlabel("AGL (m)", color=MUTED, labelpad=7)
        self.ax3.set_title("Trajectory · drag to rotate", fontsize=11, pad=10)
        span = max(max(self.x)-min(self.x), max(self.y)-min(self.y), max(self.z), 10)
        cx, cy = (max(self.x)+min(self.x))/2, (max(self.y)+min(self.y))/2
        self.ax3.set_xlim(cx-span*0.55, cx+span*0.55)
        self.ax3.set_ylim(cy-span*0.55, cy+span*0.55)
        self.ax3.set_zlim(0, span*1.08)
        self.ax3.set_box_aspect((1.1, 1.1, 1.08))
        self.ax3.view_init(elev=24, azim=-55)
        for axis in [self.ax3.xaxis, self.ax3.yaxis, self.ax3.zaxis]:
            axis.set_pane_color((0.06, 0.11, 0.17, 0.4))
            axis._axinfo["grid"]["color"] = GRID
        self.axh.plot(self.times, self.z, color=CYAN, lw=1.7)
        self.axh.set_title("Altitude & speed", fontsize=11)
        self.axh.set_xlabel("Time (s)")
        self.axh.set_ylabel("AGL (m)")
        self.speed_ax = self.axh.twinx()
        self.speed_ax.plot(self.times, [p["speed_mps"] for p in points], color="#a3a0ee", lw=1, alpha=0.75)
        self.speed_ax.set_ylabel("Speed (m/s)", color="#a3a0ee", fontsize=9)
        self.speed_ax.tick_params(colors="#a3a0ee", labelsize=8)
        for spine in self.speed_ax.spines.values():
            spine.set_color(GRID)
        self.time_cursor = self.axh.axvline(0, color=ORANGE, lw=1)
        self.axmap.plot(self.x, self.y, color=CYAN, lw=1.6)
        self.axmap.set_title("Ground track · launch reference", fontsize=11)
        self.axmap.set_xlabel("East (m)")
        self.axmap.set_ylabel("North (m)")
        self.axmap.set_aspect("equal", adjustable="datalim")
        self.map_marker, = self.axmap.plot([], [], marker="o", color=ORANGE, ms=5)
        self.axmap.scatter([0], [0], marker="^", color=FG, s=35)
        for event in self.result.events:
            kind = event["type"]
            if kind in {"apogee", "parachute_command", "full_deployment", "landing"}:
                color = ORANGE if kind in {"parachute_command", "full_deployment"} else MUTED
                marker = {"apogee": "^", "parachute_command": "D", "full_deployment": "s", "landing": "x"}[kind]
                self.ax3.scatter([event["east_m"]], [event["north_m"]], [event["altitude_agl_m"]], color=color, marker=marker, s=32)
                self.axmap.scatter([event["east_m"]], [event["north_m"]], color=color, marker=marker, s=28)
                if kind == "parachute_command":
                    self.axh.scatter([event["t_s"]], [event["altitude_agl_m"]], color=ORANGE, marker="D", s=28, zorder=5)
                    self.axh.annotate("Command", (event["t_s"], event["altitude_agl_m"]), xytext=(7, -15), textcoords="offset points", color=ORANGE, fontsize=8)
        self.body_length = max(span * 0.045, 3)
        self.chart.draw()

    def seek(self, value):
        if self.result is None:
            return
        target = max(0, min(float(value), self.times[-1]))
        self.play_clock = target
        self.slider_value.set(target)
        index = min(len(self.times)-1, bisect.bisect_left(self.times, target))
        if index == self.index and self.clock_label.cget("text") != "T+0.00 s":
            return
        self.index = index
        point = self.result.samples[index]
        self.traveled.set_data_3d(self.x[:index+1], self.y[:index+1], self.z[:index+1])
        self.rocket_marker.set_data_3d([point["east_m"]], [point["north_m"]], [point["altitude_agl_m"]])
        self.rocket_marker.set_color(ORANGE if not point["attitude_valid"] else CYAN)
        self.map_marker.set_data([point["east_m"]], [point["north_m"]])
        self.time_cursor.set_xdata([point["t_s"], point["t_s"]])
        if point["attitude_valid"]:
            q0,q1,q2,q3 = (point[f"q{i}"] for i in range(4))
            axis = (2*(q1*q3+q0*q2), 2*(q2*q3-q0*q1), 1-2*(q1*q1+q2*q2))
            self.body_axis.set_data_3d(*[[point[key]-self.body_length*component/2, point[key]+self.body_length*component/2] for key, component in zip(["east_m", "north_m", "altitude_agl_m"], axis)])
        else:
            self.body_axis.set_data_3d([], [], [])
        tilt = f"기울기 {point['tilt_deg']:.1f}°" if point["attitude_valid"] else "전개 후 자세 모델 제외"
        phase = "착지" if self.result.summary["landed"] and index == len(self.times)-1 else PHASES.get(point["phase"], point["phase"])
        self.clock_label.configure(text=f"T+{point['t_s']:.2f} s")
        self.live.set(f"{phase}   E {point['east_m']:.1f} m   N {point['north_m']:.1f} m   AGL {point['altitude_agl_m']:.1f} m   속력 {point['speed_mps']:.1f} m/s   {tilt}")
        self.chart.draw_idle()

    def _select_event(self, *_):
        selected = self.table.selection()
        if self.result is not None and selected:
            self.playing = False
            self.play_button.configure(text="▶ 재생")
            self.seek(self.result.events[int(selected[0])]["t_s"])

    def toggle_play(self):
        if self.result is None or self.busy:
            return
        if not self.playing and self.play_clock >= self.times[-1]:
            self.seek(0)
        self.playing = not self.playing
        self.last_tick = time.monotonic()
        self.play_button.configure(text="Ⅱ 일시정지" if self.playing else "▶ 재생")

    def _tick(self):
        now = time.monotonic()
        elapsed = now - self.last_tick
        self.last_tick = now
        if self.playing and self.result is not None:
            self.seek(self.play_clock + min(elapsed, 0.25)*float(self.replay_speed.get()))
            if self.play_clock >= self.times[-1]:
                self.playing = False
                self.play_button.configure(text="▶ 재생")
        self.after(40, self._tick)

    def export_result(self):
        if self.result is None:
            return
        path = filedialog.asksaveasfilename(parent=self, title="결과 JSON·CSV 저장", defaultextension=".json", initialfile="flight_result.json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            target = Path(path)
            save_json(self.result, target)
            save_csv(self.result, target.with_suffix(".csv"))
            save_csv(self.result, target.with_name(target.stem+"_events.csv"), events=True)
            self.status.set(f"JSON과 궤적·이벤트 CSV를 저장했습니다: {target.name}")
        except (ValueError, OSError) as error:
            messagebox.showerror("결과 저장 오류", str(error), parent=self)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    app = FlightApp(autorun=not args.smoke_test)
    if args.smoke_test:
        app.accept_result(simulate(default_config()))
        app.update()
        command = next(event for event in app.result.events if event["type"] == "parachute_command")
        app.seek(command["t_s"])
        app.chart.draw()
        app.update()
        if args.preview:
            app.figure.savefig(args.preview, dpi=150, facecolor=PANEL)
        assert len(app.table.get_children()) == len(app.result.events)
        assert app.export_button.cget("state") == "normal"
        print(json.dumps({"gui": "ok", "events": len(app.result.events), "samples": len(app.result.samples), "command_agl_m": command["altitude_agl_m"]}))
        app.destroy()
    else:
        app.mainloop()


if __name__ == "__main__":
    main()
