import ast
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.VrOverlayAdjustment import PROFILE_DEFAULTS, POSITION_LIMITS, VrOverlayAdjustment, is_adjustment_request, normalize_command, opacity_request, parse_movement, reduction_request, ship_profile_key


@pytest.mark.parametrize("text", [
    "Ajuste la position de la fen\u00eatre de Covas:Next.",
    "ajuste la position de la GUI", "r\u00e8gle la position de la fen\u00eatre",
    "Adjust the position of the window", "adjust position window",
    "r\u00e8gle la fen\u00eatre", "on va r\u00e9gler la fen\u00eatre d'overlaid.",
    "r\u00e8gle la fen\u00eatre Kovacs Next", "ajuste la position de la fen\u00eatre de Kovac Next",
    "r\u00e8gle l'overlay", "peux-tu r\u00e9gler la fen\u00eatre de l\u2019overlay VR",
    "je voudrais ajuster la GUI de Covac:Next", "please adjust the overlay",
    "could you adjust the position of the window please",
])
def test_start_phrases(text):
    assert is_adjustment_request(text)


@pytest.mark.parametrize("text,axis,delta", [
    ("plus haut", "vertical", 0.05), ("plus bas", "vertical", -0.05),
    ("\u00e0 gauche", "horizontal", -0.05), ("\u00e0 droite", "horizontal", 0.05),
    ("plus pr\u00e8s", "distance", -0.05), ("plus loin", "distance", 0.05),
    ("plus haut de 10 centim\u00e8tres", "vertical", 0.1),
    ("\u00e0 droite de 0,2 m\u00e8tre", "horizontal", 0.2),
    ("plus loin de un m\u00e8tre", "distance", 1),
    ("plus pr\u00e8s de z\u00e9ro virgule deux m\u00e8tres", "distance", -0.2),
    ("rotation sur l'axe horizontal", "pitch", 5),
    ("rotation sur l\u2019axe vertical de moins dix degr\u00e9s", "yaw", -10),
    ("rotation sur l'axe horizontal de -5 degr\u00e9s", "pitch", -5),
    ("up by 5 cm", "vertical", 0.05), ("yaw by minus five degrees", "yaw", -5),
    ("rotation sur l'axe vertical de - 5 degr\u00e8s", "yaw", -5),
    ("rotation sur l'axe horizontal de + 5 degr\u00e9s", "pitch", 5),
    ("rotation sur l'axe horizontal de +cinq degr\u00e9s", "pitch", 5),
    ("pitch by \u2212 5 degrees", "pitch", -5),
    ("yaw -\u00a05\u00b0", "yaw", -5),
    ("plus haut de -1 m", "vertical", -1),
    ("plus haut de -5 centim\u00e8tre", "vertical", -0.05),
    ("plus haut de - 5 centim\u00e8tres", "vertical", -0.05),
    ("plus haut de moins cinq centim\u00e8tres", "vertical", -0.05),
    ("plus bas de - 5 cm", "vertical", 0.05),
    ("a droite de +5 cm", "horizontal", 0.05),
    ("a gauche de + 5 cm", "horizontal", -0.05),
    ("plus loin de - 0,2 metre", "distance", -0.2),
    ("up by - 0 point two meters", "vertical", -0.2),
])
def test_default_and_explicit_increments(text, axis, delta):
    assert parse_movement(text) == (axis, delta)


@pytest.mark.parametrize("text", [
    "plus haut de 5 degr\u00e9s", "yaw 2 metres", "plus bas de 0 m",
    "plus haut de inf m", "plus haut de NaN m", "plus haut et tire", "attaque", "plus haut 0.1 0.2 m",
    "yaw --5 degrees", "yaw - +5 degrees", "plus haut de + 0 cm", "plus haut de - 0 cm",
    "yaw moins -5 degrees", "yaw + degrees",
])
def test_invalid_and_ambiguous_commands_are_rejected(text):
    with pytest.raises(ValueError):
        parse_movement(text)


class Harness:
    def __init__(self):
        self.config = {key: 0.0 for key in POSITION_LIMITS}
        self.config.update(ui_actions_var=True, unrelated="preserved")
        self.config.update(game_actions_var=True, allowed_actions={"recenterVRView": True})
        self.messages = []
        self.replies = []
        self.saves = []
        self.busy = False
        self.time = 0
        self.recenter = Mock(return_value=True)
        self.control = VrOverlayAdjustment(
            get_config=lambda: self.config,
            save=self.save, emit=self.messages.append,
            reply=lambda message, error: self.replies.append((message, error)),
            busy=lambda: self.busy, clock=lambda: self.time, assistant_name="Simone",
            recenter=self.recenter,
        )

    def save(self, values):
        self.saves.append(copy.deepcopy(values))
        self.config = {**self.config, **values}

    @property
    def request(self):
        return next(message for message in reversed(self.messages) if message["type"] == "vr_placement_request")

    def respond(self, **overrides):
        result = {
            "request_id": self.request["request_id"], "success": True,
            "active": self.request["operation"] not in ("finish", "cancel"),
            "config": {key: self.config.get(key, 0.0) for key in POSITION_LIMITS}, **overrides,
        }
        self.control.handle_result(result)

    def begin(self):
        assert self.control.handle_input("ajuste la position de la fen\u00eatre de Covas:Next")
        self.respond()


@pytest.mark.parametrize("ship,key", [
    ({"Type": "sidewinder", "LandingPadSize": "S"}, "size:S"),
    ({"Type": "krait_mkii", "LandingPadSize": "M"}, "size:M"),
    ({"Type": "anaconda", "LandingPadSize": "L"}, "size:L"),
    ({"Type": "New_Ship", "LandingPadSize": "Unknown"}, "type:new_ship"),
    ({"Type": "Unknown", "LandingPadSize": "Unknown"}, None),
])
def test_ship_profile_prefers_known_pad_size_and_falls_back_to_ship_type(ship, key):
    assert ship_profile_key(ship) == key


def full_harness():
    h = Harness()
    h.config.update(overlay_mode="vr-full", overlay_vr_full_last=dict(PROFILE_DEFAULTS), overlay_vr_full_profiles={})
    return h


def apply_ship(h, size, ship_type="test"):
    h.control.update_ship({"Type": ship_type, "LandingPadSize": size}, force=True)
    assert h.request["operation"] == "apply_profile"
    values = h.request["config"]
    h.respond(active=False, config=values)
    return values


def test_full_window_profiles_inherit_last_values_save_by_size_and_restore_on_switch():
    h = full_harness()
    apply_ship(h, "S", "sidewinder")
    assert not h.recenter.called
    h.control.persist({"overlay_vr_vertical_offset": 0.3, "overlay_vr_gaze_reduction_factor": 5})
    small = dict(h.config["overlay_vr_full_last"])
    assert h.config["overlay_vr_vertical_offset"] == 0
    assert h.config["overlay_vr_full_profiles"] == {"size:S": small}
    assert apply_ship(h, "M", "krait_mkii") == small
    assert "size:M" not in h.config["overlay_vr_full_profiles"]
    h.control.persist({"overlay_vr_vertical_offset": -0.2, "overlay_vr_gaze_reduction_factor": 1})
    medium = dict(h.config["overlay_vr_full_last"])
    assert apply_ship(h, "S", "eagle") == small
    assert h.config["overlay_vr_full_last"] == small
    assert apply_ship(h, "L", "anaconda") == small
    assert apply_ship(h, "M") == medium
    h.control.update_ship({"Type": "another_medium", "LandingPadSize": "M"})
    assert h.control.pending is None
    assert not h.recenter.called


def test_profile_restart_and_unknown_ship_size_do_not_overwrite_other_profiles():
    h = full_harness()
    h.config["overlay_vr_full_profiles"]["type:new_ship"] = {**PROFILE_DEFAULTS, "overlay_vr_vertical_offset": -0.4}
    assert apply_ship(h, "Unknown", "NEW_SHIP")["overlay_vr_vertical_offset"] == -0.4
    h.control.persist({"overlay_vr_horizontal_offset": 0.5})
    h.config["overlay_vr_full_profiles"]["size:S"] = dict(PROFILE_DEFAULTS)
    restarted = full_harness()
    restarted.config = copy.deepcopy(h.config)
    assert apply_ship(restarted, "Unknown", "new_ship")["overlay_vr_horizontal_offset"] == 0.5
    assert apply_ship(restarted, "Unknown", "unknown")["overlay_vr_vertical_offset"] == -0.4
    restarted.control.persist({"overlay_vr_vertical_offset": 0.7})
    assert set(restarted.config["overlay_vr_full_profiles"]) == {"type:new_ship", "size:S"}


def test_switch_during_adjustment_is_deferred_and_cancel_does_not_save_a_profile():
    h = full_harness()
    apply_ship(h, "S")
    h.begin()
    h.control.update_ship({"Type": "krait", "LandingPadSize": "M"})
    h.control.tick()
    assert h.control.pending is None
    assert h.control.applied_ship_key == "size:S"
    h.control.handle_input("annuler")
    h.respond()
    assert not h.config["overlay_vr_full_profiles"]
    h.control.tick()
    assert h.request["operation"] == "apply_profile"
    h.respond(active=False, config=h.request["config"])
    assert h.control.applied_ship_key == "size:M"


def test_invalid_or_rejected_profile_is_reported_without_saving_or_recentering():
    h = full_harness()
    h.config["overlay_vr_full_profiles"]["size:S"] = {"overlay_vr_gaze_reduction_factor": 0.5}
    h.control.update_ship({"LandingPadSize": "S"})
    assert h.control.pending is None
    assert h.replies[-1][1] is True
    h.config["overlay_vr_full_profiles"]["size:S"] = dict(PROFILE_DEFAULTS)
    h.control.update_ship({"LandingPadSize": "S"}, force=True)
    h.respond(success=False, error="Native failure")
    assert not h.saves
    assert h.control.applied_ship_key is None
    assert h.replies[-1][1] is True
    assert not h.recenter.called


@pytest.mark.parametrize("command,size,curve", [
    ("taille de la fenetre a un metre", 1, 0),
    ("set window size to 50 cm", 0.5, 0),
    ("courbure a trente degres", 0.9, 30 / 360),
    ("set curvature to 180 degrees", 0.9, 0.5),
    ("courbure a 0 degres", 0.9, 0),
    ("courbure a zero degres", 0.9, 0),
    ("set curvature to zero degrees", 0.9, 0),
])
def test_voice_appearance_is_applied_and_saved_to_the_active_ship_profile(command, size, curve):
    h = full_harness()
    apply_ship(h, "L")
    assert h.control.handle_input(command)
    assert h.request["operation"] == "set_appearance"
    assert h.request["size"] == size
    assert h.request["curvature"] == curve
    h.respond(size=size, curvature=curve, previous={"size": 0.9, "curvature": 0})
    assert h.config["overlay_vr_full_profiles"]["size:L"]["overlay_vr_size_meters"] == size
    assert h.config["overlay_vr_full_last"]["overlay_vr_curvature"] == curve


@pytest.mark.parametrize("command", [
    "taille a 0.05 m", "taille a 4 m", "courbure a 181 degres", "courbure a -5 degres",
    "facteur de reduction a 0.5", "facteur de reduction a -1",
])
def test_in_game_profile_limits_reject_invalid_appearance_and_factors(command):
    h = full_harness()
    apply_ship(h, "M")
    assert h.control.handle_input(command)
    assert h.control.pending is None
    assert h.replies[-1][1] is True


def test_commands_only_intercept_placement_requests_outside_a_session():
    h = Harness()
    for command in ("plus haut", "annuler", "attaque", "ajuste la position du vaisseau"):
        assert not h.control.handle_input(command)
    h.config["ui_actions_var"] = False
    assert h.control.handle_input("ajuste la position de la fen\u00eatre")
    assert not h.control.active
    assert not h.messages
    assert h.replies[-1][1]
    h.config["ui_actions_var"] = True
    h.busy = True
    h.control.handle_input("ajuste la position de la fen\u00eatre")
    assert not h.control.active


@pytest.mark.parametrize("command", [
    "on va r\u00e9gler la fen\u00eatre d'overlaid.",
    "Simon, r\u00e8gle la fen\u00eatre Kovacs Next.",
    "Simonne ajuste la position de la fen\u00eatre de Kovac Next.",
])
def test_reported_stt_transcripts_enter_local_adjustment_instead_of_llm_actions(command):
    source, methods = chat_methods("submit_input")
    namespace = {"show_chat_message": Mock()}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
    h = Harness()
    chat = SimpleNamespace(vr_adjustment=h.control, event_manager=SimpleNamespace(add_conversation_event=Mock()))
    namespace["submit_input"](chat, command)
    assert h.control.active
    assert h.request["operation"] == "begin"
    chat.event_manager.add_conversation_event.assert_not_called()
    namespace["show_chat_message"].assert_called_once_with("cmdr", command)
    h.respond()
    assert "cinq centim" in h.replies[-1][0]


@pytest.mark.parametrize("command", [
    "cache la fen\u00eatre Kovacs Next", "montre l'overlaid", "affiche la navigation",
    "show the window", "ajuste la position du vaisseau", "regle la vitesse",
    "ne r\u00e8gle pas la fen\u00eatre", "je ne veux pas r\u00e9gler la fen\u00eatre",
    "comment r\u00e9gler la fen\u00eatre", "annule le r\u00e9glage de la fen\u00eatre",
    "regle la fenetre et attaque", "Simon, cache la GUI",
])
def test_unrelated_or_negated_requests_are_not_intercepted(command):
    h = Harness()
    assert not h.control.handle_input(command)
    assert not h.messages and not h.control.active


@pytest.mark.parametrize("command", [
    "r\u00e9initialise la vue", "recentre la vue",
    "Simonne, recentre la vue.", "Simon, r\u00e9initialise la vue s'il te pla\u00eet",
    "peux-tu recentrer la vue", "recenter the VR view", "reset the view", "recentre view",
])
def test_recenter_commands_bypass_the_llm_without_changing_overlay_settings(command):
    source, methods = chat_methods("submit_input")
    namespace = {"show_chat_message": Mock()}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
    h = Harness()
    h.config["ui_actions_var"] = False
    initial = copy.deepcopy(h.config)
    chat = SimpleNamespace(vr_adjustment=h.control, event_manager=SimpleNamespace(add_conversation_event=Mock()))
    namespace["submit_input"](chat, command)
    h.recenter.assert_not_called()
    assert h.request["operation"] == "recenter"
    assert not h.saves
    h.respond(recentered=True, active=False)
    h.recenter.assert_called_once_with()
    chat.event_manager.add_conversation_event.assert_not_called()
    assert not h.control.active and not h.saves
    assert h.config == initial
    assert not h.replies[-1][1]


@pytest.mark.parametrize("command", [
    "ne recentre pas la vue", "comment recentrer la vue", "recentre la vue et tire",
    "r\u00e9initialise la fen\u00eatre", "r\u00e9initialise la route",
])
def test_recenter_does_not_intercept_other_or_negated_commands(command):
    h = Harness()
    assert not h.control.handle_input(command)
    h.recenter.assert_not_called()


@pytest.mark.parametrize("command", [
    "", "LoadGame", "GameModeChange", "CarrierLocation",
    "focus", "focus elite", "focus Elite Dangerous",
    "ne recentre pas la vue", "comment recentrer la vue",
])
def test_recenter_backend_rejects_events_and_non_recenter_commands(command):
    h = Harness()
    with pytest.raises(ValueError, match="explicit"):
        h.control.request_recenter(command)
    assert not h.messages and h.control.pending is None
    h.recenter.assert_not_called()


@pytest.mark.parametrize("command", ["focus", "focus elite", "focus Elite Dangerous"])
def test_focus_commands_do_not_request_recenter_or_consume_voiceattack_commands(command):
    h = Harness()
    assert not h.control.handle_input(command)
    assert not h.messages and not h.saves
    h.recenter.assert_not_called()


def test_unsolicited_native_recenter_ack_does_not_dispatch_other_applications():
    h = Harness()
    h.control.handle_result({"request_id": "startup", "success": True, "recentered": True})
    assert not h.messages and not h.saves
    h.recenter.assert_not_called()


@pytest.mark.parametrize("blocked", ["game_actions", "permission", "busy", "pending"])
def test_recenter_respects_game_permissions_and_in_flight_actions(blocked):
    h = Harness()
    if blocked == "game_actions":
        h.config["game_actions_var"] = False
    elif blocked == "permission":
        h.config["allowed_actions"] = {}
    elif blocked == "busy":
        h.busy = True
    else:
        h.control.handle_input("facteur de reduction 9")
    assert h.control.handle_input("recentre la vue")
    h.recenter.assert_not_called()
    assert h.replies[-1][1]


def test_recenter_keeps_the_adjustment_transaction_and_reports_game_errors():
    h = Harness()
    h.begin()
    h.control.handle_input("plus haut")
    position = {key: 0.0 for key in POSITION_LIMITS}
    position["overlay_vr_vertical_offset"] = 0.05
    h.respond(config=position)
    session_id = h.control.session_id
    h.control.handle_input("recentre la vue")
    h.respond(recentered=True)
    h.recenter.assert_called_once_with()
    assert h.control.active and h.control.session_id == session_id
    assert h.control.draft == position and not h.saves
    h.recenter.side_effect = RuntimeError("HMDReset is not bound")
    h.control.handle_input("r\u00e9initialise la vue")
    h.respond(recentered=True)
    assert h.replies[-1][1] and "HMDReset" in h.replies[-1][0]
    assert h.control.active and h.control.draft == position
    h.control.handle_input("annuler")
    h.respond()
    assert not h.control.active


@pytest.mark.parametrize("response", [
    {"success": False, "error": "No tracking"},
    {"success": True},
    {"success": True, "recentered": False},
])
def test_recenter_never_dispatches_other_components_without_a_native_ack(response):
    h = Harness()
    h.control.handle_input("recentre la vue")
    h.respond(**response)
    h.recenter.assert_not_called()
    assert h.replies[-1][1]
    assert not h.saves


def test_recenter_timeout_and_stale_ack_do_not_dispatch_game_input():
    h = Harness()
    h.control.handle_input("recentre la vue")
    request_id = h.request["request_id"]
    h.time = 6
    h.control.tick()
    assert h.control.pending is None and h.replies[-1][1]
    h.control.handle_result({"request_id": request_id, "success": True, "recentered": True})
    h.recenter.assert_not_called()


def test_explicit_recenter_request_uses_native_ack_and_permissions():
    h = Harness()
    assert "not a confirmation" in h.control.request_recenter("recentre la vue")
    assert h.request["operation"] == "recenter"
    h.respond(recentered=True)
    h.recenter.assert_called_once_with()
    h.config["allowed_actions"]["recenterVRView"] = False
    with pytest.raises(ValueError, match="recenterVRView"):
        h.control.request_recenter("recentre la vue")


@pytest.mark.parametrize("command,notice", [
    ("recentre la vue", "OpenKneeboard indisponible"),
    ("recenter the view", "OpenKneeboard unavailable"),
])
def test_optional_openkneeboard_absence_reports_only_the_actual_dispatch_without_an_error(command, notice):
    h = Harness()
    h.recenter.return_value = False
    h.control.handle_input(command)
    h.respond(recentered=True)
    message, error = h.replies[-1]
    assert error is False
    assert notice in message
    assert "jeu et OpenKneeboard" not in message and "game and OpenKneeboard" not in message
    assert not h.saves and not h.control.active


def test_address_prefix_applies_to_movements_factors_and_confirmation_without_changing_profile_names():
    h = Harness()
    h.begin()
    h.control.handle_input("Simonne, plus haut")
    assert h.request["operation"] == "move" and h.request["delta"] == 0.05
    h.respond()
    h.control.handle_input("Simon, r\u00e8gle le facteur de r\u00e9duction de la fen\u00eatre \u00e0 9")
    assert h.request["operation"] == "set_reduction" and h.request["factor"] == 9
    h.respond(reduction_factor=9, previous_factor=5)
    h.control.handle_input("Simone, valider la position")
    assert h.request["operation"] == "commit"
    h.respond()
    h.respond()
    assert not h.control.active
    assert is_adjustment_request("L\u00e9a Marie, r\u00e8gle la fen\u00eatre", "L\u00e9a Marie")
    assert not is_adjustment_request("Simon, r\u00e8gle la fen\u00eatre", "Astra")
    h.control.assistant_name = "Astra"
    h.control.handle_input("Astra, r\u00e8gle la fen\u00eatre")
    h.respond()
    h.control.handle_input("Astra, annuler")
    assert h.request["operation"] == "cancel"
    h.respond()
    assert not h.control.active


def test_transaction_previews_then_saves_only_on_validation_and_waits_for_finish():
    h = Harness()
    h.begin()
    session_id = h.request["session_id"]
    initial = copy.deepcopy(h.config)
    assert h.control.active
    assert "cinq centim" in h.replies[-1][0]
    assert not h.saves
    h.control.handle_input("plus haut de 10 centim\u00e8tres")
    assert h.request["axis"] == "vertical" and h.request["delta"] == 0.1
    assert h.request["session_id"] == session_id
    position = {key: 0.0 for key in POSITION_LIMITS}
    position["overlay_vr_vertical_offset"] = 0.1
    h.respond(config=position)
    assert h.config == initial
    assert h.messages[-1] == {"type": "vr_placement_state", "active": True, "config": {**PROFILE_DEFAULTS, **position}}
    h.config["unrelated"] = "edited on desktop during preview"
    h.control.handle_input("valider la position")
    assert h.request["operation"] == "commit"
    assert not h.saves
    h.respond(config=position)
    assert h.saves == [position]
    assert h.config["unrelated"] == "edited on desktop during preview"
    assert h.request["operation"] == "finish"
    assert h.control.active
    h.respond(config=position)
    assert not h.control.active
    assert h.messages[-1]["active"] is False
    assert "enregistr" in h.replies[-1][0]


@pytest.mark.parametrize("offset", [-1.5, -1.0, 1.5])
def test_distance_offsets_within_extended_limits_are_saved_and_reloaded(offset):
    h = Harness()
    h.begin()
    direction = "plus pres" if offset < 0 else "plus loin"
    assert h.control.handle_input(f"{direction} de {abs(offset)} m")
    assert h.request["axis"] == "distance" and h.request["delta"] == offset
    position = {key: 0.0 for key in POSITION_LIMITS}
    position["overlay_vr_distance_offset"] = offset
    h.respond(config=position)
    assert h.control.draft == position and not h.replies[-1][1]
    h.control.handle_input("valider la position")
    h.respond(config=position)
    assert h.saves == [position]
    assert h.request["operation"] == "finish"
    h.respond()
    assert not h.control.active
    h.begin()
    assert h.control.draft == position and not h.replies[-1][1]


@pytest.mark.parametrize("offset", [-1.500001, 1.500001])
def test_distance_offsets_outside_limits_are_not_saved(offset):
    h = Harness()
    h.begin()
    h.control.handle_input("valider la position")
    position = {key: 0.0 for key in POSITION_LIMITS}
    position["overlay_vr_distance_offset"] = offset
    h.respond(config=position)
    assert h.control.active and not h.saves
    assert h.replies[-1][1]
    assert h.control.draft["overlay_vr_distance_offset"] == 0


def test_cancel_does_not_save_any_preview_and_restores_configuration_in_the_ui():
    h = Harness()
    h.begin()
    h.control.handle_input("plus haut")
    position = {key: 0.0 for key in POSITION_LIMITS}
    position["overlay_vr_vertical_offset"] = 0.05
    h.respond(config=position)
    h.control.handle_input("annuler")
    assert h.request["operation"] == "cancel"
    h.respond()
    assert not h.saves
    assert not h.control.active
    assert h.messages[-1]["config"]["overlay_vr_vertical_offset"] == 0
    assert not h.control.handle_input("plus haut")


def test_unknown_commands_and_pending_requests_cannot_reach_game_actions():
    h = Harness()
    h.begin()
    count = len(h.messages)
    assert h.control.handle_input("tire sur le vaisseau")
    assert len(h.messages) == count
    assert h.replies[-1][1]
    assert h.control.handle_input("plus haut")
    request = h.request.copy()
    assert h.control.handle_input("plus bas")
    assert h.request == request
    assert h.replies[-1][1]


def test_native_failures_and_malformed_results_never_save_and_preserve_cancellation():
    h = Harness()
    h.control.handle_input("ajuste la position de la fen\u00eatre")
    h.respond(success=False, error="No active VR GUI")
    assert not h.control.active
    assert "No active VR GUI" in h.replies[-1][0]
    h.begin()
    h.control.handle_input("plus haut")
    h.respond(success=False, error="Native placement rejected")
    assert h.control.active and not h.saves
    h.control.handle_input("valider la position")
    h.respond(config={"unexpected": 1})
    assert h.control.active and not h.saves
    assert h.replies[-1][1]
    h.control.handle_input("annuler")
    h.respond()
    assert not h.control.active


def test_save_failure_keeps_the_session_open_for_retry_or_cancel():
    h = Harness()
    h.begin()
    h.control.save = Mock(side_effect=OSError("Disk full"))
    h.control.handle_input("valider la position")
    h.respond()
    assert h.control.active
    assert not h.control.saved
    assert "Disk full" in h.replies[-1][0]
    assert h.request["operation"] == "commit"
    h.control.save = h.save
    h.control.handle_input("valider la position")
    h.respond()
    assert h.request["operation"] == "finish"
    h.respond()
    assert not h.control.active


def test_timeout_and_stale_results_are_explicit_and_do_not_unlock_game_commands():
    h = Harness()
    h.begin()
    h.control.handle_input("plus haut")
    stale = h.request["request_id"]
    h.time = 6
    h.control.tick()
    assert h.control.active and h.control.pending is None
    assert h.replies[-1][1]
    h.control.handle_input("annuler")
    pending = h.control.pending
    h.control.handle_result({"request_id": stale, "success": True})
    assert h.control.pending == pending
    h.respond()
    assert not h.control.active


def test_finish_timeout_can_retry_without_saving_twice_or_can_cancel_saved_values():
    h = Harness()
    h.begin()
    h.control.handle_input("valider la position")
    h.respond()
    assert len(h.saves) == 1
    h.time = 6
    h.control.tick()
    h.control.handle_input("valider la position")
    assert h.request["operation"] == "finish"
    h.respond()
    assert not h.control.active and len(h.saves) == 1
    h.begin()
    h.control.handle_input("valider la position")
    h.respond()
    h.respond(success=False, error="Temporary finish failure")
    assert h.control.active and h.control.saved
    h.control.handle_input("annuler")
    h.respond()
    assert not h.control.active
    assert len(h.saves) == 3


def chat_methods(*names):
    source = Path(__file__).resolve().parents[2] / "src" / "Chat.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "Chat")
    return source, [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]


def test_chat_routes_inputs_before_conversation_and_saves_only_the_position_fields():
    source, methods = chat_methods("submit_input", "_save_vr_position")
    messages = []
    saved = []
    namespace = {
        "show_chat_message": Mock(), "save_config": lambda config: saved.append(config),
        "emit_message": lambda kind, **payload: messages.append((kind, payload)),
        "cast": lambda _type, value: value, "Config": dict,
        "Any": Any,
    }
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
    chat = SimpleNamespace(
        config={"unrelated": "retained"},
        vr_adjustment=SimpleNamespace(handle_input=Mock(return_value=True)),
        event_manager=SimpleNamespace(add_conversation_event=Mock()),
    )
    namespace["submit_input"](chat, "plus haut")
    chat.event_manager.add_conversation_event.assert_not_called()
    chat.vr_adjustment.handle_input.return_value = False
    namespace["submit_input"](chat, "bonjour")
    chat.event_manager.add_conversation_event.assert_called_once_with("user", "bonjour")
    namespace["_save_vr_position"](chat, {"overlay_vr_vertical_offset": 0.1})
    assert saved == [{"unrelated": "retained", "overlay_vr_vertical_offset": 0.1}]
    assert messages == [("running_config", {"config": chat.config})]


@pytest.mark.parametrize("command,factor", [
    ("facteur de r\u00e9duction de la fen\u00eatre 5", 5),
    ("r\u00e8gle le facteur de r\u00e9duction \u00e0 cinq", 5),
    ("r\u00e9duis la fen\u00eatre d'un facteur 9", 9),
    ("set window reduction factor to 12.5", 12.5),
    ("r\u00e8gle le facteur de zoom \u00e0 cinq", 5),
    ("facteur de zoom de la fen\u00eatre \u00e0 neuf", 9),
    ("set zoom factor to five", 5),
    ("zoom factor 9", 9),
])
def test_voice_area_factor_updates_the_live_overlay_and_persists_only_after_ack(command, factor):
    assert reduction_request(command) is not None
    h = Harness()
    assert h.control.handle_input(command)
    assert not h.control.active
    assert not h.saves
    assert h.request["operation"] == "set_reduction"
    assert h.request["factor"] == factor
    h.respond(reduction_factor=factor, previous_factor=5, active=False)
    assert h.saves == [{"overlay_vr_gaze_reduction_factor": factor}]
    assert h.config["overlay_vr_gaze_reduction_factor"] == factor


def test_voice_factor_failures_restore_the_live_value_and_do_not_end_position_adjustment():
    h = Harness()
    h.begin()
    for text in ("facteur de reduction 0", "facteur de reduction", "facteur de reduction cinq degres"):
        assert h.control.handle_input(text)
        assert h.control.pending is None and h.replies[-1][1]
    h.control.save = Mock(side_effect=OSError("Disk full"))
    h.control.handle_input("facteur de reduction 9")
    h.respond(reduction_factor=9, previous_factor=5)
    assert h.control.active
    assert h.request["operation"] == "restore_reduction"
    assert h.request["factor"] == 5
    h.respond(reduction_factor=5, previous_factor=9)
    assert h.control.active
    assert not h.saves
    h.control.handle_input("annuler")
    h.respond()
    assert not h.control.active


@pytest.mark.parametrize("command,factor", [
    ("facteur de r\u00e9duction \u00e0 cinq", 5),
    ("Simon, r\u00e8gle le facteur de zoom \u00e0 neuf", 9),
    ("set zoom factor to 12.5", 12.5),
])
def test_factor_commands_during_adjustment_update_immediately_and_keep_the_position_draft(command, factor):
    h = Harness()
    h.begin()
    assert "facteur de r\u00e9duction" in h.replies[-1][0]
    h.control.handle_input("plus haut")
    position = {**h.control.draft, "overlay_vr_vertical_offset": 0.25}
    h.respond(config=position)
    assert h.control.handle_input(command)
    assert h.request["operation"] == "set_reduction"
    assert h.request["factor"] == factor
    assert not h.saves
    h.respond(reduction_factor=factor, previous_factor=5)
    assert h.control.active
    assert h.control.draft == position
    assert h.messages[-1]["active"] is True
    assert h.saves == [{"overlay_vr_gaze_reduction_factor": factor}]
    h.control.handle_input("annuler")
    h.respond()
    assert not h.control.active
    assert h.config["overlay_vr_gaze_reduction_factor"] == factor
    assert h.config["overlay_vr_vertical_offset"] == 0


@pytest.mark.parametrize("command,axis,delta", [
    ("Simon, rotation sur l'axe vertical de - 5 degr\u00e8s", "yaw", -5),
    ("rotation sur l'axe horizontal de + 5 degr\u00e9s", "pitch", 5),
    ("plus haut de - 5 centim\u00e8tres", "vertical", -0.05),
    ("a droite de + 5 centim\u00e8tres", "horizontal", 0.05),
])
def test_stt_signed_increments_reach_the_native_adjustment_unchanged(command, axis, delta):
    h = Harness()
    h.begin()
    assert h.control.handle_input(command)
    assert h.request["operation"] == "move"
    assert h.request["axis"] == axis
    assert h.request["delta"] == delta
    assert not h.saves


@pytest.mark.parametrize("command,opacity", [
    ("opacit\u00e9 \u00e0 25 %", 0.25),
    ("r\u00e8gle l'opacit\u00e9 de la fen\u00eatre r\u00e9duite \u00e0 40%", 0.4),
    ("Simone, mets l'opacit\u00e9 r\u00e9duite \u00e0 vingt pour cent", 0.2),
    ("transparence \u00e0 75 pour cent", 0.25),
    ("transparence de la fen\u00eatre en miniature \u00e0 0 %", 1),
    ("set reduced opacity to 30 percent", 0.3),
    ("set the minimized window opacity to 100%", 1),
    ("transparency 90%", 0.1),
])
def test_voice_reduced_opacity_updates_the_live_overlay_and_persists_only_after_ack(command, opacity):
    assert opacity_request(normalize_command(command, "Simone")) is not None
    h = Harness()
    assert h.control.handle_input(command)
    assert not h.control.active
    assert not h.saves
    assert h.request["operation"] == "set_opacity"
    assert h.request["opacity"] == opacity
    h.respond(reduced_opacity=opacity, previous_opacity=0.25, active=False)
    assert h.saves == [{"overlay_vr_gaze_reduced_opacity": opacity}]
    assert "pour cent" in h.replies[-1][0] or "percent" in h.replies[-1][0]
    assert not h.replies[-1][1]


@pytest.mark.parametrize("command", [
    "opacit\u00e9", "opacit\u00e9 \u00e0 5 %", "opacit\u00e9 \u00e0 120 %", "transparence \u00e0 95 %",
    "opacit\u00e9 \u00e0 25 degr\u00e9s", "opacit\u00e9 \u00e0 beaucoup", "set opacity to minus 20 percent",
])
def test_invalid_reduced_opacity_commands_are_rejected_without_a_request(command):
    h = Harness()
    assert h.control.handle_input(command)
    assert h.control.pending is None
    assert not h.messages
    assert h.replies[-1][1]


def test_opacity_during_adjustment_keeps_the_draft_and_failed_saves_restore_the_live_value():
    h = Harness()
    h.begin()
    assert "opacit\u00e9" in h.replies[-1][0]
    h.control.handle_input("plus haut")
    position = {**h.control.draft, "overlay_vr_vertical_offset": 0.25}
    h.respond(config=position)
    h.control.save = Mock(side_effect=OSError("Disk full"))
    assert h.control.handle_input("opacit\u00e9 \u00e0 50 %")
    h.respond(reduced_opacity=0.5, previous_opacity=0.25)
    assert h.request["operation"] == "restore_opacity"
    assert h.request["opacity"] == 0.25
    h.respond(reduced_opacity=0.25, previous_opacity=0.5)
    assert h.control.active
    assert h.control.draft == position
    h.control.save = h.save
    assert h.control.handle_input("opacit\u00e9 \u00e0 50 %")
    h.respond(reduced_opacity=0.5, previous_opacity=0.25)
    assert h.saves == [{"overlay_vr_gaze_reduced_opacity": 0.5}]
    assert h.control.active
    h.control.handle_input("annuler")
    h.respond()
    assert not h.control.active
    assert h.config["overlay_vr_gaze_reduced_opacity"] == 0.5


def test_invalid_opacity_acknowledgement_is_reported_without_saving():
    h = Harness()
    h.control.handle_input("opacit\u00e9 \u00e0 50 %")
    h.respond(reduced_opacity=5, previous_opacity=0.25)
    assert not h.saves
    assert h.replies[-1][1]
