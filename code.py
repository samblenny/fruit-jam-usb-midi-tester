# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright 2025 Sam Blenny
from board import BUTTON1, CKP, CKN, D0P, D0N, D1P, D1N, D2P, D2N
from digitalio import DigitalInOut, Direction, Pull
import bitmaptools
import displayio
from displayio import Bitmap, Group, Palette, TileGrid
import framebufferio
import gc
import picodvi
import supervisor
import sys
from terminalio import FONT
from time import sleep
from usb.core import USBError, USBTimeoutError
import usb_host

from adafruit_display_text import bitmap_label
import adafruit_imageload

from sb_usb_midi import find_usb_device, MIDIInputDevice


def init_display(width, height, color_depth):
    """Initialize the picodvi display
    Video mode compatibility:
    | Video Mode     | Fruit Jam | Metro RP2350 No PSRAM    |
    | -------------- | --------- | ------------------------ |
    | (320, 240,  8) | Yes!      | Yes!                     |
    | (320, 240, 16) | Yes!      | Yes!                     |
    | (320, 240, 32) | Yes!      | MemoryError exception :( |
    | (640, 480,  8) | Yes!      | MemoryError exception :( |
    """
    displayio.release_displays()
    gc.collect()
    fb = picodvi.Framebuffer(width, height, clk_dp=CKP, clk_dn=CKN,
        red_dp=D0P, red_dn=D0N, green_dp=D1P, green_dn=D1N,
        blue_dp=D2P, blue_dn=D2N, color_depth=color_depth)
    display = framebufferio.FramebufferDisplay(fb)
    supervisor.runtime.display = display
    return display

def main():
    # Configure display with requested picodvi video mode
    display = init_display(320, 240, 16)
    display.auto_refresh = False
    grp = Group(scale=1)
    display.root_group = grp

    # Load background image and palette from file
    (bg_bitmap, bg_palette) = adafruit_imageload.load(
        "background.bmp", bitmap=Bitmap, palette=Palette)
    bg_tg = TileGrid(bg_bitmap, pixel_shader=bg_palette)
    grp.append(bg_tg)

    # Text label for input event data
    event = bitmap_label.Label(FONT, text="", color=0xFFFFFF, scale=1)
    event.line_spacing = 1.0
    event.anchor_point = (0, 0)
    event.anchored_position = (16, 160)  # Bottom left rounded rectangle
    grp.append(event)

    # Text label for status messages
    status = bitmap_label.Label(FONT, text="", color=0xFFFFFF, scale=1)
    status.line_spacing = 1.0
    status.anchor_point = (0, 0)
    status.anchored_position = (172, 160)  # Bottom right rounded rectangle
    grp.append(status)

    # Configure button #1 as input to trigger USB bus re-connect
    button_1 = DigitalInOut(BUTTON1)
    button_1.direction = Direction.INPUT
    button_1.pull = Pull.UP

    # Define status label updater with access to local vars from main()
    def set_status(msg, log_it=False):
        status.text = msg
        display.refresh()
        if log_it:
            print(msg)

    prev_b1 = button_1.value
    while True:
        set_status("USB Host\n scanning bus...", log_it=True)
        event.text = ''
        display.refresh()
        gc.collect()
        device_cache = {}
        try:
            # This loop will end as soon as it finds a ScanResult object (r)
            r = None
            while r is None:
                sleep(0.4)
                r = find_usb_device(device_cache)
            # Use ScanResult object to check if USB device descriptor info
            # matches the class/sublclass/protocol pattern for a MIDI device
            dev = MIDIInputDevice(r)
            set_status(
                "USB Host\n MIDI Device\n vid:pid %04X:%04X\n" % (r.vid, r.pid)
            )
            # Collect garbage to hopefully limit heap fragmentation. If we're
            # lucky, this may help to avoid gc pauses during MIDI input loop.
            r = None
            device_cache = {}
            gc.collect()
            # Cache function references (MicroPython performance boost trick)
            fast_wr = sys.stdout.write
            refresh = display.refresh
            # Poll for input until Button #1 pressed or USB error.
            # CAUTION: This loop needs to be as efficient as possible. Any
            # extra work here directly adds time to USB MIDI read latency.
            for data in dev.input_event_generator():
                # Check for falling edge of button press (triggers usb re-scan)
                if not button_1.value:
                    if prev_b1:
                        prev_b1 = False
                        break
                else:
                    prev_b1 = True
                # Handle midi packet (should be None or 4-byte memoryview)
                if data is None:
                    continue
                # Parse packet
                cin = data[0] & 0x0f
                chan = (data[1] & 0x0f) + 1
                if cin == 0x08:
                    # Note off
                    msg = 'ch%d NoteOff %d %d' % (chan, data[2], data[3])
                elif cin == 0x09:
                    # Note on
                    msg = 'ch%d NoteOn  %d %d' % (chan, data[2], data[3])
                elif cin == 0x0b:
                    # CC (control change)
                    msg = 'ch%d CC %d %d' % (chan, data[2], data[3])
                else:
                    msg = ' '.join(['%02x' % b for b in data])
                # Update serial console and picodvi display
                fast_wr('%s\n' % msg)
                event.text = msg
                refresh()
        except USBError as e:
            # This sometimes happens when devices are unplugged. Not always.
            print("USBError: '%s' (device unplugged?)" % e)
            show_scan_msg = True
        except ValueError as e:
            # This can happen if an initialization handshake glitches
            print(e)
            show_scan_msg = True


main()
