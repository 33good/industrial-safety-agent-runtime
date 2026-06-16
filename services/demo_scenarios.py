"""Controlled demo/replay scenarios that reuse the real alarm pipeline."""
import io
from PIL import Image, ImageDraw


def demo_alarm_body(scenario: str) -> dict:
    cases = {
        "a": "a_person_vehicle",
        "a_person_vehicle": "a_person_vehicle",
        "b": "b_ppe",
        "b_ppe": "b_ppe",
        "c": "c_vehicle",
        "c_vehicle": "c_vehicle",
        "fire": "fire",
    }
    scenario = cases.get(str(scenario or "").lower(), "a_person_vehicle")
    if scenario == "b_ppe":
        obj_info = [
            {"targetType": 0, "targetId": 301, "confidence": 94, "posRect": {"x": 360, "y": 520, "width": 90, "height": 210}},
        ]
    elif scenario == "c_vehicle":
        obj_info = [
            {"targetType": 4, "targetId": 401, "confidence": 91, "posRect": {"x": 980, "y": 560, "width": 160, "height": 100}},
        ]
    elif scenario == "fire":
        obj_info = [
            {"targetType": 3, "targetId": 501, "confidence": 96, "posRect": {"x": 1220, "y": 150, "width": 120, "height": 150}},
        ]
    else:
        obj_info = [
            {"targetType": 0, "targetId": 101, "confidence": 95, "posRect": {"x": 650, "y": 120, "width": 90, "height": 210}},
            {"targetType": 4, "targetId": 201, "confidence": 92, "posRect": {"x": 700, "y": 190, "width": 120, "height": 90}},
        ]
    return {"objInfo": obj_info, "demo": True, "scenario": scenario}


def demo_image(alarm_body: dict, scenario: str) -> bytes:
    img = Image.new("RGB", (1600, 900), (20, 29, 31))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 1599, 899], outline=(60, 95, 95), width=2)
    zones = [
        ((600, 0, 900, 500), (255, 92, 101), "VEHICLE CHANNEL"),
        ((100, 100, 500, 400), (255, 183, 74), "LIFTING AREA"),
        ((1200, 100, 1600, 350), (255, 92, 101), "HV EQUIPMENT"),
    ]
    for rect, color, label in zones:
        draw.rectangle(rect, outline=color, width=2)
        draw.text((rect[0] + 8, rect[1] + 8), label, fill=color)
    draw.text((28, 28), f"DEMO REPLAY: {str(scenario).upper()}", fill=(98, 243, 221))
    colors = {0: (98, 243, 221), 3: (255, 92, 101), 4: (255, 183, 74)}
    names = {0: "PERSON", 3: "FIRE", 4: "VEHICLE"}
    for obj in alarm_body.get("objInfo", []):
        r = obj.get("posRect", {})
        x1, y1 = int(r.get("x", 0)), int(r.get("y", 0))
        x2 = x1 + int(r.get("width", 0))
        y2 = y1 + int(r.get("height", 0))
        color = colors.get(obj.get("targetType"), (180, 180, 180))
        draw.rectangle([x1, y1, x2, y2], outline=color, width=4)
        draw.text((x1, max(0, y1 - 18)), names.get(obj.get("targetType"), "OBJ"), fill=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()
