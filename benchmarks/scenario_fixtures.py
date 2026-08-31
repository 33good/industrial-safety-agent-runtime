"""Synthetic fixtures used only by offline benchmarks and automated tests."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
A_ALARM_IMAGE = PROJECT_ROOT / "data" / "backup" / "huifang" / "a_alarm.png"


def scenario_alarm_body(scenario: str) -> dict:
    aliases = {
        "a": "a_person_vehicle",
        "a_person_vehicle": "a_person_vehicle",
        "b": "b_ppe",
        "b_ppe": "b_ppe",
        "c": "c_vehicle",
        "c_vehicle": "c_vehicle",
        "fire": "fire",
    }
    scenario = aliases.get(str(scenario or "").lower(), "a_person_vehicle")
    if scenario == "b_ppe":
        objects = [{
            "targetType": 0, "targetId": 301, "confidence": 94,
            "posRect": {"x": 360, "y": 520, "width": 90, "height": 210},
        }]
    elif scenario == "c_vehicle":
        objects = [{
            "targetType": 4, "targetId": 401, "confidence": 91,
            "posRect": {"x": 980, "y": 560, "width": 160, "height": 100},
        }]
    elif scenario == "fire":
        objects = [{
            "targetType": 3, "targetId": 501, "confidence": 96,
            "posRect": {"x": 1220, "y": 150, "width": 120, "height": 150},
        }]
    else:
        risk_box = {"x": 440, "y": 180, "width": 300, "height": 270}
        objects = [
            {"targetType": 0, "targetId": 101, "confidence": 95, "posRect": {"x": 628, "y": 306, "width": 45, "height": 112}},
            {"targetType": 1, "targetId": 103, "confidence": 93, "posRect": {"x": 640, "y": 306, "width": 18, "height": 18}},
            {"targetType": 2, "targetId": 102, "confidence": 93, "posRect": {"x": 636, "y": 323, "width": 29, "height": 61}},
            {"targetType": 4, "targetId": 201, "confidence": 92, "posRect": {"x": 456, "y": 226, "width": 132, "height": 166}},
        ]
        return {
            "objInfo": objects, "scenario": scenario,
            "riskBox": risk_box, "focusLevel": "A",
        }
    return {"objInfo": objects, "scenario": scenario}


def scenario_image(alarm_body: dict, scenario: str) -> bytes:
    if scenario == "a_person_vehicle" and A_ALARM_IMAGE.exists():
        return A_ALARM_IMAGE.read_bytes()

    image = Image.new("RGB", (1600, 900), (20, 29, 31))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 1599, 899], outline=(60, 95, 95), width=2)
    zones = [
        ((600, 0, 900, 500), (255, 92, 101), "VEHICLE CHANNEL"),
        ((100, 100, 500, 400), (255, 183, 74), "LIFTING AREA"),
        ((1200, 100, 1600, 350), (255, 92, 101), "HV EQUIPMENT"),
    ]
    for rect, color, label in zones:
        draw.rectangle(rect, outline=color, width=2)
        draw.text((rect[0] + 8, rect[1] + 8), label, fill=color)
    draw.text(
        (28, 28), f"SYNTHETIC BENCHMARK: {str(scenario).upper()}",
        fill=(98, 243, 221),
    )
    colors = {0: (98, 243, 221), 3: (255, 92, 101), 4: (255, 183, 74)}
    names = {0: "PERSON", 3: "FIRE", 4: "VEHICLE"}
    for obj in alarm_body.get("objInfo", []):
        rect = obj.get("posRect", {})
        x1, y1 = int(rect.get("x", 0)), int(rect.get("y", 0))
        x2 = x1 + int(rect.get("width", 0))
        y2 = y1 + int(rect.get("height", 0))
        color = colors.get(obj.get("targetType"), (180, 180, 180))
        draw.rectangle([x1, y1, x2, y2], outline=color, width=4)
        draw.text(
            (x1, max(0, y1 - 18)), names.get(obj.get("targetType"), "OBJ"),
            fill=color,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()
