"""Render and exercise the actual viewer methods without native window access."""
import json
import copy
import os
from pathlib import Path
import sys
import tempfile
from types import MethodType, SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['MPLBACKEND'] = 'Agg'
os.environ['MPLCONFIGDIR'] = str(Path(tempfile.gettempdir()) / 'eruca-test-mpl')
os.environ['XDG_CACHE_HOME'] = str(Path(tempfile.gettempdir()) / 'eruca-test-cache')
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from app import CM_FIELDS, FlightApp, PANEL, TRIGGERS
from simulation import SimulationResult, default_config


class Widget:
    def __init__(self, value=None):
        self.value = value
        self.options = {'text': 'T+0.00 s'}
        self.rows = {}
    def configure(self, **kwargs): self.options.update(kwargs)
    def cget(self, key): return self.options.get(key)
    def get(self): return self.value
    def set(self, value): self.value = value
    def delete(self, *keys):
        for key in keys: self.rows.pop(key, None)
    def get_children(self): return tuple(self.rows)
    def insert(self, parent, index, iid, **kwargs): self.rows[iid] = kwargs


class TextBuffer:
    def __init__(self): self.value = ''
    def delete(self, *_): self.value = ''
    def insert(self, _index, value): self.value = value
    def get(self, *_): return self.value


def config_editor(config=None):
    """Exercise the real editor conversion methods without constructing Tk."""
    fields = CM_FIELDS | {'altitude_limit_m', 'deployment_lag_s', 'initial_total_mass_kg', 'motor_dry_mass_kg', 'propellant_mass_kg'}
    view = SimpleNamespace(
        vars={key: Widget() for key in fields},
        entries={key: Widget() for key in ('trigger_altitude_m', 'trigger_time_s', 'altitude_limit_m')},
        altitude_limit_enabled=Widget(True), trigger=Widget(),
        config_text=TextBuffer(), loading=False, busy=False,
        status=Widget(), base_config={},
    )
    for name in ('_set_config', '_write_json', 'read_config', '_dirty', '_limit_changed', '_trigger_changed'):
        setattr(view, name, MethodType(getattr(FlightApp, name), view))
    view._set_config(default_config() if config is None else config)
    return view


def viewer(result):
    figure = Figure(figsize=(12, 6.2), dpi=100, facecolor=PANEL)
    view = SimpleNamespace(
        figure=figure, chart=FigureCanvasAgg(figure), result=None,
        metrics=[Widget() for _ in range(5)], table=Widget(),
        slider=Widget(), slider_value=Widget(), export_button=Widget(),
        clock_label=Widget(), status=Widget(), live=Widget(), notice=Widget(),
        play_button=Widget(), index=0,
    )
    for name in ('_axes_style', '_draw_flight', 'seek', 'accept_result'):
        setattr(view, name, MethodType(getattr(FlightApp, name), view))
    view.accept_result(result)
    return view


class ViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads((ROOT/'example_result.json').read_text())
        cls.result = SimulationResult(**data)
        cls.view = viewer(cls.result)

    def test_all_event_rows_and_values_are_displayed(self):
        view = self.view
        self.assertEqual(len(view.table.rows), len(self.result.events))
        self.assertEqual(view.export_button.cget('state'), 'normal')
        self.assertIn('m', view.metrics[0].cget('text'))

    def test_seeking_to_command_and_landing_updates_both_views(self):
        view = self.view
        command = next(e for e in self.result.events if e['type']=='parachute_command')
        view.seek(command['t_s'])
        self.assertEqual(view.clock_label.cget('text'), f"T+{command['t_s']:.2f} s")
        x,y,z = view.rocket_marker.get_data_3d()
        self.assertAlmostEqual(x[0], command['east_m'])
        self.assertAlmostEqual(z[0], command['altitude_agl_m'])
        view.seek(self.result.summary['duration_s'])
        self.assertIn('착지', view.live.get())
        self.assertIn('전개 후 자세 모델 제외', view.live.get())
        self.assertEqual(len(view.body_axis.get_data_3d()[0]), 0)

    def test_recalculation_resets_to_launch(self):
        view = self.view
        view.seek(1)
        view.accept_result(self.result)
        self.assertEqual(view.index, 0)
        self.assertEqual(view.clock_label.cget('text'), 'T+0.00 s')
        self.assertAlmostEqual(view.rocket_marker.get_data_3d()[2][0], 0)

    def test_altitude_limit_line_overshoot_and_reason_are_presented(self):
        # Presentation-only inputs: the physics of the trigger and overshoot are
        # checked separately in test_simulation.py.
        data = copy.deepcopy(self.result.to_dict())
        data['config'].update(altitude_limit_enabled=True, altitude_limit_m=80.0)
        data['summary'].update(altitude_limit_overshoot_m=12.34, command_reason='altitude_limit')
        view = viewer(SimulationResult(**data))
        limit_lines = [line for line in view.axh.lines if line.get_label().startswith('Deployment limit:')]
        self.assertEqual(len(limit_lines), 1)
        self.assertEqual(list(limit_lines[0].get_ydata()), [80.0, 80.0])
        self.assertEqual(view.metrics[4].cget('text'), '12.3 m')
        self.assertIn('고도 제한으로 사출', view.status.get())
        self.assertGreater(view.axh.get_ylim()[1], 80.0)

        data['config']['altitude_limit_enabled'] = False
        data['summary']['command_reason'] = 'apogee'
        view.accept_result(SimulationResult(**data))
        self.assertFalse(any(line.get_label().startswith('Deployment limit:') for line in view.axh.lines))
        self.assertEqual(view.metrics[4].cget('text'), '사용 안 함')
        self.assertNotIn('고도 제한으로 사출', view.status.get())

    @classmethod
    def tearDownClass(cls):
        command = next(e for e in cls.result.events if e['type']=='parachute_command')
        cls.view.seek(command['t_s'])
        cls.view.chart.draw()
        if os.environ.get('ERUCA_PREVIEW_PATH'):
            cls.view.figure.savefig(os.environ['ERUCA_PREVIEW_PATH'], dpi=150, facecolor=PANEL)


class ConfigEditorTests(unittest.TestCase):
    def test_total_mass_editor_rejects_70g_with_example_motor_before_calculation(self):
        view = config_editor()
        self.assertAlmostEqual(float(view.vars['initial_total_mass_kg'].get()), 4.7)
        view.vars['initial_total_mass_kg'].set('0.07')
        with self.assertRaisesRegex(ValueError, '합계 0.7 kg'):
            view.read_config()
        # Change only the declared total; motor values are never silently scaled.
        self.assertEqual(float(view.vars['motor_dry_mass_kg'].get()), 0.4)
        self.assertEqual(float(view.vars['propellant_mass_kg'].get()), 0.3)

    def test_total_mass_editor_preserves_legacy_vehicle_mass_on_round_trip(self):
        cfg = default_config()
        cfg['rocket_mass_kg'] = 0.07
        view = config_editor(cfg)
        self.assertAlmostEqual(float(view.vars['initial_total_mass_kg'].get()), .77)
        self.assertAlmostEqual(view.read_config()['rocket_mass_kg'], .07)

    def test_centimetre_fields_round_trip_to_metre_json(self):
        view = config_editor()
        self.assertEqual(view.vars['rocket_length_m'].get(), '70')
        self.assertEqual(view.vars['rocket_inner_diameter_m'].get(), '8')
        self.assertEqual(view.vars['nozzle_length_m'].get(), '15')
        self.assertEqual(float(view.vars['altitude_limit_m'].get()), 80.0)
        self.assertTrue(view.altitude_limit_enabled.get())
        stored = json.loads(view.config_text.value)
        self.assertAlmostEqual(stored['rocket_length_m'], 0.70)
        self.assertAlmostEqual(stored['rocket_inner_diameter_m'], 0.08)
        self.assertAlmostEqual(stored['nozzle_length_m'], 0.15)

        view.vars['rocket_length_m'].set('90')
        view.vars['rocket_inner_diameter_m'].set('10')
        view.vars['nozzle_length_m'].set('20')
        view.vars['altitude_limit_m'].set('85')
        config = view.read_config()
        self.assertAlmostEqual(config['rocket_length_m'], 0.9)
        self.assertAlmostEqual(config['rocket_inner_diameter_m'], 0.1)
        self.assertAlmostEqual(config['nozzle_length_m'], 0.2)
        self.assertAlmostEqual(config['altitude_limit_m'], 85.0)
        self.assertIsNone(config['cg_from_tail_m'])
        self.assertIsNone(config['rocket_inertia_kg_m2'])

    def test_limit_toggle_is_boolean_and_disables_its_entry(self):
        view = config_editor()
        self.assertEqual(view.entries['altitude_limit_m'].cget('state'), 'normal')
        view.altitude_limit_enabled.set(False)
        view._limit_changed()
        self.assertEqual(view.entries['altitude_limit_m'].cget('state'), 'disabled')
        config = view.read_config()
        self.assertIs(config['altitude_limit_enabled'], False)
        self.assertEqual(config['altitude_limit_m'], 80.0)
        view._set_config(config)
        self.assertIs(view.altitude_limit_enabled.get(), False)
        self.assertEqual(view.entries['altitude_limit_m'].cget('state'), 'disabled')

    def test_legacy_json_and_explicit_mass_properties_survive_editor_round_trip(self):
        config = default_config()
        config.pop('rocket_inner_diameter_m')
        config.update(rocket_length_m=1.5, rocket_radius_m=0.045,
                      cg_from_tail_m=0.82, rocket_inertia_kg_m2=[0.65, 0.65, 0.007])
        view = config_editor(config)
        restored = view.read_config()
        self.assertNotIn('rocket_radius_m', restored)
        self.assertAlmostEqual(
            restored['rocket_inner_diameter_m'] / 2 + restored['body_wall_thickness_m'],
            0.045,
        )
        self.assertEqual(restored['cg_from_tail_m'], 0.82)
        self.assertEqual(restored['rocket_inertia_kg_m2'], [0.65, 0.65, 0.007])

    def test_invalid_centimetre_inputs_are_not_silently_converted(self):
        for key in CM_FIELDS:
            for text in ('', 'seventy', '0', '-1', 'nan'):
                with self.subTest(field=key, text=text):
                    view = config_editor()
                    view.vars[key].set(text)
                    with self.assertRaises(ValueError):
                        view.read_config()


if __name__ == '__main__':
    unittest.main(verbosity=2)
