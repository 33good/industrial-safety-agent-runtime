"""一键配置盒子报警转发地址（盒子重启后运行一次即可）"""
import urllib.request, json

BOX_IP = "10.53.4.82"
BOX_PORT = 64001
RTSP_URL = "rtsp://admin:HBgkjk%402022@10.53.4.81:554/Streaming/Channels/101"
YOUR_IP = "10.53.4.99"
YOUR_PORT = 5000

data = json.dumps({
    "pipelineName": "pipeline1-1",
    "httpServerInfo": {"address": f"http://{YOUR_IP}:{YOUR_PORT}/alarm", "user": "", "pwd": ""},
    "streamInfos": [{
        "index": 1,
        "url": RTSP_URL,
        "status": "start",
        "playType": "realplay",
        "needAlarmCenter": True,
        "ruleInfo": {"detectRuleCfg": {"detectRegionList": [
            {"x": 0.01, "y": 0.01}, {"x": 0.01, "y": 0.99},
            {"x": 0.99, "y": 0.99}, {"x": 0.99, "y": 0.01}
        ]}},
        "channelList": [],
        "localIOList": []
    }],
    "confidence": 50,
    "interval": 1
}).encode()

url = f"http://{BOX_IP}:{BOX_PORT}/HeopDemo/setParams"
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
resp = urllib.request.urlopen(req, timeout=10)
result = json.loads(resp.read().decode())

if result.get("errorCode") == 0:
    print("OK - Box configured. Alarms -> http://%s:%d/alarm" % (YOUR_IP, YOUR_PORT))
else:
    print("FAIL:", result)
