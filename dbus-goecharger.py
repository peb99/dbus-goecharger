#!/usr/bin/env python
 
# import normal packages
import platform 
import logging
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
  def __init__(self, servicename, paths, productname='go-eCharger', connection='go-eCharger HTTP JSON service'):
    config = self._getConfig()
    deviceinstance = int(config['DEFAULT']['Deviceinstance'])
    hardwareVersion = int(config['DEFAULT']['HardwareVersion'])
    pauseBetweenRequests = int(config['ONPREMISE']['PauseBetweenRequests']) # in ms
    position = int(config['DEFAULT'].get('Position', '1'))

    if pauseBetweenRequests <= 20:
      raise ValueError("Pause between requests must be greater than 20")

    if hardwareVersion < 4:
      raise ValueError("Minimum hardware version required is 4.")

    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance))
    self._paths = paths
    
    logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))
    
    paths_wo_unit = [
      '/Status',  # value 'car' 1: charging station ready, no vehicle 2: vehicle loads 3: Waiting for vehicle 4: Charge finished, vehicle still connected
      '/Mode' 
    ]
    
    #get data from go-eCharger
    data = self._getGoeChargerData()

    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', connection)
    
    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    self._dbusservice.add_path('/ProductId', 0xFFFF) # 
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', productname)    
    if data:
       self._dbusservice.add_path('/FirmwareVersion', data['fwv'])
       self._dbusservice.add_path('/Serial', data['sse'])
    self._dbusservice.add_path('/HardwareVersion', hardwareVersion)
    self._dbusservice.add_path('/Connected', 1)
    self._dbusservice.add_path('/UpdateIndex', 0)
    self._dbusservice.add_path("/Position", position)

    # add paths without units
    for path in paths_wo_unit:
      self._dbusservice.add_path(path, None)
    
    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)



    # last update
    self._lastUpdate = 0
    
    # charging time in float
    self._chargingTime = 0.0

    # add _update function 'timer'
    gobject.timeout_add(pauseBetweenRequests, self._update)
    
    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)
 
  def _getConfig(self):
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config
 
 
  def _getSignOfLifeInterval(self):
    config = self._getConfig()
    value = config['DEFAULT']['SignOfLifeLog']
    
    if not value: 
        value = 0
    
    return int(value)
  
  
  def _getGoeChargerStatusUrl(self):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']
    
    if accessType == 'OnPremise': 
        URL = "http://%s/api/status?filter=fwv,sse,nrg,wh,alw,amp,ama,car" % (config['ONPREMISE']['Host'])
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))
    
    return URL
  
  def _getGoeChargerAPIPayloadUrl(self, parameter, value):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']
    
    if accessType == 'OnPremise': 
        URL = "http://%s/api/set?%s=%s" % (config['ONPREMISE']['Host'], parameter, value)
        logging.debug("Folgende URL wird getriggert: %s" % (URL))
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))
    
    return URL
  
  def _setGoeChargerValue(self, parameter, value):
    URL = self._getGoeChargerAPIPayloadUrl(parameter, str(value))
    request_data = requests.get(url = URL)
    
    # check for response
    if not request_data:
      logging.info("No response from go-eCharger - %s" % (URL))
      raise ConnectionError("No response from go-eCharger - %s" % (URL))

    
    json_data = request_data.json()

    logging.debug("Folgender Wert kam zurueck: " % json_data[parameter])

    # check for Json
    if not json_data:
        logging.info("Converting response to JSON failed")
        raise ValueError("Converting response to JSON failed")
    
    if json_data[parameter] == "true":
      return True
    else:
      logging.info("go-eCharger parameter %s not set to %s" % (parameter, str(value)))
      return False
    
 
  def _getGoeChargerData(self):
    URL = self._getGoeChargerStatusUrl()
    try:
       request_data = requests.get(url = URL, timeout=5)
    except Exception:
       return None
    
    # check for response
    if not request_data:
        raise ConnectionError("No response from go-eCharger - %s" % (URL))
    
    json_data = request_data.json()     
    
    # check for Json
    if not json_data:
        raise ValueError("Converting response to JSON failed")
    
    
    return json_data
 

  def _setGoeChargerAutomaticModeValues(self):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']
    logging.debug("AutomaticMode - Werte setzen:")
    
    if accessType == 'OnPremise': 
        bus = dbus.SystemBus()
        # pGrid ermitteln. Kumulierte Leistung des Grid auf allen Phasen
        L1gPower = float((bus.get_object('com.victronenergy.system', '/Ac/Grid/L1/Power')).GetValue())
        L2gPower = float((bus.get_object('com.victronenergy.system', '/Ac/Grid/L2/Power')).GetValue())
        L3gPower = float((bus.get_object('com.victronenergy.system', '/Ac/Grid/L3/Power')).GetValue())
        pGrid = L1gPower + L2gPower + L3gPower
        logging.info("pGrid wurde ermittelt: %s" % (pGrid))

        # pPv ermitteln. Kumulierte Leistung aller PV Anlagen auf allen Phasen
        L1pPower = float((bus.get_object('com.victronenergy.system', '/Ac/PvOnGrid/L1/Power')).GetValue())
        L2pPower = float((bus.get_object('com.victronenergy.system', '/Ac/PvOnGrid/L2/Power')).GetValue())
        L3pPower = float((bus.get_object('com.victronenergy.system', '/Ac/PvOnGrid/L3/Power')).GetValue())
        pPv = L1pPower + L2pPower + L3pPower
        logging.info("pPv wurde ermittelt: %s" % (pPv))

        # pAkku ermittelnt
        pAkku = float((bus.get_object('com.victronenergy.system', '/Dc/Battery/Power')).GetValue())
        logging.info("pAkku wurde ermittelt: %s" % (pAkku))

        URL = 'http://%s/api/set?ids={"pGrid":%s,"pAkku":%s,"pPv":%s}' % (config['ONPREMISE']['Host'],pGrid,pAkku,pPv)
        logging.info("Folgende URL wird getriggert: %s" % (URL))

        requests.get(url = URL, timeout=15)

        # http://192.168.100.4/api/set?ids={%22pGrid%22:-6027,%22pAkku%22:-186,%22pPv%22:7269}
        # Werte checken:
        # http://192.168.100.4/api/status?filter=ppv
        # http://192.168.100.4/api/status?filter=pAkku
        # http://192.168.100.4/api/status?filter=pGrid
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))
    return True




  def _signOfLife(self):
    logging.info("--- Start: sign of life ---")
    logging.info("Last _update() call: %s" % (self._lastUpdate))
    logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
    logging.info("--- End: sign of life ---")
    return True
 
  def _update(self):   
    try:
       #get data from go-eCharger
       data = self._getGoeChargerData()
       logging.debug("Update Schleife.")
       modestatus = self._dbusservice['/Mode']
       logging.debug("Mode ist: %s" % (modestatus))
       if modestatus == 1:
         self._setGoeChargerAutomaticModeValues()


       
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
          
          self._dbusservice['/StartStop'] = int(data['alw'])
          self._dbusservice['/SetCurrent'] = int(data['amp'])
          self._dbusservice['/MaxCurrent'] = int(data['ama']) 
          # update chargingTime, increment charge time only on active charging (2), reset when no car connected (1)
          timeDelta = time.time() - self._lastUpdate
          if int(data['car']) == 2 and self._lastUpdate > 0:  # vehicle loads
            self._chargingTime += timeDelta
          elif int(data['car']) == 1:  # charging station ready, no vehicle
            self._chargingTime = 0
          self._dbusservice['/ChargingTime'] = int(self._chargingTime)

          self._dbusservice['/Mode'] = 1  # 0 = Manual, 1 = Automatic
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
          self._lastUpdate = time.time()  
       else:
          logging.debug("Wallbox is not available")

    except Exception as e:
       logging.critical('Error at %s', '_update', exc_info=e)
       
    # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
    return True
 
  def _handlechangedvalue(self, path, value):
    logging.error("someone else updated %s to %s" % (path, value))
    MaxCurrent = self._dbusservice['/MaxCurrent']
    
    if path == '/SetCurrent':
      if value > MaxCurrent:
        logging.error("SetCurrent is higher than MaxCurrent. Limit reached. Set SetCurrent to MaxCurrent!")
        return self._setGoeChargerValue('amp', MaxCurrent)
      return self._setGoeChargerValue('amp', value)
    elif path == '/StartStop':
      return self._setGoeChargerValue('alw', value)
    elif path == '/MaxCurrent':
      logging.error("It's not allowed to set MaxCurrent via Victron! set MaxCurrent in your Go eCharger!")
      return False
      #return self._setGoeChargerValue('ama', value)
    else:
      logging.error("mapping for evcharger path %s does not exist" % (path))
      return False


def main():
  #configure logging
  config = configparser.ConfigParser()
  config.read(f"{(os.path.dirname(os.path.realpath(__file__)))}/config.ini")
  logging_level = config["DEFAULT"]["Logging"].upper()

  logging.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=logging_level,
                            handlers=[
                                logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                                logging.StreamHandler()
                            ])
 
  try:
      logging.info("Start")
  
      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)
     
      #formatting 
      _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
      _a = lambda p, v: (str(round(v, 1)) + 'A')
      _w = lambda p, v: (str(round(v, 1)) + 'W')
      _v = lambda p, v: (str(round(v, 1)) + 'V')
      _degC = lambda p, v: (str(v) + '°C')
      _s = lambda p, v: (str(v) + 's')
     
      #start our main-service
      pvac_output = DbusGoeChargerService(
        servicename='com.victronenergy.evcharger',
        paths={
          '/Ac/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
          '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
          '/ChargingTime': {'initial': 0, 'textformat': _s},
          
          '/Ac/Voltage': {'initial': 0, 'textformat': _v},
          '/Current': {'initial': 0, 'textformat': _a},
          '/SetCurrent': {'initial': 0, 'textformat': _a},
          '/MaxCurrent': {'initial': 0, 'textformat': _a},
          '/MCU/Temperature': {'initial': 0, 'textformat': _degC},
          '/StartStop': {'initial': 0, 'textformat': lambda p, v: (str(v))}
        }
        )
     
      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()            
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)
if __name__ == "__main__":
  main()
