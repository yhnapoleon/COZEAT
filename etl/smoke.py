"""每个外部 API 调一次，确认 key 可用、返回结构符合预期。只打印状态和少量字段，不打印 key。

用法：python -I -X utf8 etl/smoke.py [amap doubao jutuike bocha anthropic openrouter]
不带参数时全部跑一遍，总共 6 次请求。
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import need  # noqa: E402

UA = "lifeagent-dev/0.1"


def call(method, url, headers=None, body=None, form=False, timeout=30):
    h = {"User-Agent": UA, **(headers or {})}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            data = json.dumps(body, ensure_ascii=False).encode()
            h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300]}


def amap():
    q = urllib.parse.urlencode({"keywords": "火锅", "region": "440305", "city_limit": "true",
                                "show_fields": "business", "page_size": 3, "key": need("AMAP_WEB_SERVICE_KEY")})
    st, d = call("GET", "https://restapi.amap.com/v5/place/text?" + q)
    pois = d.get("pois") or []
    print(f"[amap] http={st} status={d.get('status')} info={d.get('info')} count={d.get('count')}")
    for p in pois[:3]:
        b = p.get("business") or {}
        print(f"   {p.get('name')} | {p.get('type')} | rating={b.get('rating')} cost={b.get('cost')} tag={str(b.get('tag'))[:40]}")


def doubao():
    st, d = call("POST", "https://open.feedcoopapi.com/search_api/web_search",
                 {"Authorization": "Bearer " + need("DOUBAO_SEARCH_API_KEY")},
                 {"Query": "深圳 南山 火锅 推荐", "SearchType": "web", "Count": 5,
                  "Filter": {"NeedContent": False, "NeedUrl": True, "Sites": "xiaohongshu.com"},
                  "QueryControl": {"QueryRewrite": False}})
    res = (d.get("Result") or {})
    items = res.get("WebResults") or res.get("SearchResults") or []
    err = (d.get("ResponseMetadata") or {}).get("Error")
    print(f"[doubao] http={st} error={err} results={len(items)} keys={list(res)[:6]}")
    for it in items[:5]:
        print(f"   {it.get('SiteName')} | {str(it.get('Title'))[:40]} | {it.get('Url')}")


def jutuike():
    st, d = call("POST", "http://api.jutuike.com/douyin/search_product_list",
                 body={"apikey": need("JUTUIKE_API_KEY"), "count": 3, "sort_by": 1, "order_by": 1,
                       "city_code": "440300", "longitude": 113.9365, "latitude": 22.5197,
                       "distance_max": 2000, "key_word": "火锅"}, form=True)
    data = d.get("data") or {}
    prods = data.get("product_list") or []
    print(f"[jutuike] http={st} code={d.get('code')} msg={d.get('msg')} products={len(prods)} has_more={data.get('has_more')}")
    for p in prods[:3]:
        poi = p.get("nearest_poi") or {}
        items = sum(len(g.get("item_list") or []) for g in (p.get("product_item_detail") or []))
        print(f"   {p.get('name')} | price={p.get('price')} origin={p.get('origin_price')} sold={p.get('blurred_sold_count')} "
              f"| poi={poi.get('poi_name') or poi.get('name')} | 菜品项={items}")


def bocha():
    st, d = call("POST", "https://api.bochaai.com/v1/web-search", {"Authorization": "Bearer " + need("BOCHA_API_KEY")},
                 {"query": "深圳南山 火锅 推荐", "count": 3, "summary": False})
    pages = (((d.get("data") or {}).get("webPages") or {}).get("value")) or []
    print(f"[bocha] http={st} code={d.get('code')} msg={d.get('msg')} results={len(pages)}")
    for p in pages[:3]:
        print(f"   {p.get('siteName')} | {str(p.get('name'))[:40]}")


def anthropic():
    st, d = call("GET", "https://api.anthropic.com/v1/models?limit=20",
                 {"x-api-key": need("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"})
    ids = [m.get("id") for m in d.get("data") or []]
    print(f"[anthropic] http={st} err={(d.get('error') or {}).get('type')} models={ids[:8]}")


def openrouter():
    st, d = call("GET", "https://openrouter.ai/api/v1/key", {"Authorization": "Bearer " + need("OPENROUTER_API_KEY")})
    k = d.get("data") or {}
    print(f"[openrouter] http={st} err={(d.get('error') or {}).get('message')} limit={k.get('limit')} usage={k.get('usage')}")


ALL = {"amap": amap, "doubao": doubao, "jutuike": jutuike, "bocha": bocha, "anthropic": anthropic, "openrouter": openrouter}

if __name__ == "__main__":
    for name in (sys.argv[1:] or list(ALL)):
        try:
            ALL[name]()
        except Exception as e:  # 单个失败不影响其它
            print(f"[{name}] 异常: {type(e).__name__}: {str(e)[:200]}")
