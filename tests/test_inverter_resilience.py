"""The daemon must self-heal a wedged Modbus link instead of needing a restart."""
from sma.adapters.connection_events import LOST, ConnectionEventStore
from sma.adapters.modbus_actuator import ModbusActuator
from sma.web.server import Providers


class _FakeInverter:
    def __init__(self):
        self.closed = False

    def __exit__(self, *exc):
        self.closed = True


def _providers(events, inverter):
    return Providers(
        influx=None, prices=None, metering=None, writer=None,
        events=events, evcc=None,
        inverter=inverter, actuator=ModbusActuator(inverter),
    )


def test_drop_inverter_closes_client_and_records_loss(tmp_path):
    events = ConnectionEventStore(tmp_path / "sma.db")
    inv = _FakeInverter()
    p = _providers(events, inv)

    p.drop_inverter("modbus write of 0% failed")

    assert inv.closed is True
    assert p.inverter is None
    assert p.actuator is None       # next tick's try_connect_inverter will rebuild
    recorded = events.recent()
    assert len(recorded) == 1
    assert recorded[0].kind == LOST
    assert "write of 0%" in recorded[0].detail


def test_drop_inverter_is_idempotent_when_already_down(tmp_path):
    events = ConnectionEventStore(tmp_path / "sma.db")
    p = _providers(events, _FakeInverter())
    p.inverter = None  # already disconnected

    p.drop_inverter("write failed")

    assert events.recent() == []   # no spurious loss event when there was nothing to drop
