# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:

# Copyright (c) 2017, SLAC National Laboratory / Kisensum Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in
#    the documentation and/or other materials provided with the
#    distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation
# are those of the authors and should not be interpreted as representing
# official policies, either expressed or implied, of the FreeBSD
# Project.
#
# This material was prepared as an account of work sponsored by an
# agency of the United States Government.  Neither the United States
# Government nor the United States Department of Energy, nor SLAC / Kisensum,
# nor any of their employees, nor any jurisdiction or organization that
# has cooperated in the development of these materials, makes any
# warranty, express or implied, or assumes any legal liability or
# responsibility for the accuracy, completeness, or usefulness or any
# information, apparatus, product, software, or process disclosed, or
# represents that its use would not infringe privately owned rights.
#
# Reference herein to any specific commercial product, process, or
# service by trade name, trademark, manufacturer, or otherwise does not
# necessarily constitute or imply its endorsement, recommendation, or
# favoring by the United States Government or any agency thereof, or
# SLAC / Kisensum. The views and opinions of authors
# expressed herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# }}}
import datetime
import gevent
import logging
import random

from volttron.platform.agent import utils
from volttron.platform.messaging import headers as headers_mod
from volttron.platform.messaging.topics import (DRIVER_TOPIC_BASE,
                                                DRIVER_TOPIC_ALL,
                                                DEVICES_VALUE,
                                                DEVICES_PATH)
from volttron.platform.vip.agent import BasicAgent, Core
from volttron.platform.vip.agent.errors import VIPError, Again

from driver_locks import publish_lock

utils.setup_logging()
_log = logging.getLogger(__name__)


class DriverAgent(BasicAgent):
    """
        DriverAgent for simulation interfaces.

        DriverAgent is a simplified copy of the master driver's DriverAgent.
        Its strategy for scheduling device-driver scrapes attempts to match that of the Master Driver.
        Please see services.core.MasterDriverAgent.master_driver.driver.py for additional commentary
        about this agent's implementation.
    """

    def __init__(self, parent, config, time_slot, driver_scrape_interval, device_path, **kwargs):
        super(DriverAgent, self).__init__(**kwargs)
        self.parent = parent
        self.config = config
        self.time_slot = time_slot
        self.device_path = device_path

        self.all_path_breadth = None
        self.all_path_depth = None
        self.base_topic = None
        self.device_name = ''
        self.heart_beat_point = None
        self.heart_beat_value = 0
        self.interface = None
        try:
            interval = int(config.get("interval", 60))
            if interval < 1:
                raise ValueError
        except ValueError:
            _log.warning("Invalid device scrape interval {}. Defaulting to 60 seconds.".format(config.get("interval")))
            interval = 60
        self.interval = interval
        self.meta_data = None
        self.periodic_read_event = None
        self.time_slot_offset = None
        self.vip = parent.vip           # Use the parent's vip connection
        self.update_scrape_schedule(self.time_slot, driver_scrape_interval)

    def update_scrape_schedule(self, time_slot, driver_scrape_interval):
        self.time_slot = time_slot
        self.time_slot_offset = time_slot * driver_scrape_interval
        _log.debug("{} time_slot: {}, offset: {}".format(self.device_path, time_slot, self.time_slot_offset))
        self.time_slot_offset = time_slot * driver_scrape_interval
        if self.time_slot_offset >= self.interval:
            _log.warning("Scrape offset exceeds interval. Required adjustment will cause scrapes to double up with other devices.")
            while self.time_slot_offset >= self.interval:
                self.time_slot_offset -= self.interval
        # Check whether we have run our starting method
        if self.periodic_read_event:
            self.periodic_read_event.cancel()
            next_periodic_read = self.find_starting_datetime(utils.get_aware_utc_now())
            self.periodic_read_event = self.core.schedule(next_periodic_read, self.periodic_read, next_periodic_read)

    def find_starting_datetime(self, now):
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_from_midnight = (now - midnight).total_seconds()
        offset = seconds_from_midnight % self.interval
        if offset:
            previous_in_seconds = seconds_from_midnight - offset
            next_in_seconds = previous_in_seconds + self.interval
            from_midnight = datetime.timedelta(seconds=next_in_seconds)
            return midnight + from_midnight + datetime.timedelta(seconds=self.time_slot_offset)
        else:
            return now

    def get_interface(self, driver_type, config_dict, config_string):
        module_name = "interfaces." + driver_type
        module = __import__(module_name, globals(), locals(), [], -1)
        sub_module = getattr(module, driver_type)
        klass = getattr(sub_module, "Interface")
        interface = klass(vip=self.vip, core=self.core)
        interface.configure(config_dict, config_string)
        return interface

    @Core.receiver('onstart')
    def starting(self, sender, **kwargs):
        self.setup_device()
        next_periodic_read = self.find_starting_datetime(utils.get_aware_utc_now())
        self.periodic_read_event = self.core.schedule(next_periodic_read, self.periodic_read, next_periodic_read)
        self.all_path_depth, self.all_path_breadth = self.get_paths_for_point(DRIVER_TOPIC_ALL)

    def setup_device(self):
        config = self.config
        driver_config = config["driver_config"]
        driver_type = config["driver_type"]
        registry_config = config.get("registry_config")
        self.heart_beat_point = config.get("heart_beat_point")
        self.interface = self.get_interface(driver_type, driver_config, registry_config)
        self.meta_data = {}
        for point in self.interface.get_register_names():
            register = self.interface.get_register_by_name(point)
            self.meta_data[point] = {'units': register.get_units(),
                                     'type': self.register_data_type(register),
                                     'tz': config.get('timezone', '')}
        self.base_topic = DEVICES_VALUE(campus='',
                                        building='',
                                        unit='',
                                        path=self.device_path,
                                        point=None)
        self.device_name = DEVICES_PATH(base='',
                                        node='',
                                        campus='',
                                        building='',
                                        unit='',
                                        path=self.device_path,
                                        point='')

    @staticmethod
    def register_data_type(register):
        if register.register_type == 'bit':
            return 'boolean'
        else:
            if register.python_type is int:
                return 'integer'
            elif register.python_type is float:
                return 'float'
            elif register.python_type is str:
                return 'string'
        return None

    def periodic_read(self, now):
        # we not use self.core.schedule to prevent drift.
        next_scrape_time = now + datetime.timedelta(seconds=self.interval)
        # Sanity check now.
        # This is specifically for when this is running in a VM that gets
        # suspended and then resumed.
        # If we don't make this check a resumed VM will publish one event
        # per minute of
        # time the VM was suspended for.
        test_now = utils.get_aware_utc_now()
        if test_now - next_scrape_time > datetime.timedelta(seconds=self.interval):
            next_scrape_time = self.find_starting_datetime(test_now)
        self.periodic_read_event = self.core.schedule(next_scrape_time, self.periodic_read, next_scrape_time)
        _log.debug("scraping device: " + self.device_name)
        try:
            results = self.interface.scrape_all()
        except Exception as ex:
            _log.error('Failed to scrape ' + self.device_name + ': ' + str(ex))
            return
        if results:
            utcnow_string = utils.format_timestamp(utils.get_aware_utc_now())
            headers = {headers_mod.DATE: utcnow_string,
                       headers_mod.TIMESTAMP: utcnow_string, }
            for point, value in results.iteritems():
                depth_first_topic, breadth_first_topic = self.get_paths_for_point(point)
                message = [value, self.meta_data[point]]
                self._publish_wrapper(depth_first_topic, headers=headers, message=message)
                self._publish_wrapper(breadth_first_topic, headers=headers, message=message)
            message = [results, self.meta_data]
            self._publish_wrapper(self.all_path_depth, headers=headers, message=message)
            self._publish_wrapper(self.all_path_breadth, headers=headers, message=message)

    def _publish_wrapper(self, topic, headers, message):
        while True:
            try:
                with publish_lock():
                    self.vip.pubsub.publish('pubsub', topic, headers=headers, message=message).get(timeout=10.0)
            except gevent.Timeout:
                _log.warn("Did not receive confirmation of publish to "+topic)
                break
            except Again:
                _log.warn("publish delayed: " + topic + " pubsub is busy")
                gevent.sleep(random.random())
            except VIPError as ex:
                _log.warn("driver failed to publish " + topic + ": " + str(ex))
                break
            else:
                break

    def heart_beat(self):
        if self.heart_beat_point:
            self.heart_beat_value = int(not bool(self.heart_beat_value))
            _log.debug("sending heartbeat: " + self.device_name + ' ' + str(self.heart_beat_value))
            self.set_point(self.heart_beat_point, self.heart_beat_value)

    def get_paths_for_point(self, point):
        depth_first = self.base_topic(point=point)
        parts = depth_first.split('/')
        breadth_first_parts = parts[1:]
        breadth_first_parts.reverse()
        breadth_first_parts = [DRIVER_TOPIC_BASE] + breadth_first_parts
        breadth_first = '/'.join(breadth_first_parts)
        return depth_first, breadth_first

    def get_point(self, point_name, **kwargs):
        return self.interface.get_point(point_name, **kwargs)

    def set_point(self, point_name, value, **kwargs):
        return self.interface.set_point(point_name, value, **kwargs)

    def scrape_all(self):
        return self.interface.scrape_all()

    def get_multiple_points(self, point_names, **kwargs):
        return self.interface.get_multiple_points(self.device_name, point_names, **kwargs)

    def set_multiple_points(self, point_names_values, **kwargs):
        return self.interface.set_multiple_points(self.device_name, point_names_values, **kwargs)

    def revert_point(self, point_name, **kwargs):
        self.interface.revert_point(point_name, **kwargs)

    def revert_all(self, **kwargs):
        self.interface.revert_all(**kwargs)
