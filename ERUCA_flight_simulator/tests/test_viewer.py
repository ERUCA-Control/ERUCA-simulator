"""Render and exercise the actual viewer methods without native window access."""
import json
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
from app import FlightApp, PANEL
from simulation import SimulationResult


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


def viewer(result):
    figure = Figure(figsize=(12, 6.2), dpi=100, facecolor=PANEL)
    view = SimpleNamespace(
        figure=figure, chart=FigureCanvasAgg(figure), result=None,
        metrics=[Widget() for _ in range(4)], table=Widget(),
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

    @classmethod
    def tearDownClass(cls):
        command = next(e for e in cls.result.events if e['type']=='parachute_command')
        cls.view.seek(command['t_s'])
        cls.view.chart.draw()
        if os.environ.get('ERUCA_PREVIEW_PATH'):
            cls.view.figure.savefig(os.environ['ERUCA_PREVIEW_PATH'], dpi=150, facecolor=PANEL)


if __name__ == '__main__':
    unittest.main(verbosity=2)
