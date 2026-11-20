"""List or verify capture device identities without opening a CAN channel."""
import argparse
from contextlib import ExitStack
import json

from record_episode import inventory, load_devices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List detected serials without reading local settings.")
    args = parser.parse_args()
    try:
        config = None if args.list else load_devices()
        adapters, cameras = inventory()
        with ExitStack() as stack:
            from usb.util import dispose_resources

            for adapter in adapters.values():
                stack.callback(dispose_resources, adapter)
            if config is None:
                print(json.dumps({"can": sorted(adapters), "cameras": sorted(cameras)}, indent=2))
                return
            missing = []
            for group, present in (("can", adapters), ("cameras", cameras)):
                for role, settings in config[group].items():
                    serial = settings["serial"]
                    print(f"{group}.{role}: {serial} {'PRESENT' if serial in present else 'MISSING'}")
                    if serial not in present:
                        missing.append(f"{group}.{role}")
            if missing:
                raise ValueError("Configured devices missing: " + ", ".join(missing))
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
