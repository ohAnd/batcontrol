"""
This module provides a class `FroniusWR` for handling Fronius GEN24 Inverters.
It includes methods for interacting with the inverter's API, managing battery
configurations, and controlling various inverter settings.

"""
import time
import os
import logging
import json
import hashlib
import requests
from .baseclass import InverterBaseclass

logger = logging.getLogger('__main__')
logger.info('[Inverter] loading module ')


def hash_utf8(x):
    """Hash a string or bytes object."""
    if isinstance(x, str):
        x = x.encode("utf-8")
    return hashlib.md5(x).hexdigest()


def strip_dict(original):
    """Strip all keys starting with '_' from a dictionary."""
    # return unmodified original if its not a dict
    if not type(original) == dict:
        return original
    stripped_copy = {}
    for key in original.keys():
        if not key.startswith('_'):
            stripped_copy[key] = original[key]
    return stripped_copy


TIMEOFUSE_CONFIG_FILENAME = 'config/timeofuse_config.json'
BATTERY_CONFIG_FILENAME = 'config/battery_config.json'


class FroniusWR(InverterBaseclass):
    """ Class for Handling Fronius GEN24 Inverters """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.login_attempts = 0
        self.address = config['address']
        self.capacity = -1
        self.max_grid_charge_rate = config['max_grid_charge_rate']
        self.max_pv_charge_rate = config['max_pv_charge_rate']
        self.max_bat_discharge_rate = config['max_bat_discharge_rate']
        self.nonce = 0
        self.user = config['user']
        self.password = config['password']
        self.previous_battery_config = self.get_battery_config()
        self.previous_backup_power_config = None
        # default values
        self.max_soc = 100
        self.min_soc = 5
        self.set_solar_api_active(True)
        self.subsequent_login = False

        if not self.previous_battery_config:
            raise RuntimeError(
                f'[Inverter] failed to load Battery config from Inverter at {self.address}')
        try:
            self.previous_backup_power_config = self.get_powerunit_config()
        except RuntimeError:
            logger.error(
                '[Inverter] failed to load Power Unit config from Inverter (latest).'
            )

        if not self.previous_backup_power_config:
            try:
                self.previous_backup_power_config = self.get_powerunit_config(
                    '1.2'
                    )
                logger.info(
                    '[Inverter] loaded Power Unit config from Inverter (1.2).'
                    )
            except RuntimeError:
                logger.error(
                    '[Inverter] failed to load Power Unit config from Inverter (1.2).'
                )

        if self.previous_backup_power_config:
            self.backup_power_mode = self.previous_backup_power_config[
                'backuppower']['DEVICE_MODE_BACKUPMODE_TYPE_U16']
        else:
            logger.error(
                "[Inverter] Setting backup power mode to 0 as a fallback."
                )
            self.backup_power_mode = 0
            self.previous_backup_power_config = None

        if self.backup_power_mode == 0:
            # in percent
            self.min_soc = self.previous_battery_config['BAT_M0_SOC_MIN']
        else:
            # in percent
            self.min_soc = max(
                self.previous_battery_config['BAT_M0_SOC_MIN'],
                self.previous_battery_config['HYB_BACKUP_RESERVED']
            )
        self.max_soc = self.previous_battery_config['BAT_M0_SOC_MAX']
        self.get_time_of_use()  # save timesofuse
        self.set_allow_grid_charging(True)

    def get_SOC(self):
        path = '/solar_api/v1/GetPowerFlowRealtimeData.fcgi'
        response = self.send_request(path)
        if not response:
            logger.error(
                '[Inverter] Failed to get SOC. Returning default value of 99.0'
                )
            return 99.0
        result = json.loads(response.text)
        soc = result['Body']['Data']['Inverters']['1']['SOC']
        return soc

    def get_battery_config(self):
        """ Get battery configuration from inverter and keep a backup."""
        path = '/config/batteries'
        response = self.send_request(path, auth=True)
        if not response:
            logger.error(
                '[Inverter] Failed to get battery configuration. Returning empty dict'
            )
            return {}

        result = json.loads(response.text)
        # only write file if it does not exist
        if not os.path.exists(BATTERY_CONFIG_FILENAME):
            with open(BATTERY_CONFIG_FILENAME, 'w', encoding='utf-8') as f:
                f.write(response.text)
        else:
            logger.warning(
                '[Inverter] Battery config file already exists. Not writing to %s',
                BATTERY_CONFIG_FILENAME
            )

        return result

    def get_powerunit_config(self, path_version='latest'):
        """ Get additional PowerUnit configuration for backup power.

        Parameters:
            path_version (optional):
                'latest' (default) - get via '/config/powerunit'
                '1.2'              - get via '/config/setup/powerunit'

        Returns: dict with backup power configuration
        """
        if path_version == 'latest':
            path = '/config/powerunit'
        else:
            path = '/config/setup/powerunit'

        response = self.send_request(path, auth=True)
        if not response:
            logger.error(
                '[Inverter] Failed to get power unit configuration. Returning empty dict'
            )
            return {}
        result = json.loads(response.text)
        return result

    def restore_battery_config(self):
        """ Restore the previous battery config from a backup file."""
        settings_to_restore = [
            'BAT_M0_SOC_MAX',
            'BAT_M0_SOC_MIN',
            'BAT_M0_SOC_MODE',
            'HYB_BM_CHARGEFROMAC',
            'HYB_EM_MODE',
            'HYB_EM_POWER',
            'HYB_EVU_CHARGEFROMGRID'
        ]
        settings = {}
        for key in settings_to_restore:
            if key in self.previous_battery_config.keys():
                settings[key] = self.previous_battery_config[key]
            else:
                RuntimeError(
                    f"Unable to restore settings. Parameter {key} is missing"
                )
        path = '/config/batteries'
        payload = json.dumps(settings)
        logger.info(
            '[Inverter] Restoring previous battery configuration: %s ',
            payload
        )
        response = self.send_request(
            path, method='POST', payload=payload, auth=True)
        if not response:
            raise RuntimeError('failed to restore battery config')

        response_dict = json.loads(response.text)
        expected_write_successes = settings_to_restore
        for expected_write_success in expected_write_successes:
            if not expected_write_success in response_dict['writeSuccess']:
                raise RuntimeError(f'failed to set {expected_write_success}')
        # Remove after successful restore
        try:
            os.remove(BATTERY_CONFIG_FILENAME)
        except OSError:
            logger.error(
                '[Inverter] could not remove battery config file %s', BATTERY_CONFIG_FILENAME)
        return response

    def set_allow_grid_charging(self, value: bool):
        """ Switches grid charging on (true) or off."""
        if value:
            payload = '{"HYB_EVU_CHARGEFROMGRID": true}'
        else:
            payload = '{"HYB_EVU_CHARGEFROMGRID": false}'
        path = '/config/batteries'
        response = self.send_request(
            path, method='POST', payload=payload, auth=True)
        response_dict = json.loads(response.text)
        expected_write_successes = ['HYB_EVU_CHARGEFROMGRID']
        for expected_write_success in expected_write_successes:
            if not expected_write_success in response_dict['writeSuccess']:
                raise RuntimeError(f'failed to set {expected_write_success}')
        return response

    def set_solar_api_active(self, value: bool):
        """ Switches Solar.API on (true) or off. Solar.API is required to get SOC values."""
        if value:
            payload = '{"SolarAPIv1Enabled": true}'
        else:
            payload = '{"SolarAPIv1Enabled": false}'
        path = '/config/solar_api'
        response = self.send_request(
            path, method='POST', payload=payload, auth=True)
        response_dict = json.loads(response.text)
        expected_write_successes = ['SolarAPIv1Enabled']
        for expected_write_success in expected_write_successes:
            if not expected_write_success in response_dict['writeSuccess']:
                raise RuntimeError(f'failed to set {expected_write_success}')
        return response

    def set_wr_parameters(self, minsoc, maxsoc, allow_grid_charging, grid_power):
        """set power at grid-connection point negative values for Feed-In"""
        path = '/config/batteries'
        if not type(allow_grid_charging) == bool:
            raise RuntimeError(
                f'Expected type: bool actual type: {type(allow_grid_charging)}')

        grid_power = int(grid_power)
        minsoc = int(minsoc)
        maxsoc = int(maxsoc)

        if not 0 <= grid_power <= self.max_grid_charge_rate:
            raise RuntimeError(f'gridpower out of allowed limits {grid_power}')

        if minsoc > maxsoc:
            raise RuntimeError('Min SOC needs to be higher than Max SOC')

        if minsoc < self.min_soc:
            raise RuntimeError(f'Min SOC not allowed below {self.min_soc}')

        if maxsoc > self.max_soc:
            raise RuntimeError(f'Max SOC not allowed above {self.max_soc}')

        parameters = {'HYB_EVU_CHARGEFROMGRID': allow_grid_charging,
                      'HYB_EM_POWER': grid_power,
                      'HYB_EM_MODE': 1,
                      'BAT_M0_SOC_MIN': minsoc,
                      'BAT_M0_SOC_MAX': maxsoc,
                      'BAT_M0_SOC_MODE': 'manual'
                      }

        payload = json.dumps(parameters)
        logger.info('[Inverter] Setting battery parameters: %s', payload)

        response = self.send_request(
            path, method='POST', payload=payload, auth=True)
        if not response:
            logger.error(
                '[Inverter] Failed to set parameters. No response from server'
                )
            return response
        response_dict = json.loads(response.text)
        for expected_write_success in parameters.keys():
            if not expected_write_success in response_dict['writeSuccess']:
                raise RuntimeError(f'failed to set {expected_write_success}')
        return response

    def get_time_of_use(self):
        """ Get time of use configuration from inverter and keep a backup."""
        response = self.send_request('/config/timeofuse', auth=True)
        if not response:
            return None

        result = json.loads(response.text)['timeofuse']
        # only write file if it does not exist
        if not os.path.exists(TIMEOFUSE_CONFIG_FILENAME):
            with open(TIMEOFUSE_CONFIG_FILENAME, 'w', encoding='utf-8') as f:
                f.write(json.dumps(result))
        else:
            logger.warning(
                '[Inverter] Time of use config file already exists. Not writing to %s',
                TIMEOFUSE_CONFIG_FILENAME
            )

        return result

    def set_mode_avoid_discharge(self):
        """ Set the inverter to avoid discharging the battery."""
        timeofuselist = [{'Active': True,
                          'Power': int(0),
                          'ScheduleType': 'DISCHARGE_MAX',
                          "TimeTable": {"Start": "00:00", "End": "23:59"},
                          "Weekdays": {"Mon": True, "Tue": True, "Wed": True, "Thu": True, "Fri": True, "Sat": True, "Sun": True}
                          }]
        return self.set_time_of_use(timeofuselist)

    def set_mode_allow_discharge(self):
        """ Set the inverter to discharge the battery."""      
        timeofuselist = []
        # 1. entry in schedule - limit the charge rate to max_pv_charge_rate
        if self.max_pv_charge_rate > 0:
            max_charge_item = {'Active': True,
                              'Power': int(self.max_pv_charge_rate),
                              'ScheduleType': 'CHARGE_MAX',
                              "TimeTable": {"Start": "00:00", "End": "23:59"},
                              "Weekdays": {"Mon": True, "Tue": True, "Wed": True, "Thu": True, "Fri": True, "Sat": True, "Sun": True}
                              }
            timeofuselist.append(max_charge_item)
            logger.debug('[Inverter] set_mode_allow_discharge - 1. entry in schedule - set max pv charge rate to %s', str(int(self.max_pv_charge_rate)))
        else:
            logger.debug('[Inverter] set_mode_allow_discharge - 1. entry in schedule NOT set - no max pv charge rate set')
        # 2. entry in schedule - limit the discharge rate to max_bat_discharge_rate
        if self.max_bat_discharge_rate > 0:
            max_discharge_item = {'Active': True,
                              'Power': int(self.max_bat_discharge_rate),
                              'ScheduleType': 'DISCHARGE_MAX',
                              "TimeTable": {"Start": "00:00", "End": "23:59"},
                              "Weekdays": {"Mon": True, "Tue": True, "Wed": True, "Thu": True, "Fri": True, "Sat": True, "Sun": True}
                              }
            timeofuselist.append(max_discharge_item)    
            logger.debug('[Inverter] set_mode_allow_discharge - 2. entry in schedule - set max bat discharge rate to %s', str(int(self.max_bat_discharge_rate)))
        else:
            logger.debug('[Inverter] set_mode_allow_discharge - 2. entry in schedule NOT set - no max bat discharge rate set')
        if len(timeofuselist) > 0:
            response = self.set_time_of_use(timeofuselist)
            return response
        else:
            logger.error('[Inverter] set_mode_allow_discharge - no time of use entries set')
            return None

    def set_mode_force_charge(self, chargerate=500):
        """ Set the inverter to charge the battery with a specific power from GRID."""
        # activate timeofuse rules
        if chargerate > self.max_grid_charge_rate:
            chargerate = self.max_grid_charge_rate
        timeofuselist = [{'Active': True,
                          'Power': int(chargerate),
                          'ScheduleType': 'CHARGE_MIN',
                          "TimeTable": {"Start": "00:00", "End": "23:59"},
                          "Weekdays": {"Mon": True, "Tue": True, "Wed": True, "Thu": True, "Fri": True, "Sat": True, "Sun": True}
                          }]
        return self.set_time_of_use(timeofuselist)

    def restore_time_of_use_config(self):
        """ Restore the previous time of use config from a backup file."""
        try:
            with open(TIMEOFUSE_CONFIG_FILENAME, 'r') as f:
                time_of_use_config_json = f.read()
        except OSError:
            logger.error('[Inverter] could not restore timeofuse config')
            return

        try:
            time_of_use_config = json.loads(time_of_use_config_json)
        except:
            logger.error(
                '[Inverter] could not parse timeofuse config from %s',
                TIMEOFUSE_CONFIG_FILENAME
            )
            return

        stripped_time_of_use_config = []
        for listitem in time_of_use_config:
            new_item = {}
            new_item['Active'] = listitem['Active']
            new_item['Power'] = listitem['Power']
            new_item['ScheduleType'] = listitem['ScheduleType']
            new_item['TimeTable'] = {
                'Start': listitem['TimeTable']['Start'],
                'End': listitem['TimeTable']['End']
            }
            weekdays = {}
            for day in ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']:
                weekdays[day] = listitem['Weekdays'][day]
            new_item['Weekdays'] = weekdays
            stripped_time_of_use_config.append(new_item)

        self.set_time_of_use(stripped_time_of_use_config)
        # After restoring the time of use config, delete the backup
        try:
            os.remove(TIMEOFUSE_CONFIG_FILENAME)
        except OSError:
            logger.error(
                '[Inverter] could not remove timeofuse config file %s', TIMEOFUSE_CONFIG_FILENAME
                )

    def set_time_of_use(self, timeofuselist):
        """ Get the planned battery charge/discharge schedule."""
        config = {
            'timeofuse': timeofuselist
        }
        payload = json.dumps(config)
        response = self.send_request(
            '/config/timeofuse', method='POST', payload=payload, auth=True
            )
        response_dict = json.loads(response.text)
        expected_write_successes = ['timeofuse']
        for expected_write_success in expected_write_successes:
            if not expected_write_success in response_dict['writeSuccess']:
                raise RuntimeError(f'failed to set {expected_write_success}')
        return response

    def get_capacity(self):
        """ Get the full and raw capacity of the battery in Wh."""
        if self.capacity >= 0:
            return self.capacity

        response = self.send_request(
            '/solar_api/v1/GetStorageRealtimeData.cgi')
        if not response:
            logger.warning(
                '[Inverter] capacity request failed. Returning default value'
            )
            return 1000
        result = json.loads(response.text)
        capacity = result['Body']['Data']['0']['Controller']['DesignedCapacity']
        self.capacity = capacity
        return capacity

    def send_request(self,  path, method='GET', payload="", params=None, headers={}, auth=False):
        """Send a HTTP REST request to the inverter."""
        for i in range(3):
            url = 'http://' + self.address + path
            fullpath = path
            if params:
                fullpath += '?' + \
                    "&".join(
                        [f'{k+"="+str(params[k])}' for k in params.keys()])
            if auth:
                headers['Authorization'] = self.get_auth_header(
                    method=method, path=fullpath)
            try:
                response = requests.request(
                                        method=method,
                                        url=url,
                                        params=params,
                                        headers=headers,
                                        data=payload,
                                        timeout=30
                                    )
                if response.status_code == 200:
                    return response
                elif response.status_code == 401:  # unauthorized
                    self.nonce = self.get_nonce(response)
                    if self.login_attempts >= 3:
                        logger.info(
                            '[Inverter] Login failed 3 times .. aborting'
                            )
                        raise RuntimeError(
                            '[Inverter] Login failed repeatedly .. wrong credentials?'
                            )
                    response = self.login()
                    if (response.status_code == 200):
                        logger.info('[Inverter] Login successful')
                        self.login_attempts = 0
                        self.subsequent_login = True
                    else:
                        logger.error(
                            '[Inverter] Login -%d- failed, Response: %s', i, response)
                        if self.subsequent_login:
                            logger.info(
                                "[Inverter] Retrying login in 10 seconds")
                            time.sleep(10)
                else:
                    raise RuntimeError(
                        f"[Inverter] Request failed with {response.status_code}-"
                        f"{response.reason}. \n"
                        f"\turl:{url}, \n\tparams:{params} \n\theaders {headers} \n"
                        f"\tnonce {self.nonce} \n"
                        f"\tpayload {payload}"
                    )
            except requests.exceptions.ConnectionError as err:
                logger.error(
                    "[Inverter] Connection to Inverter failed on %s. Retrying in 120 seconds",
                    self.address
                    )
                time.sleep(20)

        response = None
        return response

    def login(self):
        """Login to Fronius API"""
        path = '/commands/Login'
        self.login_attempts += 1
        return self.send_request(path, auth=True)

    def logout(self):
        """Logout from Fronius API"""
        path = '/commands/Logout'
        response = self.send_request(path, auth=True)
        if not response:
            logger.warning('[Inverter] Logout failed. No response from server')
        if response.status_code == 200:
            logger.info('[Inverter] Logout successful')
        else:
            logger.info('[Inverter] Logout failed')
        return response

    def get_nonce(self, response):
        """Get the nonce from the response headers."""
        # stupid API bug: nonce headers with different capitalization at different end points
        if 'X-WWW-Authenticate' in response.headers:
            auth_string = response.headers['X-WWW-Authenticate']
        elif 'X-Www-Authenticate' in response.headers:
            auth_string = response.headers['X-Www-Authenticate']
        else:
            auth_string = ""

        auth_list = auth_string.replace(" ", "").replace('"', '').split(',')
        auth_dict = {}
        for item in auth_list:
            key, value = item.split("=")
            auth_dict[key] = value
        return auth_dict['nonce']

    def get_auth_header(self, method, path) -> str:
        """Create the Authorization header for the request."""
        nonce = self.nonce
        realm = 'Webinterface area'
        ncvalue = "00000001"
        cnonce = "NaN"
        user = self.user
        password = self.password
        if len(self.user) < 4:
            raise RuntimeError("User needed for Authorization")
        if len(self.password) < 4:
            raise RuntimeError("Password needed for Authorization")

        A1 = f"{user}:{realm}:{password}"
        A2 = f"{method}:{path}"
        HA1 = hash_utf8(A1)
        HA2 = hash_utf8(A2)
        noncebit = f"{nonce}:{ncvalue}:{cnonce}:auth:{HA2}"
        respdig = hash_utf8(f"{HA1}:{noncebit}")
        auth_header = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{path}", algorithm="MD5", qop=auth, nc={ncvalue}, cnonce="{cnonce}", response="{respdig}"'
        return auth_header

    def shutdown(self):
        """Change back batcontrol changes."""
        logger.info('[Inverter] Reverting batcontrol created config changes')
        self.restore_battery_config()
        self.restore_time_of_use_config()
        self.logout()

    def activate_mqtt(self, api_mqtt_api):
        """
        Activates MQTT for the inverter.

        This function starts the API functions and publishes all internal values via MQTT.
        The MQTT topic is: base_topic + '/inverters/0/'

        Parameters that can be set via MQTT:
        - max_grid_charge_rate (int): Maximum power in W that can be
                                          used to load the battery from the grid.
        - max_pv_charge_rate (int): Maximum power in W that can be
                                          used to load the battery from the PV.
        - max_bat_discharge_rate (int): Maximum power in W that can be
                                          used to discharge the battery for house consumption.

        Args:
            api_mqtt_api: The MQTT API instance to be used for registering callbacks.

        """
        import mqtt_api
        self.mqtt_api = api_mqtt_api
        # /set is appended to the topic
        self.mqtt_api.register_set_callback(self.__get_mqtt_topic() + 'max_grid_charge_rate', self.api_set_max_grid_charge_rate, int)
        self.mqtt_api.register_set_callback(self.__get_mqtt_topic() + 'max_pv_charge_rate', self.api_set_max_pv_charge_rate, int)
        self.mqtt_api.register_set_callback(self.__get_mqtt_topic() + 'max_bat_discharge_rate', self.api_set_max_bat_discharge_rate, int)

    def refresh_api_values(self):
        """ Publishes all values to mqtt."""
        if self.mqtt_api:
            self.mqtt_api.generic_publish(
                self.__get_mqtt_topic() + 'SOC', self.get_SOC())
            self.mqtt_api.generic_publish(self.__get_mqtt_topic() + 'stored_energy', self.get_stored_energy())
            self.mqtt_api.generic_publish(self.__get_mqtt_topic() + 'free_capacity', self.get_free_capacity())
            self.mqtt_api.generic_publish(self.__get_mqtt_topic() + 'max_capacity', self.get_max_capacity())
            self.mqtt_api.generic_publish(self.__get_mqtt_topic() + 'usable_capacity', self.get_usable_capacity())
            self.mqtt_api.generic_publish(self.__get_mqtt_topic() + 'max_grid_charge_rate', self.max_grid_charge_rate)
            self.mqtt_api.generic_publish(self.__get_mqtt_topic() + 'max_pv_charge_rate', self.max_pv_charge_rate)
            self.mqtt_api.generic_publish(self.__get_mqtt_topic() + 'max_bat_discharge_rate', self.max_bat_discharge_rate)
            self.mqtt_api.generic_publish(self.__get_mqtt_topic() + 'min_soc', self.min_soc)
            self.mqtt_api.generic_publish(self.__get_mqtt_topic() + 'max_soc', self.max_soc)
            self.mqtt_api.generic_publish(self.__get_mqtt_topic() + 'capacity', self.get_capacity())

    def api_set_max_grid_charge_rate(self, max_grid_charge_rate: int):
        """ Set the maximum power in W that can be used to load the battery from the grid."""
        if max_grid_charge_rate < 0:
            logger.warning(
                '[Inverter] API: Invalid max_grid_charge_rate %sW',
                max_grid_charge_rate
            )
            return
        logger.info(
            '[Inverter] API: Setting max_grid_charge_rate: %.1fW',
            max_grid_charge_rate
        )
        self.max_grid_charge_rate = max_grid_charge_rate

    def api_set_max_pv_charge_rate(self, max_pv_charge_rate: int):
        """ Set the maximum power in W that can be used to load the battery from the PV."""
        if max_pv_charge_rate < 0:
            logger.warning(
                '[Inverter] API: Invalid max_pv_charge_rate %s',
                max_pv_charge_rate
            )
            return
        logger.info(
            '[Inverter] API: Setting max_pv_charge_rate: %.1fW',
            max_pv_charge_rate
        )
        self.max_pv_charge_rate = max_pv_charge_rate
        
    def api_set_max_bat_discharge_rate(self, max_bat_discharge_rate: int):
        """ Set the maximum power in W that can be used to discharge the battery for house consumption."""
        if max_bat_discharge_rate < 0:
            logger.warning(
                '[Inverter] API: Invalid max_bat_discharge_rate %s',
                max_bat_discharge_rate
            )
            return
        logger.info(
            '[Inverter] API: Setting max_bat_discharge_rate: %.1fW',
            max_bat_discharge_rate
        )
        self.max_bat_discharge_rate = max_bat_discharge_rate

    def __get_mqtt_topic(self) -> str:
        """ Used to implement the mqtt basic topic."""
        return f'inverters/{self.inverter_num}/'
