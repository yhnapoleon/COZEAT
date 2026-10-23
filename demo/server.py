"""网页 Demo 服务：托管 web/dist，/api/chat 代理到 Coze，/api/scripts 给演示模式读录好的脚本。

PAT 和高德 key 只在这里读（infra/.env），不进前端代码；高德 JS key 本来就要下发给浏览器，由 /api/config 给。
只在本机跑（用户 10-22：先不做远程访问），所以没有访问口令和限流；以后开隧道前要先加上。

用法：python -I -X utf8 demo/server.py [--port 8095]
"""
import json
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import requests  # noqa: E402
from coze_chat import chat, load_env  # noqa: E402
from memview import profile  # noqa: E402

DIST = HERE.parent / "web" / "dist"
PROC = HERE.parent / "data" / "processed"
SCRIPTS = HERE / "scripts"
app = FastAPI()


class ChatIn(BaseModel):
    text: str
    user_id: str
    conv: str


@app.get("/api/config")
def config():
    env = load_env()
    return {"amap_js_key": env.get("AMAP_JS_KEY", ""), "amap_security_code": env.get("AMAP_JS_SECURITY_CODE", "")}


@app.post("/api/chat")
def api_chat(body: ChatIn):
    text = body.text.strip()[:300]
    if not text:
        raise HTTPException(400, "空消息")
    return chat(text, body.user_id[:64], body.conv[:64])


@app.get("/api/profile")
def api_profile(uid: str):
    """"我的口味"页：线上记忆表里这个用户的偏好、待确认、反馈、推荐策略（只读）。"""
    return profile(uid)


@app.post("/api/profile/consolidate")
def api_consolidate(uid: str):
    """立即整理一次长期记忆（平时是用户安静 120 秒后由 sidecar 自动跑）。"""
    r = requests.post("http://127.0.0.1:8090/mem/consolidate", params={"uid": uid[:64]},
                      headers={"X-Sidecar-Token": load_env().get("SIDECAR_TOKEN", "")}, timeout=120)
    return {"ok": r.ok, "profile": profile(uid)}


def _load(name: str) -> dict:
    p = PROC / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


@app.get("/api/shop/{aid}/notes")
def shop_notes(aid: str):
    """某家店的评价来源：参考的小红书笔记（可信度、互动、原话、链接）和按维度的笔记口碑（etl/note_quality.py 离线算的）。"""
    q = _load("shop_quality.json").get(aid) or {}
    cred = _load("note_cred.json")
    quotes = {}
    for line in (PROC / "ugc_mentions.jsonl").read_text(encoding="utf-8").splitlines():
        m = json.loads(line)
        if m.get("amap_id") == aid and m.get("resolve") == "local":
            try:
                import ast
                d = ast.literal_eval(m.get("mention") or "{}")
            except (ValueError, SyntaxError):
                d = {}
            quotes.setdefault(m["doc_id"], {"quote": d.get("quote") or "", "cons": d.get("cons") or ""})
    notes = []
    for k in q.get("notes") or []:
        c = cred.get(k)
        if c:
            notes.append({**c, "url": "https://www.xiaohongshu.com/explore/" + c["note_id"], **quotes.get(k, {})})
    notes.sort(key=lambda x: (x["label"] != "真实体验", -int(x.get("ts") or 0)))
    return {"aspects": q.get("aspects") or {}, "ugc_score": q.get("ugc_score"), "cred": q.get("cred"),
            "n_notes": q.get("n_notes") or 0, "n_ad": q.get("n_ad") or 0, "notes": notes}


@app.get("/api/scripts")
def scripts():
    return [{"name": p.stem, "title": json.loads(p.read_text(encoding="utf-8")).get("title") or p.stem}
            for p in sorted(SCRIPTS.glob("*.json"))]


@app.get("/api/scripts/{name}")
def script(name: str):
    p = SCRIPTS / f"{name}.json"
    if not p.is_file() or p.parent != SCRIPTS:
        raise HTTPException(404, "没有这个脚本")
    return json.loads(p.read_text(encoding="utf-8"))


if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}")
def spa(path: str):
    f = DIST / path
    if path and f.is_file() and DIST in f.resolve().parents:
        return FileResponse(f)
    if not (DIST / "index.html").is_file():
        raise HTTPException(404, "前端还没打包：cd web && npm run build")
    return FileResponse(DIST / "index.html")


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8095
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
