"""Capture one labeled RGB preview per RealSense; no robot commands."""
from pathlib import Path
import json
from datetime import datetime, timezone
import numpy as np
from PIL import Image, ImageDraw
import pyrealsense2 as rs

ROOT = Path(__file__).resolve().parents[1]
out = ROOT / 'data' / 'device_identification'
out.mkdir(parents=True, exist_ok=True)
ctx = rs.context()
records, tiles = [], []
for index, dev in enumerate(ctx.query_devices(), 1):
    serial = dev.get_info(rs.camera_info.serial_number)
    rec = {'serial': serial, 'name': dev.get_info(rs.camera_info.name)}
    for field in ['usb_type_descriptor', 'firmware_version', 'physical_port']:
        info = getattr(rs.camera_info, field)
        if dev.supports(info):
            rec[field] = dev.get_info(info)
    pipeline = rs.pipeline(ctx)
    started = False
    try:
        cfg = rs.config()
        cfg.enable_device(serial)
        cfg.enable_stream(rs.stream.color, 640, 480, rs.format.rgb8, 15)
        pipeline.start(cfg)
        started = True
        for _ in range(15):
            frames = pipeline.wait_for_frames(5000)
        frame = frames.get_color_frame()
        image = Image.fromarray(np.asanyarray(frame.get_data()).copy())
        image.save(out / f'{serial}.jpg')
        tile = Image.new('RGB', (640, 520), 'white')
        tile.paste(image, (0, 40))
        ImageDraw.Draw(tile).text((15, 12), f'Camera {index}: {serial}', fill='black')
        tiles.append(tile)
        rec.update(status='captured', image_path=str(out / f'{serial}.jpg'))
    except Exception as exc:
        rec.update(status='error', error=str(exc))
    finally:
        if started:
            pipeline.stop()
    records.append(rec)
    print(json.dumps(rec), flush=True)
if tiles:
    sheet = Image.new('RGB', (640 * len(tiles), 520), 'white')
    for i, tile in enumerate(tiles):
        sheet.paste(tile, (i * 640, 0))
    sheet.save(out / 'camera_contact_sheet.jpg')
report = {'utc': datetime.now(timezone.utc).isoformat(), 'cameras': records,
          'note': 'Sequential RGB-only previews; not simultaneous streaming validation.'}
(out / 'camera_inventory.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
