"""
感知Agent：接收原始报警 → 解析 → 违规判断 → 标准化事件
支持：安全帽/背心/火焰/车辆/区域入侵/人车接近/复合风险
"""
from . import AlarmEvent, BaseAgent
from datetime import datetime
import math


class PerceptionAgent(BaseAgent):
    """一帧原始检测结果 → 违规事件列表"""

    TYPE_NAMES = {0: "人员", 1: "安全帽", 2: "反光背心", 3: "火焰", 4: "车辆", 5: "头部"}

    # 预定义危险区域 (归一化坐标 0.0-1.0，相对于画面宽高)
    # 左上角为原点，用像素坐标直接定义更方便
    DANGER_ZONES = [
        {"name": "吊装作业区",   "poly": [(100,100),(500,100),(500,400),(100,400)], "level": "A"},
        {"name": "车辆通道",     "poly": [(600,0),(900,0),(900,500),(600,500)],      "level": "A"},
        {"name": "高压设备区",   "poly": [(1200,100),(1600,100),(1600,350),(1200,350)],"level": "A"},
        {"name": "仓储缓冲带",   "poly": [(0,400),(300,400),(300,700),(0,700)],      "level": "B"},
    ]

    # 人车接近阈值 (像素距离)
    PROXIMITY_THRESHOLD = 150

    def __init__(self):
        super().__init__("感知Agent")

    def process(self, alarm_body: dict, img_bytes: bytes = b"") -> AlarmEvent:
        obj_list = alarm_body.get("objInfo", [])
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        events = self._analyze_frame(obj_list)

        # 打印检测目标坐标
        for obj in obj_list:
            tn = self.TYPE_NAMES.get(obj.get("targetType", -1), "?")
            r = obj.get("posRect", {})
            self.log(f"  检测: {tn} | x={r.get('x',0):.0f} y={r.get('y',0):.0f} w={r.get('width',0):.0f} h={r.get('height',0):.0f}")
        self.log(f"收到 {len(obj_list)} 个目标 → {len(events)} 个违规事件")
        for e in events:
            self.log(f"  {e['type']}({e['level']}级) | {e['detail']}")

        return AlarmEvent(
            timestamp=timestamp,
            events=events,
            raw_json=alarm_body,
            image_bytes=img_bytes
        )

    def _in_polygon(self, px, py, poly):
        """射线法判断点是否在多边形内"""
        inside = False
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            if ((y1 > py) != (y2 > py)) and (px < (x2 - x1) * (py - y1) / (y2 - y1) + x1):
                inside = not inside
        return inside

    def _analyze_frame(self, obj_list: list) -> list:
        persons = [o for o in obj_list if o["targetType"] == 0]
        helmets = [o for o in obj_list if o["targetType"] == 1]
        vests   = [o for o in obj_list if o["targetType"] == 2]
        fires   = [o for o in obj_list if o["targetType"] == 3]
        bikes   = [o for o in obj_list if o["targetType"] == 4]
        events = []

        # -- 基础违规：安全帽/背心 --
        for person in persons:
            rect = person["posRect"]
            conf = person["confidence"] / 10.0
            tid = person.get("targetId", 0)
            cx = rect["x"] + rect["width"] / 2
            cy = rect["y"] + rect["height"] / 2

            has_helmet = any(self._in_upper_half(rect, h["posRect"]) for h in helmets)
            has_vest   = any(self._overlap_area(rect, v["posRect"]) > 0.3 for v in vests)

            if not has_helmet:
                events.append({"type": "未戴安全帽", "level": "B",
                               "detail": f"人员未佩戴安全帽，置信度 {conf:.1f}%",
                               "bbox": rect, "targetId": tid})
            if not has_vest:
                events.append({"type": "未穿反光背心", "level": "B",
                               "detail": f"人员未穿反光背心，置信度 {conf:.1f}%",
                               "bbox": rect, "targetId": tid})

            # -- 区域入侵检测 --
            for zone in self.DANGER_ZONES:
                if self._in_polygon(cx, cy, zone["poly"]):
                    events.append({
                        "type": f"区域入侵-{zone['name']}", "level": zone["level"],
                        "detail": f"人员进入{zone['name']}，置信度 {conf:.1f}%",
                        "bbox": rect, "targetId": tid
                    })
                    break  # 只报最严重的那个区域

        # -- 人车接近检测 --
        for person in persons:
            prect = person["posRect"]
            pcx = prect["x"] + prect["width"] / 2
            pcy = prect["y"] + prect["height"] / 2
            for bike in bikes:
                brect = bike["posRect"]
                bcx = brect["x"] + brect["width"] / 2
                bcy = brect["y"] + brect["height"] / 2
                dist = math.sqrt((pcx - bcx) ** 2 + (pcy - bcy) ** 2)
                if dist < self.PROXIMITY_THRESHOLD:
                    events.append({
                        "type": "人车混行风险", "level": "A",
                        "detail": f"人员与车辆距离 {dist:.0f}px，存在碰撞风险",
                        "bbox": prect,
                        "targetId": person.get("targetId", 0)
                    })
                    break  # 每人只报一次

        # -- 火焰 --
        for f in fires:
            conf = f["confidence"] / 10.0
            events.append({"type": "火焰检测", "level": "A",
                           "detail": f"检测到火焰，置信度 {conf:.1f}%", "bbox": f["posRect"]})

        # -- 车辆 --
        for b in bikes:
            conf = b["confidence"] / 10.0
            events.append({"type": "车辆检测", "level": "C",
                           "detail": f"检测到车辆，置信度 {conf:.1f}%", "bbox": b["posRect"]})

        # -- 复合风险：同一人的多种违规合并升级 --
        self._compound_risk(events)

        return events

    def _compound_risk(self, events):
        """同一人的多种违规 → 生成复合风险事件"""
        by_tid = {}
        for e in events:
            tid = e.get("targetId", -1)
            if tid not in by_tid:
                by_tid[tid] = []
            by_tid[tid].append(e)

        for tid, evs in by_tid.items():
            types = [e["type"] for e in evs]
            # 未戴安全帽 + 区域入侵A级 → 升级为复合高危
            if "未戴安全帽" in types and any("区域入侵" in t for t in types):
                evs.append({
                    "type": "复合风险-高危", "level": "A",
                    "detail": "人员未佩戴安全帽且进入危险区域，复合风险升级为A级",
                    "bbox": evs[0]["bbox"], "targetId": tid
                })
            # 未戴安全帽 + 未穿背心 → 复合违规
            if "未戴安全帽" in types and "未穿反光背心" in types:
                evs.append({
                    "type": "复合违规-双重缺失", "level": "B",
                    "detail": "人员同时未佩戴安全帽和反光背心",
                    "bbox": evs[0]["bbox"], "targetId": tid
                })

    @staticmethod
    def _overlap_area(a, b):
        x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
        x2, y2 = min(a["x"] + a["width"], b["x"] + b["width"]), min(a["y"] + a["height"], b["y"] + b["height"])
        if x1 >= x2 or y1 >= y2:
            return 0.0
        return (x2 - x1) * (y2 - y1) / (b["width"] * b["height"]) if b["width"] * b["height"] > 0 else 0.0

    @staticmethod
    def _in_upper_half(person, obj):
        cx, cy = obj["x"] + obj["width"] / 2, obj["y"] + obj["height"] / 2
        return (cx >= person["x"] and cx <= person["x"] + person["width"] and
                cy >= person["y"] and cy <= person["y"] + person["height"] * 0.6)
