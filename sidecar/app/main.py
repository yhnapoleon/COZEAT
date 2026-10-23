"""sidecar：决策代理（接口格式与 Jev 一致，当前只有 plain 后端）+ 地理编码 + 长期记忆整理的触发 + 健康检查。

后续在这里加：Jev / logprob 后端、网页 Demo 的对话代理。检索和排序不放这里。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://litellm:4000/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
DECIDER_MODEL = os.environ.get("DECIDER_MODEL", "deepseek-flash")
SIDECAR_TOKEN = os.environ.get("SIDECAR_TOKEN", "")
DB_PATH = Path(os.environ.get("SIDECAR_DB", "/data/decide.sqlite"))

app = FastAPI(title="lifeagent-sidecar", version="0.1.0")


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""create table if not exists decide_log(
        ts real, point text, backend text, latency_ms integer, request text, response text, error text)""")
    return con


class Question(BaseModel):
    type: str                      # choice | score | noul
    instructions: str
    criteria: dict[str, str] | None = None   # choice: 选项→说明；score: 档位→说明


class DecideRequest(BaseModel):
    point: str = "unnamed"
    state: Any
    questions: dict[str, Question]
    backend: str | None = None     # 预留：jev / logprob / plain


def _auth(authorization: str | None, x_sidecar_token: str | None):
    if not SIDECAR_TOKEN:
        return
    got = x_sidecar_token or (authorization or "").removeprefix("Bearer ").strip()
    if got != SIDECAR_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")


def _prompt(req: DecideRequest) -> str:
    lines = ["你是一个只做判断的决策模块。根据【状态】回答每个问题，只输出一个 JSON 对象，不要输出别的文字。",
             "", "【状态】", json.dumps(req.state, ensure_ascii=False) if not isinstance(req.state, str) else req.state,
             "", "【问题】"]
    for qid, q in req.questions.items():
        if q.type == "choice":
            opts = "; ".join(f"{k}: {v}" for k, v in (q.criteria or {}).items())
            lines.append(f'- 问题 "{qid}"，类型 choice：{q.instructions}。选项：{opts}。输出 {{"choice": 选项键, "probabilities": {{选项键: 0-1}}}}')
        elif q.type == "score":
            levels = "; ".join(f"{k}: {v}" for k, v in (q.criteria or {}).items())
            lines.append(f'- 问题 "{qid}"，类型 score：{q.instructions}。档位：{levels}。输出 {{"score": 0-1 之间的小数}}')
        else:
            lines.append(f'- 问题 "{qid}"，类型 noul：{q.instructions}。输出 {{"noul": 为真的概率 0-1}}')
    ids = ", ".join(f'"{q}"' for q in req.questions)
    lines += ["", f'整体格式：{{"answers": {{问题 id: 上面对应的对象}}}}，问题 id 只用这几个：{ids}']
    return "\n".join(lines)


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no json in model output")
    return json.loads(m.group(0))


async def _plain(req: DecideRequest) -> dict:
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(f"{LLM_BASE_URL}/chat/completions",
                           headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                           json={"model": DECIDER_MODEL, "max_tokens": 4096,
                                 "messages": [{"role": "user", "content": _prompt(req)}]})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"] or ""
    raw = _parse(text).get("answers", {})
    # 模型偶尔把提示里的类型说明带进键名（DeepSeek 10-20 回过 "g0（noul）"），按问题 id 对回去
    answers = {}
    for k, a in (raw.items() if isinstance(raw, dict) else []):
        key = k if k in req.questions else re.split(r"[（(\s]", str(k), maxsplit=1)[0].strip().strip('"')
        if key in req.questions:
            answers[key] = a
    # 补齐 choice 的 confidence，保持和 Jev 返回一致
    for qid, a in answers.items():
        if isinstance(a, dict) and "probabilities" in a and "confidence" not in a:
            a["confidence"] = max(a["probabilities"].values() or [0])
    return answers


AMAP_KEY = os.environ.get("AMAP_WEB_SERVICE_KEY", "")
# 地理编码失败时的兜底锚点（海岸城，GCJ-02，2026-10-19 高德实测）
DEFAULT_ANCHOR = {"name": "海岸城", "lng": 113.937635, "lat": 22.518308}


@app.get("/health")
async def health():
    return {"ok": True, "decider_model": DECIDER_MODEL, "backends": ["plain"], "amap": bool(AMAP_KEY),
            "coze_pat": bool(COZE_PAT), "mem_idle_s": MEM_IDLE_S, "mem_pending": len(_pending)}


@app.get("/geo")
async def geo(address: str = "", city: str = "深圳", authorization: str | None = Header(None),
              x_sidecar_token: str | None = Header(None)):
    """地名 → GCJ-02 坐标。高德 key 只在 sidecar 里，不进画布。失败时返回兜底锚点并标 fallback。"""
    _auth(authorization, x_sidecar_token)
    return await _geocode(address, city)


# 深圳口语里有歧义的片区名：高德按字面会落到别处（"科技园"文本搜索第一条是宝安的福海国际科技园，2026-10-20 实测）
ALIAS = {"科技园": "南山科技园", "科技园南区": "南山科技园南区", "科技园北区": "南山科技园北区", "大学城": "深圳大学城",
         "西丽": "南山区西丽", "后海": "南山区后海",
         # 只说"万象城"时高德给罗湖的万象城公交站（10-21 实测），本项目范围在南山
         "万象城": "深圳湾万象城", "南山万象城": "深圳湾万象城", "深圳湾万象": "深圳湾万象城"}
ALIAS_SUFFIX = re.compile(r"(附近|周边|一带|那边|这边|旁边|那块|这块)$")


async def _geocode(address: str, city: str = "深圳") -> dict:
    address = (address or "").strip()
    base = ALIAS_SUFFIX.sub("", address)
    address = ALIAS.get(base, address)
    if not address or not AMAP_KEY:
        return {**DEFAULT_ANCHOR, "fallback": True, "reason": "empty address" if not address else "no key"}
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get("https://restapi.amap.com/v3/geocode/geo",
                              params={"address": address, "city": city, "key": AMAP_KEY})
            d = r.json()
            g = (d.get("geocodes") or [None])[0]
            if str(d.get("status")) == "1" and g:
                lng, lat = g["location"].split(",")
                return {"name": g.get("formatted_address") or address, "lng": float(lng), "lat": float(lat),
                        "fallback": False, "via": "geocode"}
            # 商圈、园区这类非标准地址，地理编码会失败，改用 POI 关键词搜索取第一条
            r = await cli.get("https://restapi.amap.com/v5/place/text",
                              params={"keywords": address, "region": city, "city_limit": "true", "page_size": 1,
                                      "key": AMAP_KEY})
            p = (r.json().get("pois") or [None])[0]
        if p and p.get("location"):
            lng, lat = p["location"].split(",")
            return {"name": p.get("name") or address, "lng": float(lng), "lat": float(lat), "fallback": False, "via": "poi"}
        return {**DEFAULT_ANCHOR, "fallback": True, "reason": f"amap {d.get('info')}"}
    except Exception as e:
        return {**DEFAULT_ANCHOR, "fallback": True, "reason": type(e).__name__}


def _line_m(a: tuple[float, float], b: tuple[float, float]) -> int:
    """两点直线距离（米），高德路线失败时兜底（设计文档 4.7）。"""
    import math
    lng1, lat1, lng2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return int(2 * 6371000 * math.asin(math.sqrt(h)))


@app.get("/route")
async def route(to_lng: float, to_lat: float, to_name: str = "", origin: str = "海岸城",
                authorization: str | None = Header(None), x_sidecar_token: str | None = Header(None)):
    """起点地名 → 门店：步行、公交地铁（3 公里以上才查）、驾车。每种方式单独失败不影响别的。"""
    _auth(authorization, x_sidecar_token)
    o = await _geocode(origin)
    a, b = (o["lng"], o["lat"]), (to_lng, to_lat)
    out: dict = {"origin": o.get("name") or origin, "origin_fallback": o.get("fallback", False), "dest": to_name,
                 "line_m": _line_m(a, b), "o_lng": a[0], "o_lat": a[1], "d_lng": b[0], "d_lat": b[1]}
    loc = lambda p: f"{p[0]:.6f},{p[1]:.6f}"  # noqa: E731
    # 网页 Demo 画路线用（10-22）：步行和驾车各留一条折线，路线格式化按距离挑一条放进 route 卡片
    base = {"origin": loc(a), "destination": loc(b), "key": AMAP_KEY, "show_fields": "cost,polyline"}
    polys: dict = {}

    async def walk(cli):
        d = (await cli.get("https://restapi.amap.com/v5/direction/walking", params=base)).json()
        p = ((d.get("route") or {}).get("paths") or [None])[0]
        if p:
            out["walk"] = {"m": int(p.get("distance") or 0), "min": round(int((p.get("cost") or {}).get("duration") or 0) / 60)}
            polys["walk"] = _poly(p)

    async def drive(cli):
        d = (await cli.get("https://restapi.amap.com/v5/direction/driving", params=base)).json()
        r = d.get("route") or {}
        p = (r.get("paths") or [None])[0]
        if p:
            out["drive"] = {"m": int(p.get("distance") or 0), "min": round(int((p.get("cost") or {}).get("duration") or 0) / 60),
                            "taxi_yuan": r.get("taxi_cost") or ""}
            polys["drive"] = _poly(p)

    async def transit(cli):
        d = (await cli.get("https://restapi.amap.com/v5/direction/transit/integrated",
                           params={**base, "city1": "0755", "city2": "0755"})).json()
        t = ((d.get("route") or {}).get("transits") or [None])[0]
        if t:
            names = []
            for seg in t.get("segments") or []:
                for bl in (seg.get("bus") or {}).get("buslines") or []:
                    names.append(str(bl.get("name") or "").split("(")[0])
            out["transit"] = {"min": round(int((t.get("cost") or {}).get("duration") or 0) / 60), "lines": " → ".join(names[:3])}

    jobs = [walk, drive] + ([transit] if out["line_m"] > 3000 else [])
    async with httpx.AsyncClient(timeout=5) as cli:
        res = await asyncio.gather(*[j(cli) for j in jobs], return_exceptions=True)
    for j, r in zip(jobs, res):
        if isinstance(r, Exception):
            out[j.__name__ + "_error"] = type(r).__name__
    # 步行 2 公里内画步行路线，否则画驾车路线
    mode = "walk" if "walk" in polys and (out.get("walk") or {}).get("m", 99999) <= 2000 else "drive" if "drive" in polys else ""
    if mode:
        out["poly_mode"], out["poly"] = mode, polys[mode]
    return out


def _poly(path: dict, max_pts: int = 120) -> str:
    """高德 v5 路线各 step 的 polyline 拼起来，均匀抽到 max_pts 个点以内，坐标保留 5 位小数（约 1 米）。"""
    pts = []
    for s in path.get("steps") or []:
        for xy in str(s.get("polyline") or "").split(";"):
            if "," in xy:
                pts.append(xy)
    if len(pts) > max_pts:
        step = (len(pts) - 1) / (max_pts - 1)
        pts = [pts[round(i * step)] for i in range(max_pts)]
    return ";".join(",".join(f"{float(v):.5f}" for v in xy.split(",")) for xy in pts)


BOCHA_KEY = os.environ.get("BOCHA_API_KEY", "")


@app.get("/hot")
async def hot(q: str, authorization: str | None = Header(None), x_sidecar_token: str | None = Header(None)):
    """榜单类问题的网页搜索（博查，约 0.036 元/次），同一问题按天缓存。"""
    _auth(authorization, x_sidecar_token)
    q = (q or "").strip()[:60]
    day = time.strftime("%Y-%m-%d")
    with _db() as con:
        con.execute("create table if not exists hot_cache(q text, day text, resp text, primary key (q, day))")
        row = con.execute("select resp from hot_cache where q = ? and day = ?", (q, day)).fetchone()
    if row:
        return {**json.loads(row[0]), "cached": True}
    if not q or not BOCHA_KEY:
        return {"items": [], "error": "empty query" if not q else "no key"}
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.post("https://api.bochaai.com/v1/web-search", headers={"Authorization": f"Bearer {BOCHA_KEY}"},
                               json={"query": q, "count": 10, "summary": True, "freshness": "oneYear"})
        pages = (((r.json().get("data") or {}).get("webPages") or {}).get("value")) or []
    except Exception as e:
        return {"items": [], "error": type(e).__name__}
    items = [{"title": p.get("name"), "site": p.get("siteName"), "date": str(p.get("datePublished") or "")[:10],
              "url": p.get("url"), "text": str(p.get("summary") or p.get("snippet") or "")[:400]} for p in pages]
    resp = {"items": items}
    if not items:
        return {**resp, "error": "no results"}  # 空结果不缓存
    with _db() as con:
        con.execute("insert or replace into hot_cache values (?,?,?)", (q, day, json.dumps(resp, ensure_ascii=False)))
    return resp


COZE_BASE = os.environ.get("COZE_BASE", "http://host.docker.internal:8888")
COZE_PAT = os.environ.get("COZE_PAT", "")
IDS_PATH = Path(os.environ.get("IDS_PATH", "/app/ids.json"))
MEM_IDLE_S = int(os.environ.get("MEM_IDLE_S", "120"))
_pending: dict[str, asyncio.Task] = {}


async def _consolidate(uid: str) -> dict:
    """用 OpenAPI 跑 WF-consolidate；ext.user_id 让按用户隔离的 user_memory 只露出这个用户的行。"""
    t0 = time.time()
    resp: dict = {}
    err = None
    try:
        wid = json.loads(IDS_PATH.read_text(encoding="utf-8"))["workflows"]["consolidate"]
        async with httpx.AsyncClient(timeout=300) as cli:
            r = await cli.post(f"{COZE_BASE}/v1/workflow/run", headers={"Authorization": f"Bearer {COZE_PAT}"},
                               json={"workflow_id": wid, "parameters": json.dumps({"noop": "1"}), "ext": {"user_id": uid}})
        resp = r.json()
        if resp.get("code"):
            err = f"coze {resp.get('code')}: {resp.get('msg')}"[:500]
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:500]
    ms = int((time.time() - t0) * 1000)
    try:
        with _db() as con:
            con.execute("create table if not exists mem_log(ts real, uid text, latency_ms integer, response text, error text)")
            con.execute("insert into mem_log values (?,?,?,?,?)", (time.time(), uid, ms, json.dumps(resp, ensure_ascii=False)[:4000], err))
    except Exception:
        pass
    return {"uid": uid, "latency_ms": ms, "error": err, "data": resp.get("data")}


async def _debounced(uid: str):
    await asyncio.sleep(MEM_IDLE_S)
    _pending.pop(uid, None)  # 先出队：整理过程中再来的 touch 会排一个新任务，不会取消正在跑的这次
    await _consolidate(uid)


@app.get("/mem/touch")
async def mem_touch(uid: str = "", authorization: str | None = Header(None), x_sidecar_token: str | None = Header(None)):
    """CF-main 每轮末尾调用（有待整理的行时 uid 非空）。会话安静 MEM_IDLE_S 秒后整理一次，期间再来就重新计时。"""
    _auth(authorization, x_sidecar_token)
    if not uid:
        return {"ok": True, "scheduled": False}
    old = _pending.get(uid)
    if old:
        old.cancel()
    _pending[uid] = asyncio.create_task(_debounced(uid))
    return {"ok": True, "scheduled": True, "after_s": MEM_IDLE_S}


@app.post("/mem/consolidate")
async def mem_consolidate(uid: str, authorization: str | None = Header(None), x_sidecar_token: str | None = Header(None)):
    """立即整理一次（测试和手动补跑用），返回工作流输出。"""
    _auth(authorization, x_sidecar_token)
    old = _pending.pop(uid, None)
    if old:
        old.cancel()
    return await _consolidate(uid)


@app.post("/decide")
async def decide(req: DecideRequest, authorization: str | None = Header(None),
                 x_sidecar_token: str | None = Header(None)):
    _auth(authorization, x_sidecar_token)
    t0 = time.time()
    backend = "plain" if req.questions else "none"
    err = None
    answers: dict = {}
    try:
        if req.questions:  # 调用方有时没东西可问（比如整理记忆时没有新的偏好组），直接返回空答案
            answers = await _plain(req)
    except Exception as e:  # 决策失败时返回空答案，由调用方走默认分支
        err = f"{type(e).__name__}: {e}"[:500]
    ms = int((time.time() - t0) * 1000)
    resp = {"answers": answers, "backend": backend, "latency_ms": ms, "error": err}
    try:
        with _db() as con:
            con.execute("insert into decide_log values (?,?,?,?,?,?,?)",
                        (time.time(), req.point, backend, ms, req.model_dump_json(), json.dumps(resp, ensure_ascii=False), err))
    except Exception:
        pass
    return resp
