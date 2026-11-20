"""Open a candleLight CAN channel without resetting the Windows USB device."""
from contextlib import contextmanager


@contextmanager
def open_can_channel(raw, settings, *, listen_only, close_device=True):
    from gs_usb.constants import GS_CAN_MODE_HW_TIMESTAMP, GS_CAN_MODE_LISTEN_ONLY
    from gs_usb.gs_usb import GS_CAN_MODE_RESET, GS_CAN_MODE_START, GsUsb, _GS_USB_BREQ_MODE
    from gs_usb.gs_usb_structures import DeviceMode
    import usb.util

    adapter = GsUsb(raw)
    started = False
    try:
        supported = adapter.device_capability.feature
        if listen_only and not supported & GS_CAN_MODE_LISTEN_ONLY:
            raise RuntimeError("Adapter lacks hardware listen-only mode")
        flags = supported & GS_CAN_MODE_HW_TIMESTAMP
        if listen_only:
            flags |= GS_CAN_MODE_LISTEN_ONLY

        def configure_mode(mode, mode_flags):
            data = DeviceMode(mode, mode_flags).pack()
            written = raw.ctrl_transfer(0x41, _GS_USB_BREQ_MODE, 0, 0, data)
            if written != len(data):
                raise RuntimeError("USB-CAN did not accept the complete channel mode request")

        # Reset only the CAN channel. GsUsb.start() resets the USB device itself.
        configure_mode(GS_CAN_MODE_RESET, 0)
        if not adapter.set_bitrate(settings["bitrate"]):
            raise RuntimeError("USB-CAN adapter rejected configured bitrate")
        configure_mode(GS_CAN_MODE_START, flags)
        started = True
        adapter.device_flags = flags
        yield adapter, bool(flags & GS_CAN_MODE_HW_TIMESTAMP)
    finally:
        try:
            if started:
                adapter.stop()
        finally:
            if close_device:
                usb.util.dispose_resources(raw)
