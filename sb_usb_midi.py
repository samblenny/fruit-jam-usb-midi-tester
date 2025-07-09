# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Copyright 2025 Sam Blenny
#
# Driver for USB MIDI devices.
#
# NOTE: This code uses performance boosting tricks to avoid bogging down the
# CPU or making a lot of heap allocations. To learn more about caching function
# references, caching instance variables, and making iterators with generator
# functions, check out the links below.
#
# Related docs:
# - https://docs.circuitpython.org/projects/logging/en/latest/api.html
# - https://learn.adafruit.com/a-logger-for-circuitpython/overview
# - https://docs.python.org/3/glossary.html#term-generator
# - https://docs.python.org/3/glossary.html#term-iterable
# - https://docs.micropython.org/en/latest/reference/speed_python.html
#
import binascii
import gc
from micropython import const
from struct import unpack, unpack_from
from supervisor import ticks_ms
from time import sleep
from usb import core
from usb.core import USBError, USBTimeoutError
from usb.util import SPEED_LOW, SPEED_FULL, SPEED_HIGH

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
            d = desc.dev_class_subclass_protocol()
            i0 = desc.class_subclass_protocol(0)
            i1 = desc.class_subclass_protocol(1)
            if d == (0, 0, 0) and i0 == (1, 1, 0) and i1 == (1, 3, 0):
                print("interface 0 is Audio Control")
                print("interface 1 is MIDI Streaming")
                return ScanResult(device, desc)
            else:
                print("IGNORING UNRECOGNIZED DEVICE")
                return None
        except ValueError as e:
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
        self.dev_info = descriptor.dev_class_subclass_protocol()
        self.int0_info = descriptor.class_subclass_protocol(0)
        self.int1_info = descriptor.class_subclass_protocol(1)


def elapsed_ms_generator():
    # Generator function for measuring time intervals efficiently.
    # - returns: an iterator
    # - iterator yields: ms since last call to next(iterator)
    #
    ms = ticks_ms      # caching function ref avoids dictionary lookups
    mask = 0x3fffffff  # (2**29)-1 because ticks_ms rolls over at 2**29
    t0 = ms()
    while True:
        t1 = ms()
        delta = (t1 - t0) & mask  # handle possible timer rollover gracefully
        t0 = t1
        yield delta


class MIDIInputDevice:
    def __init__(self, scan_result):
        # Prepare for reading input events from specified device
        # - scan_result: a ScanResult instance
        # Exceptions: may raise usb.core.USBError
        #
        device = scan_result.device
        self._prev = 0
        self.buf64 = bytearray(64)
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
        # This is a generator that makes an iterable for reading input events.
        # - returns: iterable that can be used with a for loop
        # - yields: (2 possibilities)
        #   1. A memoryview(bytearray(...)) with raw or filtered data from
        #      polling the default endpoint.
        #   2. None in the case of a timeout or rate limit throttle
        # Exceptions: may raise USBError
        #
        if self.device is None:
            return None
        else:
            return self.int1_read_generator()

    def int1_read_generator(self, filter_fn=lambda d: d):
        # Generator function: read from interface 1 and yield raw report data
        # - filter_fn: Optional lambda function to modify raw reports.
        # - yields: memoryview of bytes
        # Exceptions: may raise USBError
        #
        # Meaning of bInterval depends on negotiated speed:
        # - USB 2.0 spec: 5.6.4 Isochronous Transfer Bus Access Constraints
        # - USB 2.0 spec: 9.6.6 Endpoint (table 9-13)
        # - Low-speed: max time between polling requests = bInterval * 1 ms
        # - Full-speed: max time = bInterval * 1 ms
        # - High-speed: max time = math.pow(2, bInterval-1) * 125 µs
        #
        # This implementation alternates between two data buffers so it's
        # possible to compare the previous report with the current report
        # without having to heap allocate a new buffer every time.
        #
        in_addr = self.int1_endpoint_in.bEndpointAddress
        interval = self.int1_endpoint_in.bInterval
        if self.device.speed == SPEED_LOW:
            print('LOW SPEED, period = %d ms' % interval)
        elif self.device.speed == SPEED_FULL:
            print('FULL SPEED, period = %d ms' % interval)
        elif self.device.speed == SPEED_HIGH:
            # Units here are 125 µs or (1 ms)/8. Since timer resolution we have
            # available is 1 ms, quantize the requested interval to 1 ms units
            # (left shift 3 to divide by 8).
            interval = (2 << (interval - 1)) >> 3
            print('HIGH SPEED, period = %d ms' % interval)
        max_packet = min(64, self.int1_endpoint_in.wMaxPacketSize)
        data = bytearray(max_packet)
        view = memoryview(data)  # memoryview reduces heap allocations
        read = self.device.read  # cache function to avoid dictionary lookups

        # Make timer to throttle the polling rate because...
        # 1. Reading USB too much bogs down the system and fights with DVI
        # 2. Waiting too long to read USB will upset some devices
        poll_ms = 0
        poll_dt = elapsed_ms_generator()
        poll_target = (interval * 3) >> 2  # 75% of the max polling interval

        # Polling loop
        while True:
            poll_ms += next(poll_dt)
            if poll_ms < poll_target:
                yield None  # It's too soon to poll now
                continue
            else:
                poll_ms = 0

            # Enough time has passed, so poll endpoint.
            # NOTE: This is using a lambda function provided by the caller to
            # filter the raw data read from the endpoint. The lambda function
            # can return None when the current read should be skipped.
            try:
                    n = read(in_addr, data, timeout=1)
                    yield filter_fn(view[:n])
            except USBTimeoutError as e:
                # This is normal. Timeouts happen fairly often.
                yield None
            except USBError as e:
                # This may happen when device is unplugged (not always though)
                raise e
