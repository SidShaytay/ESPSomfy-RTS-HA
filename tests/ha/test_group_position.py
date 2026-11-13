"""HA and HomeKit group positions through the production cover and socket API."""

from copy import deepcopy

import pytest

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.espsomfy_rts.const import DOMAIN
from fake_device import HELLO, SYNC, TYPE_NAMES
from harness import UNIQUE_ID, until


def group_entity(hass):
    """The recorded native group, containing ten independently modelled shades."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "cover", DOMAIN, f"{UNIQUE_ID}_group1"
    )
    assert entity_id is not None
    return entity_id


def advertise_group_target(device, number=57):
    """Change the schema as firmware does; the test device owns its wire table."""
    device.sync = deepcopy(SYNC)
    device.hello = deepcopy(HELLO)
    device.type_names = dict(TYPE_NAMES)
    device.hello["d"]["sh"] = f"group-target-{number}"
    if number is not None:
        device.type_names[number] = "groupTarget"
        for frame in device.sync:
            if "T" in frame.get("d", {}):
                frame["d"]["T"].append([number, "groupTarget"])
                break
        else:
            raise AssertionError("Recorded sync has no type dictionary")


@pytest.fixture
def native_group_targets(device):
    """A protocol-2 firmware with explicit native group percentage support."""
    advertise_group_target(device)


async def set_group_position(hass, position):
    await hass.services.async_call(
        "cover", "set_cover_position",
        {"entity_id": group_entity(hass), "position": position}, blocking=True,
    )


@pytest.mark.parametrize("position", [0, 35, 100])
async def test_native_target_replaces_member_fanout_even_for_repeated_endpoints(
    hass, native_group_targets, loaded, device, position
):
    """An absolute group position is one request, with HA's open scale reversed."""
    await set_group_position(hass, position)
    await set_group_position(hass, position)
    assert device.ops == [("groupTarget", {"groupId": 1, "target": 100 - position})] * 2


async def test_native_target_uses_the_advertised_number(hass, entry, device):
    """Capability names, not historical type 55 or the current type 57, select it."""
    advertise_group_target(device, 71)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        await set_group_position(hass, 35)
        assert device.ops == [("groupTarget", {"groupId": 1, "target": 65})]
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("position,command", [(0, "down"), (100, "up")])
async def test_old_protocol2_endpoints_keep_native_group_arrows(
    hass, loaded, device, position, command
):
    """Older firmware can still synchronize absolute open/close via its group."""
    await set_group_position(hass, position)
    await set_group_position(hass, position)
    assert device.ops == [("groupCommand", {"groupId": 1, "command": command})] * 2


async def test_old_protocol2_intermediate_position_keeps_member_fallback(
    hass, loaded, device
):
    """Firmware without groupTarget retains its existing per-member percentages."""
    await set_group_position(hass, 35)
    assert len(device.ops) == 10
    assert {fields["shadeId"] for _, fields in device.ops} == set(range(1, 11))
    assert all(name == "shadeCommand" and fields["target"] == 65
               for name, fields in device.ops)


@pytest.mark.parametrize("operation,position", [("groupTarget", 35), ("groupCommand", 100)])
async def test_refused_group_request_propagates_without_member_retry(
    hass, entry, device, operation, position
):
    """A refusal must never change into a second, differently addressed move."""
    if operation == "groupTarget":
        advertise_group_target(device)
    device.replies[operation] = {"code": 6, "desc": "Radio TX unavailable"}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        with pytest.raises(HomeAssistantError, match="Radio TX unavailable"):
            await set_group_position(hass, position)
        assert device.ops == [(operation, {"groupId": 1, "target": 65}
                               if operation == "groupTarget" else
                               {"groupId": 1, "command": "up"})]
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_disconnected_native_target_does_not_change_to_legacy_fanout(
    hass, native_group_targets, loaded, device
):
    """Retain capability while disconnected so failure has no fallback effects."""
    api = hass.data[DOMAIN][loaded.entry_id].api
    conn = api._conn
    api.detach(conn)
    try:
        with pytest.raises(HomeAssistantError, match="Not connected"):
            await set_group_position(hass, 35)
        assert device.ops == []
    finally:
        api.attach(conn)


async def test_ambiguous_busy_does_not_retry_or_fan_out(
    hass, native_group_targets, loaded, device
):
    """An unattributed busy answer is ambiguous, never permission to replay RF."""
    device.busy_without_id["groupTarget"] = 1
    hass.data[DOMAIN][loaded.entry_id].api._conn._op_timeout = 0.2
    with pytest.raises(HomeAssistantError, match="No reply to groupTarget"):
        await set_group_position(hass, 35)
    assert device.ops == []
    assert device.busy_without_id["groupTarget"] == 0


async def test_reconnect_replaces_capability_when_firmware_schema_changes(
    hass, native_group_targets, loaded, device
):
    """A firmware downgrade removes native targets; a later upgrade restores them."""
    api = hass.data[DOMAIN][loaded.entry_id].api
    await set_group_position(hass, 35)
    assert device.ops == [("groupTarget", {"groupId": 1, "target": 65})]
    for number, expected in [
        (None, ("groupCommand", {"groupId": 1, "command": "up"})),
        (71, ("groupTarget", {"groupId": 1, "target": 0})),
    ]:
        connections = len(device.schema_params)
        advertise_group_target(device, number)
        await device.close_sockets()
        await until(lambda: len(device.schema_params) > connections and api._conn is not None)
        await hass.async_block_till_done()
        device.ops.clear()
        await set_group_position(hass, 100)
        assert device.ops == [expected]


async def test_accepted_target_waits_for_member_state_events(
    hass, native_group_targets, loaded, device
):
    """An acknowledgement is admission, not a motor-position acknowledgement."""
    entity_id = group_entity(hass)
    before = hass.states.get(entity_id).attributes["current_position"]
    await set_group_position(hass, 35)
    assert hass.states.get(entity_id).attributes["current_position"] == before
    for shade_id in range(1, 11):
        await device.send("shadeState", shadeId=shade_id, position=65, target=65, direction=0)
    await until(lambda: hass.states.get(entity_id).attributes["current_position"] == 35)


@pytest.mark.parametrize("members", [
    [{"shadeId": 1, "flipPosition": True}],
    [{"shadeId": 1}, {"shadeId": 2, "flipPosition": True}],
    [{"shadeId": 1, "shadeType": 3}],
])
async def test_native_target_leaves_member_coordinate_semantics_to_firmware(
    hass, native_group_targets, loaded, device, members
):
    """Flips and awnings must not change the absolute group target HA sends."""
    from custom_components.espsomfy_rts.cover import ESPSomfyGroup

    entity = ESPSomfyGroup(
        hass, hass.data[DOMAIN][loaded.entry_id],
        {"groupId": 1, "name": "position test", "linkedShades": members},
    )
    await entity.async_set_cover_position(position=35)
    assert device.ops == [("groupTarget", {"groupId": 1, "target": 65})]


@pytest.mark.parametrize("position", [0, 35, 100])
async def test_legacy_http_firmware_retains_group_endpoints_and_member_percentages(
    hass, entry, device, position
):
    """The capability check also preserves firmware predating protocol 2."""
    from unittest.mock import patch
    from custom_components.espsomfy_rts.controller import ESPSomfyAPI

    def same_port(api, host):
        api._host = host
        api._api_url = api._config_url = f"http://{host}"
        api._sock_url = f"ws://{host}/legacy"

    device.legacy = True
    device.legacy_groups = [{"groupId": 1, "name": "legacy group", "linkedShades": [{"shadeId": 1}]}]
    with patch.object(ESPSomfyAPI, "set_host", same_port):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            controller = hass.data[DOMAIN][entry.entry_id]
            assert controller.api.protocol == 1
            await set_group_position(hass, position)
            expected = ("shadeCommand", {"shadeId": 1, "target": 65}) if position == 35 else (
                "groupCommand", {"groupId": 1, "command": "up" if position == 100 else "down"}
            )
            assert device.puts == [expected]
            assert device.ops == []
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
