import json
from pathlib import Path

import pytest
import sys

from openai.types.chat import ChatCompletionMessageFunctionToolCall

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.lib.actions import actions_web
from src.lib.actions import Actions as actions


class FakePromptGenerator:
    def generate_status_message(self, projected_states: dict) -> str:
        return "status"


class FakeLLMModel:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, messages: list, tools: list, tool_choice: str):
        self.calls += 1
        if self.calls == 1:
            return "", [ChatCompletionMessageFunctionToolCall(
                type="function",
                id="internal_call_1",
                function={
                    "name": "get_galnet_news",
                    "arguments": json.dumps({"query": "thargoids"}),
                },
            )], {}
        return "Final report", None, {}


class FailingLLMModel:
    def generate(self, messages: list, tools: list, tool_choice: str):
        raise RuntimeError("model unavailable")


class BrokerPromptGenerator:
    def generate_status_message(self, projected_states: dict) -> str:
        return "CargoContents: 2 X Reinforced Mounting Plate"


class BrokerLLMModel:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, messages: list, tools: list, tool_choice: str):
        self.calls += 1
        if self.calls == 1:
            system_prompt = messages[0]["content"]
            assert "Technology Broker module unlock requirements" in system_prompt
            assert "CargoContents" in system_prompt
            assert "Do not send commodities to material_finder" in system_prompt
            assert "CargoContents: 2 X Reinforced Mounting Plate" in messages[1]["content"]
            return "", [ChatCompletionMessageFunctionToolCall(
                type="function",
                id="station_call_1",
                function={
                    "name": "station_finder",
                    "arguments": json.dumps({
                        "commodities": [{
                            "name": "Reinforced Mounting Plate",
                            "amount": 3,
                            "transaction": "Buy",
                        }],
                    }),
                },
            )], {}
        return "Need 5; you have 2. Buy 3 more.", None, {}


def consume_generator(generator):
    values = []
    while True:
        try:
            values.append(next(generator))
        except StopIteration as stop:
            return values, stop.value


def test_get_visuals_answers_armor_query_from_technical_loadout_id(monkeypatch) -> None:
    monkeypatch.setattr(actions, "screenshot", lambda: pytest.fail("visual query should use loadout"))
    monkeypatch.setattr(actions, "vision_model", None)

    result = actions.get_visuals(
        {"query": "Quel est le module d'armure que j'ai déjà ?"},
        {
            "Loadout": {
                "Ship": "smallcombat01_nx",
                "Modules": [
                    {
                        "Slot": "Armour",
                        "Item": "smallcombat01_nx_armour_reactive",
                        "Engineering": {
                            "BlueprintName": "Armour_Thermic",
                            "Level": 5,
                        },
                    }
                ],
            }
        },
    )

    assert "smallcombat01_nx_armour_reactive" in result
    assert "Armour_Thermic" in result
    assert "grade 5" in result


def test_web_search_agent_yields_internal_tool_processing_events(monkeypatch) -> None:
    monkeypatch.setattr(actions_web, "get_galnet_news", lambda args, projected_states: "news result")

    updates, final_result = consume_generator(actions_web.web_search_agent(
        {"query": "latest thargoid news"},
        {},
        prompt_generator=FakePromptGenerator(),
        llm_model=FakeLLMModel(),
        max_loops=3,
    ))

    assert updates == [
        {
            "status": "searching",
            "query": "latest thargoid news",
        },
        {
            "status": "started",
            "query": "latest thargoid news",
            "iteration": 1,
            "internal_tool_call_id": "internal_call_1",
            "internal_tool_name": "get_galnet_news",
            "arguments": {"query": "thargoids"},
        },
        {
            "status": "completed",
            "query": "latest thargoid news",
            "iteration": 1,
            "internal_tool_call_id": "internal_call_1",
            "internal_tool_name": "get_galnet_news",
            "result": "news result",
        },
    ]
    assert final_result == "Final report"


def test_web_search_agent_routes_broker_commodity_and_checks_cargo(monkeypatch) -> None:
    station_calls = []

    def fake_station_finder(args, projected_states):
        station_calls.append(args)
        return "Market results"

    monkeypatch.setattr(actions_web, "station_finder", fake_station_finder)

    updates, final_result = consume_generator(actions_web.web_search_agent(
        {"query": "I need 5 Reinforced Mounting Plates to unlock a Guardian module at a Technology Broker"},
        {"Cargo": {"Inventory": [{"Name": "Reinforced Mounting Plate", "Count": 2}]}},
        prompt_generator=BrokerPromptGenerator(),
        llm_model=BrokerLLMModel(),
        max_loops=3,
    ))

    assert station_calls == [{
        "commodities": [{
            "name": "Reinforced Mounting Plate",
            "amount": 3,
            "transaction": "Buy",
        }],
    }]
    assert updates[2]["result"] == "Market results"
    assert final_result == "Need 5; you have 2. Buy 3 more."


def test_web_search_agent_returns_actionable_loop_error() -> None:
    updates, final_result = consume_generator(actions_web.web_search_agent(
        {"query": "search for aluminum"},
        {},
        prompt_generator=FakePromptGenerator(),
        llm_model=FailingLLMModel(),
        max_loops=3,
    ))

    assert updates == [{"status": "searching", "query": "search for aluminum"}]
    assert final_result == (
        "Web search failed while requesting the search agent model response "
        "on iteration 1 for query 'search for aluminum'. RuntimeError: model unavailable"
    )


def test_blueprint_finder_handles_default_inventory_states() -> None:
    projected_states = {
        "Materials": {
            "Raw": [{"Name": "carbon", "Count": 0, "Name_Localised": None}],
            "Manufactured": [],
            "Encoded": [],
        },
        "ShipLocker": {
            "Items": None,
            "Components": None,
            "Data": None,
            "Consumables": None,
        },
    }

    result = actions_web.blueprint_finder({
        "modifications": ["Extra Backpack Capacity"],
    }, projected_states)

    assert "Extra Backpack Capacity" in result
    assert actions_web.material_finder({}, projected_states) == "No materials found"


def test_plot_name_match_score_prefers_exact_match() -> None:
    assert actions_web.plot_name_match_score("Sol", "Sol") == 1.0
    assert actions_web.plot_name_match_score("Jameson Memorial", "jameson memorial") == 1.0
    assert actions_web.plot_name_match_score("HIP 58412 8 D", "HIP584128D") == 1.0
    assert actions_web.plot_name_match_score("Sol", "Alpha Centauri") == 0.0
    assert actions_web.is_plot_name_exact_match("Earth", "Earth") is True
    assert actions_web.is_plot_name_exact_match("Earth", "Earth Expeditionary Fleet 4") is False


def test_select_best_plot_target_prefers_exact_match_over_fuzzy() -> None:
    candidates = [
        actions_web.ResolvedPlotTarget(
            target_type="body",
            name="Earth Expeditionary Fleet 4",
            system_name="Earth Expeditionary Fleet",
            system_map_category="LANDFALL PLANETS",
            distance=22032.0,
            match_score=0.9,
            details={},
            is_landable=True,
        ),
        actions_web.ResolvedPlotTarget(
            target_type="body",
            name="Earth",
            system_name="Sol",
            system_map_category="LANDFALL PLANETS",
            distance=0.0,
            match_score=1.0,
            details={},
            is_landable=False,
        ),
    ]

    best_match = actions_web._select_best_plot_target(candidates, "Earth")
    assert best_match is not None
    assert best_match.name == "Earth"
    assert best_match.system_name == "Sol"


def test_select_best_plot_target_uses_match_score_then_distance() -> None:
    candidates = [
        actions_web.ResolvedPlotTarget(
            target_type="system",
            name="Sol",
            system_name="Sol",
            system_map_category=None,
            distance=10.0,
            match_score=0.7,
            details={},
        ),
        actions_web.ResolvedPlotTarget(
            target_type="station",
            name="Jameson Memorial",
            system_name="Shinrarta Dezhra",
            system_map_category="ORBITAL PORTS",
            distance=5.0,
            match_score=1.0,
            details={},
        ),
    ]

    best_match = actions_web._select_best_plot_target(candidates, "Jameson Memorial")
    assert best_match is not None
    assert best_match.name == "Jameson Memorial"


def test_system_map_category_for_station_uses_spansh_types() -> None:
    assert actions_web.system_map_category_for_station({"type": "Coriolis Starport"}) == "ORBITAL PORTS"
    assert actions_web.system_map_category_for_station({"type": "Planetary Outpost"}) == "PLANETARY PORTS"
    assert actions_web.system_map_category_for_station({"type": "Drake-Class Carrier"}) == "FLEET CARRIERS"
    assert actions_web.system_map_category_for_station({"type": "Mega ship"}) == "INSTALLATIONS"
    assert actions_web.system_map_category_for_station({"type": "Surface Settlement"}) == "SURFACE SETTLEMENTS"
    assert actions_web.system_map_category_for_station({"type": "Settlement"}) == "ODYSSEY SETTLEMENTS"
    assert actions_web.system_map_category_for_station({"type": "Unknown Type"}) is None


def test_ensure_in_system_plot_target_rejects_unlandable_body() -> None:
    target = actions_web.ResolvedPlotTarget(
        target_type="body",
        name="Earth",
        system_name="Sol",
        system_map_category="LANDFALL PLANETS",
        distance=0.0,
        match_score=1.0,
        details={"is_landable": False},
        is_landable=False,
    )

    try:
        actions_web.ensure_in_system_plot_target(target)
    except Exception as error:
        assert "not landable" in str(error)
    else:
        raise AssertionError("Expected unlandable body to be rejected")


def test_plot_candidates_from_bodies_skips_non_planets_and_requests_one_result(monkeypatch) -> None:
    captured_sizes: list[int] = []
    captured_names: list[str] = []
    captured_requests: list[dict] = []

    def fake_prepare_body_request(obj, projected_states):
        captured_sizes.append(obj["size"])
        captured_names.append(obj["name"])
        return {"filters": {}, "size": obj["size"], "page": 0}

    def fake_post(url: str, request_body: dict) -> dict:
        captured_requests.append(request_body)
        return {
            "results": [
                {
                    "name": "Earth Expeditionary Fleet",
                    "system_name": "Earth Expeditionary Fleet",
                    "type": "Star",
                    "distance": 22032.0,
                    "is_landable": False,
                },
                {
                    "name": "Earth",
                    "system_name": "Sol",
                    "type": "Planet",
                    "distance": 0.0,
                    "is_landable": False,
                },
            ]
        }

    monkeypatch.setattr(actions_web, "prepare_body_request", fake_prepare_body_request)
    monkeypatch.setattr(actions_web, "_spansh_post", fake_post)

    candidates = actions_web._plot_candidates_from_bodies("Earth", {
        "Location": {
            "StarSystem": "Sol",
            "StarPos": [0, 0, 0],
        }
    })

    assert captured_sizes == [1]
    assert captured_names == ["Eart?"]
    assert captured_requests[0]["size"] == 1
    assert captured_requests[0]["sort"] == [{"distance": {"direction": "asc"}}]
    assert captured_requests[0]["reference_coords"] == {"x": 0, "y": 0, "z": 0}
    assert len(candidates) == 1
    assert candidates[0].name == "Earth"
    assert candidates[0].is_landable is False


def test_plot_candidates_from_stations_requests_nearest_named_station(monkeypatch) -> None:
    captured_requests: list[dict] = []

    def fake_prepare_station_request(obj, projected_states):
        return {
            "filters": {
                "distance": {"min": "0", "max": "50000"},
                "name": {"value": obj["name"]},
                "type": {"value": ["Outpost"]},
                "has_large_pad": {"value": True},
            },
            "sort": [],
            "size": obj["size"],
            "page": 0,
        }

    def fake_post(url: str, request_body: dict) -> dict:
        captured_requests.append(request_body)
        return {
            "results": [{
                "name": "Moskowitz Enterprise",
                "system_name": "Lyncis Sector XK-O b6-1",
                "type": "Outpost",
                "distance": 12.0,
            }]
        }

    monkeypatch.setattr(actions_web, "prepare_station_request", fake_prepare_station_request)
    monkeypatch.setattr(actions_web, "_spansh_post", fake_post)

    candidates = actions_web._plot_candidates_from_stations("Moskowitz Enterprise", {
        "Location": {
            "StarSystem": "Lyncis Sector XK-O b6-1",
            "StarPos": [-82.625, 75.84375, -79.46875],
        }
    })

    assert captured_requests[0] == {
        "filters": {
            "distance": {"min": "0", "max": "50000"},
            "name": {"value": "Moskowitz Enterpris?"},
        },
        "sort": [{"distance": {"direction": "asc"}}],
        "size": 1,
        "page": 0,
        "reference_coords": {
            "x": -82.625,
            "y": 75.84375,
            "z": -79.46875,
        },
    }
    assert len(candidates) == 1
    assert candidates[0].name == "Moskowitz Enterprise"


def test_plot_search_query_prefers_station_then_body_then_system() -> None:
    assert actions_web.plot_search_query(station="Jameson Memorial", body="Earth", system="Sol") == "Jameson Memorial"
    assert actions_web.plot_search_query(body="Earth", system="Sol") == "Earth"
    assert actions_web.plot_search_query(system="Sol") == "Sol"
    assert actions_web.plot_search_query() is None


def test_spansh_plot_search_name_disables_fuzzy_matching() -> None:
    assert actions_web.spansh_plot_search_name("Earth") == "Eart?"
    assert actions_web.spansh_plot_search_name("Sol") == "So?"
    assert actions_web.spansh_plot_search_name("A") == "?"
    assert actions_web.spansh_plot_search_name("") == ""
    assert actions_web._plot_search_obj("Jameson Memorial") == {
        "name": "Jameson Memoria?",
        "size": 1,
    }


def test_station_request_excludes_carriers_and_includes_planetary_stations_by_default() -> None:
    request = actions_web.prepare_station_request({}, {})

    assert "Drake-Class Carrier" not in request["filters"]["type"]["value"]
    assert "is_planetary" not in request["filters"]


def test_station_request_includes_carriers_and_excludes_planetary_stations() -> None:
    request = actions_web.prepare_station_request({
        "include_player_fleetcarrier": True,
        "include_planetary_stations": False,
    }, {})

    assert request["filters"]["type"]["value"][-1] == "Drake-Class Carrier"
    assert request["filters"]["is_planetary"] == {"value": False}


def test_station_request_uses_default_commodity_market_data_age() -> None:
    request = actions_web.prepare_station_request({
        "commodities": [{"name": "Tritium", "amount": 100, "transaction": "Buy"}],
    }, {})

    assert request["filters"]["market_updated_at"] == {
        "comparison": "<=>",
        "value": ["now-2d", "now"],
    }


def test_station_request_uses_requested_commodity_market_data_age() -> None:
    request = actions_web.prepare_station_request({
        "commodities": [{"name": "Tritium", "amount": 100, "transaction": "Buy"}],
        "market_days_old": 7,
    }, {})

    assert request["filters"]["market_updated_at"]["value"] == ["now-7d", "now"]


def test_station_request_rejects_invalid_commodity_market_data_age() -> None:
    try:
        actions_web.prepare_station_request({
            "commodities": [{"name": "Tritium", "amount": 100, "transaction": "Buy"}],
            "market_days_old": 0,
        }, {})
    except Exception as error:
        assert str(error) == "market_days_old must be a positive whole number."
    else:
        raise AssertionError("Expected an invalid market data age to be rejected")


def test_structured_station_search_handles_multiple_module_filters(monkeypatch) -> None:
    captured = []

    def fake_prepare(obj, states):
        captured.append(obj)
        return {"filters": {"modules": obj["modules"]}, "size": obj["size"]}

    monkeypatch.setattr(actions_web, "prepare_station_request", fake_prepare)
    monkeypatch.setattr(
        actions_web,
        "_execute_station_search",
        lambda request: {
            "amount_total": 1,
            "results": [{
                "name": "Jameson Memorial",
                "system": "Shinrarta Dezhra",
                "distance": 12.0,
                "distance_to_arrival": 100.0,
                "is_planetary": False,
                "services": ["outfitting"],
                "modules": [
                    {"name": "Beam Laser", "class": "3", "rating": "A", "price": 100},
                    {"name": "Power Plant", "class": "5", "rating": "A", "price": 200},
                ],
                "matched_filters": request["filters"]["modules"],
            }],
        },
    )

    result = actions_web.search_stations_for_modules(
        [
            {"name": "Beam Laser", "class": ["3"], "rating": ["A"]},
            {"name": "Power Plant", "class": ["5"], "rating": ["A"]},
        ],
        {},
    )

    assert result["status"] == "ok"
    assert len(result["stations"]) == 1
    station = result["stations"][0]
    assert station["name"] == "Jameson Memorial"
    assert len(station["modules"]) == 2
    assert len(station["matched_filters"]) == 2
    assert station["stock_status"] == "unknown"
    assert len(captured[0]["modules"]) == 2


def test_structured_station_search_returns_errors_as_data(monkeypatch) -> None:
    def fail(_request):
        raise RuntimeError("Spansh unavailable")

    monkeypatch.setattr(actions_web, "prepare_station_request", lambda obj, states: {"filters": {}, "size": 1})
    monkeypatch.setattr(actions_web, "_execute_station_search", fail)

    result = actions_web.search_stations_for_modules([{"name": "Beam Laser"}], {})

    assert result["status"] == "error"
    assert result["stations"] == []
    assert "Spansh unavailable" in result["error"]


def test_structured_station_search_returns_errors_as_data(monkeypatch) -> None:
    def fail(_request, **_kwargs):
        raise RuntimeError("Spansh unavailable")

    monkeypatch.setattr(actions_web, "prepare_station_request", lambda obj, states: {"filters": {}, "size": 1})
    monkeypatch.setattr(actions_web, "_execute_station_search", fail)

    result = actions_web.search_stations_for_modules([{"name": "Beam Laser"}], {})

    assert result["status"] == "error"
    assert result["stations"] == []
    assert "Spansh unavailable" in result["error"]


def test_station_response_reports_effective_commodity_market_data_age() -> None:
    response = actions_web.filter_station_response({
        "filters": {
            "market": [{"name": "Tritium"}],
            "market_updated_at": {"comparison": "<=>", "value": ["now-7d", "now"]},
        },
    }, {
        "count": 1,
        "size": 1,
        "results": [{
            "name": "Test Station",
            "system_name": "Sol",
            "distance": 0,
            "distance_to_arrival": 10,
            "is_planetary": False,
            "services": [],
            "market": [],
        }],
    })

    assert response["market_data_max_age_days"] == 7


def _spansh_station(**extra) -> dict:
    return {
        "name": "Broglie Terminal",
        "system_name": "61 Cygni",
        "system_x": -11.2,
        "system_y": -1.2,
        "system_z": 1.4,
        "distance": 11.4,
        "distance_to_arrival": 10,
        "is_planetary": False,
        "services": [],
        **extra,
    }


def test_structured_station_rows_expose_system_coordinates() -> None:
    request = {"filters": {}}
    response = {"count": 1, "size": 1, "results": [_spansh_station(material_trader="Raw")]}

    structured = actions_web.filter_station_response(request, response, include_match_metadata=True)
    llm_facing = actions_web.filter_station_response(request, response)

    assert structured["results"][0]["coords"] == {"x": -11.2, "y": -1.2, "z": 1.4}
    assert structured["results"][0]["material_trader"] == "Raw"
    assert "coords" not in llm_facing["results"][0]
    assert "material_trader" not in llm_facing["results"][0]


def test_material_trader_search_filters_type_and_large_pad(monkeypatch) -> None:
    posted: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"count": 1, "size": 5, "results": [_spansh_station(material_trader="Encoded")]}

    monkeypatch.setattr(
        actions_web.requests, "post",
        lambda url, json, timeout: posted.append(json) or FakeResponse(),
    )

    result = actions_web.search_material_traders(
        "Encoded",
        {"ShipInfo": {"LandingPadSize": "L"}, "Location": {"StarSystem": "Sol", "StarPos": [0, 0, 0]}},
    )

    filters = posted[0]["filters"]
    assert filters["material_trader"] == {"value": ["Encoded"]}
    assert filters["has_large_pad"] == {"value": True}
    assert posted[0]["sort"] == [{"distance": {"direction": "asc"}}]
    assert result["status"] == "ok"
    station = result["stations"][0]
    assert station["system"] == "61 Cygni"
    assert station["coords"] == {"x": -11.2, "y": -1.2, "z": 1.4}
    assert station["material_trader"] == "Encoded"


def test_material_trader_search_returns_errors_as_data(monkeypatch) -> None:
    def fail(_request):
        raise RuntimeError("Spansh unavailable")

    monkeypatch.setattr(actions_web, "_execute_station_search", fail)

    result = actions_web.search_material_traders("Raw", {})

    assert result["status"] == "error"
    assert result["stations"] == []
    assert "Spansh unavailable" in result["error"]


def test_resolve_plot_target_queries_all_spansh_endpoints(monkeypatch) -> None:
    posted_urls: list[str] = []

    def fake_post(url: str, request_body: dict) -> dict:
        posted_urls.append(url)
        assert request_body["size"] == 1
        assert request_body["sort"] == [{"distance": {"direction": "asc"}}]
        assert request_body["reference_system"] == "Alpha Centauri"
        if url == actions_web.SPANSH_SYSTEMS_URL:
            return {"results": [{"name": "Sol", "distance": 100.0}]}
        if url == actions_web.SPANSH_STATIONS_URL:
            return {"results": []}
        if url == actions_web.SPANSH_BODIES_URL:
            return {"results": []}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(actions_web, "_spansh_post", fake_post)

    resolved = actions_web.lookup_plot_target(system="Sol", projected_states={"Location": {"StarSystem": "Alpha Centauri"}})

    assert sorted(posted_urls) == sorted([
        actions_web.SPANSH_BODIES_URL,
        actions_web.SPANSH_STATIONS_URL,
        actions_web.SPANSH_SYSTEMS_URL,
    ])
    assert resolved is not None
    assert resolved.target_type == "system"
    assert resolved.system_name == "Sol"
