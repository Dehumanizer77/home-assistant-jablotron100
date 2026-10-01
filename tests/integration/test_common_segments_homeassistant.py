"""Common segments against a real Home Assistant.

`tests/test_common_segments.py` covers the logic with fakes. These tests cover
what fakes cannot: that the feature is actually wired into the integration's
startup, and that it holds up against Home Assistant's real registries, state
machine and service layer.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

from homeassistant.components.alarm_control_panel import AlarmControlPanelEntityFeature, AlarmControlPanelState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import translation
import pytest
import voluptuous as vol

from custom_components.jablotron100.alarm_control_panel import (
	JablotronAlarmControlPanelEntity,
	JablotronCommonSegmentEntity,
)
from custom_components.jablotron100.const import (
	CONF_COMMON_SEGMENTS,
	CONF_REQUIRE_CODE_TO_DISARM,
	CommonSegmentData,
	DOMAIN,
	EntityType,
	UI_CONTROL_MODIFY_SECTION,
)
from custom_components.jablotron100.jablotron import (
	COMMON_SEGMENT_PARTIALLY_ARMED,
	Jablotron,
	JablotronCommonSegment,
)


pytestmark = pytest.mark.asyncio

SEGMENT_ID = "abcd1234"
SEGMENT_CONTROL_ID = "common_segment_abcd1234"

DISARMED = 0x01
ARMED = 0x03
ARMING = 0x83
TRIGGERED = 0x1B


def sections_packet(*sections: int) -> bytes:
	"""A sections states packet carrying one state byte per section."""
	states = b"".join(bytes([section, 0x00]) for section in sections)
	return b"\x51" + bytes([len(states) + 2]) + states + b"\x07\x00"


def segment_options(sections: list[int], segment_id: str | None = SEGMENT_ID) -> dict:
	segment: dict = {CommonSegmentData.NAME.value: "Whole house", CommonSegmentData.SECTIONS.value: sections}
	if segment_id is not None:
		segment[CommonSegmentData.ID.value] = segment_id
	return {CONF_COMMON_SEGMENTS: [segment]}


def modify_packet(offset: int, section: int) -> bytes:
	return Jablotron.create_packet_ui_control(UI_CONTROL_MODIFY_SECTION, Jablotron.int_to_bytes(offset + section))


async def start(jablotron: Jablotron, *sections: int) -> None:
	"""Run the part of the startup that discovers sections and builds segments."""
	with patch.object(jablotron, "_detect_sections_and_pg_outputs", return_value=[sections_packet(*sections)]):
		await jablotron._detect_and_create_devices_and_sections_and_pg_outputs()


@pytest.fixture
def panel(hass, jablotron, entity_component):
	"""A panel whose config entry and central unit device exist, as after setup."""
	component = entity_component("alarm_control_panel")
	dr.async_get(hass).async_get_or_create(
		config_entry_id=jablotron._config_entry_id,
		identifiers={(DOMAIN, jablotron.central_unit().unique_id)},
	)
	jablotron._options = segment_options([1, 2])
	return jablotron, component


async def add_entities(jablotron: Jablotron, component) -> dict[str, object]:
	entities = {}
	for control_id, control in jablotron.entities[EntityType.ALARM_CONTROL_PANEL].items():
		entity_class = JablotronCommonSegmentEntity if isinstance(control, JablotronCommonSegment) else JablotronAlarmControlPanelEntity
		entity = entity_class(jablotron, control)
		entity.entity_id = "alarm_control_panel.jablotron_{}".format(control_id)
		entities[control_id] = entity
	await component.async_add_entities(list(entities.values()))
	return entities


async def receive(hass, jablotron: Jablotron, *sections: int) -> None:
	"""Deliver a sections packet the way the reader thread does."""
	await hass.async_add_executor_job(jablotron._parse_sections_states_packet, sections_packet(*sections))
	await hass.async_block_till_done()


async def test_startup_creates_common_segments_once_sections_exist(panel):
	"""The segment is built during startup, from the sections found by it.

	Its initial state can only be correct if the sections were created first:
	built any earlier, the segment would have nothing to aggregate.
	"""
	jablotron, _ = panel

	await start(jablotron, ARMED, DISARMED)

	control = jablotron.entities[EntityType.ALARM_CONTROL_PANEL][SEGMENT_CONTROL_ID]
	assert isinstance(control, JablotronCommonSegment)
	assert control.sections == [1, 2]
	assert jablotron.entities_states[SEGMENT_CONTROL_ID] == COMMON_SEGMENT_PARTIALLY_ARMED


async def test_startup_is_repeatable(panel):
	jablotron, _ = panel

	await start(jablotron, ARMED, ARMED)
	await start(jablotron, ARMED, ARMED)

	segments = [
		control for control in jablotron.entities[EntityType.ALARM_CONTROL_PANEL].values()
		if isinstance(control, JablotronCommonSegment)
	]
	assert len(segments) == 1


async def test_startup_backfills_a_missing_segment_id_into_the_config_entry(hass, panel):
	jablotron, _ = panel
	jablotron._options = segment_options([1, 2], segment_id=None)

	await start(jablotron, DISARMED, DISARMED)

	stored = hass.config_entries.async_get_entry(jablotron._config_entry_id).options[CONF_COMMON_SEGMENTS]
	assert len(stored) == 1
	segment_id = stored[0][CommonSegmentData.ID.value]
	assert segment_id
	assert "common_segment_{}".format(segment_id) in jablotron.entities[EntityType.ALARM_CONTROL_PANEL]


async def test_entity_reports_the_aggregate_state_to_home_assistant(hass, panel):
	jablotron, component = panel
	await start(jablotron, ARMED, DISARMED)
	entities = await add_entities(jablotron, component)
	segment = entities[SEGMENT_CONTROL_ID]

	try:
		state = hass.states.get(segment.entity_id)
		assert state.state == "armed_custom_bypass"
		assert state.attributes["sections"] == [1, 2]
		assert not state.attributes["supported_features"] & AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
	finally:
		for entity in entities.values():
			await entity.async_remove()


async def test_entity_is_registered_with_its_own_device_and_translation_key(hass, panel):
	jablotron, component = panel
	await start(jablotron, DISARMED, DISARMED)
	entities = await add_entities(jablotron, component)
	segment = entities[SEGMENT_CONTROL_ID]

	try:
		entry = er.async_get(hass).async_get(segment.entity_id)
		assert entry.translation_key == "common_segment"
		assert entry.unique_id == "{}.{}.{}".format(DOMAIN, jablotron.central_unit().unique_id, SEGMENT_CONTROL_ID)

		devices = dr.async_get(hass)
		device = devices.async_get(entry.device_id)
		assert (DOMAIN, SEGMENT_CONTROL_ID) in device.identifiers
		assert device.via_device_id == jablotron.central_unit_device_id()
	finally:
		for entity in entities.values():
			await entity.async_remove()


@pytest.mark.parametrize(
	("sections", "expected"),
	[
		pytest.param((ARMED, ARMED), AlarmControlPanelState.ARMED_AWAY, id="all-armed"),
		pytest.param((ARMED, DISARMED), COMMON_SEGMENT_PARTIALLY_ARMED, id="partially-armed"),
		pytest.param((ARMING, ARMING), AlarmControlPanelState.ARMING, id="all-arming"),
		pytest.param((ARMING, DISARMED), COMMON_SEGMENT_PARTIALLY_ARMED, id="partially-arming"),
		pytest.param((TRIGGERED, DISARMED), AlarmControlPanelState.TRIGGERED, id="triggered"),
	],
)
async def test_live_packets_move_the_entity_state(hass, panel, sections, expected):
	jablotron, component = panel
	await start(jablotron, DISARMED, DISARMED)
	entities = await add_entities(jablotron, component)
	segment = entities[SEGMENT_CONTROL_ID]

	try:
		assert hass.states.get(segment.entity_id).state == AlarmControlPanelState.DISARMED

		await receive(hass, jablotron, *sections)

		assert hass.states.get(segment.entity_id).state == expected
	finally:
		for entity in entities.values():
			await entity.async_remove()


async def test_segment_leaves_arming_with_the_packet_that_arms_the_sections(hass, panel):
	"""Regression: the segment used to stay in ARMING one packet too long.

	Section entities get their state through the event loop, so while a packet
	is being parsed `entities_states` still holds the previous one. The segment
	has to be derived from the packet itself, not from that stale view.
	"""
	jablotron, component = panel
	await start(jablotron, DISARMED, DISARMED)
	entities = await add_entities(jablotron, component)
	segment = entities[SEGMENT_CONTROL_ID]

	try:
		await receive(hass, jablotron, ARMING, ARMING)
		assert hass.states.get(segment.entity_id).state == AlarmControlPanelState.ARMING

		await receive(hass, jablotron, ARMED, ARMED)
		assert hass.states.get(segment.entity_id).state == AlarmControlPanelState.ARMED_AWAY
	finally:
		for entity in entities.values():
			await entity.async_remove()


@pytest.mark.parametrize(
	("code_required", "entered_code", "login_code"),
	[
		pytest.param(False, None, "1234", id="configured-code"),
		pytest.param(True, "9876", "9876", id="entered-code"),
	],
)
async def test_disarm_service_on_a_partially_armed_segment_disarms_every_section(
	hass, panel, code_required, entered_code, login_code,
):
	jablotron, component = panel
	jablotron._options[CONF_REQUIRE_CODE_TO_DISARM] = code_required
	await start(jablotron, ARMED, DISARMED)
	entities = await add_entities(jablotron, component)
	segment = entities[SEGMENT_CONTROL_ID]
	component.async_register_entity_service("alarm_disarm", {vol.Optional("code"): str}, "async_alarm_disarm")
	loop_thread = threading.get_ident()
	batches: list[list[bytes]] = []

	def wait_for_response(timeout):
		assert threading.get_ident() != loop_thread
		assert jablotron._authorisation_lock.locked()
		return False

	try:
		with (
			patch.object(jablotron._stream_stop_event, "wait", side_effect=wait_for_response),
			patch.object(jablotron, "_send_packets", side_effect=lambda batch: batches.append(list(batch))),
			patch.object(jablotron, "_send_packet", side_effect=lambda packet: batches.append([packet])),
		):
			await hass.services.async_call(
				"alarm_control_panel", "alarm_disarm",
				{"entity_id": segment.entity_id} | ({"code": entered_code} if entered_code else {}), blocking=True,
			)

		assert Jablotron.create_packet_authorisation_code(login_code) in batches[0]
		assert batches[1] == [modify_packet(143, 1), modify_packet(143, 2)]
		assert not jablotron._authorisation_lock.locked()
		assert not jablotron._authorisation_restore_pending
	finally:
		for entity in entities.values():
			await entity.async_remove()


async def test_startup_removes_the_registry_entries_of_a_deleted_segment(hass, panel):
	jablotron, _ = panel
	config_entry = hass.config_entries.async_get_entry(jablotron._config_entry_id)
	unique_id_prefix = "{}.{}.".format(DOMAIN, jablotron.central_unit().unique_id)
	devices = dr.async_get(hass)
	entities = er.async_get(hass)

	def register(control_id: str):
		device = devices.async_get_or_create(config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, control_id)})
		entity = entities.async_get_or_create(
			"alarm_control_panel", DOMAIN, unique_id_prefix + control_id, config_entry=config_entry, device_id=device.id,
		)
		return device, entity

	kept_device, kept_entity = register(SEGMENT_CONTROL_ID)
	deleted_device, deleted_entity = register("common_segment_dead0000")
	section_device, section_entity = register("section_1")

	await start(jablotron, DISARMED, DISARMED)

	assert entities.async_get(deleted_entity.entity_id) is None
	assert devices.async_get(deleted_device.id) is None
	assert entities.async_get(kept_entity.entity_id) is not None
	assert devices.async_get(kept_device.id) is not None
	assert entities.async_get(section_entity.entity_id) is not None
	assert devices.async_get(section_device.id) is not None


@pytest.mark.parametrize(
	("language", "label"),
	[
		pytest.param("sk", "Čiastočne zabezpečený", id="sk"),
		pytest.param("cs", "Částečně zabezpečeno", id="cs"),
		pytest.param("en", "Partially armed", id="en"),
	],
)
async def test_home_assistant_serves_the_partially_armed_label(hass, language, label):
	"""Home Assistant itself has to hand the override out, not just the JSON files.

	This is the key the frontend asks for when it renders the state of an entity
	that has a translation key: component.<platform>.entity.<domain>.<key>.state.<state>.
	"""
	translation.async_setup(hass)

	served = await translation.async_get_translations(hass, language, "entity", integrations={DOMAIN})

	key = "component.{}.entity.alarm_control_panel.common_segment.state.{}".format(DOMAIN, COMMON_SEGMENT_PARTIALLY_ARMED)
	assert served[key] == label
