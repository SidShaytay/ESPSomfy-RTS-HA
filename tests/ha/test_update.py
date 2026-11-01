"""The update entity: protocol 2's latest release, from GitHub."""

from __future__ import annotations

import aiohttp
from harness import UNIQUE_ID, until
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.components.update import UpdateEntityFeature
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.espsomfy_rts import update
from custom_components.espsomfy_rts.const import DOMAIN


def update_state(hass):
    """The update entity's state."""
    found = er.async_get(hass).async_get_entity_id(
        "update", DOMAIN, f"update_{UNIQUE_ID}"
    )
    assert found is not None
    return hass.states.get(found)


def check_again(hass) -> None:
    """Let RELEASE_CHECK_INTERVAL pass.

    The device's socket times out over that time too: the entity is
    unavailable until it is back.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + update.RELEASE_CHECK_INTERVAL)


def latest_version(hass) -> str | None:
    """The latest version, None while the entity is unavailable."""
    return update_state(hass).attributes.get("latest_version")


async def test_a_newer_release_is_an_update(hass, loaded, device, hass_ws_client):
    """GitHub's latest release is the latest version, with its page and notes."""
    await until(lambda: update_state(hass).state == STATE_ON)
    state = update_state(hass)
    assert state.attributes["installed_version"] == "v3.0.14"
    assert state.attributes["latest_version"] == "v4.0.1"
    assert state.attributes["release_url"] == (
        "https://github.com/cjkas/ESPSomfy-RTS/releases/tag/v4.0.1"
    )
    # Installed from the device's own page, never from here.
    assert state.attributes["supported_features"] == UpdateEntityFeature.RELEASE_NOTES
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "update/release_notes", "entity_id": state.entity_id}
    )
    reply = await client.receive_json()
    assert reply["success"], reply
    assert reply["result"] == "What's new in v4.0.1"


async def test_the_latest_release_installed_is_up_to_date(hass, entry, device):
    """The same version installed as released: no update."""
    device.release[1]["tag_name"] = "v3.0.14"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: update_state(hass).state == STATE_OFF)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("fw", "expected"),
    [
        ("v4.0.0", STATE_ON),
        ("v4.0.0.beta", STATE_ON),
        ("v4.0.1.beta", STATE_OFF),
        ("v4.1.0.beta", STATE_OFF),
        ("v4.0.2", STATE_OFF),
    ],
)
async def test_only_higher_numbers_are_an_update(hass, entry, device, fw, expected):
    """X.Y.Z are compared; what follows them (.beta) is left out."""
    device.fw = fw
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: update_state(hass).state == expected)
    assert update_state(hass).attributes["installed_version"] == fw
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("version", "numbers"),
    [
        ("v4.0.1", (4, 0, 1)),
        ("4.0.1", (4, 0, 1)),
        ("v4.0.0.beta", (4, 0, 0)),
        ("v4.10.0-rc1", (4, 10, 0)),
        ("v4.0", None),
        ("beta", None),
    ],
)
def test_the_numbers_leave_out_what_follows(version, numbers):
    """A version's X.Y.Z, without what follows them."""
    assert update.release_numbers(version) == numbers


async def test_flashing_the_latest_release_is_up_to_date(hass, loaded, device):
    """The device back with the latest release installed: no update."""
    await until(lambda: update_state(hass).state == STATE_ON)
    device.fw = "v4.0.1"
    # Flashed, the device reboots: its socket closes and comes back.
    await device.close_sockets()
    await until(lambda: update_state(hass).state == STATE_OFF)
    assert update_state(hass).attributes["installed_version"] == "v4.0.1"


async def test_the_latest_is_unknown_until_github_answers(hass, entry, device):
    """A check that fails leaves it unknown; the next one tells."""
    device.release = (503, {"message": "busy"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: device.release_checks == 1)
    await hass.async_block_till_done()
    assert update_state(hass).state == STATE_UNKNOWN
    device.release = (200, {"tag_name": "v4.0.1"})
    check_again(hass)
    await until(lambda: update_state(hass).state == STATE_ON)
    assert latest_version(hass) == "v4.0.1"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_failed_check_keeps_the_last_release(hass, loaded, device):
    """GitHub not answering doesn't forget the release it told of."""
    await until(lambda: update_state(hass).state == STATE_ON)
    device.release = (500, {})
    check_again(hass)
    await until(
        lambda: device.release_checks == 2
        and update_state(hass).state != STATE_UNAVAILABLE
    )
    await hass.async_block_till_done()
    assert update_state(hass).state == STATE_ON
    assert latest_version(hass) == "v4.0.1"


async def test_a_later_release_replaces_the_last(hass, loaded, device):
    """Each check takes the release GitHub has now."""
    await until(lambda: update_state(hass).state == STATE_ON)
    device.release = (200, {"tag_name": "v4.1.0"})
    check_again(hass)
    await until(lambda: latest_version(hass) == "v4.1.0")


async def test_a_release_without_its_page_links_its_tag(device, host):
    """The page is the tag's when GitHub leaves it out; no notes are empty ones."""
    device.release = (200, {"tag_name": "v4.0.1", "html_url": None, "body": None})
    async with aiohttp.ClientSession() as session:
        release = await update.fetch_latest_release(session)
    assert release == update.Release(
        version="v4.0.1",
        url="https://github.com/cjkas/ESPSomfy-RTS/releases/tag/v4.0.1",
        notes="",
    )


@pytest.mark.parametrize(
    "body",
    [[], "v4.0.1", {}, {"tag_name": ""}, {"tag_name": 401}],
    ids=["list", "string", "no tag", "empty tag", "number tag"],
)
async def test_an_answer_without_a_version_is_no_release(device, host, body):
    """Whatever GitHub answers without a tag is no release, not an error."""
    device.release = (200, body)
    async with aiohttp.ClientSession() as session:
        assert await update.fetch_latest_release(session) is None


async def test_github_out_of_reach_is_no_release(monkeypatch, socket_enabled):
    """No connection is no release, not an error."""
    monkeypatch.setattr(update, "LATEST_RELEASE_URL", "http://127.0.0.1:9/")
    async with aiohttp.ClientSession() as session:
        assert await update.fetch_latest_release(session) is None
