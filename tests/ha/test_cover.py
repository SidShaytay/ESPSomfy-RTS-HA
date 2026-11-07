"""Position commands from HomeKit and Home Assistant's cover slider."""

import pytest

from homeassistant.helpers import entity_registry as er
from homeassistant.exceptions import HomeAssistantError

from custom_components.espsomfy_rts.const import DOMAIN
from harness import UNIQUE_ID


def group_entity(hass):
    """The native group from the device's sync."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "cover", DOMAIN, f"{UNIQUE_ID}_group1"
    )
    assert entity_id is not None
    return entity_id


@pytest.mark.parametrize("position,command", [(0, "down"), (100, "up")])
async def test_group_endpoint_uses_one_native_command(
    hass, loaded, device, position, command
):
    """HomeKit endpoints use the group remote, including repeated requests."""
    entity_id = group_entity(hass)
    for _ in range(2):
        await hass.services.async_call(
            "cover", "set_cover_position",
            {"entity_id": entity_id, "position": position}, blocking=True,
        )
    assert device.ops == [
        ("groupCommand", {"groupId": 1, "command": command}),
        ("groupCommand", {"groupId": 1, "command": command}),
    ]


async def test_group_intermediate_position_keeps_individual_targets(hass, loaded, device):
    """A timed intermediate position still targets each member separately."""
    await hass.services.async_call(
        "cover", "set_cover_position",
        {"entity_id": group_entity(hass), "position": 35}, blocking=True,
    )
    assert len(device.ops) == 10
    assert {fields["shadeId"] for _, fields in device.ops} == set(range(1, 11))
    assert all(name == "shadeCommand" and fields["target"] == 65
               for name, fields in device.ops)


async def test_group_endpoint_reports_a_refused_transmission(hass, loaded, device):
    """A refused native command is surfaced rather than silently fanned out."""
    device.replies["groupCommand"] = {"code": 6, "desc": "Radio TX unavailable"}
    with pytest.raises(HomeAssistantError, match="Radio TX unavailable"):
        await hass.services.async_call(
            "cover", "set_cover_position",
            {"entity_id": group_entity(hass), "position": 100}, blocking=True,
        )
    assert device.ops == [("groupCommand", {"groupId": 1, "command": "up"})]


@pytest.fixture
def native_group_targets(device):
    """Advertise the new operation without changing old-firmware fixtures."""
    device.group_targets = True


@pytest.mark.parametrize("position", [0, 35, 100])
async def test_group_positions_use_advertised_native_target(
    hass, native_group_targets, loaded, device, position
):
    """HomeKit positions become one logical target, including repeated endpoints."""
    for _ in range(2):
        await hass.services.async_call(
            "cover", "set_cover_position",
            {"entity_id": group_entity(hass), "position": position}, blocking=True,
        )
    assert device.ops == [("groupTarget", {"groupId": 1, "target": 100 - position})] * 2


async def test_native_group_target_refusal_does_not_fan_out(
    hass, native_group_targets, loaded, device
):
    """A rejected plan fails the service; it must not start individual motors."""
    device.replies["groupTarget"] = {"code": 6, "desc": "Radio TX unavailable"}
    with pytest.raises(HomeAssistantError, match="Radio TX unavailable"):
        await hass.services.async_call(
            "cover", "set_cover_position",
            {"entity_id": group_entity(hass), "position": 35}, blocking=True,
        )
    assert device.ops == [("groupTarget", {"groupId": 1, "target": 65})]


async def test_group_target_does_not_claim_physical_position(
    hass, native_group_targets, loaded, device
):
    """An accepted target leaves member estimates to controller state events."""
    entity_id = group_entity(hass)
    before = hass.states.get(entity_id).attributes["current_position"]
    await hass.services.async_call(
        "cover", "set_cover_position",
        {"entity_id": entity_id, "position": 35}, blocking=True,
    )
    assert hass.states.get(entity_id).attributes["current_position"] == before


async def test_native_group_target_disconnect_fails_without_legacy_fallback(
    hass, native_group_targets, loaded, device
):
    """Loss of connection cannot change a native target into member commands."""
    api = hass.data[DOMAIN][loaded.entry_id].api
    conn = api._conn
    api.detach(conn)
    try:
        with pytest.raises(HomeAssistantError, match="Not connected"):
            await hass.services.async_call(
                "cover", "set_cover_position",
                {"entity_id": group_entity(hass), "position": 35}, blocking=True,
            )
        assert device.ops == []
    finally:
        api.attach(conn)


@pytest.mark.parametrize("members", [
    [{"shadeId": 1, "flipPosition": True}],
    [{"shadeId": 1}, {"shadeId": 2, "flipPosition": True}],
    [{"shadeId": 1, "shadeType": 3}],
])
async def test_native_group_target_leaves_member_semantics_to_firmware(
    hass, native_group_targets, loaded, device, members
):
    """Uniform, mixed flips and awnings all use the same logical percentage."""
    from custom_components.espsomfy_rts.cover import ESPSomfyGroup

    entity = ESPSomfyGroup(
        hass, hass.data[DOMAIN][loaded.entry_id],
        {"groupId": 1, "name": "test group", "linkedShades": members},
    )
    await entity.async_set_cover_position(position=35)
    assert device.ops == [("groupTarget", {"groupId": 1, "target": 65})]


async def test_native_group_state_follows_member_events(
    hass, native_group_targets, loaded, device
):
    """The group remains an aggregate of estimates reported by its members."""
    from harness import until

    entity_id = group_entity(hass)
    for shade_id in range(1, 11):
        await device.send("shadeState", shadeId=shade_id, position=65, target=65, direction=0)
    await until(lambda: hass.states.get(entity_id).attributes["current_position"] == 35)


@pytest.fixture
def flipped_group_model(device, request):
    """Give every group member the selected real shade position semantics."""
    from copy import deepcopy
    from fake_device import KEYS

    shade_type, flip = request.param
    device.group_targets = True
    device.sync = deepcopy(device.sync)
    for frame in device.sync:
        for shade in frame["d"].get("S", []):
            shade[KEYS["shadeType"]] = shade_type
            shade[KEYS["flipPosition"]] = int(flip)
    return shade_type, flip


@pytest.mark.parametrize("flipped_group_model", [(4, False), (4, True), (3, False), (3, True)], indirect=True)
async def test_native_group_readback_matches_requested_ha_percentage(
    hass, flipped_group_model, loaded, device
):
    """Stored position, display flip and awning semantics round-trip to HA."""
    from harness import until

    shade_type, flip = flipped_group_model
    entity_id = group_entity(hass)
    await hass.services.async_call(
        "cover", "set_cover_position",
        {"entity_id": entity_id, "position": 35}, blocking=True,
    )
    assert device.ops == [("groupTarget", {"groupId": 1, "target": 65})]
    # Firmware stores logical closed percent, reversed for awnings; published
    # member position applies flipPosition. The group aggregates HA positions.
    stored = 35 if shade_type == 3 else 65
    published = 100 - stored if flip else stored
    for shade_id in range(1, 11):
        await device.send("shadeState", shadeId=shade_id, position=published, target=published, direction=0)
    await until(lambda: hass.states.get(entity_id).attributes["current_position"] == 35)


async def test_native_group_ambiguous_busy_reply_is_not_retried(
    hass, native_group_targets, loaded, device
):
    """A short op without an attributable reply times out without replaying."""
    device.busy_without_id["groupTarget"] = 1
    hass.data[DOMAIN][loaded.entry_id].api._conn._op_timeout = 0.2
    with pytest.raises(HomeAssistantError, match="No reply to groupTarget"):
        await hass.services.async_call(
            "cover", "set_cover_position",
            {"entity_id": group_entity(hass), "position": 35}, blocking=True,
        )
    assert device.ops == []
    assert device.busy_without_id["groupTarget"] == 0
