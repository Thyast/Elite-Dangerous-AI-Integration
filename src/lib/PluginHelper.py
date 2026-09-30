from pydantic.main import BaseModel
import os
from typing import Any, Callable, TypeVar

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

# Type alias for projected states dictionary
ProjectedStates = dict[str, BaseModel]

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

class PluginHelper():
    """Contains all built-inservices and managers that can be used by plugins"""

    _plugin_manager: 'PluginManager'
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
                     system_db: SystemDatabase, ed_keys: EDKeys, assistant: Assistant):
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

    def get_plugin_data_path(self, plugin_manifest: 'PluginManifest') -> str:
        """Get a plugin data path, from the plugin data folder"""
        plugin_data_path = os.path.abspath(os.path.join(self.PLUGIN_DATA_PATH, plugin_manifest.guid))
        if not os.path.exists(plugin_data_path):
            os.makedirs(plugin_data_path, exist_ok=True)
        return plugin_data_path

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
        self._event_manager.incoming.put(event)

    def send_key(self, key_name: str, *args, **kwargs):
        """Send a key"""
        self._keys.send(key_name, *args, **kwargs)

    def register_event(self, name: str, should_reply_check: Callable[[PluginEvent], bool], prompt_generator: Callable[[PluginEvent], str]):
        """Register an event type

        :param name: The name of the plugin event to register
        :param should_reply_check: A callable that takes a PluginEvent and returns True if the assistant should reply to it, False otherwise
        :param prompt_generator: A callable that takes a PluginEvent and returns a string prompt to add to the assistant conversation
        """
        def _prompt_handler(event: Event) -> str| None:
            if not isinstance(event, PluginEvent):
                return None
            if event.plugin_event_name != name:
                return None
            try:
                response = prompt_generator(event)
            except Exception as e:
                log('error', f"Plugin prompt_generator raised an exception: {e}")
                return None
            return response
        self._prompt_generator.register_prompt_event_handler(_prompt_handler)
        
        def _should_reply_check(event: Event, context: dict[str, Any]) -> bool | None:
            if not isinstance(event, PluginEvent):
                return None
            if event.plugin_event_name != name:
                return None
            try:
                return should_reply_check(event)
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
