#!/usr/bin/python3
import time
import paho.mqtt.client as mqtt
import RPi.GPIO as GPIO

# --- MQTT settings ---
BROKER = "192.168.10.1"       # or your MQTT broker IP
PORT = 1883
TOPIC = "alarm/state"
CLIENT_ID = "alarm_gpio_publisher"
QOS = 1                    # Use QOS 1 for retained state reliability

# --- GPIO settings ---
GPIO_PIN = 21

GPIO.setmode(GPIO.BCM)
GPIO.setup(GPIO_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# --- MQTT setup ---
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, CLIENT_ID, clean_session=False)
client.connect(BROKER, PORT, keepalive=60)

# Publish retained so new subscribers get the last known state
def publish_state(state: str):
    client.publish(TOPIC, payload=state, qos=QOS, retain=True)
    print(f"[MQTT] Published: {TOPIC} = {state}")

# --- main loop ---
def main():
    last_state = None
    print("[GPIO] Monitoring GPIO 21 for alarm state changes...")
    while True:
        gpio_state = GPIO.input(GPIO_PIN)
        state_str = "Disarmed" if gpio_state == GPIO.HIGH else "Armed"
        if state_str != last_state:
            publish_state(state_str)
            last_state = state_str
        client.loop(timeout=1.0)
        time.sleep(1.0)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        GPIO.cleanup()
        client.disconnect()
