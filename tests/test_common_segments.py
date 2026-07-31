"""Tests for the common segment feature.

Focused on the two areas that had to be merged by hand when the feature was
rebased onto upstream 3.33.3:

* `Jablotron._modify_alarm_control_panel_sections_state()` - upstream renamed
  the `code` parameter to `entered_code` and hoisted the configured code into a
  local, while this fork turned the single-section method into a list-taking
  one shared with common segments.
* `alarm_control_panel.add_entities()` - upstream added an `isinstance()` guard
  so only `JablotronAlarmControlPanel` controls become entities, while this
  fork dispatches `JablotronCommonSegment` controls to their own entity class.

Plus full coverage of the state aggregation, which is where the feature's
user-visible behaviour lives.
"""

from __future__ import annotations

import _ha_entity_stubs  # noqa: F401  # must be imported before the modules below

import asyncio
from typing import Any, Callable, Dict, List, Tuple

from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.const import CONF_PASSWORD
from homeassistant.helpers import device_registry as ha_dr
from homeassistant.helpers import entity_registry as ha_er
import pytest

from custom_components.jablotron100 import jablotron as jablotron_module
from custom_components.jablotron100.alarm_control_panel import (
	JablotronAlarmControlPanelEntity,
	JablotronCommonSegmentEntity,
	async_setup_entry,
)
from custom_components.jablotron100.const import (
	COMMAND_GET_SECTIONS_AND_PG_OUTPUTS_STATES,
	DOMAIN,
	EntityType,
	PartiallyArmingMode,
	UI_CONTROL_AUTHORISATION_END,
	UI_CONTROL_MODIFY_SECTION,
)
from custom_components.jablotron100.jablotron import (
	Jablotron,
	JablotronAlarmControlPanel,
	JablotronCentralUnit,
	JablotronCommonSegment,
	JablotronControl,
	JablotronHassDevice,
)


CONFIGURED_CODE = "1234"


def section_alarm_id(section: int) -> str:
	return Jablotron._get_section_alarm_id(section)


def modify_packet(state: AlarmControlPanelState, section: int) -> bytes:
	"""Rebuild the expected "modify section" packet the way upstream does."""
	offsets = {
		AlarmControlPanelState.DISARMED: 143,
		AlarmControlPanelState.ARMED_AWAY: 159,
		AlarmControlPanelState.ARMED_HOME: 175,
		AlarmControlPanelState.ARMED_NIGHT: 175,
	}
	return Jablotron.create_packet_ui_control(
		UI_CONTROL_MODIFY_SECTION,
		Jablotron.int_to_bytes(offsets[state] + section),
	)


def states_refresh_packet() -> bytes:
	return Jablotron.create_packet_command(COMMAND_GET_SECTIONS_AND_PG_OUTPUTS_STATES)


# ---------------------------------------------------------------------------
# State aggregation
# ---------------------------------------------------------------------------


def make_jablotron(section_states: Dict[int, AlarmControlPanelState | None] | None = None) -> Jablotron:
	"""A bare instance carrying only what the aggregation reads."""
	instance = Jablotron.__new__(Jablotron)
	instance.entities_states = {}

	for section, state in (section_states or {}).items():
		instance.entities_states[section_alarm_id(section)] = state

	return instance


@pytest.mark.parametrize(
	("section_states", "expected"),
	[
		pytest.param(
			{1: AlarmControlPanelState.DISARMED, 2: AlarmControlPanelState.DISARMED},
			AlarmControlPanelState.DISARMED,
			id="all-disarmed",
		),
		pytest.param(
			{1: AlarmControlPanelState.ARMED_AWAY, 2: AlarmControlPanelState.ARMED_AWAY},
			AlarmControlPanelState.ARMED_AWAY,
			id="all-armed-away",
		),
		pytest.param(
			{1: AlarmControlPanelState.ARMING, 2: AlarmControlPanelState.ARMING},
			AlarmControlPanelState.ARMING,
			id="all-arming",
		),
		pytest.param(
			{1: AlarmControlPanelState.ARMING, 2: AlarmControlPanelState.ARMED_AWAY},
			AlarmControlPanelState.ARMING,
			id="arming-plus-armed",
		),
		pytest.param(
			{1: AlarmControlPanelState.ARMED_AWAY, 2: AlarmControlPanelState.ARMED_HOME},
			AlarmControlPanelState.ARMED_HOME,
			id="mixed-armed-reports-lowest-level",
		),
		pytest.param(
			{1: AlarmControlPanelState.ARMED_AWAY, 2: AlarmControlPanelState.ARMED_NIGHT},
			AlarmControlPanelState.ARMED_NIGHT,
			id="mixed-armed-night-reports-lowest-level",
		),
		pytest.param(
			{1: AlarmControlPanelState.TRIGGERED, 2: AlarmControlPanelState.DISARMED},
			AlarmControlPanelState.TRIGGERED,
			id="triggered-beats-disarmed",
		),
		pytest.param(
			{1: AlarmControlPanelState.TRIGGERED, 2: AlarmControlPanelState.PENDING},
			AlarmControlPanelState.TRIGGERED,
			id="triggered-beats-pending",
		),
		pytest.param(
			{1: AlarmControlPanelState.PENDING, 2: AlarmControlPanelState.DISARMED},
			AlarmControlPanelState.PENDING,
			id="pending-beats-disarmed",
		),
		pytest.param(
			{1: AlarmControlPanelState.PENDING, 2: AlarmControlPanelState.ARMED_AWAY},
			AlarmControlPanelState.PENDING,
			id="pending-beats-armed",
		),
	],
)
def test_derive_common_segment_alarm_state(
	section_states: Dict[int, AlarmControlPanelState],
	expected: AlarmControlPanelState,
) -> None:
	instance = make_jablotron(section_states)

	assert instance._derive_common_segment_alarm_state(list(section_states)) == expected


def test_single_arming_section_keeps_common_segment_disarmed() -> None:
	"""Arming one delayed section must not flip the whole segment to ARMING."""
	instance = make_jablotron({
		1: AlarmControlPanelState.ARMING,
		2: AlarmControlPanelState.DISARMED,
		3: AlarmControlPanelState.DISARMED,
	})

	assert instance._derive_common_segment_alarm_state([1, 2, 3]) == AlarmControlPanelState.DISARMED


def test_single_armed_section_keeps_common_segment_disarmed() -> None:
	instance = make_jablotron({
		1: AlarmControlPanelState.ARMED_AWAY,
		2: AlarmControlPanelState.DISARMED,
	})

	assert instance._derive_common_segment_alarm_state([1, 2]) == AlarmControlPanelState.DISARMED


def test_derive_returns_none_without_sections() -> None:
	instance = make_jablotron({1: AlarmControlPanelState.DISARMED})

	assert instance._derive_common_segment_alarm_state([]) is None


def test_derive_returns_none_when_no_section_state_is_known() -> None:
	instance = make_jablotron()

	assert instance._derive_common_segment_alarm_state([1, 2]) is None


def test_derive_skips_sections_without_state() -> None:
	"""A section the panel never reported must not drag the segment down."""
	instance = make_jablotron({1: AlarmControlPanelState.ARMED_AWAY})

	assert instance._derive_common_segment_alarm_state([1, 99]) == AlarmControlPanelState.ARMED_AWAY


def test_derive_treats_unexpected_state_as_disarmed() -> None:
	instance = make_jablotron({1: AlarmControlPanelState.ARMED_AWAY, 2: "something-else"})

	assert instance._derive_common_segment_alarm_state([1, 2]) == AlarmControlPanelState.DISARMED


def test_overrides_take_precedence_over_stored_states() -> None:
	instance = make_jablotron({1: AlarmControlPanelState.DISARMED, 2: AlarmControlPanelState.DISARMED})

	derived = instance._derive_common_segment_alarm_state(
		[1, 2],
		section_state_overrides={
			1: AlarmControlPanelState.ARMED_AWAY,
			2: AlarmControlPanelState.ARMED_AWAY,
		},
	)

	assert derived == AlarmControlPanelState.ARMED_AWAY


def test_overrides_are_used_per_section() -> None:
	"""Sections missing from the overrides still fall back to stored state."""
	instance = make_jablotron({1: AlarmControlPanelState.ARMED_AWAY, 2: AlarmControlPanelState.ARMED_AWAY})

	derived = instance._derive_common_segment_alarm_state(
		[1, 2],
		section_state_overrides={1: AlarmControlPanelState.DISARMED},
	)

	assert derived == AlarmControlPanelState.DISARMED


def test_override_with_none_skips_the_section() -> None:
	"""An override of None (section in service/blocked) is ignored, not counted."""
	instance = make_jablotron({1: AlarmControlPanelState.ARMED_AWAY, 2: AlarmControlPanelState.DISARMED})

	derived = instance._derive_common_segment_alarm_state(
		[1, 2],
		section_state_overrides={2: None},
	)

	assert derived == AlarmControlPanelState.ARMED_AWAY


def test_stuck_arming_regression() -> None:
	"""Regression: the segment must leave ARMING once every section is armed.

	`entities_states` lags behind by one packet for entities Home Assistant has
	already instantiated (`_update_entity_state` hands the value to the entity
	via the event loop instead of writing it back synchronously), so the
	aggregation has to trust the states parsed from the packet being processed.
	"""
	instance = make_jablotron({
		1: AlarmControlPanelState.ARMING,
		2: AlarmControlPanelState.ARMING,
	})

	derived = instance._derive_common_segment_alarm_state(
		[1, 2],
		section_state_overrides={
			1: AlarmControlPanelState.ARMED_AWAY,
			2: AlarmControlPanelState.ARMED_AWAY,
		},
	)

	assert derived == AlarmControlPanelState.ARMED_AWAY


def test_refresh_updates_only_common_segments() -> None:
	instance = make_jablotron({1: AlarmControlPanelState.ARMED_AWAY, 2: AlarmControlPanelState.ARMED_AWAY})
	central_unit = JablotronCentralUnit("unit", "model", "hw", "fw")
	hass_device = JablotronHassDevice("common_segment_abcd1234", "Whole house", "common_segment", {})

	instance.entities = {entity_type: {} for entity_type in EntityType.__members__.values()}
	instance.hass_entities = {}
	instance.entities[EntityType.ALARM_CONTROL_PANEL]["section_1"] = JablotronAlarmControlPanel(
		central_unit, hass_device, "section_1", 1,
	)
	instance.entities[EntityType.ALARM_CONTROL_PANEL]["common_segment_abcd1234"] = JablotronCommonSegment(
		central_unit, hass_device, "common_segment_abcd1234", "Whole house", [1, 2],
	)

	instance._refresh_common_segments_states()

	assert instance.entities_states["common_segment_abcd1234"] == AlarmControlPanelState.ARMED_AWAY
	# The plain section entity keeps whatever the section parser wrote.
	assert instance.entities_states["section_1"] == AlarmControlPanelState.ARMED_AWAY


# ---------------------------------------------------------------------------
# Packet generation (hand-merged conflict #1)
# ---------------------------------------------------------------------------


class FakeLoop:
	def __init__(self) -> None:
		self.scheduled: List[Tuple[Callable[..., Any], Tuple[Any, ...]]] = []

	def call_soon_threadsafe(self, callback: Callable[..., Any], *args: Any) -> None:
		self.scheduled.append((callback, args))

	def run_pending(self) -> None:
		pending, self.scheduled = self.scheduled, []
		for callback, args in pending:
			callback(*args)


class FakeHass:
	def __init__(self) -> None:
		self.loop = FakeLoop()

	def async_add_executor_job(self, target: Callable[..., Any], *args: Any) -> None:
		target(*args)


class PanelRecorder:
	"""Captures everything `_modify_alarm_control_panel_sections_state` emits."""

	def __init__(self, instance: Jablotron) -> None:
		self.instance = instance
		self.packets: List[bytes] = []
		self.batches: List[List[bytes]] = []
		self.login_errors = 0
		self.delayed: List[Tuple[float, Callable[..., Any]]] = []

	def send_packet(self, packet: bytes) -> None:
		self.packets.append(packet)
		self.batches.append([packet])

	def send_packets(self, packets: List[bytes]) -> None:
		self.packets.extend(packets)
		self.batches.append(list(packets))

	def login_error(self) -> None:
		self.login_errors += 1

	def async_call_later(self, hass: Any, delay: float, action: Any) -> None:
		target = getattr(action, "target", action)
		self.delayed.append((delay, target))

	def fire_delayed(self) -> None:
		pending, self.delayed = self.delayed, []
		for _, target in pending:
			target(None)


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch) -> PanelRecorder:
	instance = Jablotron.__new__(Jablotron)
	instance._config = {CONF_PASSWORD: CONFIGURED_CODE}
	instance._hass = FakeHass()
	instance._successful_login = True
	instance.entities = {entity_type: {} for entity_type in EntityType.__members__.values()}

	recorder = PanelRecorder(instance)
	instance._send_packet = recorder.send_packet  # type: ignore[method-assign]
	instance._send_packets = recorder.send_packets  # type: ignore[method-assign]
	instance._login_error = recorder.login_error  # type: ignore[method-assign]
	monkeypatch.setattr(jablotron_module, "async_call_later", recorder.async_call_later)

	return recorder


def drive(recorder: PanelRecorder) -> None:
	"""Run the callbacks the panel scheduled onto the event loop."""
	recorder.instance._hass.loop.run_pending()
	recorder.fire_delayed()


def test_single_section_emits_the_upstream_packet(panel: PanelRecorder) -> None:
	panel.instance.modify_alarm_control_panel_section_state(1, AlarmControlPanelState.ARMED_AWAY, None)
	panel.instance._hass.loop.run_pending()

	assert panel.packets == [modify_packet(AlarmControlPanelState.ARMED_AWAY, 1)]


@pytest.mark.parametrize(
	"state",
	[
		AlarmControlPanelState.DISARMED,
		AlarmControlPanelState.ARMED_AWAY,
		AlarmControlPanelState.ARMED_HOME,
		AlarmControlPanelState.ARMED_NIGHT,
	],
)
def test_single_section_packet_for_every_state(panel: PanelRecorder, state: AlarmControlPanelState) -> None:
	panel.instance.modify_alarm_control_panel_section_state(3, state, None)
	panel.instance._hass.loop.run_pending()

	assert panel.packets == [modify_packet(state, 3)]


def test_multiple_sections_are_sent_in_one_batch(panel: PanelRecorder) -> None:
	panel.instance._modify_alarm_control_panel_sections_state(
		[1, 2, 5], AlarmControlPanelState.ARMED_AWAY, None,
	)
	panel.instance._hass.loop.run_pending()

	assert panel.batches == [[
		modify_packet(AlarmControlPanelState.ARMED_AWAY, 1),
		modify_packet(AlarmControlPanelState.ARMED_AWAY, 2),
		modify_packet(AlarmControlPanelState.ARMED_AWAY, 5),
	]]


def test_empty_section_list_sends_nothing(panel: PanelRecorder) -> None:
	panel.instance._modify_alarm_control_panel_sections_state([], AlarmControlPanelState.ARMED_AWAY, None)
	drive(panel)

	assert panel.packets == []
	assert panel.login_errors == 0
	assert panel.instance._hass.loop.scheduled == []


def test_short_code_reports_login_error_and_refreshes_states(panel: PanelRecorder) -> None:
	panel.instance._modify_alarm_control_panel_sections_state(
		[1, 2], AlarmControlPanelState.ARMED_AWAY, "12",
	)
	drive(panel)

	assert panel.login_errors == 1
	assert panel.packets == [states_refresh_packet()]


def test_configured_code_skips_login_and_logout(panel: PanelRecorder) -> None:
	panel.instance._modify_alarm_control_panel_sections_state(
		[1], AlarmControlPanelState.ARMED_AWAY, CONFIGURED_CODE,
	)
	panel.instance._hass.loop.run_pending()

	assert panel.packets == [modify_packet(AlarmControlPanelState.ARMED_AWAY, 1)]


def test_entered_code_logs_in_then_out_around_the_modify(panel: PanelRecorder) -> None:
	panel.instance._modify_alarm_control_panel_sections_state(
		[1, 2], AlarmControlPanelState.ARMED_AWAY, "4321",
	)

	# The login batch goes out synchronously, before anything is scheduled.
	assert panel.batches == [[
		Jablotron.create_packet_ui_control(UI_CONTROL_AUTHORISATION_END),
		Jablotron.create_packet_authorisation_code("4321"),
	]]

	panel.instance._hass.loop.run_pending()
	panel.fire_delayed()

	assert panel.batches[1] == [
		modify_packet(AlarmControlPanelState.ARMED_AWAY, 1),
		modify_packet(AlarmControlPanelState.ARMED_AWAY, 2),
		Jablotron.create_packet_ui_control(UI_CONTROL_AUTHORISATION_END),
		*Jablotron.create_packets_keepalive(CONFIGURED_CODE),
	]


def test_failed_login_skips_modify_but_still_logs_out(panel: PanelRecorder) -> None:
	panel.instance._modify_alarm_control_panel_sections_state(
		[1, 2], AlarmControlPanelState.ARMED_AWAY, "4321",
	)
	panel.instance._hass.loop.run_pending()

	# The panel rejected the code while the login callback was pending.
	panel.instance._successful_login = False
	panel.fire_delayed()

	assert panel.batches[1] == [
		Jablotron.create_packet_ui_control(UI_CONTROL_AUTHORISATION_END),
		*Jablotron.create_packets_keepalive(CONFIGURED_CODE),
	]
	assert modify_packet(AlarmControlPanelState.ARMED_AWAY, 1) not in panel.packets


def test_states_are_refreshed_after_the_modify(panel: PanelRecorder) -> None:
	panel.instance._modify_alarm_control_panel_sections_state(
		[1], AlarmControlPanelState.ARMED_AWAY, None,
	)
	panel.instance._hass.loop.run_pending()
	panel.fire_delayed()

	assert panel.packets[-1] == states_refresh_packet()


# ---------------------------------------------------------------------------
# Common segment -> sections fan-out
# ---------------------------------------------------------------------------


def add_section(instance: Jablotron, section: int) -> None:
	central_unit = JablotronCentralUnit("unit", "model", "hw", "fw")
	hass_device = JablotronHassDevice("section_{}".format(section), "Section", "section", {})
	instance.entities[EntityType.ALARM_CONTROL_PANEL][section_alarm_id(section)] = JablotronAlarmControlPanel(
		central_unit, hass_device, section_alarm_id(section), section,
	)


def make_common_segment(sections: List[int]) -> JablotronCommonSegment:
	central_unit = JablotronCentralUnit("unit", "model", "hw", "fw")
	hass_device = JablotronHassDevice("common_segment_abcd1234", "Whole house", "common_segment", {})
	return JablotronCommonSegment(
		central_unit, hass_device, "common_segment_abcd1234", "Whole house", sections,
	)


def test_common_segment_arms_every_configured_section(panel: PanelRecorder) -> None:
	for section in (1, 2, 3):
		add_section(panel.instance, section)

	panel.instance.modify_common_segment_state(
		make_common_segment([1, 2, 3]), AlarmControlPanelState.ARMED_AWAY, None,
	)
	panel.instance._hass.loop.run_pending()

	assert panel.packets == [
		modify_packet(AlarmControlPanelState.ARMED_AWAY, 1),
		modify_packet(AlarmControlPanelState.ARMED_AWAY, 2),
		modify_packet(AlarmControlPanelState.ARMED_AWAY, 3),
	]


def test_common_segment_skips_sections_the_panel_does_not_have(panel: PanelRecorder) -> None:
	add_section(panel.instance, 1)

	panel.instance.modify_common_segment_state(
		make_common_segment([1, 99]), AlarmControlPanelState.DISARMED, None,
	)
	panel.instance._hass.loop.run_pending()

	assert panel.packets == [modify_packet(AlarmControlPanelState.DISARMED, 1)]


def test_common_segment_without_any_valid_section_sends_nothing(panel: PanelRecorder) -> None:
	panel.instance.modify_common_segment_state(
		make_common_segment([98, 99]), AlarmControlPanelState.ARMED_AWAY, None,
	)
	drive(panel)

	assert panel.packets == []
	assert panel.login_errors == 0


# ---------------------------------------------------------------------------
# Registry cleanup
#
# These exercise `er.` / `dr.` module access, which is how the missing
# `entity_registry` import that the 3.33.3 rebase silently dropped shows up:
# `_cleanup_orphaned_common_segments()` runs on every integration load, so a
# missing alias takes the whole integration down at startup with a NameError.
# ---------------------------------------------------------------------------


class FakeRegistryEntry:
	def __init__(self, entity_id: str, unique_id: str) -> None:
		self.entity_id = entity_id
		self.unique_id = unique_id


class FakeEntityRegistry:
	def __init__(self, entries: List[FakeRegistryEntry]) -> None:
		self.entries = entries
		self.removed: List[str] = []

	def async_remove(self, entity_id: str) -> None:
		self.removed.append(entity_id)


class FakeDeviceEntry:
	def __init__(self, device_id: str, identifiers: set) -> None:
		self.id = device_id
		self.identifiers = identifiers


class FakeDeviceRegistry:
	def __init__(self, entries: List[FakeDeviceEntry]) -> None:
		self.entries = entries
		self.removed: List[str] = []

	def async_remove_device(self, device_id: str) -> None:
		self.removed.append(device_id)


def unique_id_for(control_id: str) -> str:
	return "{}.unit.{}".format(DOMAIN, control_id)


def make_cleanup_jablotron(common_segments: List[str]) -> Jablotron:
	instance = Jablotron.__new__(Jablotron)
	instance._central_unit = JablotronCentralUnit("unit", "model", "hw", "fw")
	instance._hass = FakeHass()
	instance._config_entry_id = "config-entry"
	instance.entities = {entity_type: {} for entity_type in EntityType.__members__.values()}
	instance.entities_states = {}

	for segment_id in common_segments:
		control_id = "common_segment_{}".format(segment_id)
		hass_device = JablotronHassDevice(control_id, "Segment", "common_segment", {})
		instance.entities[EntityType.ALARM_CONTROL_PANEL][control_id] = JablotronCommonSegment(
			instance._central_unit, hass_device, control_id, "Segment", [1],
		)

	return instance


def install_registries(
	monkeypatch: pytest.MonkeyPatch,
	entity_entries: List[FakeRegistryEntry],
	device_entries: List[FakeDeviceEntry] | None = None,
) -> Tuple[FakeEntityRegistry, FakeDeviceRegistry]:
	entity_reg = FakeEntityRegistry(entity_entries)
	device_reg = FakeDeviceRegistry(device_entries or [])

	monkeypatch.setattr(ha_er, "async_get", lambda hass: entity_reg, raising=False)
	monkeypatch.setattr(ha_er, "async_entries_for_config_entry", lambda reg, cid: reg.entries, raising=False)
	monkeypatch.setattr(ha_dr, "async_get", lambda hass: device_reg, raising=False)
	monkeypatch.setattr(ha_dr, "async_entries_for_config_entry", lambda reg, cid: reg.entries, raising=False)

	return entity_reg, device_reg


def test_cleanup_removes_the_entity_of_a_deleted_common_segment(monkeypatch: pytest.MonkeyPatch) -> None:
	instance = make_cleanup_jablotron(["abcd1234"])
	instance.entities_states["common_segment_deadbeef"] = AlarmControlPanelState.DISARMED
	entity_reg, _ = install_registries(
		monkeypatch,
		[
			FakeRegistryEntry("alarm_control_panel.kept", unique_id_for("common_segment_abcd1234")),
			FakeRegistryEntry("alarm_control_panel.stale", unique_id_for("common_segment_deadbeef")),
		],
	)

	instance._cleanup_orphaned_common_segments()

	assert entity_reg.removed == ["alarm_control_panel.stale"]
	assert "common_segment_deadbeef" not in instance.entities_states


def test_cleanup_never_touches_section_entities(monkeypatch: pytest.MonkeyPatch) -> None:
	instance = make_cleanup_jablotron([])
	entity_reg, _ = install_registries(
		monkeypatch,
		[FakeRegistryEntry("alarm_control_panel.section_1", unique_id_for("section_1"))],
	)

	instance._cleanup_orphaned_common_segments()

	assert entity_reg.removed == []


def test_cleanup_removes_the_device_of_a_deleted_common_segment(monkeypatch: pytest.MonkeyPatch) -> None:
	instance = make_cleanup_jablotron(["abcd1234"])
	entity_reg, device_reg = install_registries(
		monkeypatch,
		[FakeRegistryEntry("alarm_control_panel.stale", unique_id_for("common_segment_deadbeef"))],
		[
			FakeDeviceEntry("device-kept", {(DOMAIN, "common_segment_abcd1234")}),
			FakeDeviceEntry("device-stale", {(DOMAIN, "common_segment_deadbeef")}),
			FakeDeviceEntry("device-section", {(DOMAIN, "section_1")}),
		],
	)

	instance._cleanup_orphaned_common_segments()

	assert device_reg.removed == ["device-stale"]


def test_cleanup_skips_the_device_walk_when_nothing_is_orphaned(monkeypatch: pytest.MonkeyPatch) -> None:
	instance = make_cleanup_jablotron(["abcd1234"])
	install_registries(
		monkeypatch,
		[FakeRegistryEntry("alarm_control_panel.kept", unique_id_for("common_segment_abcd1234"))],
	)

	def explode(*args: Any, **kwargs: Any) -> None:
		raise AssertionError("device registry must not be walked without entity orphans")

	monkeypatch.setattr(ha_dr, "async_entries_for_config_entry", explode, raising=False)

	instance._cleanup_orphaned_common_segments()


def test_cleanup_is_a_noop_before_the_central_unit_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
	instance = make_cleanup_jablotron([])
	instance._central_unit = None
	entity_reg, _ = install_registries(
		monkeypatch,
		[FakeRegistryEntry("alarm_control_panel.stale", unique_id_for("common_segment_deadbeef"))],
	)

	instance._cleanup_orphaned_common_segments()

	assert entity_reg.removed == []


# ---------------------------------------------------------------------------
# Entity dispatch (hand-merged conflict #2)
# ---------------------------------------------------------------------------


class FakeJablotron:
	"""Just enough of `Jablotron` to build and drive the panel entities."""

	def __init__(self) -> None:
		self.entities: Dict[EntityType, Dict[str, JablotronControl]] = {
			entity_type: {} for entity_type in EntityType.__members__.values()
		}
		self.entities_states: Dict[str, Any] = {}
		self.hass_entities: Dict[str, Any] = {}
		self.in_service_mode = False
		self.last_update_success = True
		self.section_calls: List[Tuple[int, AlarmControlPanelState, str | None]] = []
		self.common_segment_calls: List[Tuple[JablotronCommonSegment, AlarmControlPanelState, str | None]] = []

	def partially_arming_mode(self) -> PartiallyArmingMode:
		return PartiallyArmingMode.NIGHT_MODE

	def is_code_required_for_disarm(self) -> bool:
		return False

	def is_code_required_for_arm(self) -> bool:
		return False

	def code_contains_asterisk(self) -> bool:
		return False

	def last_authorized_user_or_device(self) -> str | None:
		return None

	def signal_entities_added(self) -> str:
		return "jablotron_entities_added"

	def subscribe_hass_entity_for_updates(self, control_id: str, hass_entity: Any) -> None:
		self.hass_entities[control_id] = hass_entity

	def modify_alarm_control_panel_section_state(
		self, section: int, state: AlarmControlPanelState, code: str | None,
	) -> None:
		self.section_calls.append((section, state, code))

	def modify_common_segment_state(
		self, common_segment: JablotronCommonSegment, state: AlarmControlPanelState, code: str | None,
	) -> None:
		self.common_segment_calls.append((common_segment, state, code))


class FakeConfigEntry:
	def __init__(self, runtime_data: Any) -> None:
		self.runtime_data = runtime_data
		self.unloads: List[Any] = []

	def async_on_unload(self, func: Any) -> None:
		self.unloads.append(func)


def setup_entities(jablotron: FakeJablotron) -> List[Any]:
	added: List[Any] = []
	asyncio.run(async_setup_entry(None, FakeConfigEntry(jablotron), added.extend))
	return added


def test_dispatch_picks_the_entity_class_per_control_type() -> None:
	jablotron = FakeJablotron()
	central_unit = JablotronCentralUnit("unit", "model", "hw", "fw")
	hass_device = JablotronHassDevice("device", "Device", "section", {})
	panels = jablotron.entities[EntityType.ALARM_CONTROL_PANEL]

	panels["section_1"] = JablotronAlarmControlPanel(central_unit, hass_device, "section_1", 1)
	panels["common_segment_abcd1234"] = JablotronCommonSegment(
		central_unit, hass_device, "common_segment_abcd1234", "Whole house", [1],
	)

	entities = setup_entities(jablotron)

	assert [type(entity) for entity in entities] == [
		JablotronAlarmControlPanelEntity,
		JablotronCommonSegmentEntity,
	]


def test_dispatch_ignores_controls_that_are_not_alarm_panels() -> None:
	"""Upstream's isinstance guard must survive: a bare control creates nothing."""
	jablotron = FakeJablotron()
	central_unit = JablotronCentralUnit("unit", "model", "hw", "fw")
	jablotron.entities[EntityType.ALARM_CONTROL_PANEL]["stray"] = JablotronControl(
		central_unit, None, "stray", "Stray",
	)

	assert setup_entities(jablotron) == []


def test_dispatch_skips_controls_that_already_have_an_entity() -> None:
	jablotron = FakeJablotron()
	central_unit = JablotronCentralUnit("unit", "model", "hw", "fw")
	hass_device = JablotronHassDevice("device", "Device", "section", {})
	jablotron.entities[EntityType.ALARM_CONTROL_PANEL]["section_1"] = JablotronAlarmControlPanel(
		central_unit, hass_device, "section_1", 1,
	)
	jablotron.hass_entities["section_1"] = object()

	assert setup_entities(jablotron) == []


def make_common_segment_entity(sections: List[int]) -> Tuple[FakeJablotron, JablotronCommonSegmentEntity]:
	jablotron = FakeJablotron()
	control = make_common_segment(sections)
	jablotron.entities[EntityType.ALARM_CONTROL_PANEL][control.id] = control
	entity = JablotronCommonSegmentEntity(jablotron, control)
	return jablotron, entity


def test_common_segment_entity_exposes_its_sections() -> None:
	_, entity = make_common_segment_entity([1, 2, 3])

	assert entity._attr_extra_state_attributes == {"sections": [1, 2, 3]}


def test_common_segment_entity_copies_the_section_list() -> None:
	jablotron, entity = make_common_segment_entity([1, 2])
	control = jablotron.entities[EntityType.ALARM_CONTROL_PANEL]["common_segment_abcd1234"]

	entity._attr_extra_state_attributes["sections"].append(99)

	assert control.sections == [1, 2]


def test_common_segment_entity_arms_through_the_common_segment_path() -> None:
	jablotron, entity = make_common_segment_entity([1, 2])
	jablotron.entities_states[entity._control.id] = AlarmControlPanelState.DISARMED

	entity.alarm_arm_away()

	assert jablotron.section_calls == []
	assert len(jablotron.common_segment_calls) == 1
	control, state, code = jablotron.common_segment_calls[0]
	assert control.sections == [1, 2]
	assert state == AlarmControlPanelState.ARMED_AWAY
	assert code is None


def test_section_entity_still_arms_through_the_section_path() -> None:
	jablotron = FakeJablotron()
	central_unit = JablotronCentralUnit("unit", "model", "hw", "fw")
	hass_device = JablotronHassDevice("device", "Device", "section", {})
	control = JablotronAlarmControlPanel(central_unit, hass_device, "section_4", 4)
	entity = JablotronAlarmControlPanelEntity(jablotron, control)
	jablotron.entities_states["section_4"] = AlarmControlPanelState.DISARMED

	entity.alarm_arm_away()

	assert jablotron.common_segment_calls == []
	assert jablotron.section_calls == [(4, AlarmControlPanelState.ARMED_AWAY, None)]


def test_common_segment_entity_ignores_a_noop_arm() -> None:
	jablotron, entity = make_common_segment_entity([1, 2])
	jablotron.entities_states[entity._control.id] = AlarmControlPanelState.ARMED_AWAY
	entity._update_attributes()

	entity.alarm_arm_away()

	assert jablotron.common_segment_calls == []


def test_common_segment_entity_disarms() -> None:
	jablotron, entity = make_common_segment_entity([1, 2])
	jablotron.entities_states[entity._control.id] = AlarmControlPanelState.ARMED_AWAY

	entity.alarm_disarm()

	assert len(jablotron.common_segment_calls) == 1
	assert jablotron.common_segment_calls[0][1] == AlarmControlPanelState.DISARMED


def test_common_segment_entity_rejects_a_non_alarm_state() -> None:
	"""Upstream's type guard now covers the common segment entity too."""
	jablotron, entity = make_common_segment_entity([1])
	jablotron.entities_states[entity._control.id] = "not-an-alarm-state"

	with pytest.raises(TypeError):
		entity._get_state()


def test_common_segment_entity_is_unavailable_without_a_state() -> None:
	jablotron, entity = make_common_segment_entity([1])

	assert entity.available is False

	jablotron.entities_states[entity._control.id] = AlarmControlPanelState.DISARMED

	assert entity.available is True
