# System Imports
import network
import utime
import json 
from time import sleep
from umqtt.simple import MQTTClient
import ubluetooth
import struct
import ubinascii

# I/O Imports + ds18x20
import ds18x20
import machine, onewire

DEBUG = False

# Tilt color mapping - Using hex strings for easier comparison
TILT_COLORS = {
    "a495bb10c5b14b44b5121370f02d74de": "red",
    "a495bb20c5b14b44b5121370f02d74de": "green", 
    "a495bb30c5b14b44b5121370f02d74de": "black",
    "a495bb40c5b14b44b5121370f02d74de": "purple",
    "a495bb50c5b14b44b5121370f02d74de": "orange",
    "a495bb60c5b14b44b5121370f02d74de": "blue",
    "a495bb70c5b14b44b5121370f02d74de": "yellow",
    "a495bb80c5b14b44b5121370f02d74de": "pink"
}

# Topics to publish (keeping your existing topics)
topic_outside_temp_garage = "home/outside/temperature_garage"
topic_outside_temp_ferm_1 = "home/outside/temperature_fermenter_1"
topic_outside_temp_ferm_2 = "home/outside/temperature_fermenter_2"
topic_outside_brewery_tilt_blue_rssi = "home/outside/tilt/blue/rssi"
topic_outside_brewery_tilt_blue_temperature = "home/outside/brewery/tilt/blue/temperature"
topic_outside_brewery_tilt_blue_gravity = "home/outside/brewery/tilt/blue/gravity"
topic_outside_brewery_tilt_blue_data = "home/outside/brewery/tilt/blue/data"
topic_outside_brewery_tilt_green_rssi = "home/outside/tilt/green/rssi"
topic_outside_brewery_tilt_green_temperature = "home/outside/brewery/tilt/green/temperature"
topic_outside_brewery_tilt_green_gravity = "home/outside/brewery/tilt/green/gravity"
topic_outside_brewery_tilt_green_data = "home/outside/brewery/tilt/green/data"

# Freq to publish the topics
PUBLISH_FREQ = 15

# Wifi settings
ssid = 'Shinkle_2.4ghz'
password = 'C00p3r0805'

# RPI Pico network settings
HOSTNAME = 'ashinkl-rpipw-0'
PORT = 12345

# Global variables for BLE
ble = None
ble_last_readings = {}
scanning = False
client = None

# Configure LED on the board as an output
led = machine.Pin('LED', machine.Pin.OUT)
led.value(False)

# Connect to WLAN 
wlan = network.WLAN(network.STA_IF)
network.hostname(HOSTNAME)
wlan.active(True)
led.value(True)
sleep(5)
wlan.connect(ssid, password)
sleep(15)
led.value(False)

# Wait for connection
while not wlan.isconnected():
    sleep(1)
    if DEBUG:
        print("Waiting for WiFi connection...")

if DEBUG:
    print(f"WiFi connected: {wlan.ifconfig()}")

# Initialize one wire to be able to read the DS18B20
ds_pin = machine.Pin(22)
ds_sensor = ds18x20.DS18X20(onewire.OneWire(ds_pin))

# Find the current sensors
sensor = ds_sensor.scan()

if DEBUG:
    print('Found DS devices: ', sensor)

# Connects to the Home Assistant MQTT Broker
def mqtt_connect():
    global client
    client = MQTTClient(client_id=b"python-mqtt-572",
                        server=b"10.0.0.152",
                        port=1883,
                        keepalive=3600,
                        user=b"mqtt-user",
                        password=b"mqtt-user"
                        )
    client.connect()
    if DEBUG:
        print("MQTT connected")
    return client

def reconnect():
    sleep(15)
    machine.reset()

def parse_tilt_data(manufacturer_data):
    """Parse Tilt hydrometer data from manufacturer data"""
    if len(manufacturer_data) < 25:
        if DEBUG:
            print(f"  Manufacturer data too short: {len(manufacturer_data)} bytes (need at least 25)")
        return None
        
    try:
        # Parse the complete payload for debugging
        payload_hex = ubinascii.hexlify(manufacturer_data).decode()
        if DEBUG:
            print(f"  Full manufacturer payload: {payload_hex} (length: {len(manufacturer_data)})")
        
        # Company ID is in LITTLE ENDIAN format in BLE manufacturer data
        company_id = struct.unpack('<H', manufacturer_data[0:2])[0]
        
        if DEBUG:
            print(f"  Company ID: 0x{company_id:04x}")
        
        # Check for Apple company ID (0x004C)
        if company_id == 0x004C:
            # Check if we have enough data for iBeacon
            if len(manufacturer_data) >= 25:
                # iBeacon type field is in BIG ENDIAN
                beacon_type = struct.unpack('>H', manufacturer_data[2:4])[0]
                
                if DEBUG:
                    print(f"  Beacon type: 0x{beacon_type:04x}")
                
                # Check for iBeacon type (0x0215)
                if beacon_type == 0x0215:
                    uuid = manufacturer_data[4:20]
                    major = struct.unpack('>H', manufacturer_data[20:22])[0]
                    minor = struct.unpack('>H', manufacturer_data[22:24])[0]
                    
                    uuid_hex = ubinascii.hexlify(uuid).decode()
                    
                    if DEBUG:
                        print(f"  iBeacon UUID: {uuid_hex}")
                        print(f"  Major (temp): {major}")
                        print(f"  Minor (gravity): {minor}")
                        print(f"  Checking against known Tilt UUIDs...")
                    
                    # Check if this is a Tilt UUID using hex string comparison
                    color = TILT_COLORS.get(uuid_hex)
                    if color:
                        temperature = major  # Temperature in Fahrenheit
                        gravity = minor / 1000.0  # Specific gravity
                        if DEBUG:
                            print(f"  *** FOUND TILT {color.upper()}: {temperature}°F, SG: {gravity} ***")
                        return color, temperature, gravity
                    else:
                        if DEBUG:
                            print(f"  UUID not recognized as Tilt: {uuid_hex}")
                            print(f"  Expected green UUID: a495bb20c5b14b44b5121370f02d74de")
                            print(f"  Actual UUID:        {uuid_hex}")
                            # Check character by character
                            expected = "a495bb20c5b14b44b5121370f02d74de"
                            if len(uuid_hex) == len(expected):
                                print(f"  Character comparison:")
                                for i, (a, b) in enumerate(zip(expected, uuid_hex)):
                                    if a != b:
                                        print(f"    Position {i}: expected '{a}', got '{b}'")
                else:
                    if DEBUG:
                        print(f"  Not iBeacon format (expected 0x0215, got 0x{beacon_type:04x})")
            else:
                if DEBUG:
                    print(f"  Apple device but insufficient data for iBeacon: {len(manufacturer_data)} bytes")
        else:
            if DEBUG:
                print(f"  Not Apple device (Company ID: 0x{company_id:04x}, expected 0x004C)")
                    
    except Exception as e:
        if DEBUG:
            print(f"Error parsing Tilt data: {e}")
            import sys
            sys.print_exception(e)
        
    return None

def publish_tilt_data(color, temperature, gravity, rssi):
    """Publish Tilt data to MQTT"""
    global client
    
    print(f"*** PUBLISHING TILT DATA FOR {color.upper()} ***")
    print(f"  Temperature: {temperature}°F")
    print(f"  Gravity: {gravity}")
    print(f"  RSSI: {rssi}")
    
    if not client:
        print("  ERROR: No MQTT client available!")
        return
        
    try:
        # Create data payload
        data = {
            "color": color,
            "temperature": temperature,
            "gravity": gravity,
            "rssi": rssi,
            "timestamp": utime.time()
        }
        
        print(f"  Data payload: {data}")
        
        # Publish based on color
        if color == "blue":
            print("  Publishing to BLUE topics...")
            client.publish(topic_outside_brewery_tilt_blue_temperature, str(temperature))
            client.publish(topic_outside_brewery_tilt_blue_gravity, str(gravity))
            client.publish(topic_outside_brewery_tilt_blue_rssi, str(rssi))
            client.publish(topic_outside_brewery_tilt_blue_data, json.dumps(data))
            print("  BLUE topics published successfully!")
        elif color == "green":
            print("  Publishing to GREEN topics...")
            client.publish(topic_outside_brewery_tilt_green_temperature, str(temperature))
            client.publish(topic_outside_brewery_tilt_green_gravity, str(gravity))
            client.publish(topic_outside_brewery_tilt_green_rssi, str(rssi))
            client.publish(topic_outside_brewery_tilt_green_data, json.dumps(data))
            print("  GREEN topics published successfully!")
        else:
            # For other colors, create dynamic topics
            print(f"  Publishing to {color.upper()} dynamic topics...")
            base_topic = f"home/outside/brewery/tilt/{color}"
            client.publish(f"{base_topic}/temperature", str(temperature))
            client.publish(f"{base_topic}/gravity", str(gravity))
            client.publish(f"{base_topic}/rssi", str(rssi))
            client.publish(f"{base_topic}/data", json.dumps(data))
            print(f"  {color.upper()} dynamic topics published successfully!")
        
        print(f"*** SUCCESSFULLY PUBLISHED {color.upper()} TILT DATA ***")
            
    except Exception as e:
        print(f"*** MQTT PUBLISH ERROR for {color}: {e} ***")
        import sys
        sys.print_exception(e)

def ble_irq(event, data):
    """Bluetooth IRQ handler"""
    global ble_last_readings
    
    if event == 5:  # _IRQ_SCAN_RESULT
        addr_type, addr, connectable, rssi, adv_data = data
        addr_str = ubinascii.hexlify(addr).decode()
        
        if DEBUG:
            print(f"BLE Device: {addr_str}, RSSI: {rssi}, Connectable: {connectable}")
            print(f"  Full adv_data: {ubinascii.hexlify(adv_data).decode()}")
        
        # Parse advertisement data
        i = 0
        while i < len(adv_data):
            if i >= len(adv_data):
                break
                
            length = adv_data[i]
            if length == 0 or length > len(adv_data) - i - 1:
                break
                
            ad_type = adv_data[i + 1]
            payload = adv_data[i + 2:i + 1 + length]
            
            if DEBUG:
                print(f"  AD Type: 0x{ad_type:02x}, Length: {length}, Payload: {ubinascii.hexlify(payload).decode()}")
            
            # Look for manufacturer specific data (type 0xFF)
            if ad_type == 0xFF:
                if DEBUG:
                    print(f"  Found manufacturer data, payload length: {len(payload)}")
                
                # The payload should be at least 25 bytes for Tilt iBeacon data
                # Your data shows 26 bytes which is correct (25 + 1 extra byte at end)
                if len(payload) >= 25:
                    result = parse_tilt_data(payload)
                    if result:
                        color, temperature, gravity = result
                        
                        # Avoid duplicate readings
                        current_time = utime.time()
                        last_time = ble_last_readings.get(color, 0)
                        
                        if current_time - last_time > 10:  # Reduced to 10 seconds for testing
                            ble_last_readings[color] = current_time
                            publish_tilt_data(color, temperature, gravity, rssi)
                        else:
                            if DEBUG:
                                print(f"  Skipping {color} - published {current_time - last_time} seconds ago")
                    else:
                        if DEBUG:
                            print(f"  Manufacturer data not recognized as Tilt")
                else:
                    if DEBUG:
                        print(f"  Manufacturer data too short for Tilt: {len(payload)} bytes (need at least 25)")
            
            i += 1 + length
    
    elif event == 6:  # _IRQ_SCAN_COMPLETE
        if DEBUG:
            print("BLE scan complete, restarting scan...")
        # Restart scanning
        if ble:
            ble.gap_scan(0, 10000, 10000)

def init_bluetooth():
    """Initialize Bluetooth with more aggressive scanning"""
    global ble, scanning
    
    ble = ubluetooth.BLE()
    ble.active(True)
    ble.irq(ble_irq)
    
    # Start scanning with more aggressive parameters
    if not scanning:
        if DEBUG:
            print("Starting BLE scan for Tilt devices...")
        # Scan indefinitely, with shorter intervals for better detection
        ble.gap_scan(0, 10000, 10000)  # 10ms intervals instead of 30ms
        scanning = True
    
    if DEBUG:
        print("Bluetooth initialized and scanning started")

def publish_ds18b20_topics():
    """Publish DS18B20 temperature data"""
    global sensor, client
    
    led.value(True)
            
    sensor_dictionary = {
        "Garage_Temperature_F": "NoData",
        "Fermentation_1_Temperature_F": "NoData",
        "Fermentation_2_Temperature_F": "NoData"
    }

    try:
        ds_sensor.convert_temp()
        sleep(1)
        sensor_index = 0
        for sensor_id in sensor:
            if DEBUG:
                print(f"Reading sensor {sensor_index}: {sensor_id}")
            tempF = ds_sensor.read_temp(sensor_id) * (9/5) + 32
            if DEBUG:
                print('temperature (ºF):', "{:.2f}".format(tempF))

            if sensor_index == 0:
                sensor_dictionary["Garage_Temperature_F"] = tempF
            elif sensor_index == 1:
                sensor_dictionary["Fermentation_1_Temperature_F"] = tempF
            elif sensor_index == 2:
                sensor_dictionary["Fermentation_2_Temperature_F"] = tempF
            sensor_index = sensor_index + 1

        led.value(False)
        sleep(1)
        
        if client:
            client.publish(topic_outside_temp_garage, str(sensor_dictionary["Garage_Temperature_F"]))
            client.publish(topic_outside_temp_ferm_1, str(sensor_dictionary["Fermentation_1_Temperature_F"]))
            client.publish(topic_outside_temp_ferm_2, str(sensor_dictionary["Fermentation_2_Temperature_F"]))
            
            if DEBUG:
                print("Published DS18B20 temperatures")
                
    except Exception as e:
        if DEBUG:
            print(f"Error reading DS18B20 sensors: {e}")

def main_loop():
    """Main execution loop"""
    global client
    
    # Initialize Bluetooth scanning
    init_bluetooth()
    
    # Try to connect to the MQTT broker
    try:
        if DEBUG:
            print("Connecting to MQTT client")
        client = mqtt_connect()
    except OSError as e:
        if DEBUG:
            print(f"MQTT connection failed: {e}")
        reconnect()

    # Main loop
    try:
        last_ds_publish = 0
        
        while True:
            current_time = utime.time()
            
            # Keep MQTT connection alive
            if client:
                try:
                    client.check_msg()
                except:
                    if DEBUG:
                        print("MQTT connection lost, reconnecting...")
                    try:
                        client = mqtt_connect()
                    except:
                        reconnect()
            
            # Publish DS18B20 data every PUBLISH_FREQ seconds
            if current_time - last_ds_publish >= PUBLISH_FREQ:
                publish_ds18b20_topics()
                last_ds_publish = current_time
            
            sleep(1)

    except KeyboardInterrupt:
        if DEBUG:
            print("Shutting down...")
        if scanning and ble:
            ble.gap_scan(None)
        if client:
            client.disconnect()
    except OSError as e:
        if DEBUG:
            print(f"Main loop error: {e}")
        reconnect()

# Start the main program
if __name__ == "__main__":
    main_loop()