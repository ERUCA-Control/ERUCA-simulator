"""Killable, bounded calculation process shared by the GUI and CLI."""
from __future__ import annotations

import math
import multiprocessing as mp
import time


def _calculate(connection, config, dt):
    try:
        from simulation import simulate
        connection.send(("result", simulate(config, dt)))
    except Exception as error:
        connection.send(("error", f"{type(error).__name__}: {error}"))
    finally:
        connection.close()


class CalculationJob:
    def __init__(self, timeout_s=45.0, *, _worker_target=None):
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be a positive finite number")
        self.timeout_s = float(timeout_s)
        self._target = _worker_target or _calculate
        self._context = mp.get_context("spawn")
        self._process = None
        self._connection = None
        self._started = None

    @property
    def running(self):
        return self._process is not None

    @property
    def elapsed_s(self):
        return 0.0 if self._started is None else time.monotonic() - self._started

    def start(self, config, dt=0.1):
        if self.running:
            raise RuntimeError("이미 계산 중입니다. 먼저 취소하세요.")
        receiver, sender = self._context.Pipe(duplex=False)
        process = self._context.Process(target=self._target, args=(sender, config, dt), daemon=True)
        try:
            process.start()
        except Exception:
            receiver.close()
            sender.close()
            raise
        sender.close()
        self._connection = receiver
        self._process = process
        self._started = time.monotonic()

    def poll(self):
        if not self.running:
            return None
        if self._connection.poll():
            try:
                response = self._connection.recv()
            except (EOFError, OSError):
                response = ("error", "계산 프로세스가 결과 없이 종료되었습니다. 입력 조건과 계산 환경을 확인하세요.")
            self.cancel()
            return response
        if not self._process.is_alive():
            self.cancel()
            return ("error", "계산 프로세스가 결과 없이 종료되었습니다. 입력 조건과 계산 환경을 확인하세요.")
        if self.elapsed_s >= self.timeout_s:
            self.cancel()
            return ("error", f"실제 계산 시간이 {self.timeout_s:g}초를 넘어 중단했습니다. 결과는 생성하지 않았습니다. 현재 모델이 처리하기 어려운 조건일 수 있으므로 제원과 사출 조건을 확인하세요.")
        return None

    def cancel(self):
        process, connection = self._process, self._connection
        self._process = self._connection = self._started = None
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.5)
            if not process.is_alive():
                process.close()
        if connection is not None:
            connection.close()
