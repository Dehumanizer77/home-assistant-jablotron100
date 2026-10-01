"""The common segments options flow against a real Home Assistant.

Both things checked here only go wrong against the real config entry and
device registries: a stand-in that copies options on its own, or that knows a
single panel, behaves correctly and hides the bug.
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import translation
import pytest

from custom_components.jablotron100.config_flow import JablotronOptionsFlow
from custom_components.jablotron100.const import (
	CONF_COMMON_SEGMENTS,
	CONF_DEVICES,
	CONF_NUMBER_OF_DEVICES,
	CONF_NUMBER_OF_PG_OUTPUTS,
	CONF_SERIAL_PORT,
	CONF_UNIQUE_ID,
	CommonSegmentData,
	DOMAIN,
	EntityType,
)
from custom_components.jablotron100.jablotron import Jablotron, JablotronAlarmControlPanel, JablotronCentralUnit


pytestmark = pytest.mark.asyncio

ID = CommonSegmentData.ID.value
NAME = CommonSegmentData.NAME.value
SECTIONS = CommonSegmentData.SECTIONS.value


def add_entry(hass, entry_id: str) -> ConfigEntry:
	entry = ConfigEntry(
		entry_id=entry_id, domain=DOMAIN, title=entry_id, data={}, options={},
		source="user", unique_id=entry_id, version=1, minor_version=1,
		discovery_keys=MappingProxyType({}), subentries_data=None,
	)
	hass.config_entries._entries[entry_id] = entry
	return entry


def open_flow(hass, entry: ConfigEntry) -> JablotronOptionsFlow:
	flow = JablotronOptionsFlow(entry)
	flow.hass = hass
	return flow


@pytest.fixture
def tracked(hass):
	"""A config entry with its reloads and scheduled writes counted.

	The comparison and the update itself are Home Assistant's own; only the
	two things that would follow a change are replaced, so they can be counted.
	"""
	entry = add_entry(hass, "test-entry")
	listener = AsyncMock()
	entry.add_update_listener(listener)

	with patch.object(hass.config_entries, "_async_schedule_save") as schedule_save:
		async def changes() -> tuple[int, int]:
			await hass.async_block_till_done()
			return listener.await_count, schedule_save.call_count

		yield entry, changes


async def submit(flow: JablotronOptionsFlow, name: str, sections: list[int]) -> None:
	await flow.async_step_common_segment_form({"name": name, "sections": [str(section) for section in sections]})


async def test_every_change_made_in_one_dialog_reaches_home_assistant(hass, tracked):
	"""Add, add, edit, remove without closing the dialog: four reloads, four writes."""
	entry, changes = tracked
	flow = open_flow(hass, entry)

	await submit(flow, "Ground floor", [1, 2])
	assert await changes() == (1, 1)

	await submit(flow, "Upstairs", [3])
	assert await changes() == (2, 2)

	first, second = entry.options[CONF_COMMON_SEGMENTS]

	await flow.async_step_common_segments({"action": "edit_{}".format(second[ID])})
	await submit(flow, "Upstairs", [3, 4])
	assert await changes() == (3, 3)

	await flow.async_step_common_segments({"action": "remove_{}".format(first[ID])})
	assert await changes() == (4, 4)

	assert entry.options[CONF_COMMON_SEGMENTS] == [{ID: second[ID], NAME: "Upstairs", SECTIONS: [3, 4]}]


async def test_saved_options_are_detached_from_the_dialog(hass, tracked):
	"""What the dialog does next must not leak into the stored options."""
	entry, _ = tracked
	flow = open_flow(hass, entry)
	await submit(flow, "Ground floor", [1, 2])
	saved = {ID: entry.options[CONF_COMMON_SEGMENTS][0][ID], NAME: "Ground floor", SECTIONS: [1, 2]}

	flow._options[CONF_COMMON_SEGMENTS][0][SECTIONS].append(9)
	flow._options[CONF_COMMON_SEGMENTS].append({ID: "ffffffff", NAME: "Unsaved", SECTIONS: [5]})
	flow._options["unsaved"] = True

	assert dict(entry.options) == {CONF_COMMON_SEGMENTS: [saved]}


async def test_closing_the_dialog_after_a_save_does_not_reload_again(hass, tracked):
	entry, changes = tracked
	flow = open_flow(hass, entry)
	await submit(flow, "Ground floor", [1, 2])
	assert await changes() == (1, 1)

	result = await flow.async_step_common_segments({"action": "done"})

	assert result["type"] == FlowResultType.CREATE_ENTRY
	# What OptionsFlowManager.async_finish_flow() does with the result.
	hass.config_entries.async_update_entry(entry, options=result["data"])
	assert await changes() == (1, 1)


@pytest.fixture
def panels(hass):
	"""Two panels that both have a section 1, registered as their own devices."""
	translation.async_setup(hass)
	registry = dr.async_get(hass)
	result = {}
	for entry_id in ("panel-a", "panel-b"):
		entry = add_entry(hass, entry_id)
		instance = Jablotron(
			hass, entry_id,
			{
				CONF_UNIQUE_ID: entry_id, CONF_SERIAL_PORT: "/dev/test-only", CONF_PASSWORD: "1234",
				CONF_NUMBER_OF_DEVICES: 0, CONF_NUMBER_OF_PG_OUTPUTS: 0, CONF_DEVICES: [],
			},
			{},
		)
		instance._central_unit = JablotronCentralUnit(entry_id, "JA-103K", "1", "1")
		section_device = Jablotron._create_section_hass_device(1)
		instance.entities[EntityType.ALARM_CONTROL_PANEL]["section_1"] = JablotronAlarmControlPanel(
			instance.central_unit(), section_device, "section_1", 1,
		)
		entry.runtime_data = instance
		device = registry.async_get_or_create(config_entry_id=entry_id, identifiers={(DOMAIN, section_device.id)})
		result[entry_id] = (entry, device)
	return result


async def test_section_labels_come_from_the_dialog_s_own_panel(hass, panels):
	"""Section identifiers repeat across panels; the names must not cross over."""
	registry = dr.async_get(hass)
	names = {"panel-a": "Panel A - Garage", "panel-b": "Panel B - Bedroom"}
	for entry_id, (_, device) in panels.items():
		registry.async_update_device(device.id, name_by_user=names[entry_id])

	for entry_id, (entry, _) in panels.items():
		options = await open_flow(hass, entry)._get_section_selector_options()

		assert options == [{"value": "1", "label": names[entry_id]}]


async def section_labels(hass, panels) -> dict[str, str]:
	labels = {}
	for entry_id, (entry, _) in panels.items():
		options = await open_flow(hass, entry)._get_section_selector_options()
		assert [option["value"] for option in options] == ["1"]
		labels[entry_id] = options[0]["label"]
	return labels


async def test_a_rename_on_one_panel_stays_on_that_panel(hass, panels):
	"""Checked from both panels, so it does not matter which device a lookup
	that ignores the config entry happens to find first: one side is wrong."""
	hass.config.language = "cs"
	_, renamed = panels["panel-a"]
	dr.async_get(hass).async_update_device(renamed.id, name_by_user="Panel A - Garage")

	assert await section_labels(hass, panels) == {"panel-a": "Panel A - Garage", "panel-b": "Sekce 1"}


async def test_section_without_a_registered_device_gets_the_translated_name(hass, panels):
	"""The other panel's device is the only match left - and must not be used."""
	hass.config.language = "cs"
	registry = dr.async_get(hass)
	_, other = panels["panel-a"]
	registry.async_update_device(other.id, name_by_user="Panel A - Garage")
	_, device = panels["panel-b"]
	registry.async_remove_device(device.id)

	assert await section_labels(hass, panels) == {"panel-a": "Panel A - Garage", "panel-b": "Sekce 1"}
