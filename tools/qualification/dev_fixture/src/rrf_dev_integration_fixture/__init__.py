"""Dev-only supervised no-radio adapter; metadata import has no side effects."""
import math
from pathlib import Path
import sys
import time

_DEFAULTS = {'start_delay': 0.0, 'stop_delay': 0.0, 'fail_start': False, 'fail_stop': False, 'exit_after': 0.0}


class Adapter:
    def __init__(self, context):
        from .server import guard
        from .backend import serial
        root, _ = guard()
        if type(context.selector) is not dict or set(context.selector) != {'serial'}:
            raise ValueError('one synthetic selector is required')
        serial(context.selector['serial'])
        if not Path(context.work_dir).resolve().is_relative_to(root) or context.processes is None:
            raise RuntimeError('owned dev work directory and process scope required')
        self.context, self.process_id, self.stopped = context, None, False
        self.configuration = dict(_DEFAULTS)
        self.calls = 0
    def validate(self, profile, configuration):
        if profile != 'test' or type(configuration) is not dict or set(configuration) - set(_DEFAULTS):
            raise ValueError('unknown dev fixture configuration')
        value = {**_DEFAULTS, **configuration}
        for name, maximum in [('start_delay', 10), ('stop_delay', 35), ('exit_after', 10)]:
            if type(value[name]) not in (int, float) or not math.isfinite(value[name]) or not 0 <= value[name] <= maximum:
                raise ValueError('invalid bounded dev delay')
        if any(type(value[name]) is not bool for name in ('fail_start', 'fail_stop')):
            raise ValueError('failure flags must be booleans')
        return value
    def start(self, profile, configuration):
        self.configuration = self.validate(profile, configuration)
        if self.context.cancelled.wait(self.configuration['start_delay']):
            raise RuntimeError('dev start cancelled')
        self.process_id = self.context.processes.launch(
            [str(Path(sys.executable).absolute()), '-I', '-u', '-m', 'rrf_dev_integration_fixture.worker',
             str(self.configuration['exit_after'])],
            env={'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}, cwd=self.context.work_dir,
            required_while_running=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.context.cancelled.is_set():
                raise RuntimeError('dev start cancelled')
            self._process()
            if b'NON_RADIO_TEST_FIXTURE_READY' in self.context.processes.log_tail(self.process_id, 'stdout'):
                if self.configuration['fail_start']:
                    raise RuntimeError('requested synthetic startup failure')
                return
            time.sleep(0.01)
        raise RuntimeError('dev child did not become ready')
    def _process(self):
        rows = self.context.processes.snapshot()['processes']
        row = next((row for row in rows if row['id'] == self.process_id), None)
        if row is None or row['returncode'] is not None:
            raise RuntimeError('synthetic supervised process exited')
        return row
    def status(self):
        if self.process_id is not None and not self.stopped:
            self._process()
        return {'ready': self.process_id is not None and not self.stopped, 'stopped': self.stopped,
                'fixture': 'NON_RADIO_TEST_FIXTURE', 'calls': self.calls}
    def call(self, action, parameters):
        if type(parameters) is not dict:
            raise ValueError('action parameters must be an object')
        self._process()
        if action == 'health' and not parameters:
            return self.status()
        if action == 'echo':
            self.calls += 1
            return {'echo': parameters, 'calls': self.calls, 'fixture': 'NON_RADIO_TEST_FIXTURE'}
        raise ValueError('unknown fixture action or parameters')
    def stop(self):
        if self.stopped:
            return
        time.sleep(self.configuration['stop_delay'])
        if not self.context.processes.stop(timeout=3):
            raise RuntimeError('dev child cleanup incomplete')
        self.stopped = True
        if self.configuration['fail_stop']:
            raise RuntimeError('requested synthetic stop failure')


def create_adapter(context):
    return Adapter(context)
