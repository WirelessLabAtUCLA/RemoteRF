"""A side-effect-free sample adapter. It never opens a radio or starts a process."""
from threading import Lock


class Counter:
    def __init__(self, context):
        self._context = context
        self._lock = Lock()
        self._running = False
        self._value = 0

    def validate(self, profile, configuration):
        if profile != "counter" or set(configuration) - {"initial"}:
            raise ValueError("unsupported counter configuration")
        initial = configuration.get("initial", 0)
        if type(initial) is not int or not -(1 << 63) <= initial < (1 << 63):
            raise ValueError("initial must be an int64")
        return {"initial": initial}

    def start(self, profile, configuration):
        configuration = self.validate(profile, configuration)
        with self._lock:
            if self._context.cancelled.is_set():
                raise RuntimeError("counter context is cancelled")
            if self._running:
                raise RuntimeError("counter already started")
            self._value = configuration["initial"]
            self._running = True

    def status(self):
        with self._lock:
            return {"running": self._running, "value": self._value}

    def call(self, action, parameters):
        if action != "increment" or set(parameters) != {"delta"} or type(parameters["delta"]) is not int:
            raise ValueError("increment requires an integer delta")
        with self._lock:
            if not self._running or self._context.cancelled.is_set():
                raise RuntimeError("counter is not active")
            result = self._value + parameters["delta"]
            if not -(1 << 63) <= result < (1 << 63):
                raise ValueError("counter result exceeds int64")
            self._value = result
            return {"value": result}

    def stop(self):
        with self._lock:
            self._running = False


def create_adapter(context):
    return Counter(context)
