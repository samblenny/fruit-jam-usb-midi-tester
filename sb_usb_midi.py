# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright 2025 Sam Blenny
#
# Driver for USB MIDI devices.
#
# NOTE: USB MIDI is CPU intensive. To help keep latency low, this code uses
# performance boosting tricks with a special focus on limiting the amount of
# heap allocations. Related docs:
# - https://docs.python.org/3/glossary.html#term-generator
# - https://docs.python.org/3/glossary.html#term-iterable
# - https://docs.micropython.org/en/latest/reference/speed_python.html
#
import gc
from usb import core
from usb.core import USBError, USBTimeoutError

import sb_usb_descriptor


def find_usb_device(device_cache):
    # Find a usb midi device by inspecting usb device descriptors
    # - device_cache: dictionary of previously checked device descriptors
    # - return: ScanResult object for success or None for failure.
    # Exceptions: may raise usb.core.USBError or usb.core.USBTimeoutError
    #
    for device in core.find(find_all=True):
        # Read descriptors to identify devices by type
        try:
            desc = sb_usb_descriptor.Descriptor(device)
            k = str(desc.to_bytes())
            if k in device_cache:
                return None
            # Remember this device to avoid repeatedly checking it later
            device_cache[k] = True
            # Compare descriptor to expected midi device fingerprint
            desc.read_configuration(device)
            print(desc)
            # Get tuples of class/subclass/protocol for device and interfaces
            d = desc.dev_class_subclass()
            i0 = desc.int_class_subclass(0)
            i1 = desc.int_class_subclass(1)
            if d == (0, 0) and i0 == (1, 1) and i1 == (1, 3):
                print("interface 0 is Audio Control")
                print("interface 1 is MIDI Streaming")
                return ScanResult(device, desc)
            else:
                print("IGNORING UNRECOGNIZED DEVICE")
                return None
        except ValueError as e:
            # This can happen if we get a 0 length device descriptor. Usually
            # it works fine to ignore the error and try again.
            print(e)
        except USBError as e:
            print("find_usb_device() USBError: '%s'" % e)
    return None


class ScanResult:
    def __init__(self, device, descriptor):
        self.device = device
        self.descriptor = descriptor
        self.vid = descriptor.idVendor
        self.pid = descriptor.idProduct
        self.dev_info = descriptor.dev_class_subclass()
        self.int0_info = descriptor.int_class_subclass(0)
        self.int1_info = descriptor.int_class_subclass(1)


class MIDIInputDevice:
    def __init__(self, scan_result):
        # Prepare for reading input events from specified device
        # - scan_result: a ScanResult instance
        # Exceptions: may raise usb.core.USBError
        #
        device = scan_result.device
        self.device = device
        # Make sure CircuitPython core is not claiming the device
        interface = 1
        if device.is_kernel_driver_active(interface):
            print('Detaching interface %d from kernel' % interface)
            device.detach_kernel_driver(interface)
        # Set configuration
        device.set_configuration()
        # Figure out which endpoints to use
        ins = scan_result.descriptor.input_endpoints(interface)
        outs = scan_result.descriptor.output_endpoints(interface)
        endpoint_in  = None if (len(ins) < 1) else ins[0]
        endpoint_out = None if (len(outs) < 1) else outs[0]
        self.int1_endpoint_in = endpoint_in
        self.int1_endpoint_out = endpoint_out

    def input_event_generator(self):
        # Read USB input events _as efficiently as possible_.
        #
        # This is a generator that makes an iterable for reading input events.
        # The code structure here is weird because it's using MicroPython
        # performance boosting tricks to reduce CPU cycles spent on dictionary
        # lookups, function calls, and heap allocations. The goal is to read
        # input fast enough to avoid audible latency glitches.
        #
        # - returns: iterable that can be used with a for loop
        # - iterable can yield:
        #   1. A memoryview(bytearray(...)) with a 4 byte usb midi packet, or
        #   2. None (read timeout, filtered out clock timing packet, etc)
        # Exceptions: may raise USBError
        #
        addr = self.int1_endpoint_in.bEndpointAddress
        max_packet = min(64, self.int1_endpoint_in.wMaxPacketSize)
        data = bytearray(max_packet)
        view = memoryview(data)  # using memoryview reduces heap allocations
        read = self.device.read  # caching function avoids dictionary lookups
        ms = 3                   # read timeout
        while True:
            try:
                # In theory, using a positional argument for the timeout should
                # be faster than using a `timeout=ms` keyword argument
                n = read(addr, data, ms)
                # Bulk read result will be 0 or more 4-byte midi packets, so
                # split that up into 4-byte memoryview slices
                nada = True
                for i in range(0, n, 4):
                    cin = view[i] & 0x0f
                    if cin == 0x0f and (0xf8 <= view[i+1] <= 0xff):
                        nada = False
                        yield view[i:i+4]
                    else:
                        # Allow note, cc, non-realtime system exclusive, etc.
                        nada = False
                        yield view[i:i+4]
                if nada:
                    # Bulk read was 0 bytes long or nothing passed the filter
                    yield None
            except USBTimeoutError as e:
                # This is normal. Timeouts happen fairly often.
                yield None
            except USBError as e:
                # This may happen when device is unplugged (not always though)
                raise e
