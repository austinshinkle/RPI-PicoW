# System Imports
import network
import utime
import json 
import gc
from time import sleep
from umqtt.simple import MQTTClient
import ubluetooth
import struct
import ubinascii

# I/O Imports + ds18x20
import ds18x20
import machine, onewire

DEBUG = True
VERBOSE_DEBUG = False

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
topic_outside_brewery_status = "home/outside/brewery/system/status"

# Optional fixed sensor-to-role mapping by DS18B20 ROM hex (lowercase).
# Example:
# SENSOR_ROM_ROLE_MAP = {
#     "28ff4c3a7216039d": "Garage_Temperature_F",
#     "28ffa91f7216033b": "Fermentation_1_Temperature_F",
#     "28ffc81172160342": "Fermentation_2_Temperature_F",
# }
SENSOR_ROM_ROLE_MAP = {}

SENSOR_ROLE_TO_TOPIC = {
    "Garage_Temperature_F": topic_outside_temp_garage,
    "Fermentation_1_Temperature_F": topic_outside_temp_ferm_1,
    "Fermentation_2_Temperature_F": topic_outside_temp_ferm_2,
}

FALLBACK_SENSOR_ROLES = (
    "Garage_Temperature_F",
    "Fermentation_1_Temperature_F",
    "Fermentation_2_Temperature_F",
)

# Freq to publish the topics
PUBLISH_FREQ = 15
STATUS_PUBLISH_INTERVAL_S = 60

# Connectivity retry behavior
WIFI_CONNECT_TIMEOUT_S = 15
WIFI_RETRY_BACKOFF_S = (2, 5, 10, 20, 30)
MQTT_RETRY_BACKOFF_S = (2, 5, 10, 20, 30)

# DS18B20 read behavior
DS18B20_TEMP_MIN_F = -40.0
DS18B20_TEMP_MAX_F = 212.0
DS18B20_READ_RETRIES = 2

# Logging and health reporting
BLE_SCAN_DEBUG = False
BLE_ADV_DEBUG = False
HEALTH_LOG_INTERVAL_S = 300
TILT_PARSE_DEBUG = False
TILT_PUBLISH_DEBUG = False

# Watchdog safety (Pico W RP2040 max practical timeout is ~8s)
WATCHDOG_ENABLED = True
WATCHDOG_TIMEOUT_MS = 8000

# Headless boot/reset audit state
RUNTIME_STATE_FILE = "runtime_state.json"
DEVICE_CONFIG_FILE = "config.yaml"
DEVICE_CONFIG_REQUIRED = (
    "wifi_ssid",
    "wifi_password",
    "hostname",
    "mqtt_server",
    "mqtt_port",
    "mqtt_user",
    "mqtt_password",
    "mqtt_keepalive",
    "mqtt_client_id",
)

# Global variables for BLE
ble = None
ble_last_readings = {}
scanning = False
client = None

# BLE event constants and queue settings
_IRQ_SCAN_RESULT = 5
_IRQ_SCAN_COMPLETE = 6
TILT_MIN_PUBLISH_INTERVAL = 10
TILT_QUEUE_MAX_SIZE = 32
# Raw BLE scan queue consumed by main loop (not parsed in IRQ)
tilt_event_queue = []
tilt_events_dropped = 0

# Watchdog runtime state
watchdog = None
watchdog_feed_count = 0
last_reset_cause_name = "unknown"
runtime_state = {
    "boots": 0,
    "pwr_on_resets": 0,
    "hard_resets": 0,
    "wdt_resets": 0,
    "soft_resets": 0,
    "other_resets": 0,
}

# Runtime health counters
wifi_connect_attempts = 0
wifi_connect_failures = 0
mqtt_connect_attempts = 0
mqtt_connect_failures = 0
mqtt_disconnects = 0
ds_read_failures = 0
tilt_events_enqueued = 0
tilt_publishes = 0
boot_time = utime.time()
device_config = {}

# Configure LED on the board as an output
led = machine.Pin('LED', machine.Pin.OUT)
led.value(False)

# Configure WLAN interface
wlan = network.WLAN(network.STA_IF)
wlan.active(True)

# Initialize one wire to be able to read the DS18B20
ds_pin = machine.Pin(22)
ds_sensor = ds18x20.DS18X20(onewire.OneWire(ds_pin))

# Find the current sensors
sensor = ds_sensor.scan()

if DEBUG:
    print('Found DS devices: ', sensor)

# Connects to the Home Assistant MQTT Broker
def _to_bytes(value):
    if isinstance(value, bytes):
        return value
    return str(value).encode()

def _parse_yaml_scalar(raw_value):
    """Parse a simple YAML scalar value (string, int, bool)."""
    value = raw_value.strip()

    if value == "":
        return ""

    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]

    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False

    try:
        # Keep leading-zero values as strings (e.g., IDs) rather than ints.
        if len(value) > 1 and value[0] == "0" and value[1].isdigit():
            return value
        return int(value)
    except Exception:
        return value

def _load_simple_yaml(path):
    """Load a flat key:value YAML file for MicroPython environments."""
    parsed = {}

    with open(path, "r") as f:
        line_number = 0
        for raw_line in f:
            line_number += 1
            line = raw_line.strip()

            if not line or line.startswith("#"):
                continue

            if ":" not in line:
                raise ValueError(f"Invalid YAML at line {line_number}: missing ':'")

            key, value_part = line.split(":", 1)
            key = key.strip()
            value_part = value_part.strip()

            if not key:
                raise ValueError(f"Invalid YAML at line {line_number}: empty key")

            # Remove inline comments for unquoted values.
            if "#" in value_part and not value_part.startswith("'") and not value_part.startswith('"'):
                value_part = value_part.split("#", 1)[0].strip()

            parsed[key] = _parse_yaml_scalar(value_part)

    return parsed

def load_device_config():
    """Load credentialed runtime settings from local YAML config."""
    global device_config

    loaded = {}
    try:
        loaded = _load_simple_yaml(DEVICE_CONFIG_FILE)
    except Exception as e:
        if DEBUG:
            print(f"Failed to read {DEVICE_CONFIG_FILE}: {e}")
        return False

    if not isinstance(loaded, dict):
        if DEBUG:
            print(f"Invalid config format in {DEVICE_CONFIG_FILE} (expected object)")
        return False

    missing = []
    for key in DEVICE_CONFIG_REQUIRED:
        value = loaded.get(key)
        if value is None or value == "":
            missing.append(key)

    if missing:
        if DEBUG:
            print(f"Missing required config keys in {DEVICE_CONFIG_FILE}: {missing}")
        return False

    device_config = loaded

    try:
        device_config["mqtt_port"] = int(device_config["mqtt_port"])
        device_config["mqtt_keepalive"] = int(device_config["mqtt_keepalive"])
    except Exception as e:
        if DEBUG:
            print(f"Invalid numeric config values in {DEVICE_CONFIG_FILE}: {e}")
        return False

    network.hostname(device_config["hostname"])

    if DEBUG:
        print(f"Loaded config from {DEVICE_CONFIG_FILE}")

    return True

def mqtt_connect():
    global client
    client = MQTTClient(client_id=_to_bytes(device_config.get("mqtt_client_id")),
                        server=_to_bytes(device_config.get("mqtt_server")),
                        port=device_config.get("mqtt_port"),
                        keepalive=device_config.get("mqtt_keepalive"),
                        user=_to_bytes(device_config.get("mqtt_user")),
                        password=_to_bytes(device_config.get("mqtt_password"))
                        )
    client.connect()
    if DEBUG:
        print("MQTT connected")
    return client

def connect_wifi_with_timeout(timeout_s=WIFI_CONNECT_TIMEOUT_S):
    """Connect to WiFi with timeout and return True/False."""
    global wifi_connect_attempts, wifi_connect_failures

    if wlan.isconnected():
        return True

    if DEBUG:
        print("Connecting to WiFi...")

    led.value(True)
    try:
        wifi_connect_attempts += 1
        wlan.active(True)

        # Start each attempt from a clean station state.
        try:
            wlan.disconnect()
            sleep(0.2)
        except Exception:
            pass

        wlan.connect(device_config.get("wifi_ssid"), device_config.get("wifi_password"))

        start = utime.time()
        while not wlan.isconnected() and (utime.time() - start) < timeout_s:
            feed_watchdog()
            sleep(0.5)

    except Exception as e:
        if DEBUG:
            print(f"WiFi connect exception: {e}")
        return False
    finally:
        led.value(False)

    if wlan.isconnected():
        if DEBUG:
            print(f"WiFi connected: {wlan.ifconfig()}")
        return True

    if DEBUG:
        print("WiFi connect timed out")
    wifi_connect_failures += 1
    return False

def disconnect_mqtt_client():
    """Best-effort MQTT disconnect and clear client handle."""
    global client, mqtt_disconnects
    if client:
        try:
            client.disconnect()
        except Exception:
            pass
        mqtt_disconnects += 1
    client = None

def log_health_status(now):
    """Periodic health report to aid long-run monitoring and debugging."""
    uptime = now - boot_time
    free_mem = -1
    try:
        gc.collect()
        free_mem = gc.mem_free()
    except Exception:
        pass

    print(
        "HEALTH "
        f"uptime_s={uptime} wifi_ok={wlan.isconnected()} mqtt_ok={client is not None} "
        f"free_mem={free_mem} queue_len={len(tilt_event_queue)} queue_dropped={tilt_events_dropped} "
        f"wifi_attempts={wifi_connect_attempts} wifi_failures={wifi_connect_failures} "
        f"mqtt_attempts={mqtt_connect_attempts} mqtt_failures={mqtt_connect_failures} "
        f"mqtt_disconnects={mqtt_disconnects} ds_read_failures={ds_read_failures} "
        f"tilt_enqueued={tilt_events_enqueued} tilt_publishes={tilt_publishes} "
        f"wdt_enabled={WATCHDOG_ENABLED} wdt_feeds={watchdog_feed_count} "
        f"reset_cause={last_reset_cause_name} boots={runtime_state.get('boots', 0)} "
        f"wdt_resets={runtime_state.get('wdt_resets', 0)}"
    )

def load_runtime_state():
    """Load persisted runtime counters from local storage."""
    try:
        with open(RUNTIME_STATE_FILE, "r") as f:
            loaded = json.loads(f.read())
            if isinstance(loaded, dict):
                runtime_state.update(loaded)
    except Exception:
        # Missing or invalid state file: continue with defaults.
        pass

def save_runtime_state():
    """Persist runtime counters to local storage."""
    try:
        with open(RUNTIME_STATE_FILE, "w") as f:
            f.write(json.dumps(runtime_state))
    except Exception as e:
        if DEBUG:
            print(f"Failed to save runtime state: {e}")

def track_boot_and_reset_cause():
    """Capture reset cause and persist boot counters for headless diagnostics."""
    global last_reset_cause_name

    load_runtime_state()
    runtime_state["boots"] = runtime_state.get("boots", 0) + 1

    cause = machine.reset_cause()
    pwr_on = getattr(machine, "PWRON_RESET", -1)
    hard = getattr(machine, "HARD_RESET", -1)
    wdt = getattr(machine, "WDT_RESET", -1)
    soft = getattr(machine, "SOFT_RESET", -1)

    if cause == pwr_on:
        last_reset_cause_name = "PWRON_RESET"
        runtime_state["pwr_on_resets"] = runtime_state.get("pwr_on_resets", 0) + 1
    elif cause == hard:
        last_reset_cause_name = "HARD_RESET"
        runtime_state["hard_resets"] = runtime_state.get("hard_resets", 0) + 1
    elif cause == wdt:
        last_reset_cause_name = "WDT_RESET"
        runtime_state["wdt_resets"] = runtime_state.get("wdt_resets", 0) + 1
    elif cause == soft:
        last_reset_cause_name = "SOFT_RESET"
        runtime_state["soft_resets"] = runtime_state.get("soft_resets", 0) + 1
    else:
        last_reset_cause_name = f"OTHER({cause})"
        runtime_state["other_resets"] = runtime_state.get("other_resets", 0) + 1

    save_runtime_state()

    if DEBUG:
        print(
            f"Boot #{runtime_state.get('boots', 0)} "
            f"reset_cause={last_reset_cause_name} "
            f"wdt_resets={runtime_state.get('wdt_resets', 0)}"
        )

def init_watchdog():
    """Initialize watchdog so hard stalls self-recover via reset."""
    global watchdog

    if not WATCHDOG_ENABLED:
        return

    try:
        watchdog = machine.WDT(timeout=WATCHDOG_TIMEOUT_MS)
        if DEBUG:
            print(f"Watchdog enabled (timeout={WATCHDOG_TIMEOUT_MS}ms)")
    except Exception as e:
        watchdog = None
        if DEBUG:
            print(f"Watchdog init failed: {e}")

def feed_watchdog():
    """Feed watchdog heartbeat in long-running healthy paths."""
    global watchdog_feed_count

    if watchdog is None:
        return

    try:
        watchdog.feed()
        watchdog_feed_count += 1
    except Exception:
        # If feeding fails, let watchdog policy trigger reset if needed.
        pass

def publish_with_reconnect(topic, payload):
    """Publish a single MQTT payload and drop client on failure."""
    if not client:
        return False

    try:
        client.publish(topic, payload)
        return True
    except Exception as e:
        if DEBUG:
            print(f"MQTT publish failed for {topic}: {e}")
        disconnect_mqtt_client()
        return False

def build_status_payload(now):
    """Build a compact JSON payload for headless status monitoring."""
    return {
        "uptime_s": now - boot_time,
        "wifi_ok": wlan.isconnected(),
        "mqtt_ok": client is not None,
        "free_mem": gc.mem_free(),
        "queue_len": len(tilt_event_queue),
        "queue_dropped": tilt_events_dropped,
        "boots": runtime_state.get("boots", 0),
        "reset_cause": last_reset_cause_name,
        "wdt_resets": runtime_state.get("wdt_resets", 0),
        "wifi_failures": wifi_connect_failures,
        "mqtt_failures": mqtt_connect_failures,
        "mqtt_disconnects": mqtt_disconnects,
        "ds_read_failures": ds_read_failures,
    }

def publish_status_heartbeat(now):
    """Publish status heartbeat to MQTT for remote liveness checks."""
    payload = json.dumps(build_status_payload(now))
    return publish_with_reconnect(topic_outside_brewery_status, payload)

def rom_to_hex(rom_bytes):
    """Convert DS18B20 ROM bytes to lowercase hex string."""
    return ubinascii.hexlify(rom_bytes).decode()

def read_temp_f_with_retry(sensor_id):
    """Read DS18B20 temperature in Fahrenheit with retry and range checks."""
    for _ in range(DS18B20_READ_RETRIES):
        temp_c = ds_sensor.read_temp(sensor_id)
        if temp_c is None:
            continue

        temp_f = temp_c * (9 / 5) + 32
        if DS18B20_TEMP_MIN_F <= temp_f <= DS18B20_TEMP_MAX_F:
            return temp_f

    return None

def assign_sensor_roles(sensor_ids):
    """Assign sensor roles by ROM map, then deterministic fallback order."""
    assigned = {}
    unassigned = []

    for sensor_id in sensor_ids:
        rom_hex = rom_to_hex(sensor_id)
        role = SENSOR_ROM_ROLE_MAP.get(rom_hex)

        if role in SENSOR_ROLE_TO_TOPIC and role not in assigned:
            assigned[role] = sensor_id
        else:
            unassigned.append(sensor_id)

    for role in FALLBACK_SENSOR_ROLES:
        if role in assigned:
            continue
        if not unassigned:
            break
        assigned[role] = unassigned.pop(0)

    return assigned

def parse_tilt_data(manufacturer_data):
    """Parse Tilt hydrometer data from manufacturer data"""
    if len(manufacturer_data) < 25:
        if DEBUG:
            print(f"  Manufacturer data too short: {len(manufacturer_data)} bytes (need at least 25)")
        return None
        
    try:
        # Parse the complete payload for debugging
        payload_hex = ubinascii.hexlify(manufacturer_data).decode()
        if VERBOSE_DEBUG:
            print(f"  Full manufacturer payload: {payload_hex} (length: {len(manufacturer_data)})")
        
        # Company ID is in LITTLE ENDIAN format in BLE manufacturer data
        company_id = struct.unpack('<H', manufacturer_data[0:2])[0]
        
        if VERBOSE_DEBUG:
            print(f"  Company ID: 0x{company_id:04x}")
        
        # Check for Apple company ID (0x004C)
        if company_id == 0x004C:
            # Check if we have enough data for iBeacon
            if len(manufacturer_data) >= 25:
                # iBeacon type field is in BIG ENDIAN
                beacon_type = struct.unpack('>H', manufacturer_data[2:4])[0]
                
                if VERBOSE_DEBUG:
                    print(f"  Beacon type: 0x{beacon_type:04x}")
                
                # Check for iBeacon type (0x0215)
                if beacon_type == 0x0215:
                    uuid = manufacturer_data[4:20]
                    major = struct.unpack('>H', manufacturer_data[20:22])[0]
                    minor = struct.unpack('>H', manufacturer_data[22:24])[0]
                    
                    uuid_hex = ubinascii.hexlify(uuid).decode()
                    
                    if DEBUG and TILT_PARSE_DEBUG:
                        print(f"  iBeacon UUID: {uuid_hex}")
                        print(f"  Major (temp): {major}")
                        print(f"  Minor (gravity): {minor}")
                        print(f"  Checking against known Tilt UUIDs...")
                    
                    # Check if this is a Tilt UUID using hex string comparison
                    color = TILT_COLORS.get(uuid_hex)
                    if color:
                        temperature = major  # Temperature in Fahrenheit
                        gravity = minor / 1000.0  # Specific gravity
                        if DEBUG and TILT_PARSE_DEBUG:
                            print(f"  *** FOUND TILT {color.upper()}: {temperature}°F, SG: {gravity} ***")
                        return color, temperature, gravity
                    else:
                        if DEBUG and VERBOSE_DEBUG:
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
                    if VERBOSE_DEBUG:
                        print(f"  Not iBeacon format (expected 0x0215, got 0x{beacon_type:04x})")
            else:
                if VERBOSE_DEBUG:
                    print(f"  Apple device but insufficient data for iBeacon: {len(manufacturer_data)} bytes")
        else:
            if VERBOSE_DEBUG:
                print(f"  Not Apple device (Company ID: 0x{company_id:04x}, expected 0x004C)")
                    
    except Exception as e:
        if VERBOSE_DEBUG:
            print(f"Error parsing Tilt data: {e}")
            import sys
            sys.print_exception(e)
        
    return None

def publish_tilt_data(color, temperature, gravity, rssi):
    """Publish Tilt data to MQTT"""
    global client, tilt_publishes
    
    if DEBUG and TILT_PUBLISH_DEBUG:
        print(f"Publishing Tilt {color}: temp_f={temperature} gravity={gravity} rssi={rssi}")
    
    if not client:
        if DEBUG:
            print("No MQTT client available for Tilt publish")
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
        
        if DEBUG and VERBOSE_DEBUG:
            print(f"Tilt data payload: {data}")
        
        # Publish based on color
        if color == "blue":
            client.publish(topic_outside_brewery_tilt_blue_temperature, str(temperature))
            client.publish(topic_outside_brewery_tilt_blue_gravity, str(gravity))
            client.publish(topic_outside_brewery_tilt_blue_rssi, str(rssi))
            client.publish(topic_outside_brewery_tilt_blue_data, json.dumps(data))
        elif color == "green":
            client.publish(topic_outside_brewery_tilt_green_temperature, str(temperature))
            client.publish(topic_outside_brewery_tilt_green_gravity, str(gravity))
            client.publish(topic_outside_brewery_tilt_green_rssi, str(rssi))
            client.publish(topic_outside_brewery_tilt_green_data, json.dumps(data))
        else:
            # For other colors, create dynamic topics
            base_topic = f"home/outside/brewery/tilt/{color}"
            client.publish(f"{base_topic}/temperature", str(temperature))
            client.publish(f"{base_topic}/gravity", str(gravity))
            client.publish(f"{base_topic}/rssi", str(rssi))
            client.publish(f"{base_topic}/data", json.dumps(data))

        if DEBUG and TILT_PUBLISH_DEBUG:
            print(f"Tilt publish complete for {color}")
        tilt_publishes += 1
            
    except Exception as e:
        print(f"*** MQTT PUBLISH ERROR for {color}: {e} ***")
        import sys
        sys.print_exception(e)
        disconnect_mqtt_client()

def enqueue_scan_result(rssi, adv_data):
    """Store raw BLE scan results from IRQ for deferred processing."""
    global tilt_events_dropped, tilt_events_enqueued

    # Keep queue bounded. Drop newest when full to keep IRQ work minimal.
    if len(tilt_event_queue) >= TILT_QUEUE_MAX_SIZE:
        tilt_events_dropped += 1
        return

    # Copy adv_data so processing outside IRQ sees stable bytes.
    tilt_event_queue.append((rssi, bytes(adv_data), utime.time()))
    tilt_events_enqueued += 1

def dequeue_tilt_event():
    """Atomically pop one Tilt event from the queue."""
    irq_state = machine.disable_irq()
    try:
        if tilt_event_queue:
            return tilt_event_queue.pop(0)
        return None
    finally:
        machine.enable_irq(irq_state)

def process_tilt_event_queue():
    """Process queued raw BLE results and publish from non-IRQ context."""
    global ble_last_readings

    # Prevent BLE storms from starving the rest of the loop.
    processed = 0
    max_events_per_cycle = 5

    while processed < max_events_per_cycle:
        event = dequeue_tilt_event()
        if not event:
            break

        rssi, adv_data, event_time = event

        # Parse advertisement data outside IRQ context.
        i = 0
        while i < len(adv_data):
            length = adv_data[i]
            if length == 0 or length > len(adv_data) - i - 1:
                break

            ad_type = adv_data[i + 1]
            payload = adv_data[i + 2:i + 1 + length]

            # Look for manufacturer specific data (0xFF), then parse Tilt payload.
            if ad_type == 0xFF and len(payload) >= 25:
                result = parse_tilt_data(payload)
                if result:
                    color, temperature, gravity = result
                    last_time = ble_last_readings.get(color, 0)

                    if event_time - last_time > TILT_MIN_PUBLISH_INTERVAL:
                        ble_last_readings[color] = event_time
                        publish_tilt_data(color, temperature, gravity, rssi)
                    elif DEBUG and VERBOSE_DEBUG:
                        print(f"  Skipping {color} - published {event_time - last_time} seconds ago")

            i += 1 + length

        processed += 1

def ble_irq(event, data):
    """Bluetooth IRQ handler (keep this path lightweight)."""
    
    if event == _IRQ_SCAN_RESULT:
        _, _, _, rssi, adv_data = data
        enqueue_scan_result(rssi, adv_data)
    
    elif event == _IRQ_SCAN_COMPLETE:
        if DEBUG and VERBOSE_DEBUG:
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
    global sensor, client, ds_read_failures
    
    led.value(True)
            
    sensor_dictionary = {
        "Garage_Temperature_F": "NoData",
        "Fermentation_1_Temperature_F": "NoData",
        "Fermentation_2_Temperature_F": "NoData"
    }

    try:
        # Rescan each cycle so disconnect/reconnect is handled automatically.
        sensor = sorted(ds_sensor.scan())
        if DEBUG:
            print('Found DS devices: ', sensor)

        if not sensor:
            if DEBUG:
                print("No DS18B20 sensors found")
        else:
            ds_sensor.convert_temp()
            sleep(1)

            role_assignments = assign_sensor_roles(sensor)
            for role, sensor_id in role_assignments.items():
                tempF = read_temp_f_with_retry(sensor_id)
                if tempF is None:
                    ds_read_failures += 1
                    if DEBUG:
                        print(f"Invalid temperature from {role} ({rom_to_hex(sensor_id)})")
                    continue

                sensor_dictionary[role] = tempF
                if DEBUG:
                    print(f"{role} ({rom_to_hex(sensor_id)}): {tempF:.2f} F")

        led.value(False)
        sleep(1)
        
        if client:
            for role, topic in SENSOR_ROLE_TO_TOPIC.items():
                publish_with_reconnect(topic, str(sensor_dictionary[role]))
            
            if DEBUG:
                print("Published DS18B20 temperatures")
                
    except Exception as e:
        if DEBUG:
            print(f"Error reading DS18B20 sensors: {e}")
        disconnect_mqtt_client()

def main_loop():
    """Main execution loop"""
    global client, mqtt_connect_attempts, mqtt_connect_failures
    
    # Initialize Bluetooth scanning
    if not load_device_config():
        # Config is required for reliable headless operation.
        # Retry periodically so the device can recover after config is updated.
        if DEBUG:
            print(f"Configuration invalid or missing in {DEVICE_CONFIG_FILE}; retrying every 30s")
        while True:
            if load_device_config():
                break
            sleep(30)

    track_boot_and_reset_cause()
    init_bluetooth()
    init_watchdog()
    
    # Main loop
    try:
        last_ds_publish = 0
        wifi_retry_index = 0
        mqtt_retry_index = 0
        next_wifi_retry_at = 0
        next_mqtt_retry_at = 0
        next_health_log_at = 0
        next_status_publish_at = 0
        
        while True:
            current_time = utime.time()
            feed_watchdog()

            # Publish queued Tilt readings outside IRQ context.
            process_tilt_event_queue()

            # Emit periodic health snapshot for long-run observability.
            if current_time >= next_health_log_at:
                log_health_status(current_time)
                next_health_log_at = current_time + HEALTH_LOG_INTERVAL_S

            # Keep WiFi connected with bounded retry/backoff.
            if not wlan.isconnected() and current_time >= next_wifi_retry_at:
                if connect_wifi_with_timeout():
                    wifi_retry_index = 0
                    next_wifi_retry_at = current_time
                    # Force immediate MQTT reconnect once WiFi recovers.
                    next_mqtt_retry_at = current_time
                else:
                    delay = WIFI_RETRY_BACKOFF_S[min(wifi_retry_index, len(WIFI_RETRY_BACKOFF_S) - 1)]
                    wifi_retry_index += 1
                    next_wifi_retry_at = current_time + delay
                    if DEBUG:
                        print(f"WiFi retry in {delay}s")

            # Keep MQTT connected with bounded retry/backoff.
            if wlan.isconnected() and client is None and current_time >= next_mqtt_retry_at:
                try:
                    mqtt_connect_attempts += 1
                    if DEBUG:
                        print("Connecting to MQTT client")
                    client = mqtt_connect()
                    mqtt_retry_index = 0
                    next_mqtt_retry_at = current_time
                except Exception as e:
                    mqtt_connect_failures += 1
                    disconnect_mqtt_client()
                    delay = MQTT_RETRY_BACKOFF_S[min(mqtt_retry_index, len(MQTT_RETRY_BACKOFF_S) - 1)]
                    mqtt_retry_index += 1
                    next_mqtt_retry_at = current_time + delay
                    if DEBUG:
                        print(f"MQTT connect failed: {e}")
                        print(f"MQTT retry in {delay}s")
            
            # Keep MQTT connection alive
            if client:
                try:
                    client.check_msg()
                except Exception as e:
                    if DEBUG:
                        print(f"MQTT connection lost: {e}")
                    disconnect_mqtt_client()
                    # Trigger reconnect quickly after link loss.
                    next_mqtt_retry_at = current_time + MQTT_RETRY_BACKOFF_S[0]

            # Publish low-frequency heartbeat to MQTT for headless monitoring.
            if client and current_time >= next_status_publish_at:
                publish_status_heartbeat(current_time)
                next_status_publish_at = current_time + STATUS_PUBLISH_INTERVAL_S
            
            # Publish DS18B20 data every PUBLISH_FREQ seconds
            if current_time - last_ds_publish >= PUBLISH_FREQ:
                publish_ds18b20_topics()
                last_ds_publish = current_time
            
            feed_watchdog()
            sleep(1)

    except KeyboardInterrupt:
        if DEBUG:
            print("Shutting down...")
        if scanning and ble:
            ble.gap_scan(None)
        disconnect_mqtt_client()
    except Exception as e:
        if DEBUG:
            print(f"Main loop error: {e}")
        disconnect_mqtt_client()

# Start the main program
if __name__ == "__main__":
    main_loop()