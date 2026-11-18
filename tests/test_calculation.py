"""Regression checks for the 70 g report and killable calculation lifecycle."""
import multiprocessing as mp
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calculation import CalculationJob
from simulation import SimulationConfig, default_config, simulate, vehicle_mass_from_total
from app import FlightApp


def _worker_fixture(connection, config, dt):
    try:
        time.sleep(config.get('delay', 0))
        if config.get('crash'):
            return
        connection.send(('result', {'dt': dt, 'tag': config.get('tag')}))
    finally:
        connection.close()


def response(job, deadline=8):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        result = job.poll()
        if result is not None:
            return result
        time.sleep(.02)
    job.cancel()
    raise AssertionError('Job did not terminate within regression-test deadline')


class MassRegressionTests(unittest.TestCase):
    def test_initial_total_70g_is_rejected_before_motor_calculations(self):
        with self.assertRaisesRegex(ValueError, '합계 0.7 kg'):
            SimulationConfig.from_dict({'initial_total_mass_kg': .07})

    def test_total_mass_and_legacy_vehicle_mass_are_distinguished(self):
        cfg = SimulationConfig.from_dict({'initial_total_mass_kg': 4.7})
        self.assertAlmostEqual(cfg.rocket_mass_kg, 4)
        with self.assertRaisesRegex(ValueError, '일치하지'):
            SimulationConfig.from_dict({'initial_total_mass_kg': 4.7, 'rocket_mass_kg': .07})

    def test_total_mass_conversion_rejects_nonphysical_inputs(self):
        for value in (0, -.07, float('nan'), float('inf'), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                vehicle_mass_from_total(value, .4, .3)

    def test_previous_70g_vehicle_hang_fails_before_flight_integration(self):
        config = default_config()
        config['rocket_mass_kg'] = .07
        with patch('rocketpy.Flight', side_effect=AssertionError('Flight must not start')):
            with self.assertRaisesRegex(ValueError, '불안정'):
                simulate(config)


class CalculationJobTests(unittest.TestCase):
    def setUp(self):
        self.job = CalculationJob(timeout_s=5, _worker_target=_worker_fixture)
        self.addCleanup(self.job.cancel)

    def test_successful_result_crosses_process_boundary_and_cleans_up(self):
        self.job.start({'tag': 'fresh'}, dt=.2)
        self.assertEqual(response(self.job), ('result', {'dt': .2, 'tag': 'fresh'}))
        self.assertFalse(self.job.running)

    def test_cancel_stops_child_and_allows_restart_without_stale_result(self):
        self.job.start({'delay': 120, 'tag': 'stale'})
        pid = self.job._process.pid
        self.job.cancel()
        self.assertFalse(self.job.running)
        self.assertNotIn(pid, [p.pid for p in mp.active_children()])
        self.job.start({'tag': 'new'})
        self.assertEqual(response(self.job)[1]['tag'], 'new')

    def test_wall_clock_timeout_stops_child_without_a_fake_flight_result(self):
        self.job.timeout_s = .5
        self.job.start({'delay': 120})
        pid = self.job._process.pid
        kind, message = response(self.job)
        self.assertEqual(kind, 'error')
        self.assertIn('초를 넘어 중단', message)
        self.assertFalse(self.job.running)
        self.assertNotIn(pid, [p.pid for p in mp.active_children()])

    def test_unexpected_child_exit_is_an_error_and_not_an_infinite_wait(self):
        self.job.start({'crash': True})
        self.assertEqual(response(self.job)[0], 'error')
        self.assertFalse(self.job.running)

    def test_gui_busy_button_cancels_current_job(self):
        view = SimpleNamespace(busy=True, job=Mock(), run_button=Mock(), status=Mock())
        FlightApp.calculate(view)
        view.job.cancel.assert_called_once()
        self.assertFalse(view.busy)
        view.run_button.configure.assert_called_once_with(text='비행 계산', state='normal')

    def test_gui_error_restores_controls(self):
        view = SimpleNamespace(busy=True, job=Mock(), run_button=Mock(), status=Mock(), after=Mock())
        view._poll = Mock()
        view.job.poll.return_value = ('error', 'test failure')
        with patch('app.messagebox.showerror') as dialog:
            FlightApp._poll(view)
        self.assertFalse(view.busy)
        dialog.assert_called_once()
        view.after.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
