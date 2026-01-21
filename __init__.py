#!/usr/bin/env python3
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
#  Copyright 2023      Ronny Schulz                   ronny_schulz@gmx.de
#########################################################################
#  This file is part of SmartHomeNG.
#  https://www.smarthomeNG.de
#  https://knx-user-forum.de/forum/supportforen/smarthome-py
#
#  Inverter plugin for the Huawei SUN2000 to run with SmartHomeNG version
#  1.8 and upwards.
#
#  SmartHomeNG is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHomeNG is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHomeNG. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

from lib.model.smartplugin import SmartPlugin
from lib.item import Items

from .webif import WebInterface

import time
from huawei_solar import AsyncHuaweiSolar, register_names as rn
import asyncio


class EquipmentCheck:
    def __init__(self, register, true_value, true_comparator, status=False):
        self.register = register
        self.true_value = true_value
        self.true_comparator = true_comparator
        self.status = status


EquipmentDictionary = {
    "STORAGE": EquipmentCheck(rn.STORAGE_RATED_CAPACITY, 0, '>'),
    "STORAGE_UNIT_1": EquipmentCheck(rn.STORAGE_UNIT_1_NO, 0, '>'),
    "STORAGE_UNIT_1_BATTERY_PACK_1": EquipmentCheck(rn.STORAGE_UNIT_1_PACK_1_NO, 0, '>'),
    "STORAGE_UNIT_1_BATTERY_PACK_2": EquipmentCheck(rn.STORAGE_UNIT_1_PACK_2_NO, 0, '>'),
    "STORAGE_UNIT_1_BATTERY_PACK_3": EquipmentCheck(rn.STORAGE_UNIT_1_PACK_3_NO, 0, '>'),
    "STORAGE_UNIT_2": EquipmentCheck(rn.STORAGE_UNIT_2_NO, 0, '>'),
    "STORAGE_UNIT_2_BATTERY_PACK_1": EquipmentCheck(rn.STORAGE_UNIT_2_PACK_1_NO, 0, '>'),
    "STORAGE_UNIT_2_BATTERY_PACK_2": EquipmentCheck(rn.STORAGE_UNIT_2_PACK_2_NO, 0, '>'),
    "STORAGE_UNIT_2_BATTERY_PACK_3": EquipmentCheck(rn.STORAGE_UNIT_2_PACK_3_NO, 0, '>')
}


ITEM_CYCLE_DEFAULT = "default"
ITEM_CYCLE_STARTUP = "startup"
ITEM_SLAVE_DEFAULT = "default"

class ReadItem:
    def __init__(self, register, cycle=ITEM_CYCLE_DEFAULT, slave=ITEM_SLAVE_DEFAULT, equipment=None, initialized=False, skip=False):
        self.register = register
        self.cycle = cycle
        self.slave = slave
        self.equipment = equipment
        self.initialized = initialized
        self.skip = skip


class Huawei_Sun2000(SmartPlugin):
    PLUGIN_VERSION = '1.0.0'    # (must match the version specified in plugin.yaml), use '1.0.0' for your initial plugin Release

    def __init__(self, sh):
        # Call init code of parent class (SmartPlugin)
        super().__init__()

        # get parameters
        self._host = self.get_parameter_value('host')
        self._port = self.get_parameter_value('port')
        self._slave = self.get_parameter_value('slave')
        self._cycle = self.get_parameter_value('cycle')
        self._max_connection_retries = self.get_parameter_value('connection_retries')
        self._connection_retries = 0

        # global vars
        self._read_item_dictionary = {}
        self._write_buffer = []
        self._client = None
        self._connecting = False
        self._equipment_validated = False
        self._poll_item = None
        self._created = None

        # On initialization error use:
        #   self._init_complete = False
        #   return

        self.init_webinterface(WebInterface)
        # if plugin should not start without web interface
        # if not self.init_webinterface():
        #     self._init_complete = False
        return

    async def plugin_coro(self):
        """
        Coroutine for the plugin session (only needed, if using asyncio)

        This coroutine is run as the PluginTask and should
        only terminate, when the plugin is stopped
        """
        self.logger.notice("asyncio coroutine started")
        self.alive = True

        while self.alive:
            stop = await self.check_forstop()
            if stop:
                return

            if self._connection_retries >= self._max_connection_retries:
                self.logger.info("Max connection retries reached. Doing nothing....")
                self._connection_retries = -1
            if self._connection_retries == -1:
                await asyncio.sleep(1)
                continue

            self._client = await self.connect()

            if self._client:
                self.logger.debug("Client connected")
                self._connection_retries = 0
                await self.poll()
                self.logger.debug(f"Waiting {self._cycle} seconds based on plugin cycle parameter")

                total_sleep = 0
                while total_sleep < self._cycle and self.alive:
                    stop = await self.check_forstop()
                    if stop:
                        return
                    await asyncio.sleep(0.5)
                    total_sleep += 0.5
            else:
                self._connection_retries += 1
                self.logger.info(f"Connection retries: {self._connection_retries}/{self._max_connection_retries}")

        await self.wait_for_asyncio_termination()
        self.logger.notice("asyncio coroutine finished")
        return

    async def connect(self):
        """
        Baut die Verbindung auf. Ein Client ist nur 'ready', wenn Modbus antwortet.
        """
        self.logger.debug("Connecting..")
        try:
            if not self._client:
                client = await asyncio.wait_for(AsyncHuaweiSolar.create(self._host, self._port, self._slave), timeout=9)
                self._created = client
        except asyncio.TimeoutError:
            self.logger.error(f"Time out (9s) while creating client.")
            self._client = None
            return None
        except Exception as e:
            self.logger.error(f"Unexpected client creation error: {type(e).__name__}: {repr(e)}")
            self._client = None
            return None
        try:
            await asyncio.wait_for(client.get(rn.MODEL_NAME, self._slave), timeout=4)
        except asyncio.TimeoutError:
            self.logger.error(f"Connect timed out (4s) while trying to get model register")
            #return None
        self._client = client
        self.logger.debug(f"Connected to {self._host}:{self._port}, slave_id {self._slave}")
        return client

    async def disconnect(self):
        try:
            await self._created.stop()
        except Exception:
            pass
        self.logger.debug(f"Disconnected client {self._created}")
        self._client = None

    async def inverter_read(self, hold_connection=False):
        for item in self._read_item_dictionary:
            if not self.alive or not self._client:
                self.logger.error(f"inverter_read problem: alive: {self.alive}, client: {self._client}")
                break
            await self.write_buffer(True)
            cycle = self._read_item_dictionary[item].cycle
            equipment = self._read_item_dictionary[item].equipment
            initialized = self._read_item_dictionary[item].initialized
            skip = self._read_item_dictionary[item].skip
            if not skip:
                if not initialized or cycle == ITEM_CYCLE_DEFAULT or cycle != ITEM_CYCLE_STARTUP or cycle < item.property.last_update_age:
                    if equipment is None or equipment.status:
                        # get register and set item
                        try:
                            result = await asyncio.wait_for(self._client.get(getattr(rn, self._read_item_dictionary[item].register), self._read_item_dictionary[item].slave), timeout=4)
                            item(result.value, self.get_shortname())
                            self._read_item_dictionary[item].initialized = True
                        except asyncio.TimeoutError:
                            self.logger.warning(f"Time out (4s) while reading register '{self._read_item_dictionary[item].register}' from {self._host}:{self._port}, slave_id {self._read_item_dictionary[item].slave}. Stop reading registers.")
                            break
                        except Exception as e:
                            self.logger.error(f"inverter_read: Error reading register '{self._read_item_dictionary[item].register}' from {self._host}:{self._port}, slave_id {self._read_item_dictionary[item].slave}: {repr(e)}")
                            # if 'IllegalAddress' occurs the register will be dropped out
                            ex = str(e)
                            if len(ex) == 101 and ex[86:-1] == 'IllegalAddress':
                            #if ex[86:-1] == 'IllegalAddress':
                                self.logger.debug(f"inverter_read: register '{self._read_item_dictionary[item].register}' will not be checked anymore")
                                self._read_item_dictionary[item].skip = True
                    else:
                        self.logger.debug(f"Equipment check skipped item '{item.property.path}'")
            else:
                self.logger.debug(f"Illegal address! Item '{item.property.path}' skipped")
        if not hold_connection:
            await self.disconnect()

    async def poll(self):
        if not self.alive:
            return
        if not self._client:
            self.logger.debug("Poll skipped: no connection")
            return
        if not self._equipment_validated:
            try:
                ok = await self.validate_equipment()
                self._equipment_validated = ok
            except Exception as e:
                self.logger.error(f"Equipment validation failed during poll: {e}")
                return
        self.logger.debug("Polling")
        try:
            await self.inverter_read()
        except Exception as e:
            self.logger.error(f"Poll failed: {e}")

    async def inverter_write(self, register, value, slave, hold_connection=False):
        if self._client is None:
            self.logger.error("inverter_write: Client not connected")
            return
        try:
            await asyncio.wait_for(self._client.set(getattr(rn, register), value, slave), timeout=4)
            self.logger.info(f"inverter_write: Register '{register}' to {self._host}:{self._port}, slave_id {slave} with value '{value}' written")
        except asyncio.TimeoutError:
            self.logger.error(f"Time out (4s) while writing register '{register}' to {self._host}:{self._port}, slave_id {slave}.")
        except Exception as e:
            self.logger.error(f"inverter_write: Error writing register '{register}' to {self._host}:{self._port}, slave_id {slave}: {e}")
        finally:
            if not hold_connection:
                await self.disconnect()

    async def write_buffer(self, hold_connection=False):
        while len(self._write_buffer) > 0:
            first = self._write_buffer[0]
            register = first[0]
            value = first[1]
            slave = first[2]
            self._write_buffer.pop(0)
            self.inverter_write(register, value, slave, hold_connection)

    async def validate_equipment(self):
        if not self._client:
            self.logger.error("validate_equipment: no connection")
            return False

        for item, read_item in self._read_item_dictionary.items():
            eq = read_item.equipment
            if not eq:
                continue

            try:
                result = await asyncio.wait_for(self._client.get(eq.register, read_item.slave), timeout=4)
                match eq.true_comparator:
                    case ">":
                        eq.status = result.value > eq.true_value
                    case "<":
                        eq.status = result.value < eq.true_value
                    case _:
                        eq.status = result.value == eq.true_value

                self.logger.debug(
                    f"Equipment {eq.register}: status={eq.status}"
                )
            except asyncio.TimeoutError:
                self.logger.error(f"Time out (4s) while checking equipment.")
                eq.status = False
            except Exception as e:
                self.logger.error(f"Equipment check failed: {e}")
                eq.status = False
        return True

    async def check_forstop(self):
        if not self._run_queue.empty():
            queue_command = await self.get_command_from_run_queue()
            if queue_command == 'STOP':
                self.logger.info("Plugin stop detected.")
                self.alive = False
                await self.disconnect()
                return True

    def string_to_seconds_special(self, input_str):
        input_str = input_str.lower()
        if input_str == ITEM_CYCLE_STARTUP:
            return ITEM_CYCLE_STARTUP
        if input_str == ITEM_CYCLE_DEFAULT:
            return ITEM_CYCLE_DEFAULT
        if input_str.isnumeric():
            time_value = float(input_str)
            if time_value > 0:
                return time_value
            else:
                return ITEM_CYCLE_DEFAULT
        time_len = len(input_str)
        if time_len > 1:
            time_format = input_str[-1:]
            time_value = float(input_str[:-1])
            match time_format:
                case "m":
                    time_value *= 60
                case "h":
                    time_value *= 60*60
                case "d":
                    time_value *= 60*60*24
                case "w":
                    time_value *= 60*60*24*7
            if time_value > 0:
                return time_value
            else:
                return ITEM_CYCLE_DEFAULT
        else:
            return ITEM_CYCLE_DEFAULT

    def string_to_int_special(self, input_str, default_str, default_value):
        if input_str.lower() == default_str.lower():
            return default_value
        if input_str.isnumeric():
            return int(input_str)
        return default_value

    def run(self):
        self.logger.debug("Run method called")
        self.start_asyncio(self.plugin_coro())
        return

    def stop(self):
        self.logger.debug("Stop method called")
        self.scheduler_remove_all()

        self.stop_asyncio()

    def parse_item(self, item):
        # check for attribute 'sun2000_read'
        if self.has_iattr(item.conf, 'sun2000_read'):
            self.logger.debug(f"Parse sun2000_read item: {item}")
            register = self.get_iattr_value(item.conf, 'sun2000_read')
            if hasattr(rn, register):
                # check for slave id
                slave = self._slave
                if self.has_iattr(item.conf, 'sun2000_slave'):
                    slave = self.string_to_int_special(self.get_iattr_value(item.conf, 'sun2000_slave'), ITEM_SLAVE_DEFAULT, self._slave)
                    self.logger.debug(f"Item {item.property.path}, slave {slave}")
                # check for sun2000_cycle
                cycle = self._cycle
                if self.has_iattr(item.conf, 'sun2000_cycle'):
                    cycle = self.string_to_seconds_special(self.get_iattr_value(item.conf, 'sun2000_cycle'))
                    self.logger.debug(f"Item {item.property.path}, cycle {cycle}")
                # check equipment
                equipment = None
                if self.has_iattr(item.conf, 'sun2000_equipment'):
                    equipment_key = self.get_iattr_value(item.conf, 'sun2000_equipment')
                    if equipment_key in EquipmentDictionary:
                        equipment = EquipmentDictionary[equipment_key]
                        self.logger.debug(f"Item {item.property.path}, equipment {equipment_key}")
                    else:
                        self.logger.warning(f"Invalid key for sun2000_equipment '{equipment_key}' configured")
                self._read_item_dictionary.update({item: ReadItem(register, cycle, slave, equipment)})
            else:
                self.logger.warning(f"Invalid key for 'sun2000_read' '{register}' configured")
        # check for attribute 'sun2000_write'
        if self.has_iattr(item.conf, 'sun2000_write'):
            self.logger.debug(f"Parse sun2000_write item: {item}")
            register = self.get_iattr_value(item.conf, 'sun2000_write')
            if hasattr(rn, register):
                return self.update_item
            else:
                self.logger.warning(f"Invalid key for 'sun2000_write' '{register}' configured")
        # check for attribute 'sun2000_poll'
        if self.has_iattr(item.conf, 'sun2000_runpoll'):
            self.logger.debug(f"Parse sun2000_runpoll item: {item}")
            self._poll_item = item
            return self.update_item

    def parse_logic(self, logic):
        pass

    def update_item(self, item, caller=None, source=None, dest=None):
        if self.alive and caller != self.get_shortname():
            if item == self._poll_item:
                if item() is True and not self.asyncio_state() == "stopped":
                    self._connection_retries = 0
                    self.logger.debug(f"Item {item.property.path} initiated poll restart.")
                return
            # get attribute for 'sun2000_write'
            register = self.get_iattr_value(item.conf, 'sun2000_write')
            value = item()
            # check for slave id
            if self.has_iattr(item.conf, 'sun2000_slave'):
                slave = self.string_to_int_special(self.get_iattr_value(item.conf, 'sun2000_slave'), ITEM_SLAVE_DEFAULT, self._slave)
                self.logger.debug(f"Item {item.property.path}, slave {slave}")
            else:
                slave = self._slave
            self.logger.debug(f"Update_item was called with item {item.property.path} from caller {caller}, source {source} and dest {dest}")
            self._write_buffer.append((register, value, slave))
            self.logger.debug(f"Buffered write {register}={value}")
