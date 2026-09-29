import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.PluginHelper import PluginHelper


def _helper(states):
    helper = PluginHelper.__new__(PluginHelper)
    helper._event_manager = SimpleNamespace(get_current_state=lambda: ([], states))
    return helper


def test_station_search_uses_local_outfitting_without_network(monkeypatch):
    import lib.actions.actions_web as web

    calls = []
    monkeypatch.setattr(
        web,
        "search_stations_for_modules",
        lambda *args, **kwargs: calls.append(args) or {"status": "ok", "stations": []},
    )
    helper = _helper({
        "Location": SimpleNamespace(model_dump=lambda: {"StarSystem": "Sol", "StarPos": [0, 0, 0]}),
        "Outfitting": SimpleNamespace(model_dump=lambda: {
            "StationName": "Jameson Memorial",
            "StarSystem": "Shinrarta Dezhra",
            "timestamp": "now",
            "Items": [
                {"Name": "Beam Laser", "Class": 3, "Rating": "A", "Price": 100},
                {"Name": "Power Plant", "Class": 5, "Rating": "A", "Price": 200},
            ],
        }),
    })
    requests = [
        {"name": "Beam Laser", "class": ["3"], "rating": ["A"]},
        {"name": "Power Plant", "class": ["5"], "rating": ["A"]},
    ]

    result = helper.search_stations_for_modules(requests, cache={})

    assert not calls
    assert result["status"] == "ok"
    station = result["stations"][0]
    assert station["distance_ly"] == 0
    assert station["source"] == "local Outfitting projection"
    assert len(station["matched_filters"]) == 2
    assert station["stock_status"] == "station_catalog"


def test_station_search_queries_only_modules_missing_from_local_outfitting(monkeypatch):
    import lib.actions.actions_web as web

    calls = []
    monkeypatch.setattr(
        web,
        "search_stations_for_modules",
        lambda modules, states, **kwargs: calls.append(modules) or {
            "status": "ok",
            "stations": [{
                "id": "sol::outfit", "name": "Remote Outfit", "system": "Sol",
                "distance_ly": 3, "modules": [],
                "matched_filters": modules,
            }],
        },
    )
    helper = _helper({
        "Location": SimpleNamespace(model_dump=lambda: {"StarSystem": "Sol", "StarPos": [0, 0, 0]}),
        "Outfitting": SimpleNamespace(model_dump=lambda: {
            "StationName": "Current Station",
            "StarSystem": "Sol",
            "timestamp": "now",
            "Items": [{"Name": "Beam Laser", "Class": 3, "Rating": "A", "Price": 100}],
        }),
    })
    requests = [
        {"name": "Beam Laser", "class": ["3"], "rating": ["A"]},
        {"name": "Power Plant", "class": ["5"], "rating": ["A"]},
    ]

    result = helper.search_stations_for_modules(requests, cache={})

    assert calls == [[requests[1]]]
    assert [station["name"] for station in result["stations"]] == [
        "Current Station", "Remote Outfit"
    ]


def test_station_search_calculation_cache_avoids_duplicate_requests(monkeypatch):
    import lib.actions.actions_web as web

    calls = []
    monkeypatch.setattr(
        web,
        "search_stations_for_modules",
        lambda modules, states, **kwargs: calls.append(modules) or {
            "status": "ok", "stations": []
        },
    )
    helper = _helper({"Location": SimpleNamespace(model_dump=lambda: {"StarSystem": "Sol", "StarPos": [0, 0, 0]})})
    cache = {}
    modules = [{"name": "Beam Laser", "class": ["3"], "rating": ["A"]}]

    helper.search_stations_for_modules(modules, cache=cache, cache_key="grouped")
    helper.search_stations_for_modules([dict(modules[0])], cache=cache, cache_key="grouped")
    helper.search_stations_for_modules(modules, cache=cache, cache_key="module:beam")

    assert len(calls) == 2
