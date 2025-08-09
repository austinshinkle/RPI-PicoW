# System Imports
import network
import utime
import json 
from time import sleep
from umqtt.simple import MQTTClient
import machine
import ds18x20
import onewire

DEBUG = True

# Deep sleep duration in milliseconds (60 seconds = 60000ms)
SLEEP_DURATION_MS = 60000

# Topic to publish
topic_outside_temp = "home/outside/temperature"

# Wifi settings
ssid = 'Shinkle_2.4ghz'
password = 'C00p3r0805'

# RPI Pico network settings
HOSTNAME = 'ashinkl-rpipw-1'

# MQTT settings
MQTT_SERVER = "10.0.0.152"
MQTT_PORT = 1883
MQTT_USER = "mqtt-user"
MQTT_PASSWORD = "mqtt-user"
CLIENT_ID = "python-mqtt-572-battery"

# Configure LED on the board as an output
led = machine.Pin('LED', machine.Pin.OUT)

def connect_wifi():
    """Connect to WiFi with timeout"""
    if DEBUG:
        print("Connecting to WiFi...")
    
    led.value(True)
    
    wlan = network.WLAN(network.STA_IF)
    network.hostname(HOSTNAME)
    wlan.active(True)
    
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        
        # Wait for connection with timeout
        timeout = 15  # 15 second timeout
        start_time = utime.time()
        
        while not wlan.isconnected() and (utime.time() - start_time) < timeout:
            sleep(0.5)
            if DEBUG:
                print(".", end="")
    
    if wlan.isconnected():
        if DEBUG:
            print(f"\nWiFi connected: {wlan.ifconfig()}")
        led.value(False)
        return wlan
    else:
        if DEBUG:
            print("\nWiFi connection failed!")
        led.value(False)
        return None

def mqtt_connect():
    """Connect to MQTT broker"""
    try:
        client = MQTTClient(
            client_id=CLIENT_ID,
            server=MQTT_SERVER,
            port=MQTT_PORT,
            keepalive=60,
            user=MQTT_USER,
            password=MQTT_PASSWORD
        )
        client.connect()
        if DEBUG:
            print("MQTT connected")
        return client
    except Exception as e:
        if DEBUG:
            print(f"MQTT connection failed: {e}")
        return None

def read_and_publish_temperatures():
    """Read DS18B20 sensors and publish to MQTT"""
    if DEBUG:
        print("Reading temperature sensors...")
    
    # Initialize DS18B20 sensors
    ds_pin = machine.Pin(22)
    ds_sensor = ds18x20.DS18X20(onewire.OneWire(ds_pin))
    
    # Find sensors
    sensors = ds_sensor.scan()
    
    if DEBUG:
        print(f'Found DS devices: {sensors}')
    
    if not sensors:
        if DEBUG:
            print("No DS18B20 sensors found!")
        return False
    
    # Connect to MQTT
    client = mqtt_connect()
    if not client:
        return False
    
    try:
        # Start temperature conversion
        ds_sensor.convert_temp()
        sleep(1)  # Wait for conversion
        
        # Read the sensor and publish (only expecting one sensor)
        sensor_id = sensors[0]  # Get the first (and only) sensor
        try:
            temp_c = ds_sensor.read_temp(sensor_id)
            temp_f = temp_c * (9/5) + 32
            
            if DEBUG:
                print(f'Temperature: {temp_f:.2f}°F')
            
            # Publish to MQTT topic
            client.publish(topic_outside_temp, f"{temp_f:.2f}")
            
        except Exception as e:
            if DEBUG:
                print(f"Error reading sensor: {e}")
        
        # Give MQTT time to send messages
        sleep(1)
        client.disconnect()
        
        if DEBUG:
            print("Temperature readings published successfully")
        return True
        
    except Exception as e:
        if DEBUG:
            print(f"Error in temperature reading/publishing: {e}")
        try:
            client.disconnect()
        except:
            pass
        return False

def go_to_sleep():
    """Enter deep sleep mode"""
    if DEBUG:
        print(f"Going to deep sleep for {SLEEP_DURATION_MS/1000} seconds...")
    
    # Turn off LED
    led.value(False)
    
    # Disable WiFi to save power
    wlan = network.WLAN(network.STA_IF)
    wlan.active(False)
    
    # Configure wake-up timer
    # Note: On Pico W, we use machine.lightsleep() as deepsleep may not work reliably
    # with the WiFi chip. lightsleep still provides significant power savings.
    machine.lightsleep(SLEEP_DURATION_MS)

def main():
    """Main execution function"""
    if DEBUG:
        print("=== Battery Powered Temperature Monitor Starting ===")
        print(f"Will sleep for {SLEEP_DURATION_MS/1000} seconds between readings")
    
    # Flash LED to indicate startup
    for i in range(3):
        led.value(True)
        sleep(0.2)
        led.value(False)
        sleep(0.2)
    
    while True:
        try:
            # Connect to WiFi
            wlan = connect_wifi()
            if not wlan:
                if DEBUG:
                    print("WiFi connection failed, going to sleep...")
                go_to_sleep()
                continue
            
            # Read temperatures and publish to MQTT
            success = read_and_publish_temperatures()
            
            if not success:
                if DEBUG:
                    print("Temperature reading/publishing failed")
            
            # Disconnect WiFi to save power
            wlan.active(False)
            
            # Go to sleep
            go_to_sleep()
            
        except Exception as e:
            if DEBUG:
                print(f"Main loop error: {e}")
            
            # Try to clean up before sleeping
            try:
                wlan = network.WLAN(network.STA_IF)
                wlan.active(False)
            except:
                pass
            
            # Sleep before retrying
            go_to_sleep()

# Start the program
if __name__ == "__main__":
    main()