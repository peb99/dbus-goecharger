#!/usr/bin/env python

# import normal packages
import platform 
import logging
import logging.handlers
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests # for http GET
import configparser # for config/ini file

# for AutomaticMode
import dbus

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusGoeChargerService:
  def __init__(self, config, host_section):
    # common config values
    self._sign_of_life_log = int(config["DEFAULT"]["SignOfLifeLog"])
    self._logging_level = config["DEFAULT"]["Logging"].upper()

    # host-specific config values
    self._access_type = config[host_section]["AccessType"]
    self._device_instance = int(config[host_section]["Deviceinstance"])
    self._hardware_version = int(config[host_section]["HardwareVersion"])
    self._position = int(config[host_section]["Position"])
    self._host = config[host_section]["Host"]
    self._pause_between_requests = int(config[host_section]["PauseBetweenRequests"])

    if self._pause_between_requests <= 20:
      raise ValueError("Pause between requests must be greater than 20")

    if self._hardware_version < 3:
      raise ValueError("Minimum hardware version required is 3.")

    self._dbusservice = VeDbusService("{}.http_{:02d}".format("com.victronenergy.evcharger", self._device_instance))
    self._paths = {
      '/Ac/Power': {'initial': 0, 'textformat': lambda p, v: (str(round(v, 1)) + 'W')},
      '/Ac/L1/Power': {'initial': 0, 'textformat': lambda p, v: (str(round(v, 1)) + 'W')},
      '/Ac/L2/Power': {'initial': 0, 'textformat': lambda p, v: (str(round(v, 1)) + 'W')},
      '/Ac/L3/Power': {'initial': 0, 'textformat': lambda p, v: (str(round(v, 1)) + 'W')},
      '/Ac/Energy/Forward': {'initial': 0, 'textformat': lambda p, v: (str(round(v, 2)) + 'kWh')},
      '/ChargingTime': {'initial': 0, 'textformat': lambda p, v: (str(v) +'s')},
      '/Ac/Voltage': {'initial': 0, 'textformat': lambda p, v: (str(round(v, 1)) + 'V')},
      '/Current': {'initial': 0, 'textformat': lambda p, v: (str(round(v, 1)) + 'A')},
      '/SetCurrent': {'initial': 0, 'textformat': lambda p, v: (str(round(v, 1)) + 'A')},
      '/MaxCurrent': {'initial': 0, 'textformat': lambda p, v: (str(round(v, 1)) + 'A')},
      '/MCU/Temperature': {'initial': 0, 'textformat': lambda p, v: (str(v) + '°C')},
      '/StartStop': {'initial': 0, 'textformat': lambda p, v: (str(v))},
      '/Mode': {'initial': 0, 'textformat': lambda p, v: (str(v))},
      '/Status': {'initial': 0, 'textformat': lambda p, v: (str(v))},
    }

    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python'+ platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', f"{self._access_type} {self._host}")

    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', self._device_instance)
    self._dbusservice.add_path('/ProductId', 0xFFFF) # 
    self._dbusservice.add_path('/ProductName', 'go-eCharger')
    self._dbusservice.add_path('/CustomName', 'go-eCharger')    
    self._dbusservice.add_path('/HardwareVersion', self._hardware_version)
    self._dbusservice.add_path('/Connected', 1)
    self._dbusservice.add_path('/UpdateIndex', 0)
    self._dbusservice.add_path("/Position", self._position)

    # add paths to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # last update
    self._last_update = 0
    
    # charging time in float
    self._charging_time = 0.0

    # add _update function 'timer'
    gobject.timeout_add(self._pause_between_requests, self._update)
    
    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._get_sign_of_life_interval()*60*1000, self._sign_of_life)

  def _get_sign_of_life_interval(self):
    return self._sign_of_life_log

  def _get_goe_charger_status_url(self):
    return f"http://{self._host}/api/status?filter=fwv,sse,nrg,wh,alw,amp,ama,car"

  def _get_goe_charger_data(self):
    url = self._get_goe_charger_status_url()
    try:
       request_data = requests.get(url = url, timeout=5)
    except Exception:
       return None
    
    # check for response
    if not request_data:
        raise ConnectionError("No response from go-eCharger - %s" % (url))
    
    json_data = request_data.json()     
    
    # check for Json
    if not json_data:
        raise ValueError("Converting response to JSON failed")
    
    return json_data

  def _set_goe_charger_value(self, parameter, value):
    logging.info("Parameter hat folgenden Wert: %s" % (parameter))
    logging.info("value hat folgenden Wert: %s" % (value))
    url = f"http://{self._host}/api/set?{parameter}={value}"
    logging.info("URL auf %s gesetzt" % (url))
    request_data = requests.get(url = url)
    logging.info("Request_data hat Inhalt: %s" % (request_data))

    # check for response
    if not request_data:
      logging.info("No response from go-eCharger - %s" % (url))
      raise ConnectionError("No response from go-eCharger - %s" % (url))

    
    json_data = request_data.json()

    logging.info("json_data[parameter] hat folgenden Wert: %s" % json_data[parameter])

    # check for Json
    if not json_data:
        logging.error("Converting response to JSON failed")
        raise ValueError("Converting response to JSON failed")
    
    if str(json_data[parameter]) == "true" or str(json_data[parameter]) == "True" or json_data[parameter] == str(value):
      return True
    else:
      logging.error("go-eCharger parameter %s not set to %s" % (parameter, str(value)))
      return False

  def _set_goe_charger_automatic_mode_values(self):
    logging.warning("AutomaticMode - Werte setzen:")
    
    bus = dbus.SystemBus()
    # pGrid ermitteln. Kumulierte Leistung des Grid auf allen Phasen
    L1gPower = float((bus.get_object('com.victronenergy.system', '/Ac/Grid/L1/Power')).GetValue())
    L2gPower = float((bus.get_object('com.victronenergy.system', '/Ac/Grid/L2/Power')).GetValue())
    L3gPower = float((bus.get_object('com.victronenergy.system', '/Ac/Grid/L3/Power')).GetValue())
    pGrid = L1gPower + L2gPower + L3gPower
    logging.warning("pGrid wurde ermittelt: %s" % (pGrid))

    # pPv ermitteln. Kumulierte Leistung aller PV Anlagen auf allen Phasen
    L1pPower = float((bus.get_object('com.victronenergy.system', '/Ac/PvOnGrid/L1/Power')).GetValue())
    L2pPower = float((bus.get_object('com.victronenergy.system', '/Ac/PvOnGrid/L2/Power')).GetValue())
    L3pPower = float((bus.get_object('com.victronenergy.system', '/Ac/PvOnGrid/L3/Power')).GetValue())
    #DcPvPower = float((bus.get_object('com.victronenergy.system', '/Dc/Pv/Power')).GetValue())
    pPv = L1pPower + L2pPower + L3pPower #+ DcPvPower
    logging.warning("pPv wurde ermittelt: %s" % (pPv))

    # pAkku ermittelnt
    pAkku = float((bus.get_object('com.victronenergy.system', '/Dc/Battery/Power')).GetValue()) * -1
    logging.warning("pAkku wurde ermittelt: %s" % (pAkku))

    url = f'http://{self._host}/api/set?ids={{"pGrid":{pGrid},"pAkku":{pAkku},"pPv":{pPv}}}'
    logging.warning("Folgende URL wird getriggert: %s" % (url))

    requests.get(url = url, timeout=15)

    # http://192.168.100.4/api/set?ids={%22pGrid%22:-6027,%22pAkku%22:-186,%22pPv%22:7269}
    # Werte checken:
    # http://192.168.100.4/api/status?filter=ppv
    # http://192.168.100.4/api/status?filter=pAkku
    # http://192.168.100.4/api/status?filter=pGrid
    return True

  def _sign_of_life(self):
    logging.info("--- Start: sign of life ---")
    logging.info("Last _update() call: %s" % (self._last_update))
    logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
    logging.info("--- End: sign of life ---")
    return True

  def _update(self):   
    try:
       #get data from go-eCharger
       data = self._get_goe_charger_data()
       logging.debug("Update Schleife.")
       modestatus = self._dbusservice['/Mode']

       logging.warning("Mode ist: %s" % (modestatus))
       if modestatus == 1:
         logging.warning("Mode-Schleife hat zugeschlagen.")
         self._set_goe_charger_automatic_mode_values()
         MaxCurrent = self._dbusservice['/MaxCurrent']
         SetCurrent = self._dbusservice['/SetCurrent']
         
         if SetCurrent < MaxCurrent:
           self._set_goe_charger_value('amp', MaxCurrent)



       
       if data is not None:

          '''
          data['nrg']
          0 = U L1
          1 = U L2
          2 = U L3
          3 = U N
          4 = I L1
          5 = I L2
          6 = I L3
          7 = P L1
          8 = P L2
          9 = P L3
          10 = P N
          11 = P Total
          12 = PF L1
          13 = PF L2
          14 = PF L3
          15 = PF N
          '''

          #send data to DBus
          self._dbusservice['/Ac/L1/Power'] = int(data['nrg'][7])
          self._dbusservice['/Ac/L2/Power'] = int(data['nrg'][8])
          self._dbusservice['/Ac/L3/Power'] = int(data['nrg'][9])
          self._dbusservice['/Ac/Power'] = int(data['nrg'][11])
          self._dbusservice['/Current'] = max(data['nrg'][4], data['nrg'][5], data['nrg'][6])
          self._dbusservice['/Ac/Energy/Forward'] = round(data['wh'] / 1000, 2)
          
          #self._dbusservice['/StartStop'] = int(data['alw'])
          self._dbusservice['/SetCurrent'] = int(data['amp'])
          self._dbusservice['/MaxCurrent'] = int(data['ama']) 
          # update chargingTime, increment charge time only on active charging (2), reset when no car connected (1)
          timeDelta = time.time() - self._last_update
          if int(data['car']) == 2 and self._last_update > 0:  # vehicle loads
            self._charging_time += timeDelta
          elif int(data['car']) == 1:  # charging station ready, no vehicle
            self._charging_time = 0
          self._dbusservice['/ChargingTime'] = int(self._charging_time)

          #self._dbusservice['/Mode'] = 1  # 0 = Manual, 1 = Automatic
          # i dont know how to trace the change of /Mode via VRM...

          # value 'car' 1: charging station ready, no vehicle 2: vehicle loads 3: Waiting for vehicle 4: Charge finished, vehicle still connected
          status = 0
          if int(data['car']) == 1:
            status = 0
          elif int(data['car']) == 2:
            status = 2
          elif int(data['car']) == 3:
            status = 6
          elif int(data['car']) == 4:
            status = 3
          self._dbusservice['/Status'] = status

          #logging
          logging.debug("Wallbox Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
          logging.debug("Wallbox Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
          logging.debug("---")
          
          # increment UpdateIndex - to show that new data is available
          index = self._dbusservice['/UpdateIndex'] + 1  # increment index
          if index > 255:   # maximum value of the index
            index = 0       # overflow from 255 to 0
          self._dbusservice['/UpdateIndex'] = index

          #update lastupdate vars
          self._last_update = time.time()  
       else:
          logging.debug("Wallbox is not available")

    except Exception as e:
       logging.critical('Error at %s', '_update', exc_info=e)
       
    # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
    return True

  def _handlechangedvalue(self, path, value):
    logging.warning("someone else updated %s to %s" % (path, value))
    MaxCurrent = self._dbusservice['/MaxCurrent']
    
    if path == '/SetCurrent':
      if value > MaxCurrent:
        logging.warning("SetCurrent is higher than MaxCurrent. Limit reached. Set SetCurrent to MaxCurrent!")
        return self._set_goe_charger_value('amp', MaxCurrent)
      return self._set_goe_charger_value('amp', value)
    elif path == '/StartStop':
      # Wenn Automatisch (Modestatus = 1), dann im GoECharger auf 0 (Ueberschuss-Laden aktivieren) oder 1 (Laden deaktivieren) stellen
      # wenn geplant (Modestatus = 2), dann im GoECharger auf x stellen - nicht implementiert
      # wenn manuell (Modestatus = 0), dann im GoECharger auf 1 (Laden deaktivieren) und 2 (Laden aktivieren) stellen
      modestatus = self._dbusservice['/Mode']
      if modestatus == 0:
        return self._set_goe_charger_value('frc', value + 1)

      elif modestatus == 1:
        # pruefen, wo StartStop steht
        if value == 1:
          return self._set_goe_charger_value('frc', 0)
        if value == 0:
          return self._set_goe_charger_value('frc', 1)
      else:
        return False

    elif path == '/MaxCurrent':
      logging.warning("It's not allowed to set MaxCurrent via Victron! set MaxCurrent in your Go eCharger!")
      return False
      #return self._setGoeChargerValue('ama', value)
    elif path == '/Mode':
      logging.info("/Mode value %s" % (value))
      StartStop = self._dbusservice['/StartStop']
      logging.info("StartStop ist: %s" % (StartStop))
      lmo = 0
      frc = 1
      # Victron Mode 0 = manual | Go eCharger Loading Mode:"basic", parameter lmo = 3 
      # Victron Mode 1 = automatic | go eCharger Loading Mode: "eco", parameter lmo = 4
      # Victron Mode 2 = scheduled | go eCharger Loading Mode: "daily trip", parameter lmo = 5 - nicht implementiert
      if value == 0:
        lmo = 3
        if (StartStop == 1):
          frc = 2
        logging.info("lmo auf %s gesetzt" % (lmo))
      elif value == 1:
        lmo = 4
        if (StartStop == 1):
          frc = 0
        logging.info("lmo auf %s gesetzt" % (lmo))
      elif value == 2:
        lmo = 5
        logging.info("lmo auf %s gesetzt" % (lmo))
      else:
        logging.info("lmo nicht gesetzt, ELSE Part")
        return False
      logging.info("jetzt wird setGoeChargerValue mit lmo = %s aufgerufen." % (lmo))
      modeswitch = False
      if (self._set_goe_charger_value('lmo', lmo) == True and self._set_goe_charger_value('frc', frc) == True):
        modeswitch = True
      return modeswitch
    else:
      logging.error("mapping for evcharger path %s does not exist" % (path))
      return False


def main():
  #configure logging
  config = configparser.ConfigParser()
  config.read(f"{(os.path.dirname(os.path.realpath(__file__)))}/config.ini")
  logging_level = config["DEFAULT"]["Logging"].upper()

  log_rotate_handler = logging.handlers.RotatingFileHandler(
    maxBytes=512000,
    backupCount=2,
    encoding=None,
    delay=0,
    filename="%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))
  )


  logging.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=logging_level,
                             #logfile="%s/current.log" % (os.path.dirname(os.path.realpath(__file__))),
                            handlers=[ #logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))), # removed due to hint from Philipp Trenz @ Community.victron.com and added log_rotate_handler
                                logging.StreamHandler(),
                                log_rotate_handler
                            ])


  try:
      logging.info("Start")

      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)
     
      # Support multiple hosts
      num_hosts = int(config["HOSTS"]["num_hosts"])
      for i in range(1, num_hosts + 1):
        host_section = f"HOST_{i}"
        logging.info(f"Initializing DBus service for {host_section} ({config[host_section]['Host']})")

        # Create a separate DBus service for each host
        dbus_service = DbusGoeChargerService(config, host_section)

        # Update loop for each host
        gobject.timeout_add(dbus_service._pause_between_requests, dbus_service._update)

      # Main loop
      mainloop = gobject.MainLoop()
      mainloop.run()

  except Exception as e:
    logging.critical('Error at %s','main', exc_info=e)

if __name__ == "__main__":
  main()
