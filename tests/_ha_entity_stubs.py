"""Extra Home Assistant stubs needed to import the entity platform module.

`conftest.py` stubs just enough of Home Assistant to import
`custom_components.jablotron100.jablotron`. Importing
`custom_components.jablotron100.alarm_control_panel` on top of that needs the
alarm panel base class, the entity platform helper types and the
`JablotronConfigEntry` alias re-exported from the package `__init__`.

This lives outside `conftest.py` on purpose: the fork tracks upstream, and the
extra surface here is only needed by the common segment tests, so leaving
upstream's files untouched keeps rebases clean.

Every addition is guarded, so importing this module is a no-op when the real
Home Assistant package is installed.
"""

from __future__ import annotations

from enum import IntFlag, StrEnum
import sys
from types import ModuleType
from typing import Any

from homeassistant import core as _core
from homeassistant.components import alarm_control_panel as _alarm
from homeassistant.helpers import dispatcher as _dispatcher
from homeassistant.helpers.entity import Entity as _Entity


def _ensure(target: Any, name: str, value: Any) -> None:
	if not hasattr(target, name):
		setattr(target, name, value)


class _AlarmControlPanelEntityFeature(IntFlag):
	ARM_HOME = 1
	ARM_AWAY = 2
	ARM_NIGHT = 4
	TRIGGER = 8
	ARM_VACATION = 32


class _CodeFormat(StrEnum):
	TEXT = "text"
	NUMBER = "number"


class _AlarmControlPanelEntity(_Entity):
	"""Minimal stand-in for the Home Assistant alarm panel base class."""

	_attr_code_arm_required: bool = True
	_attr_code_format: Any = None
	_attr_alarm_state: Any = None
	_attr_supported_features: Any = _AlarmControlPanelEntityFeature(0)

	def code_or_default_code(self, code: str | None) -> str | None:
		# Real Home Assistant falls back to the default code configured on the
		# entity. The tests never configure one, which is the case where Home
		# Assistant returns the passed code unchanged.
		return code


class _HassJob:
	def __init__(self, target: Any, *args: Any, **kwargs: Any) -> None:
		self.target = target

	def __call__(self, *args: Any, **kwargs: Any) -> Any:
		return self.target(*args, **kwargs)


_ensure(_alarm, "AlarmControlPanelEntityFeature", _AlarmControlPanelEntityFeature)
_ensure(_alarm, "CodeFormat", _CodeFormat)
_ensure(_alarm, "AlarmControlPanelEntity", _AlarmControlPanelEntity)

_ensure(_core, "HassJob", _HassJob)

_ensure(_dispatcher, "async_dispatcher_connect", lambda *args, **kwargs: (lambda: None))

_ensure(_Entity, "schedule_update_ha_state", lambda self, force_refresh=False: None)
_ensure(_Entity, "registry_entry", None)
_ensure(_Entity, "hass", None)

try:
	import homeassistant.helpers.entity_platform  # noqa: F401
except ModuleNotFoundError:
	import homeassistant.helpers as _helpers

	_entity_platform = ModuleType("homeassistant.helpers.entity_platform")
	_entity_platform.AddEntitiesCallback = object  # type: ignore[attr-defined]
	sys.modules["homeassistant.helpers.entity_platform"] = _entity_platform
	_helpers.entity_platform = _entity_platform  # type: ignore[attr-defined]

_integration_package = sys.modules.get("custom_components.jablotron100")
if _integration_package is not None:
	_ensure(_integration_package, "JablotronConfigEntry", object)
