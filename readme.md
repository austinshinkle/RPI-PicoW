# Introduction
This repository contains various projects for the RPI Pico W, primarily used for Home Assistant Integration

# Software Setup for RPI Pico W

## 1. Download Micropython firmware to RPI Pico W
Put the device into BOOT mode and copy the correct UF2 file (named something like RPI_PICO_W-20241025-v1.24.0.uf2) onto the system. If this is a new RPI Pico W, it will automatically start in BOOT mode. Additional instructions can be found on the official Raspberry Pi website <https://www.raspberrypi.com/documentation/microcontrollers/micropython.html#drag-and-drop-micropython>

## 2. Install the umqtt.simple micropython library
From Thonny, go to "Tools...Manage packages...", search for "micropython-umqtt.simple" and install it

## 3. Copy main.py onto the target
From Thonny, go to "File...Save as...", select the Raspberry Pi Pico and save the files to the main folder

## Reference Website
https://randomnerdtutorials.com/raspberry-pi-pico-ds18b20-micropython/  
Note: The code needed updated to solve some runtime issues but it was a good place to start.
