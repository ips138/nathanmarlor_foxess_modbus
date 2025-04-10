"""
Custom integration to integrate FoxESS Modbus with Home Assistant.

For more details about this integration, please refer to
https://github.com/nathanmarlor/foxess_modbus
"""

import asyncio
import copy
import logging
import uuid
from typing import Any

from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import UNDEFINED
from slugify import slugify

from .client.modbus_client import ModbusClient
from .common.types import HassData
from .common.types import HassDataEntry
from .const import ADAPTER_ID
from .const import ADAPTER_WAS_MIGRATED
from .const import CONFIG_SAVE_TIME
from .const import DOMAIN
from .const import ENTITY_ID_PREFIX
from .const import FRIENDLY_NAME
from .const import HOST
from .const import INVERTER_CONN
from .const import INVERTERS
from .const import MAX_READ
from .const import MODBUS_SLAVE
from .const import MODBUS_TYPE
from .const import PLATFORMS
from .const import POLL_RATE
from .const import RTU_OVER_TCP
from .const import SERIAL
from .const import STARTUP_MESSAGE
from .const import TCP
from .const import UDP
from .const import UNIQUE_ID_PREFIX
from .inverter_adapters import ADAPTERS
from .inverter_profiles import inverter_connection_type_profile_from_config
from .modbus_controller import ModbusController
from .services import read_registers_service
from .services import update_charge_period_service
from .services import websocket_api
from .services import write_registers_service

_LOGGER: logging.Logger = logging.getLogger(__package__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up this integration using UI."""

    if DOMAIN not in hass.data:
        _LOGGER.info(STARTUP_MESSAGE)

    # It turns out that HA really doesn't like us mutating the ConfigEntry it passes us!
    # Since we merge in the options etc, do this on a copy.
    # From here on, do not access entry.data and entry.options directly!
    entry_data = copy.deepcopy(dict(entry.data))
    entry_options = copy.deepcopy(dict(entry.options))

    # Create this before throwing ConfigEntryAuthFailed, so the sensors, etc, platforms don't fail
    hass.data.setdefault(DOMAIN, HassData()).setdefault(
        entry.entry_id, HassDataEntry(controllers=[], modbus_clients=[])
    )

    def create_controller(client: ModbusClient, inverter: dict[str, Any]) -> None:
        controller = ModbusController(
            hass,
            client,
            inverter_connection_type_profile_from_config(inverter),
            inverter,
            inverter[MODBUS_SLAVE],
            inverter[POLL_RATE],
            inverter[MAX_READ],
        )
        controllers.append(controller)

    controllers: list[ModbusController] = []

    # {(modbus_type, host): client}
    clients: dict[tuple[str, str], ModbusClient] = {}
    for inverter_id, inverter in entry_data[INVERTERS].items():
        # Remember that there might not be any options
        options = entry_options.get(INVERTERS, {}).get(inverter_id, {})

        # Pick the adapter out of the user options if it's there
        adapter_id = options.get(ADAPTER_ID, inverter[ADAPTER_ID])
        adapter = ADAPTERS[adapter_id]

        # Merge in adapter options. This lets us tweak the adapters later, and those settings are reflected back to
        # users.
        # Do this after the lines above, so we can respond to an adapter in the options
        inverter.update(adapter.config.inverter_config(inverter[MODBUS_TYPE]))

        # Merge in the user's options, if any. These can override the adapter options set above
        if options:
            inverter.update(options)

        client_key = (inverter[MODBUS_TYPE], inverter[HOST])
        client = clients.get(client_key)
        if client is None:
            if inverter[MODBUS_TYPE] in [TCP, UDP, RTU_OVER_TCP]:
                host_parts = inverter[HOST].split(":")
                params = {"host": host_parts[0], "port": int(host_parts[1])}
            elif inverter[MODBUS_TYPE] == SERIAL:
                params = {"port": inverter[HOST], "baudrate": 9600}
            else:
                raise AssertionError()
            client = ModbusClient(hass, inverter[MODBUS_TYPE], adapter, params)
            clients[client_key] = client
        create_controller(client, inverter)

    read_registers_service.register(hass, controllers)
    write_registers_service.register(hass, controllers)
    update_charge_period_service.register(hass, controllers)
    websocket_api.register(hass)

    hass_data: HassData = hass.data[DOMAIN]
    hass_data[entry.entry_id]["controllers"] = controllers
    hass_data[entry.entry_id]["modbus_clients"] = list(clients.values())
    hass_data[entry.entry_id]["unload"] = entry.add_update_listener(async_reload_entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Perform any necessary migrations on the config entry"""

    _LOGGER.debug("Migrating from version %s", config_entry.version)

    data = copy.deepcopy(dict(config_entry.data))
    version = config_entry.version
    new_options = UNDEFINED

    if version == 1:
        # Introduce adapter selection
        new_data = {
            INVERTERS: {},
            CONFIG_SAVE_TIME: data[CONFIG_SAVE_TIME],
        }
        if config_entry.options:
            inverter_options = {
                POLL_RATE: config_entry.options[POLL_RATE],
                MAX_READ: config_entry.options[MAX_READ],
            }
            new_options = {INVERTERS: {}}
        else:
            inverter_options = {}
            new_options = UNDEFINED

        for modbus_type, modbus_type_inverters in config_entry.data.items():
            if modbus_type in [TCP, UDP, SERIAL]:  # Didn't have RTU_OVER_TCP then
                for host, host_inverters in modbus_type_inverters.items():
                    for friendly_name, inverter in host_inverters.items():
                        if friendly_name == "null":
                            friendly_name = ""
                        inverter[MODBUS_TYPE] = modbus_type
                        inverter[HOST] = host
                        inverter[FRIENDLY_NAME] = friendly_name
                        # We can infer what the adapter type is, ish
                        if modbus_type == TCP:
                            if inverter[INVERTER_CONN] == "LAN":
                                adapter = ADAPTERS["direct"]
                            else:
                                adapter = ADAPTERS["network_other"]
                        elif modbus_type == SERIAL:
                            adapter = ADAPTERS["serial_other"]
                        else:
                            raise AssertionError()
                        inverter[ADAPTER_ID] = adapter.adapter_id
                        inverter[ADAPTER_WAS_MIGRATED] = True

                        inverter_id = str(uuid.uuid4())
                        new_data[INVERTERS][inverter_id] = inverter
                        if inverter_options:
                            new_options[INVERTERS][inverter_id] = inverter_options

        data = new_data
        version = 2

    if version == 2:
        # Fix a badly-set-up energy dashboard
        energy_manager = await async_get_manager(hass)
        if energy_manager.data is not None:
            energy_data = copy.deepcopy(energy_manager.data)
            for energy_source in energy_data.get("energy_sources", []):
                if energy_source["type"] == "solar":
                    energy_source.setdefault("config_entry_solar_forecast", None)
                elif energy_source["type"] == "grid":
                    for flow_from in energy_source.get("flow_from", []):
                        flow_from.setdefault("stat_cost", None)
                        flow_from.setdefault("entity_energy_price", None)
                        flow_from.setdefault("number_energy_price", None)
                    for flow_to in energy_source.get("flow_to", []):
                        flow_to.setdefault("stat_compensation", None)
                        flow_to.setdefault("entity_energy_price", None)
                        flow_to.setdefault("number_energy_price", None)
            await energy_manager.async_update(energy_data)

        version = 3

    if version == 3:
        # Add entity ID prefix
        for inverter in data.get(INVERTERS, {}).values():
            inverter[ENTITY_ID_PREFIX] = inverter[FRIENDLY_NAME]

        version = 4

    if version == 4:
        # Old versions accidentally mutated ConfigEntry.data
        for inverter in data.get(INVERTERS, {}).values():
            inverter.pop(POLL_RATE, None)
            inverter.pop(MAX_READ, None)
            if inverter[FRIENDLY_NAME] is None:
                inverter[FRIENDLY_NAME] = ""
            if inverter[ENTITY_ID_PREFIX] is None:
                inverter[ENTITY_ID_PREFIX] = ""

        version = 5

    if version == 5:
        # Having "TCP" / "UDP" / "SERIAL" in all-caps is annoying for translations in the config flow
        # Also change "TCP+RTU" to "rtu_over_tcp" (to remove "+", which makes translations annoying)
        for inverter in data.get(INVERTERS, {}).values():
            if inverter[MODBUS_TYPE] == "TCP+RTU":
                inverter[MODBUS_TYPE] = "rtu_over_tcp"
            else:
                inverter[MODBUS_TYPE] = inverter[MODBUS_TYPE].lower()

        version = 6

    if version == 6:
        # We still have users with entity ID prefixes which aren't valid in entity IDs. HA will automatically convert
        # them, but this causes the charge period card to complain. Fix them once and for all.
        for inverter in data.get(INVERTERS, {}).values():
            inverter[UNIQUE_ID_PREFIX] = inverter[ENTITY_ID_PREFIX]
            if inverter[ENTITY_ID_PREFIX]:
                inverter[ENTITY_ID_PREFIX] = slugify(inverter[ENTITY_ID_PREFIX], separator="_").rstrip("_")

        version = 7

    _LOGGER.info("Migration from version %s to version %s successful", config_entry.version, version)
    hass.config_entries.async_update_entry(entry=config_entry, data=data, options=new_options, version=version)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    unloaded = all(
        await asyncio.gather(
            *[hass.config_entries.async_forward_entry_unload(entry, platform) for platform in PLATFORMS]
        )
    )

    if unloaded:
        hass_data: HassData = hass.data[DOMAIN]
        controllers = hass_data[entry.entry_id]["controllers"]
        for controller in controllers:
            controller.unload()

        clients = hass_data[entry.entry_id]["modbus_clients"]
        await asyncio.gather(*[client.close() for client in clients])

        hass_data[entry.entry_id]["unload"]()
        hass_data.pop(entry.entry_id)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def options_update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)
