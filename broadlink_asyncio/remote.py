"""Support for the Xiaomi IR Remote (Chuangmi IR)."""
import logging
import asyncio
from datetime import timedelta
import re

import voluptuous as vol
import binascii
from base64 import b64decode, b64encode

from homeassistant.components.remote import (
    PLATFORM_SCHEMA, DOMAIN, ATTR_NUM_REPEATS, ATTR_DELAY_SECS,
    DEFAULT_DELAY_SECS, RemoteDevice, ATTR_HOLD_SECS, DEFAULT_HOLD_SECS)
from homeassistant.const import (
    CONF_NAME, CONF_HOST, CONF_MAC, CONF_TIMEOUT,
    ATTR_ENTITY_ID, STATE_OFF, STATE_ON)
import homeassistant.helpers.config_validation as cv
from homeassistant.util import Throttle

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=30)

REQUIREMENTS = ['pybroadlink>=1.0']

_LOGGER = logging.getLogger(__name__)

STATE_LEARNING = "learning"
STATE_LEARNING_INIT = "learning_init"
STATE_LEARNING_OK = "learning_ok"
STATE_LEARNING_KEY = "learning_key"

SERVICE_LEARN = 'broadlink_asyncio_learn'
DATA_KEY = 'remote.broadlink_asyncio'

CONF_REMOTES = 'remotes'
CONF_KEYS = 'keys'

DEFAULT_TIMEOUT = 5

LEARN_COMMAND_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): vol.All(str),
    vol.Optional(CONF_TIMEOUT, default=30): vol.All(int, vol.Range(min=10)),
    vol.Optional(CONF_KEYS, default=["NA_1"]): vol.All(cv.ensure_list, [cv.slug])
})

COMMAND_SCHEMA = vol.All(cv.ensure_list, [cv.string])

KEYS_SCHEMA = cv.schema_with_slug_keys(COMMAND_SCHEMA)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_NAME): cv.slug,
    vol.Required(CONF_HOST): cv.string,
    vol.Required(CONF_MAC): cv.string,
    vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT):
        vol.All(int, vol.Range(min=1)),
    vol.Optional(CONF_REMOTES, default={}):
        cv.schema_with_slug_keys(KEYS_SCHEMA),
}, extra=vol.ALLOW_EXTRA)


async def async_setup_platform(hass, config, async_add_entities,
                               discovery_info=None):
    """Set up the Xiaomi IR Remote (Chuangmi IR) platform."""
    from pybroadlink.broadlink_udp import (BroadlinkRM3, PORT)
    ip_addr = config.get(CONF_HOST)
    mac_addr = binascii.unhexlify(
        config.get(CONF_MAC).encode().replace(b':', b''))

    # Create handler
    # The Chuang Mi IR Remote Controller wants to be re-discovered every
    # 5 minutes. As long as polling is disabled the device should be
    # re-discovered (lazy_discover=False) in front of every command.

    # Check that we can communicate with device.

    if DATA_KEY not in hass.data:
        hass.data[DATA_KEY] = {}

    friendly_name = config.get(CONF_NAME)
    timeout = config.get(CONF_TIMEOUT)
    device = BroadlinkRM3((ip_addr, PORT), mac_addr, timeout=timeout)

    # cmnds = fill_commands(config.get(CONF_COMMANDS)
    remotes = config.get(CONF_REMOTES)
    allcmnds = dict()
    for remnm, remkeys in remotes.items():
        for keynm, keycmnds in remkeys.items():
            allcmnds[remnm + "@" + keynm] = keycmnds
    xiaomi_miio_remote = BroadlinkRemote(friendly_name, device, allcmnds, '')
    hass.data[DATA_KEY][friendly_name] = xiaomi_miio_remote
    lstent = [xiaomi_miio_remote]
    for remnm, remkeys in remotes.items():
        xiaomi_miio_remote = BroadlinkRemote(friendly_name+"_"+remnm, device, remkeys, friendly_name)
        lstent.append(xiaomi_miio_remote)
    async_add_entities(lstent)

    async def async_service_handler(service):
        """Handle a learn command."""
        if service.service != SERVICE_LEARN:
            _LOGGER.error("We should not handle service: %s", service.service)
            return

        entity_id = service.data.get(ATTR_ENTITY_ID)
        if entity_id.startswith("remote."):
            entity_id = entity_id[len("remote."):]

        if entity_id not in hass.data[DATA_KEY]:
            _LOGGER.error("entity_id: '%s' not found", entity_id)
            return
        entity = hass.data[DATA_KEY][entity_id]

        timeout = service.data.get(CONF_TIMEOUT, 30)
        keynames = service.data.get(CONF_KEYS, ["NA_1"])
        numkeys = len(keynames)
        pn = hass.components.persistent_notification
        allnot = ''
        msg = ''

        for xx in range(numkeys):
            try:
                if await entity.enter_learning_mode():
                    await asyncio.sleep(3)
                    keyname = keynames[xx]
                    msg = "Press the key you want Home Assistant to learn [%s] %d/%d" % (keyname, xx+1, numkeys)
                    _LOGGER.info(msg)
                    pn.async_create(msg, title='Broadlink RM', notification_id='broadlink_asyncio_learning')
                    packet = await entity.get_learned_key(timeout, keyname)
                    if packet:
                        b64k = b64encode(packet).decode('utf8')
                        notif = '{}:\n    - "r{}"\n'.format(keyname, b64k)
                        msg = "Received is: r{} or h{}".format(b64k, binascii.hexlify(packet).decode('utf8'))
                    else:
                        notif = ''
                        msg = "Did not receive any key"
                    _LOGGER.info(msg)
                    allnot += notif + '\n'
                    pn.async_create(allnot, title='Broadlink RM', notification_id='broadlink_asyncio_learned')
                    pn.async_dismiss(notification_id='broadlink_asyncio_learning')
                else:
                    msg = "Failed entering learning mode"
                    _LOGGER.error(msg)
                    pn.async_create(msg, title='Broadlink RM', notification_id='broadlink_asyncio_learning')
            except BaseException as ex:
                _LOGGER.error("Learning error %s", ex)
        msg = "Learning ends NOW"
        _LOGGER.info(msg)
        await entity.exit_learning_mode()

    hass.services.async_register(DOMAIN, SERVICE_LEARN, async_service_handler,
                                 schema=LEARN_COMMAND_SCHEMA)


class BroadlinkRemote(RemoteDevice):
    """Representation of a Xiaomi Miio Remote device."""

    def __init__(self, friendly_name, device, commands, main_entity):
        """Initialize the remote."""
        self._name = friendly_name
        self._device = device
        self._state = STATE_OFF
        self._commands = commands
        self._states = dict(last_learned=dict(), key_to_learn='')
        self._main = main_entity

    @property
    def name(self):
        """Return the name of the remote."""
        return self._name

    @property
    def device(self):
        """Return the remote object."""
        return self._device

    @property
    def state(self):
        """Return the state."""
        return self._state

    @property
    def is_on(self):
        """Return False if device is unreachable, else True."""
        return self._state != STATE_OFF

    @property
    def should_poll(self):
        """We should not be polled for device up state."""
        return True

    async def enter_learning_mode(self, timeout=-1, retry=3):
        self._state = STATE_LEARNING_INIT
        # self._states['last_learned'] = dict()
        await self.async_update_ha_state()
        rv = await self._device.enter_learning_mode(timeout=timeout, retry=retry)
        if rv:
            self._state = STATE_LEARNING_OK
        return rv

    async def exit_learning_mode(self, timeout=-1, retry=3):
        self._state = STATE_ON
        await self.async_update_ha_state()
        return True

    async def get_learned_key(self, timeout=30, keyname='NA'):
        self._state = STATE_LEARNING_KEY
        self._states['key_to_learn'] = keyname
        await self.async_update_ha_state()
        rv = await self._device.get_learned_key(timeout=timeout)
        if rv:
            self._states['last_learned'][keyname] = binascii.hexlify(rv).decode('utf8')
        self._state = STATE_LEARNING_OK
        self._states['key_to_learn'] = ''
        await self.async_update_ha_state()
        return rv

    @property
    def device_state_attributes(self):
        """Hide remote by default."""
        return self._states

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def async_update(self):
        if len(self._main):
            sto = self.hass.states.get("remote." + self._main)
            self._state = sto.state
            self._states = sto.attributes
        else:
            if not self._state.startswith(STATE_LEARNING):
                if await self._device.auth():
                    if self._state == STATE_OFF:
                        self._state = STATE_ON
                else:
                    self._state = STATE_OFF
                    self._states['last_learned'] = dict()
                _LOGGER.debug("New state is %s", self._state)

    async def async_turn_on(self, **kwargs):
        """Turn the device on."""
        _LOGGER.error("Device does not support turn_on, "
                      "please use 'remote.send_command' to send commands.")

    async def async_turn_off(self, **kwargs):
        """Turn the device off."""
        _LOGGER.error("Device does not support turn_off, "
                      "please use 'remote.send_command' to send commands.")

    async def _send_command(self, packet, totretry):
        try:
            if type(packet) is tuple:
                num = packet[1]
                pid = packet[0][0]
                packet = packet[0][1:]
            else:
                pid = packet[0]
                packet = packet[1:]
                num = -1
            _LOGGER.info("Pid is %s, Len is %d Rep is %d", pid, len(packet), num)
            if pid == 'r':
                extra = len(packet) % 4
                if extra > 0:
                    packet = packet + ('=' * (4 - extra))
                payload = b64decode(packet)
                add = "b64dec"
            elif pid == 'h':
                payload = binascii.unhexlify(packet)
                add = "unhex"
            elif pid == "t":
                await asyncio.sleep(float(packet))
                return True
            else:
                return False
        except BaseException as ex:
            _LOGGER.error("Err1: %s ", ex)
            return False
        if num > 0:
            if num > 100:
                num = 100
            _LOGGER.info("Changing payload")
            payload = bytes([payload[0]])+bytes([num])+payload[2:]
        _LOGGER.info("I am sending %s, Final len is %d", add, len(payload))
        await self._device.emit_ir(payload, retry=totretry)
        return False

    def command2payloads(self, command):
        _LOGGER.info("Searching for %s", command)
        if command in self._commands:
            _LOGGER.info("%s found in commands", command)
            return self._commands[command]
        elif command.startswith('@'):
            return [command[1:]]
        else:
            mo = re.search("^(([a-zA-Z0-9_]*)@)?ch([0-9]+)$", command)
            pre = '' if not mo or not mo[1] else mo[1]
            if mo is not None and pre+'ch1' in self._commands:
                commands = [self._commands[pre + 'ch' + x][0] for x in mo[3]]
            else:
                mo = re.search("^([a-zA-Z0-9_]+)#([0-9]+)$", command)
                if mo is not None:
                    nm = mo.group(1)
                    num = int(mo.group(2))
                    _LOGGER.info("%s rep %d. Searching...", nm, num)
                    if nm in self._commands:
                        _LOGGER.info("%s found in commands", nm)
                        cmdl = self._commands[nm]
                        return list(zip(cmdl, [num for _ in range(len(cmdl))]))
                    else:
                        return []
                else:
                    commands = [command]
            return commands

    async def async_send_command(self, command, **kwargs):
        """Send a command."""
        num_repeats = kwargs.get(ATTR_NUM_REPEATS, 1)

        delay = kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS)
        hold = kwargs.get(ATTR_HOLD_SECS, DEFAULT_HOLD_SECS)

        for k in range(num_repeats):
            j = 0
            for c in command:
                payloads = self.command2payloads(c)
                i = 0
                for local_payload in payloads:
                    pause = await self._send_command(local_payload, 3)
                    i += 1
                    if i < len(payloads) and not pause:
                        await asyncio.sleep(hold)
                j += 1
                if j < len(command) and k < num_repeats - 1:
                    await asyncio.sleep(delay)
