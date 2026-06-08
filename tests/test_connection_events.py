from sma.adapters.connection_events import (
    LOST,
    RECONNECTED,
    ConnectionEventStore,
)


def test_record_and_recent_newest_first(tmp_path):
    store = ConnectionEventStore(tmp_path / "sma.db")
    store.record("inverter", LOST, "write failed")
    store.record("inverter", RECONNECTED, "back at 192.168.1.3")

    events = store.recent()
    assert [e.kind for e in events] == [RECONNECTED, LOST]
    assert events[0].component == "inverter"
    assert events[1].detail == "write failed"


def test_recent_respects_limit(tmp_path):
    store = ConnectionEventStore(tmp_path / "sma.db")
    for i in range(5):
        store.record("inverter", LOST, f"loss {i}")
    assert len(store.recent(limit=3)) == 3


def test_summary_counts_losses_and_reports_last(tmp_path):
    store = ConnectionEventStore(tmp_path / "sma.db")
    store.record("inverter", LOST, "first")
    store.record("inverter", LOST, "second")
    store.record("inverter", RECONNECTED, "healed")

    summary = store.summary("inverter")
    assert summary["losses_in_window"] == 2
    assert summary["last_kind"] == RECONNECTED
    assert summary["last_detail"] == "healed"


def test_summary_empty_store(tmp_path):
    store = ConnectionEventStore(tmp_path / "sma.db")
    summary = store.summary("inverter")
    assert summary["losses_in_window"] == 0
    assert summary["last_kind"] is None


def test_creates_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "state"
    store = ConnectionEventStore(nested / "sma.db")
    store.record("inverter", LOST, "x")
    assert (nested / "sma.db").exists()
