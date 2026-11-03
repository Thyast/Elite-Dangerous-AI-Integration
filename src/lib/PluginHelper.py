from pydantic.main import BaseModel
from copy import copy, deepcopy
from dataclasses import replace
import os
from typing import Any, Callable, Literal, TypedDict, TypeVar

from .Models import LLMModel, STTModel, TTSModel, EmbeddingModel
from .Logger import log
from .EDKeys import EDKeys
from .EventManager import EventManager, Projection as _Projection
from .ActionManager import ActionManager
from .SystemDatabase import SystemDatabase
from .Config import Config, save_config
from .Event import Event, PluginEvent as _PluginEvent
from .PromptGenerator import PromptGenerator
from .Assistant import Assistant
from .PluginNamespace import PluginNamespace

# Type alias for projected states dictionary
ProjectedStates = dict[str, BaseModel]


class NavigationPreparation(TypedDict):
    status: Literal["ready", "arrived", "blocked", "failed"]
    system: str
    active_destination: str | None
    next_jump: str | None
    message: str

# reexport Projection and PluginEvent for plugins
Projection = _Projection
PluginEvent = _PluginEvent

# reexport Model base classes for plugins to extend
LLMModel = LLMModel
STTModel = STTModel
TTSModel = TTSModel
EmbeddingModel = EmbeddingModel

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .PluginManager import PluginManager
    from .PluginBase import PluginManifest

class _OwnedPluginManager:
    def __init__(self, manager: 'PluginManager', namespace: PluginNamespace) -> None:
        self.__manager = manager
        self.__namespace = namespace

    def update_plugin_setting(self, plugin_guid: str, key: str, value: Any) -> bool:
        if plugin_guid != self.__namespace.guid:
            raise ValueError('Plugin settings must belong to their owner')
        return self.__manager.update_plugin_setting(self.__namespace.guid, key, value)

    def republish_settings(self) -> None:
        self.__manager.republish_settings()

    def register_language_detector(self, plugin_guid: str, detector: Callable[[str], str | None]) -> None:
        if plugin_guid != self.__namespace.guid:
            raise ValueError('Language detectors must belong to their owner')
        self.__manager.register_language_detector(plugin_guid, detector)


class PluginHelper():
    """Contains all built-inservices and managers that can be used by plugins"""

    _plugin_manager: 'PluginManager | _OwnedPluginManager'
    _assistant: Assistant
    _prompt_generator: PromptGenerator
    _keys: EDKeys
    _vision_model: LLMModel | None = None
    _llm_model: LLMModel
    _vision_model_name: str | None = None
    _event_manager: EventManager
    _action_manager: ActionManager
    _config: Config
    _system_db: SystemDatabase
    PLUGIN_DATA_PATH: str = "plugin_data"

    def __init__(self, plugin_manager: 'PluginManager', prompt_generator: PromptGenerator, config: Config, action_manager: ActionManager, event_manager: EventManager, llm_model: LLMModel, vision_model: LLMModel | None,
                     system_db: SystemDatabase, ed_keys: EDKeys, assistant: Assistant) -> None:
        self._plugin_manager = plugin_manager
        self._prompt_generator = prompt_generator
        self._keys = ed_keys
        self._system_db = system_db
        self._vision_model = vision_model
        self._llm_model = llm_model
        self._event_manager = event_manager
        self._action_manager = action_manager
        self._config = config
        self._assistant = assistant
        self._namespace: PluginNamespace | None = None

    def _for_plugin(self, namespace: PluginNamespace) -> 'PluginHelper':
        helper = copy(self)
        helper._namespace = namespace
        helper._plugin_manager = _OwnedPluginManager(self._plugin_manager, namespace)
        helper._config = deepcopy(self._config)
        helper._config['plugin_settings'] = {namespace.guid: helper._config.get('plugin_settings', {}).get(namespace.guid, {})}
        return helper

    def get_plugin_data_path(self, plugin_manifest: 'PluginManifest | None' = None) -> str:
        """Get a plugin data path, from the plugin data folder"""
        if self._namespace is None:
            raise RuntimeError('Plugin helper is not bound to a registered plugin')
        if plugin_manifest is not None and plugin_manifest.guid != self._namespace.guid:
            raise ValueError('Plugin data paths must belong to their owner')
        plugin_data_path = os.path.abspath(os.path.join(self.PLUGIN_DATA_PATH, self._namespace.guid))
        if not os.path.exists(plugin_data_path):
            os.makedirs(plugin_data_path, exist_ok=True)
        return plugin_data_path

    def get_current_state(self) -> tuple[list[Event], ProjectedStates]:
        return self._event_manager.get_current_state()

    def prepare_navigation(self, system: str) -> NavigationPreparation:
        """Plot an exact destination, close the map and select the next jump.

        Works aboard the main ship, including while docked or landed. Uses the
        enabled host actions; never undocks, starts the FSD or changes throttle.
        """
        import json
        from openai.types.chat import ChatCompletionMessageFunctionToolCall
        from openai.types.chat.chat_completion_message_function_tool_call import Function
        from .projections.common import get_state_dict

        system = system.strip()
        if not system or system.casefold() == "unknown":
            raise ValueError("A known navigation destination is required")

        def state() -> ProjectedStates:
            return self.get_current_state()[1]

        def matches(value: str | None) -> bool:
            return bool(value and value.strip().casefold() == system.casefold())

        def result(status: Literal["ready", "arrived", "blocked", "failed"], message: str) -> NavigationPreparation:
            nav = get_state_dict(state(), "NavInfo")
            route = nav.get("NavRoute") or []
            destination = route[-1].get("StarSystem") if route else None
            if status in {"failed", "blocked"}:
                log("warn", f"Navigation preparation for {system}: {message}; active destination: {destination}")
            return {
                "status": status, "system": system,
                "active_destination": destination,
                "next_jump": nav.get("NextJumpTarget"), "message": message,
            }

        states = state()
        if matches(get_state_dict(states, "Location").get("StarSystem")):
            return result("arrived", "Already in the destination system. Complete the local stop before continuing.")
        current = get_state_dict(states, "CurrentStatus")
        flags = current.get("flags") or {}
        if (not flags.get("InMainShip") or (current.get("flags2") or {}).get("OnFoot")
                or any(flags.get(key) for key in ("FsdCharging", "FsdJump"))):
            return result("blocked", "Navigation preparation requires the commander aboard the main ship, "
                          "with no FSD charge or jump in progress.")
        available = {
            tool["function"]["name"] for tool in self._action_manager.getToolsList(
                "mainship", self._config.get("game_actions_var", False), False, False,
                self._config.get("allowed_actions", {}),
            )
        }
        required = {"plotToTarget", "target_next_system_in_route", "galaxyMapOpenOrClose"}
        if not required <= available:
            return result("blocked", "Required navigation actions are disabled or unavailable: "
                          + ", ".join(sorted(required - available)))

        def run(name: str, arguments: dict[str, Any]) -> str:
            response = self._action_manager.runAction(ChatCompletionMessageFunctionToolCall(
                id=f"plugin-navigation-{name}", type="function",
                function=Function(name=name, arguments=json.dumps(arguments)),
            ), state())
            return str(response["content"])

        nav = get_state_dict(states, "NavInfo")
        route = nav.get("NavRoute") or []
        if not route or not matches(route[-1].get("StarSystem")):
            plotted = run("plotToTarget", {"system": system})
            if plotted.startswith("ERROR:"):
                return result("failed", plotted)
            nav = get_state_dict(state(), "NavInfo")
            route = nav.get("NavRoute") or []
            if not route or not matches(route[-1].get("StarSystem")):
                return result("failed", f"Destination {system} was not confirmed. {plotted} "
                              "Do not jump using an unverified route; this is not evidence the destination is unreachable.")

        if get_state_dict(state(), "CurrentStatus").get("GuiFocus") == "GalaxyMap":
            closed = run("galaxyMapOpenOrClose", {"desired_state": "close"})
            if closed.startswith("ERROR:"):
                return result("failed", closed)
            try:
                self._event_manager.wait_for_condition(
                    "CurrentStatus", lambda s: s.GuiFocus != "GalaxyMap", 4,
                )
            except TimeoutError:
                return result("failed", f"Could not confirm the galaxy map was closed. {closed}")

        targeted = run("target_next_system_in_route", {})
        if targeted.startswith("ERROR:"):
            return result("failed", targeted)
        try:
            self._event_manager.wait_for_condition(
                "NavInfo",
                lambda s: bool(s.NavRoute and matches(s.NavRoute[-1].StarSystem)
                               and s.NextJumpTarget
                               and s.NextJumpTarget.casefold() == s.NavRoute[0].StarSystem.casefold()),
                4,
            )
        except TimeoutError:
            return result("failed", "The requested destination or next jump target was not confirmed. Do not jump.")
        nav = get_state_dict(state(), "NavInfo")
        route = nav.get("NavRoute") or []
        if (not route or not matches(route[-1].get("StarSystem"))
                or nav.get("NextJumpTarget") != route[0].get("StarSystem")
                or get_state_dict(state(), "CurrentStatus").get("GuiFocus") == "GalaxyMap"):
            return result("failed", "Navigation changed during preparation; the destination and next jump are not verified.")
        return result("ready", "Destination verified, galaxy map closed and next route system targeted. "
                      "FSD not engaged; no takeoff or throttle changes performed.")

    def register_language_detector(self, detector: Callable[[str], str | None]) -> None:
        """Register a local text-language detector for this assistant run."""
        if self._namespace is None:
            raise RuntimeError('Plugin helper is not bound to a registered plugin')
        self._plugin_manager.register_language_detector(self._namespace.guid, detector)

    def search_stations_for_modules(
        self,
        modules: list[dict[str, Any]],
        *,
        distance: int | float = 50000,
        size: int = 25,
        cache: dict[str, dict[str, Any]] | None = None,
        cache_key: str | None = None,
    ) -> dict[str, Any]:
        """Structured Spansh station lookup, shared with station_finder.

        ``cache`` is scoped by the caller to one planner calculation; it is
        never persisted. Identical normalized requests in that calculation
        make one HTTP request. ``cache_key`` lets the planner cache its one
        grouped query independently from per-module fallback queries.
        Spansh catalog matches do not guarantee live stock.
        """
        from .actions.actions_web import search_stations_for_modules
        from .UI import serialize_object
        import json

        _events, states = self._event_manager.get_current_state()
        location = states.get("Location")
        reference = (
            location.model_dump() if hasattr(location, "model_dump")
            else serialize_object(location) if location is not None else {}
        )
        outfitting = states.get("Outfitting")
        outfitting_snapshot = (
            outfitting.model_dump() if hasattr(outfitting, "model_dump")
            else serialize_object(outfitting) if outfitting is not None else {}
        )
        normalized_modules = sorted(
            (
                str(module.get("name", "")).casefold(),
                tuple(sorted(str(v).casefold() for v in (module.get("class") or []))),
                tuple(sorted(str(v).casefold() for v in (module.get("rating") or []))),
            )
            for module in modules
        )
        request_key = cache_key or json.dumps(
            {"modules": normalized_modules, "distance": distance, "size": size,
             "reference": reference,
             "outfitting_station": outfitting_snapshot.get("StationName"),
             "outfitting_timestamp": outfitting_snapshot.get("timestamp")},
            sort_keys=True,
            separators=(",", ":"),
        )
        if cache is not None and request_key in cache:
            return cache[request_key]
        # Local Outfitting is authoritative for the currently docked station.
        # Return immediately if it covers the full query; otherwise combine it
        # with the Spansh catalog results.
        outfitting_items = outfitting_snapshot.get("Items") or []
        station_name = outfitting_snapshot.get("StationName")
        station_system = outfitting_snapshot.get("StarSystem")
        local_modules = []
        local_matches = []
        if station_name and outfitting_items:
            for requested in modules:
                wanted_name = str(requested.get("name", "")).casefold()
                wanted_class = {str(v).casefold() for v in (requested.get("class") or [])}
                wanted_rating = {str(v).casefold() for v in (requested.get("rating") or [])}
                for item in outfitting_items:
                    item_dict = item if isinstance(item, dict) else serialize_object(item)
                    offered_name = str(item_dict.get("Name_Localised") or item_dict.get("Name") or "")
                    if wanted_name not in offered_name.casefold():
                        continue
                    offered_class = item_dict.get("Class")
                    offered_rating = item_dict.get("Rating")
                    if wanted_class and str(offered_class).casefold() not in wanted_class:
                        continue
                    if wanted_rating and str(offered_rating).casefold() not in wanted_rating:
                        continue
                    local_modules.append({
                        "name": offered_name,
                        "class": offered_class,
                        "rating": offered_rating,
                        "price": item_dict.get("Price"),
                        "stock_status": "station_catalog",
                    })
                    local_matches.append(requested)
                    break
        local_station = None
        if local_matches:
            star_pos = reference.get("StarPos")
            local_station = {
                "id": f"current::{str(station_name).casefold()}",
                "name": station_name,
                "system": station_system or reference.get("StarSystem", ""),
                "distance_ly": 0.0,
                "coords": (
                    dict(zip(("x", "y", "z"), star_pos))
                    if isinstance(star_pos, (list, tuple)) and len(star_pos) >= 3 else None
                ),
                "arrival_ls": 0.0,
                "is_planetary": False,
                "services": ["outfitting"],
                "modules": local_modules,
                "matched_filters": local_matches,
                "stock_status": "station_catalog",
                "source": "local Outfitting projection",
            }
        if len(local_matches) == len(modules):
            result = {"status": "ok", "stations": [local_station], "count": 1,
                      "stock_status": "station_catalog"}
        else:
            remote_modules = [request for request in modules if request not in local_matches]
            result = search_stations_for_modules(
                remote_modules or modules, states, distance=distance, size=size
            )
            if local_station:
                result = dict(result)
                result["stations"] = [local_station] + [
                    station for station in result.get("stations", [])
                    if station.get("id") != local_station["id"]
                ]
                if result.get("status") != "ok":
                    result["status"] = "ok"
        if cache is not None:
            cache[request_key] = result
        return result

    def search_material_traders(
        self,
        category: str,
        *,
        distance: int | float = 50000,
        size: int = 5,
        cache: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Nearest material traders of one type (Raw/Manufactured/Encoded).

        ``cache`` is scoped by the caller to one planner calculation, like
        ``search_stations_for_modules``; it is never persisted."""
        from .actions.actions_web import search_material_traders

        request_key = f"material_trader:{category.casefold()}:{distance}:{size}"
        if cache is not None and request_key in cache:
            return cache[request_key]
        _events, states = self._event_manager.get_current_state()
        result = search_material_traders(category, states, distance=distance, size=size)
        if cache is not None:
            cache[request_key] = result
        return result

    _ActionModelT = TypeVar('_ActionModelT', bound=BaseModel)

    def register_action(self, name: str, description: str, parameters: type[_ActionModelT], method: Callable[[_ActionModelT, dict[str, Any]], str], action_type="ship", input_template: Callable[[dict[str, Any], dict[str, Any]], str]|None=None, cache_prefill: dict[str, dict[str, Any]] | None=None):
        """Register an action"""
        def _wrapper(args: dict[str, Any], context: dict[str, Any]) -> str:
            try:
                model = parameters(**args)
                return method(model, context)
            except Exception as e:
                log('error', f"Plugin action '{name}' raised an exception: {e}")
                return f"Error executing action {name}: {str(e)}"

        self._action_manager.registerAction(
            name=name,
            description=description,
            parameters=parameters.model_json_schema(),
            method=_wrapper,
            action_type=action_type,
            input_template=input_template,
            cache_prefill=cache_prefill,
        )

    def register_projection(self, projection: Projection):
        """Register a projection to maintain state over time
        
        :param projection: The projection to register,
        """
        self._event_manager.register_projection(projection, raise_error = False)
        
    def register_sideeffect(self, sideeffect: Callable[[Event, dict[str, Any]], None]):
        """Register a sideeffect to react to events programmatically
        
        :param sideeffect: A callable that takes any incoming Event and the current projected states dict
        """
        def _sideeffect_wrapper(event: Event, context: dict[str, Any]):
            try:
                sideeffect(event, context)
            except Exception as e:
                log('error', f"Plugin sideeffect raised an exception: {e}")
        self._event_manager.register_sideeffect(_sideeffect_wrapper)

    def dispatch_event(self, event: PluginEvent):
        """Dispatch an event from an outside source
        
        :param event: The event to dispatch
        """
        if not isinstance(event, PluginEvent):
            raise ValueError("Event must be of type PluginEvent")
        if self._namespace is None:
            raise RuntimeError('Plugin helper is not bound to a registered plugin')
        self._event_manager.incoming.put(replace(event, plugin_event_name=self._namespace.event_name(event.plugin_event_name)))

    def send_key(self, key_name: str, *args, **kwargs):
        """Send a key"""
        self._keys.send(key_name, *args, **kwargs)

    def register_event(self, name: str, should_reply_check: Callable[[PluginEvent], bool], prompt_generator: Callable[[PluginEvent], str]):
        """Register an event type

        :param name: The name of the plugin event to register
        :param should_reply_check: A callable that takes a PluginEvent and returns True if the assistant should reply to it, False otherwise
        :param prompt_generator: A callable that takes a PluginEvent and returns a string prompt to add to the assistant conversation
        """
        if self._namespace is None:
            raise RuntimeError('Plugin helper is not bound to a registered plugin')
        scoped_name = self._namespace.event_name(name)
        local_name = scoped_name.removeprefix(f'{self._namespace.guid}.')

        def _prompt_handler(event: Event) -> str| None:
            if not isinstance(event, PluginEvent):
                return None
            if event.plugin_event_name != scoped_name:
                return None
            try:
                response = prompt_generator(replace(event, plugin_event_name=local_name))
            except Exception as e:
                log('error', f"Plugin prompt_generator raised an exception: {e}")
                return None
            return response
        self._prompt_generator.register_prompt_event_handler(_prompt_handler)
        
        def _should_reply_check(event: Event, context: dict[str, Any]) -> bool | None:
            if not isinstance(event, PluginEvent):
                return None
            if event.plugin_event_name != scoped_name:
                return None
            try:
                return should_reply_check(replace(event, plugin_event_name=local_name))
            except Exception as e:
                log('error', f"Plugin should_reply_check raised an exception: {e}")
                return False
        self._assistant.register_should_reply_handler(_should_reply_check)
        
    def register_status_generator(self, status_generator: Callable[[ProjectedStates], list[tuple[str, Any]]]):
        """
        Register a status generator callback, for adding stuff to the models status context (Like ship info).
            
        :param status_generator: A callable that takes the current projected states and returns a list of (title, content) tuples.
        """
        def _status_generator_wrapper(states: ProjectedStates) -> list[tuple[str, Any]]:
            try:
                return status_generator(states)
            except Exception as e:
                log('error', f"Plugin status_generator raised an exception: {e}")
                return []
        self._prompt_generator.register_status_generator(_status_generator_wrapper)

    def wait_for_condition(self, projection_name: str, condition_fn, timeout=None):
        """Block until `condition_fn` is satisfied by the current or future
        state of the specified projection.

        :param projection_name: Name/identifier of the projection to watch.
        :param condition_fn: A callable that takes a dict (the current projection state)
                             and returns True/False.
        :param timeout: Optional timeout (seconds).
        :return: The state dict that satisfied the condition.
        :raises TimeoutError: If the condition isn't met within `timeout`.
        """
        def _condition_fn_wrapper(state: dict) -> bool:
            try:
                return condition_fn(state)
            except Exception as e:
                log('error', f"Plugin wait_for_condition condition_fn raised an exception: {e}")
                return False
        return self._event_manager.wait_for_condition(projection_name, _condition_fn_wrapper, timeout)
