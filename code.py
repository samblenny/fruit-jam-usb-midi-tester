# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright 2025 Sam Blenny
from board import BUTTON1, CKP, CKN, D0P, D0N, D1P, D1N, D2P, D2N
from digitalio import DigitalInOut, Direction, Pull
import bitmaptools
import displayio
from displayio import Bitmap, Group, Palette, TileGrid
import framebufferio
import gc
from micropython import const
import picodvi
import supervisor
import sys
from terminalio import FONT
from time import sleep
from usb.core import USBError, USBTimeoutError
import usb_host
import usb_midi

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

    # Nested function to visualize note on/off event
    # - chan: midi channel in 1-16
    # - num: note number in range 21-108 (midi range of full size piano)
    # - note_on: True for note-on event, False for note-off event
    def visualize(chan, num, note_on):
        if not ((1 <= chan <= 16) and (21 <= num <= 108)):
            return
        # Calculate coordinates for a rectangle in the background image's grid
        # of channels and notes. These formulas come from measuring pixels
        # in an image editor (dot grid spacing is 3px per note, 6px per chan)
        x1 = 28 + (3 * (num - 21))
        y1 = 16 + (6 * (chan - 1))
        color_val = 2 if note_on else 0
        bitmaptools.fill_region(bg_bitmap, x1, y1, x1+2, y1+5, color_val)

    # Nested function to update status label text
    def set_status(msg, log_it=False):
        status.text = msg
        display.refresh()
        if log_it:
            print(msg)

    # Main loop: scan for usb MIDI device, connect, handle input events
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
            # matches the class/subclass/protocol pattern for a MIDI device
            dev = MIDIInputDevice(r)
            set_status(
                "USB Host\n MIDI Device\n vid:pid %04X:%04X\n" % (r.vid, r.pid)
            )
            # Collect garbage to hopefully limit heap fragmentation. If we're
            # lucky, this may help to avoid gc pauses during MIDI input loop.
            r = None
            device_cache = {}
            gc.collect()
            # Cache fn and obj references (MicroPython performance boost trick)
            fast_wr = sys.stdout.write
            refresh = display.refresh
            port_out = None
            for p in usb_midi.ports:
                if isinstance(p, usb_midi.PortOut):
                    port_out = p
                    break
            # Poll for input until Button #1 pressed or USB error.
            # CAUTION: This loop needs to be as efficient as possible. Any
            # extra work here directly adds time to USB MIDI read latency.
            # The pp_skip and cp_skip variables help with thinning out channel
            # and polyphonic key pressure (aftertouch) messages.
            SKIP = const(6)
            pp_skip = SKIP
            cp_skip = SKIP
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

                # Begin Parsing Packet
                # NOTE: The & 0x0f below is a bitwise logical operation for
                # masking off the CN (Cable Number) bits that indicate which
                # midi port the message arrived from. Ignoring the cable number
                # lets us filter more efficiently.
                #     For example, on my BeatStep Pro, regular sequencer notes
                # and CC happen on CN==0, while CN==1 has "MCU" (Mackie
                # Control) messages. When set for "Control Mode" with the
                # "MCU/HUI" knob sub-mode, the BSP sends relative knob turn
                # amount events for use with DAW software. Ignoring CN lets us
                # merge all the midi input streams to filter more efficiently.
                cin = data[0] & 0x0f

                # Filter out all System Real-Time messages. Sequencer playback
                # commonly sends start/stop messages along with _many_ timing
                # clocks. Dropping real-time messages conserves CPU to spend on
                # handling note and cc messages.
                if cin == 0x0f and (0xf8 <= data[1] <= 0xff):
                    continue

                # Handle notes, cc, aftertouch, pitchbend, etc.
                chan = (data[1] & 0x0f) + 1
                num = data[2]    # note or control number
                if cin == 0x08:
                    # Note off
                    msg = 'Off %d %d %d\n' % (chan, num, data[3])
                    visualize(chan, num, False)        # visualize in note grid
                elif cin == 0x09:
                    # Note on
                    msg = 'On  %d %d %d\n' % (chan, num, data[3])
                    visualize(chan, num, True)         # visualize in note grid
                elif cin == 0x0a:
                    # Polyphonic key pressure (aftertouch)
                    if pp_skip > 0:
                        # Ignore some of the polyphonic pressure messages
                        # because processing them all can destroy our latency
                        pp_skip -= 1
                        continue
                    pp_skip = SKIP
                    msg = 'PP  %d %d %d\n' % (chan, num, data[3])
                elif cin == 0x0b:
                    # CC (control change)
                    msg = 'CC  %d %d %d\n' % (chan, num, data[3])
                elif cin == 0x0d:
                    # Channel key pressure (aftertouch)
                    if cp_skip > 0:
                        # Ignore some of the channel pressure messages
                        # because processing them all can destroy our latency
                        cp_skip -= 1
                        continue
                    cp_skip = SKIP
                    msg = 'CP  %d %d\n' % (chan, num)
                elif cin == 0x0e:
                    # Pitch bend
                    msg = 'PB  %d %d %d\n' % (chan, num, data[3])
                else:
                    # Hexdump other messages: SysEx or whatever
                    msg = '%02x %02x %02x %02x\n' % tuple(data)
                # Echo message upstream to host computer (usb midi device)
                if port_out:
                    port_out.write(data)
                # Send message to serial console
                fast_wr(msg)
                # Visualize non-note messages in text box
                if cin != 0x08 and cin != 0x09:
                    event.text = msg
                # Draw the picodvi updates
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
