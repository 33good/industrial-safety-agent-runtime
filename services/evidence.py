"""Evidence image rendering for safety incidents."""
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


COLORS = {
    "未戴安全帽": (255, 50, 50),
    "未穿反光背心": (255, 165, 0),
    "火焰检测": (255, 0, 0),
    "车辆检测": (50, 120, 255),
}


def _load_font():
    for path in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ):
        try:
            return ImageFont.truetype(path, 20)
        except Exception:
            continue
    return None


FONT = _load_font()


def annotate_image(image_bytes: bytes, events: list[dict]) -> bytes:
    """Draw risk evidence boxes while avoiding duplicated A-level labels."""
    # Camera or synthetic benchmark evidence may be RGBA PNG. Convert before JPEG export so
    # annotation never silently falls back to the unmarked source image.
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(image)
    drawn_a_labels = set()

    for event in events:
        rect = event["bbox"]
        x1, y1 = rect["x"], rect["y"]
        x2, y2 = x1 + rect["width"], y1 + rect["height"]
        level = event.get("level", "")
        if level == "A":
            color, line_width = (255, 0, 0), 7
        elif level == "B":
            color, line_width = COLORS.get(event["type"], (255, 165, 0)), 4
        else:
            color, line_width = COLORS.get(event["type"], (50, 120, 255)), 3

        for offset in range(line_width):
            draw.rectangle([x1 - offset, y1 - offset, x2 + offset, y2 + offset], outline=color)

        label_key = (x1, y1, x2, y2) if level == "A" else None
        if label_key in drawn_a_labels:
            continue
        if label_key:
            drawn_a_labels.add(label_key)
        label = f"{event['type']} {level}级"
        text_box = draw.textbbox((0, 0), label, font=FONT) if FONT else (0, 0, len(label) * 12, 20)
        width, height = text_box[2] - text_box[0], text_box[3] - text_box[1]
        label_x = x1 + 2
        label_y = y2 + 6 if level == "A" and y2 + height + 12 < image.height else y1 - height - 6
        label_y = max(0, label_y)
        draw.rectangle([label_x - 3, label_y - 3, label_x + width + 5, label_y + height + 3], fill=color)
        draw.text((label_x, label_y), label, fill=(255, 255, 255), font=FONT)

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def save_evidence(alarm_dir: Path, image_bytes: bytes, prefix: str = "alarm") -> Path:
    from datetime import datetime

    alarm_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    path = alarm_dir / filename
    path.write_bytes(image_bytes)
    return path
