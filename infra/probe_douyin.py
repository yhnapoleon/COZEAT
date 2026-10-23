"""探测抖音开放平台权限：client_token 能否拿到，视频搜索和 POI 搜索是否有权限。

用法：
  1. 在同目录建 .env，写两行（不要提交，不要贴到聊天里）：
       DOUYIN_CLIENT_KEY=你的 client_key 或小程序 AppID
       DOUYIN_CLIENT_SECRET=对应的 secret
  2. python -I probe_douyin.py

只打印状态码和错误信息，不打印密钥和 token。
"""
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def call(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300]}


def brief(d):
    """只保留错误码和说明，去掉 token。"""
    out = {}
    for k in ("err_no", "err_msg", "error_code", "description", "message", "log_id"):
        if k in d:
            out[k] = d[k]
    inner = d.get("data")
    if isinstance(inner, dict):
        for k in ("error_code", "description", "err_no", "err_msg"):
            if k in inner:
                out["data." + k] = inner[k]
        if "video_list" in json.dumps(inner, ensure_ascii=False):
            out["has_video_list"] = True
        if "pois" in inner:
            out["poi_count"] = len(inner.get("pois") or [])
    return out or {"keys": list(d.keys())[:10]}


def main():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        sys.exit("缺少 infra/.env，见文件头说明")
    env = load_env(path)
    key, secret = env.get("DOUYIN_CLIENT_KEY"), env.get("DOUYIN_CLIENT_SECRET")
    if not key or not secret:
        sys.exit(".env 里缺 DOUYIN_CLIENT_KEY 或 DOUYIN_CLIENT_SECRET")

    st, d = call("POST", "https://open.douyin.com/oauth/client_token/",
                 {"Content-Type": "application/json"},
                 {"client_key": key, "client_secret": secret, "grant_type": "client_credential"})
    token = (d.get("data") or {}).get("access_token")
    print("[1] client_token:", st, json.dumps(brief(d), ensure_ascii=False), "| 拿到 token:", bool(token))
    if not token:
        return

    h = {"access-token": token, "Content-Type": "application/json"}
    q = urllib.parse.urlencode({"keyword": "深圳 火锅", "count": 1, "cursor": 0,
                                "device_id": random.randint(10**15, 10**16 - 1)})
    st, d = call("GET", "https://open.douyin.com/dy_open_api/v1/search/video/?" + q, h)
    print("[2] 视频搜索 aweme.dy.video_search:", st, json.dumps(brief(d), ensure_ascii=False))

    q = urllib.parse.urlencode({"keyword": "火锅", "city": "深圳", "count": 1, "cursor": 0})
    st, d = call("GET", "https://open.douyin.com/poi/search/keyword/?" + q, h)
    print("[3] POI 搜索 poi.search:", st, json.dumps(brief(d), ensure_ascii=False))


if __name__ == "__main__":
    main()
