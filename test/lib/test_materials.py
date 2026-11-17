import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.Event import GameEvent
from lib.projections.materials import Materials


@pytest.mark.parametrize("name,category,bucket", [
    ("iron", "raw", "Raw"),
    ("chemicalprocessors", "Manufactured", "Manufactured"),
    ("dataminedwake", "Encoded", "Encoded"),
    ("iron", None, "Raw"),
])
def test_material_discard_updates_stock_timestamp_and_clamps_at_zero(
    name: str, category: str | None, bucket: str,
) -> None:
    projection = Materials()
    projection.process(GameEvent(
        content={"event": "Materials", bucket: [{"Name": name, "Count": 3}]},
        historic=False,
    ))

    for count, expected in [(1, 2), (5, 0)]:
        projection.process(GameEvent(content={
            "event": "MaterialDiscarded", "Name": name, "Count": count,
            "Category": category, "timestamp": "2026-10-18T07:00:00Z",
        }, historic=False))
        entry = next(entry for entry in getattr(projection.state, bucket) if entry.Name == name)
        assert entry.Count == expected
        assert projection.state.LastUpdated == "2026-10-18T07:00:00Z"

    projection.process(GameEvent(content={
        "event": "MaterialCollected", "Name": name, "Count": 2,
        "Category": category, "timestamp": "2026-10-18T07:00:01Z",
    }, historic=False))
    assert entry.Count == 2
    assert projection.state.LastUpdated == "2026-10-18T07:00:01Z"
