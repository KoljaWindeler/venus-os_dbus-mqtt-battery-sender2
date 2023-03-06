#!/usr/bin/env python

from gi.repository import GLib
import platform
import logging
import sys
import os
import time
import json
import paho.mqtt.client as mqtt
import configparser # for config/ini file
import _thread

# import Victron Energy packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
from vedbus import VeDbusService


# get values from config.ini file
try:
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    if (config['MQTT']['broker_address'] == "IP_ADDR_OR_FQDN"):
        print("ERROR:config.ini file is using invalid default values like IP_ADDR_OR_FQDN. The driver restarts in 60 seconds.")
        time.sleep(60)
        sys.exit()
except:
    print("ERROR:config.ini file not found. Copy or rename the config.sample.ini to config.ini. The driver restarts in 60 seconds.")
    time.sleep(60)
    sys.exit()


# Get logging level from config.ini
# ERROR = shows errors only
# WARNING = shows ERROR and warnings
# INFO = shows WARNING and running functions
# DEBUG = shows INFO and data/values
if 'DEFAULT' in config and 'logging' in config['DEFAULT']:
    if config['DEFAULT']['logging'] == 'DEBUG':
        logging.basicConfig(level=logging.DEBUG)
    elif config['DEFAULT']['logging'] == 'INFO':
        logging.basicConfig(level=logging.INFO)
    elif config['DEFAULT']['logging'] == 'ERROR':
        logging.basicConfig(level=logging.ERROR)
    else:
        logging.basicConfig(level=logging.WARNING)
else:
    logging.basicConfig(level=logging.WARNING)


# set variables
connected = 0

battery_power = -1
battery_voltage = 0
battery_current = 0
battery_temperature = 0

battery_consumed_amphours = 0
battery_soc = 0

battery_voltageMin = 0
battery_voltageMax = 0


# MQTT requests
def on_disconnect(client, userdata, rc):
    global connected
    logging.warning("MQTT client: Got disconnected")
    if rc != 0:
        logging.warning('MQTT client: Unexpected MQTT disconnection. Will auto-reconnect')
    else:
        logging.warning('MQTT client: rc value:' + str(rc))

    try:
        logging.warning("MQTT client: Trying to reconnect")
        client.connect(config['MQTT']['broker_address'])
        connected = 1
    except Exception as e:
        logging.error("MQTT client: Error in retrying to connect with broker: %s" % e)
        connected = 0

def on_connect(client, userdata, flags, rc):
    global connected
    if rc == 0:
        logging.info("MQTT client: Connected to MQTT broker!")
        connected = 1
        client.subscribe(config['MQTT']['topic_battery'])
    else:
        logging.error("MQTT client: Failed to connect, return code %d\n", rc)

def on_message(client, userdata, msg):
    try:

        global \
            battery_power, battery_voltage, battery_current, battery_temperature, \
            battery_consumed_amphours, battery_soc, \
            battery_voltageMin, battery_voltageMax

        # get JSON from topic
        if msg.topic == config['MQTT']['topic_battery']:
            if msg.payload != '' and msg.payload != b'':
                jsonpayload = json.loads(msg.payload)

                battery_power   = float(jsonpayload["dc"]["power"])
                battery_voltage = float(jsonpayload["dc"]["voltage"])
                battery_current = float(jsonpayload["dc"]["current"])
                battery_temperature = float(jsonpayload["dc"]["temperature"])

                battery_consumed_amphours = float(jsonpayload["consumed_amphours"])
                battery_soc = float(jsonpayload["soc"])

                battery_voltageMin = float(jsonpayload["history"]["voltageMin"])
                battery_voltageMax = float(jsonpayload["history"]["voltageMax"])
            else:
                logging.warning("Received JSON MQTT message was empty and therefore it was ignored")
                logging.debug("MQTT payload: " + str(msg.payload)[1:])

    except ValueError as e:
        logging.error("Received message is not a valid JSON. %s" % e)
        logging.debug("MQTT payload: " + str(msg.payload)[1:])

    except Exception as e:
        logging.error("Exception occurred: %s" % e)
        logging.debug("MQTT payload: " + str(msg.payload)[1:])



class DbusMqttBatteryService:
    def __init__(
        self,
        servicename,
        deviceinstance,
        paths,
        productname='MQTT Battery',
        connection='MQTT Battery service'
    ):

        self._dbusservice = VeDbusService(servicename)
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/CustomName', productname)
        self._dbusservice.add_path('/FirmwareVersion', '0.0.2')
        #self._dbusservice.add_path('/HardwareVersion', '')
        self._dbusservice.add_path('/Connected', 1)

        self._dbusservice.add_path('/Latency', None)

        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue
                )

        GLib.timeout_add(1000, self._update) # pause 1000ms before the next request


    def _update(self):
        self._dbusservice['/Dc/0/Power'] =  round(battery_power, 2) # positive: charging, negative: discharging
        self._dbusservice['/Dc/0/Voltage'] = round(battery_voltage, 2)
        self._dbusservice['/Dc/0/Current'] = round(battery_current, 2)
        self._dbusservice['/Dc/0/Temperature'] = round(battery_temperature, 2)

        self._dbusservice['/ConsumedAmphours'] = round(battery_consumed_amphours, 2)
        self._dbusservice['/Soc'] = round(battery_soc, 2)

        # For all alarms: 0=OK; 1=Warning; 2=Alarm
        if battery_voltage == 0:
            alarm_lowvoltage = 0
        elif battery_voltage < float(config['BATTERY']['VoltageLowCritical']):
            alarm_lowvoltage = 2
        elif battery_voltage < float(config['BATTERY']['VoltageLowWarning']):
            alarm_lowvoltage = 1
        else:
            alarm_lowvoltage = 0
        self._dbusservice['/Alarms/LowVoltage'] = alarm_lowvoltage

        if battery_voltage == 0:
            alarm_highvoltage = 0
        elif battery_voltage > float(config['BATTERY']['VoltageHighCritical']):
            alarm_highvoltage = 2
        elif battery_voltage > float(config['BATTERY']['VoltageHighWarning']):
            alarm_highvoltage = 1
        else:
            alarm_highvoltage = 0
        self._dbusservice['/Alarms/HighVoltage'] = alarm_highvoltage

        if battery_soc == 0:
            alarm_lowsoc = 0
        elif battery_soc < float(config['BATTERY']['LowSocCritical']):
            alarm_lowsoc = 2
        elif battery_soc < float(config['BATTERY']['LowSocWarning']):
            alarm_lowsoc = 1
        else:
            alarm_lowsoc = 0
        self._dbusservice['/Alarms/LowSoc'] = alarm_lowsoc

        self._dbusservice['/History/MinimumVoltage'] = round(battery_voltageMin, 2)
        self._dbusservice['/History/MaximumVoltage'] = round(battery_voltageMax, 2)

        logging.debug("Battery SoC: {:.2f} V - {:.2f} %".format(battery_voltage, battery_soc))


        # increment UpdateIndex - to show that new data is available
        index = self._dbusservice['/UpdateIndex'] + 1  # increment index
        if index > 255:   # maximum value of the index
            index = 0       # overflow from 255 to 0
        self._dbusservice['/UpdateIndex'] = index
        return True

    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True # accept the change



def main():
    _thread.daemon = True # allow the program to quit

    from dbus.mainloop.glib import DBusGMainLoop
    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)


    # MQTT setup
    client = mqtt.Client("MqttBattery")
    client.on_disconnect = on_disconnect
    client.on_connect = on_connect
    client.on_message = on_message

    # check tls and use settings, if provided
    if 'tls_enabled' in config['MQTT'] and config['MQTT']['tls_enabled'] == '1':
        logging.info("MQTT client: TLS is enabled")

        if 'tls_path_to_ca' in config['MQTT'] and config['MQTT']['tls_path_to_ca'] != '':
            logging.info("MQTT client: TLS: custom ca \"%s\" used" % config['MQTT']['tls_path_to_ca'])
            client.tls_set(config['MQTT']['tls_path_to_ca'], tls_version=2)
        else:
            client.tls_set(tls_version=2)

        if 'tls_insecure' in config['MQTT'] and config['MQTT']['tls_insecure'] != '':
            logging.info("MQTT client: TLS certificate server hostname verification disabled")
            client.tls_insecure_set(True)

    # check if username and password are set
    if 'username' in config['MQTT'] and 'password' in config['MQTT'] and config['MQTT']['username'] != '' and config['MQTT']['password'] != '':
        logging.info("MQTT client: Using username \"%s\" and password to connect" % config['MQTT']['username'])
        client.username_pw_set(username=config['MQTT']['username'], password=config['MQTT']['password'])

     # connect to broker
    client.connect(
        host=config['MQTT']['broker_address'],
        port=int(config['MQTT']['broker_port'])
    )
    client.loop_start()

    # wait to receive first data, else the JSON is empty and phase setup won't work
    i = 0
    while battery_power == -1:
        if i % 12 != 0 or i == 0:
            logging.info("Waiting 5 seconds for receiving first data...")
        else:
            logging.warning("Waiting since %s seconds for receiving first data..." % str(i * 5))
        time.sleep(5)
        i += 1


    #formatting
    _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
    _a = lambda p, v: (str(round(v, 2)) + 'A')
    _ah = lambda p, v: (str(round(v, 2)) + 'Ah')
    _w = lambda p, v: (str(round(v, 2)) + 'W')
    _v = lambda p, v: (str(round(v, 2)) + 'V')
    _p = lambda p, v: (str(round(v, 2)) + '%')
    _t = lambda p, v: (str(round(v, 2)) + '°C')
    _n = lambda p, v: (str(round(v, 0)))

    paths_dbus = {
        '/Dc/0/Power': {'initial': 0, 'textformat': _w},
        '/Dc/0/Voltage': {'initial': 0, 'textformat': _v},
        '/Dc/0/Current': {'initial': 0, 'textformat': _a},
        '/Dc/0/Temperature': {'initial': None, 'textformat': _t},

        '/ConsumedAmphours': {'initial': 0, 'textformat': _ah},
        '/Soc': {'initial': 0, 'textformat': _p},

        '/Info/MaxChargeCurrent': {'initial': 50, 'textformat': _a},
        '/Info/MaxDischargeCurrent': {'initial': 50, 'textformat': _a},
        '/Info/MaxChargeVoltage': {'initial': 15.0, 'textformat': _v},
        '/Info/BatteryLowVoltage': {'initial': 11.5, 'textformat': _v},
        '/Info/ChargeRequest': {'initial': 0, 'textformat': _n},

        '/Alarms/LowVoltage': {'initial': 0, 'textformat': _n},
        '/Alarms/HighVoltage': {'initial': 0, 'textformat': _n},
        '/Alarms/LowSoc': {'initial': 0, 'textformat': _n},
        '/Alarms/HighChargeCurrent': {'initial': 0, 'textformat': _n},
        '/Alarms/HighDischargeCurrent': {'initial': 0, 'textformat': _n},
        '/Alarms/HighCurrent': {'initial': 0, 'textformat': _n},
        '/Alarms/HighChargeTemperature': {'initial': 0, 'textformat': _n},
        '/Alarms/LowChargeTemperature': {'initial': 0, 'textformat': _n},
        '/Alarms/LowCellVoltage': {'initial': 0, 'textformat': _n},
        '/Alarms/LowTemperature': {'initial': 0, 'textformat': _n},
        '/Alarms/HighTemperature': {'initial': 0, 'textformat': _n},

        '/History/MinimumVoltage': {'initial': None, 'textformat': _v},
        '/History/MaximumVoltage': {'initial': None, 'textformat': _v},

        '/UpdateIndex': {'initial': 0, 'textformat': _n},
    }


    pvac_output = DbusMqttBatteryService(
        servicename='com.victronenergy.battery.mqtt_battery',
        deviceinstance=41,
        paths=paths_dbus
        )

    logging.info('Connected to dbus and switching over to GLib.MainLoop() (= event based)')
    mainloop = GLib.MainLoop()
    mainloop.run()



if __name__ == "__main__":
  main()
