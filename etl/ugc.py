"""UGC 管线：发现 → 抓取 → 抽取 → 对齐，产出门店级证据片段。

来源三个，都只用普通 GET 访问公开页面，不带 Cookie，不碰签名接口：
  xhs     小红书笔记页 /explore/<id>，解析 window.__INITIAL_STATE__
  dp      点评探店笔记 m.dianping.com/discovery/<id>，正文 + relateShopInfoList
  dy      抖音话题聚合页 douyin.com/shipin/<id>，视频文案 + asrText 口播转写
发现用博查 web-search 的 include 参数限定站点。

用法（小规模）：python -I -X utf8 etl/ugc.py --query "深圳 海岸城 美食" --per-source 2
输出：data/raw/ugc/<source>/<id>.html（缓存）、data/processed/ugc_mentions.jsonl
"""
import argparse
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.env import need  # noqa: E402
from common.llm import chat_json  # noqa: E402
from etl.smoke import call  # noqa: E402

RAW = ROOT / "data" / "raw" / "ugc"
OUT = ROOT / "data" / "processed" / "ugc_mentions.jsonl"
SHOPS = ROOT / "data" / "processed" / "shops.jsonl"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
FETCH_GAP_S = 4  # 每次抓取间隔

SOURCES = {
    "xhs": {"include": "xiaohongshu.com", "pattern": r"xiaohongshu\.com/explore/([0-9a-f]{24})",
            "url": "https://www.xiaohongshu.com/explore/{id}"},
    "dp": {"include": "dianping.com", "pattern": r"m\.dianping\.com/discovery/(\d+)",
           "url": "https://m.dianping.com/discovery/{id}"},
    "dy": {"include": "douyin.com", "pattern": r"douyin\.com/shipin/(\d+)",
           "url": "https://www.douyin.com/shipin/{id}"},
}


# ---------- 发现 ----------
def discover(query: str, source: str, count: int = 10) -> list[str]:
    st, d = call("POST", "https://api.bochaai.com/v1/web-search", {"Authorization": "Bearer " + need("BOCHA_API_KEY")},
                 {"query": query, "count": count, "summary": False, "include": SOURCES[source]["include"]})
    pages = (((d.get("data") or {}).get("webPages") or {}).get("value")) or []
    ids = []
    for p in pages:
        m = re.search(SOURCES[source]["pattern"], p.get("url") or "")
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    print(f"[discover] {source} q={query!r} 结果 {len(pages)} 条，可用 id {len(ids)} 个")
    return ids


# ---------- 抓取 ----------
def fetch(source: str, pid: str) -> str | None:
    cache = RAW / source / f"{pid}.html"
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace")
    url = SOURCES[source]["url"].format(id=pid)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            final, body = r.geturl(), r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"[fetch] {source}/{pid} 失败 {type(e).__name__}")
        return None
    finally:
        time.sleep(FETCH_GAP_S)
    if "verify" in final or "spiderindefence" in final or "login" in final:
        print(f"[fetch] {source}/{pid} 被拦截 → {final[:80]}")
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(body, encoding="utf-8")
    return body


# ---------- 解析 ----------
def parse_xhs(s: str) -> list[dict]:
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", s, re.S)
    if not m:
        return []
    st = json.loads(re.sub(r"\bundefined\b", "null", m.group(1)))
    docs = []
    for nid, v in ((st.get("note") or {}).get("noteDetailMap") or {}).items():
        n = (v or {}).get("note") or {}
        if not n:
            continue
        ii = n.get("interactInfo") or {}
        docs.append({"doc_id": nid, "title": n.get("title") or "", "text": n.get("desc") or "",
                     "ts": int((n.get("time") or 0) / 1000), "likes": ii.get("likedCount"),
                     "collects": ii.get("collectedCount"), "shop_hint": None})
    return docs


def parse_dp(s: str) -> list[dict]:
    title = re.search(r"<title>(.*?)</title>", s, re.S)
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S)
    text = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", body)))
    shops = []
    for m in re.finditer(r'"relateShopInfoList":(\[.*?\])', s):
        try:
            shops += json.loads(m.group(1))
        except ValueError:
            pass
    hint = [{"name": x.get("shopName"), "branch": x.get("branchName"), "area": x.get("mainRegionName"),
             "cate": x.get("mainCategoryName"), "star": (x.get("shopPower") or 0) / 10, "avg_price": x.get("avgPrice"),
             "dp_shop_id": x.get("shopId")} for x in shops]
    date = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    return [{"doc_id": None, "title": (title.group(1).strip() if title else ""), "text": text[:4000],
             "ts": int(time.mktime(time.strptime(date.group(1), "%Y-%m-%d"))) if date else 0,
             "likes": None, "collects": None, "shop_hint": hint or None}]


def parse_dy(s: str) -> list[dict]:
    u = urllib.parse.unquote(s).replace('\\"', '"')
    docs, seen = [], set()
    for m in re.finditer(r'"awemeId":"(\d+)"', u):
        aid = m.group(1)
        if aid in seen:
            continue
        seg = u[m.start():m.start() + 6000]
        txt = re.search(r'"text":"(.*?)","uid"', seg)
        asr = re.search(r'"asrText":"(.*?)","followerCount"', seg)
        if not (txt or asr):
            continue
        seen.add(aid)
        dig = re.search(r'"diggCount":(\d+)', seg)
        ts = re.search(r'"updateTS":(\d+)', seg)
        docs.append({"doc_id": aid, "title": txt.group(1) if txt else "", "text": asr.group(1) if asr else "",
                     "ts": int(ts.group(1)) if ts else 0, "likes": int(dig.group(1)) if dig else None,
                     "collects": None, "shop_hint": None})
    return docs


PARSERS = {"xhs": parse_xhs, "dp": parse_dp, "dy": parse_dy}

# ---------- 抽取 ----------
EXTRACT_PROMPT = """从下面这段关于深圳餐饮的内容里，抽出它评价或介绍到的每一家店。只输出 JSON 数组，没有店就输出 []。
每个元素：{{"shop": 店名（不含分店）, "branch": 分店或商场名（没有写 null）, "area": 区或商圈（没有写 null）,
"avg_price": 人均元（数字，没有写 null）, "dishes": [推荐菜], "scenes": [适合场景，如约会、聚餐、带娃、一人食、商务],
"queue": 排队情况原话（没有写 null）, "deal": 提到的团购或套餐原话（没有写 null）, "cons": 缺点或避雷原话（没有写 null）,
"quote": 最能代表评价的一句原话（不超过 80 字）}}

标题：{title}
正文：{text}"""


def extract(doc: dict) -> list[dict]:
    text = (doc["text"] or "")[:3000]
    if len(text) + len(doc["title"] or "") < 30:
        return []
    try:
        out = chat_json(EXTRACT_PROMPT.format(title=doc["title"], text=text), max_tokens=4096)
        return out if isinstance(out, list) else []
    except Exception as e:
        print(f"[extract] 失败 {type(e).__name__}: {str(e)[:120]}")
        return []


# ---------- 对齐 ----------
# 店名里常见的繁体字 → 简体，只用于对齐比较（"籠籠駕到"和"笼笼驾到"要算同一家）；不引第三方库，见到漏的往里加
_T = ("籠駕記麵館廳燒雞魚鮮飯麥餅餃湯漢鍋貓龍鳳門東車馬點樓滷鹵煲燉廚鄉園頭麼鐵灣廣陳劉張黃華興發輕"
      "實雙島國際紅綠藍糰糕蝦蠔鰻鱈鴨鵝豬牛羊醬醃滷燴爐竈鬆萊專壽喫們這個時間樂業風氣義無與萬貝買賣"
      "夢歡達運過還邊廠薈晉當滿潮韓臺台灣齋蘇聯體價條燈會網紅點")
_S = ("笼驾记面馆厅烧鸡鱼鲜饭麦饼饺汤汉锅猫龙凤门东车马点楼卤卤煲炖厨乡园头么铁湾广陈刘张黄华兴发轻"
      "实双岛国际红绿蓝团糕虾蚝鳗鳕鸭鹅猪牛羊酱腌卤烩炉灶松莱专寿吃们这个时间乐业风气义无与万贝买卖"
      "梦欢达运过还边厂荟晋当满潮韩台台湾斋苏联体价条灯会网红点")
T2S = str.maketrans(_T, _S)


def norm(s: str) -> str:
    return re.sub(r"[（(].*?[)）]|\s|·|•", "", s or "").translate(T2S).lower()


# 10-21 重写（起因见 etl/audit_mentions.py）：名字要有证据，位置不一致一票否决。
# ugc、xhs_ingest、dy_notes、resolve_mentions 都调 align，audit_mentions 用同一套规则复核。
CJK_RE = re.compile(r"[㐀-鿿]")
# 只共享这些词不算同一家（"穗 Sushi"和"Sushi Minoru"只共享 sushi）
GENERIC_LATIN = {"the", "and", "cafe", "coffee", "tea", "bar", "bistro", "kitchen", "restaurant", "bakery", "house",
                 "club", "dining", "grill", "burger", "pizza", "ramen", "sushi", "steak", "brunch", "food", "shop",
                 "store", "studio", "lab", "box", "plus", "pro", "new"}
# 只有品类词的中文部分不算证据（"Susea寿司"的"寿司"会配上"寿司郎"）
GENERIC_CJK = {"寿司", "火锅", "烤肉", "咖啡", "面馆", "拉面", "烧烤", "茶餐厅", "料理", "日料", "西餐", "甜品", "酒馆",
               "餐厅", "小馆", "食堂", "饭店", "酒家", "茶室", "面包", "披萨", "汉堡", "牛排", "串串", "冒菜", "米线",
               "粉面", "酸菜鱼", "烤鱼", "海鲜", "湘菜", "川菜", "粤菜", "潮汕", "牛肉", "日本料理", "韩国料理", "越南粉",
               "奶茶", "茶饮", "糖水", "早茶", "点心", "烘焙", "西餐厅", "居酒屋", "烧肉", "云南菜", "新疆菜", "江西菜"}
SZ_OTHER = ["福田", "罗湖", "宝安", "龙华", "龙岗", "光明", "坪山", "盐田", "大鹏"]
# 本片区和常被提到的商场。海岸城和深圳湾万象城只隔约 900 米、来福士离海岸城约 1.3 公里，单靠距离分不开，
# 所以两边都写了商场时按商场比。顺序要紧："万象前海"要排在"万象城"前面。
MALLS = [("万象前海", ["万象前海", "前海万象"]), ("深圳湾万象城", ["深圳湾万象城", "万象城"]), ("万象天地", ["万象天地"]),
         ("海岸城", ["海岸城"]), ("来福士", ["来福士"]), ("保利", ["保利"]), ("天利", ["天利"]), ("欢乐海岸", ["欢乐海岸"]),
         ("益田假日", ["益田假日"]), ("后海汇", ["后海汇"]), ("南头古城", ["南头古城"]), ("卓悦", ["卓悦"]),
         ("壹方", ["壹方"]), ("cocopark", ["cocopark"]), ("海雅", ["海雅"]), ("茂业", ["茂业"]), ("花园城", ["花园城"]),
         ("南海意库", ["南海意库"]), ("鹏润达", ["鹏润达"]), ("宝能", ["宝能"]), ("京基", ["京基"]), ("华润", ["华润"])]
# 保利文化广场、天利中央商务广场和海岸城都隔一条街（200–400 米），高德里不少"（海岸城店）"的地址就在天利
NEAR_MALLS = [{"海岸城", "保利", "天利"}]
OTHER_CITY = ["广州", "东莞", "惠州", "珠海", "佛山", "中山", "香港", "澳门", "上海", "北京", "杭州", "成都", "长沙",
              "重庆", "武汉", "南京", "苏州", "厦门", "西安"]
ANCHORS = ["海岸城", "深圳湾万象城", "南山科技园"]
KWD_ANCHOR = {"海岸城": "海岸城", "后海": "海岸城", "万象城": "深圳湾万象城", "深圳湾": "深圳湾万象城", "科技园": "南山科技园"}
BRANCH_NEAR_M = 1000  # 认不出商场时，笔记写的分店地址编码后离门店超过这么远，就不是这家
BROAD_M = {"南山科技园": 3000, "后海": 2500, "深圳湾": 3000, "蛇口": 3000, "前海": 3000, "南头": 2000, "粤海": 2500,
           "西丽": 3000}  # 片区名的地理编码是个中心点，放宽
LOC_ALIAS = {"科技园": "南山科技园"}  # 高德"科技园"落到宝安（sidecar ALIAS 同一个问题）
BAD_GEO_LEVEL = {"国家", "省", "市", "区县", "开发区", "乡镇", "道路", "未知"}  # 这些级别的编码点太粗，当作查不到
AMAP_CACHE = ROOT / "data" / "processed" / "amap_cache.json"


def dist_m(lng1, lat1, lng2, lat2) -> float:
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


class Amap:
    """高德地理编码、关键字搜索，结果缓存到 data/processed/amap_cache.json（重跑不重复花额度）。
    offline=True 时不发请求，没缓存的当查不到，记在 missed 里（先估调用量用）。"""

    def __init__(self, offline: bool = False):
        self.offline, self.calls, self.missed = offline, 0, set()
        self.cache = json.loads(AMAP_CACHE.read_text(encoding="utf-8")) if AMAP_CACHE.exists() else {}

    def _get(self, k: str, fn):
        if k in self.cache:
            return self.cache[k]
        if self.offline:
            self.missed.add(k)
            return None
        from etl.amap_crawl import amap
        for i in range(3):
            try:
                v = fn(amap)
                break
            except Exception as e:  # noqa: BLE001  网络错误不缓存
                if i == 2:
                    print(f"[高德] {k} 失败：{str(e)[:80]}")
                    return None
                time.sleep(1.5)
        self.calls += 1
        self.cache[k] = v
        time.sleep(0.35)
        return v

    def geo(self, addr: str) -> dict | None:
        """→ {lng, lat, level, city, district}；查不到是 {}。"""
        def f(amap):
            g = (amap("/v3/geocode/geo", address=addr, city="深圳").get("geocodes") or [None])[0]
            if not g or not g.get("location"):
                return {}
            lng, lat = map(float, g["location"].split(","))
            city = g.get("city")
            return {"lng": lng, "lat": lat, "level": g.get("level"), "district": g.get("district") or "",
                    "city": city if isinstance(city, str) else ""}
        return self._get("geo:" + addr, f)

    def text(self, kw: str) -> list[dict]:
        def f(amap):
            from etl.amap_crawl import FOOD_TYPES
            return amap("/v5/place/text", keywords=kw, region="440300", city_limit="true", types=FOOD_TYPES,
                        show_fields="business", page_size=10).get("pois") or []
        return self._get("text:" + kw, f) or []

    def save(self):
        if self.calls:
            AMAP_CACHE.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")


def _strip_branch(name: str) -> str:
    return re.sub(r"[（(].*?[)）]", "", name or "")


def _cjk(s: str) -> str:
    return "".join(CJK_RE.findall(s))


def _latin(s: str) -> list[str]:
    """拉丁字母和数字按整词切开，2 个字符以下的词不算（"le""la"）。"""
    return [t for t in re.findall(r"[a-z]+|\d+", s.lower()) if len(t) >= 3]


def _head(s: str) -> str:
    """"·"等分隔符前的品牌名。"""
    return re.split(r"[·•・.．\s\-]", s.strip())[0]


def name_match(mention_name: str, poi_name: str) -> tuple[int, str]:
    """店名有没有证据指向这家 → (分数, 规则)；0 表示不是。
    中文部分：较短的一方是较长一方的子串（2 个字时要是开头），或"·"前的品牌名是对方的开头；两边中文都有却对不上算冲突。
    拉丁部分：按整词比，一方的词都在另一方里且不全是通用词，或去空格后相同（Sokosoko / SOKO SOKO）；两边都有拉丁词却一个都不共享算冲突。"""
    from rapidfuzz import fuzz
    raw, base = _strip_branch(mention_name).translate(T2S), _strip_branch(poi_name).translate(T2S)
    nm, nb = norm(raw), norm(base)
    if not nm or not nb or nm in ("none", "null"):
        return 0, ""
    if nm == nb:
        return 100, "同名"
    cm, cb = _cjk(raw), _cjk(base)
    cm, cb = ("" if cm in GENERIC_CJK else cm), ("" if cb in GENERIC_CJK else cb)
    lm, lb = set(_latin(raw)), set(_latin(base))
    cjk_ok = cjk_conflict = cjk_fuzzy = False
    if len(cm) >= 2 and len(cb) >= 2:
        s, l = sorted((cm, cb), key=len)
        if s in l and (len(s) >= 3 or l.startswith(s)):
            cjk_ok = True
        else:
            s2, l2 = sorted((_cjk(_head(raw)), _cjk(_head(base))), key=len)
            cjk_ok = len(s2) >= 2 and s2 not in GENERIC_CJK and l2.startswith(s2)
        if not cjk_ok:
            # 一两个字的出入（"至正潮味 / 至正潮菜""阿一猪扒米线 / 阿一猪扒酸辣米线"）：开头至少 2 个字相同且整体相似
            cjk_ok = cjk_fuzzy = len(os.path.commonprefix([cm, cb])) >= 2 and fuzz.ratio(cm, cb) >= 75
        cjk_conflict = not cjk_ok
    elif cm and cm == cb:
        cjk_ok = True  # 单字中文完全相同（"穗 Sushi"），还要拉丁词共享
    jm, jb = "".join(re.findall(r"[a-z0-9]+", raw.lower())), "".join(re.findall(r"[a-z0-9]+", base.lower()))
    lat_ok = lat_conflict = False
    if lm and lb:
        s, l = sorted((lm, lb), key=len)
        lat_ok = (len(jm) >= 4 and jm == jb) or (s <= l and bool(s - GENERIC_LATIN))
        lat_conflict = not (lm & lb) and jm != jb
    if cjk_conflict or lat_conflict:
        return 0, ""
    if len(cm) >= 2 and cjk_ok:
        return (90, "中文近似") if cjk_fuzzy else (100, "中文名")
    if lat_ok:
        return 95, "拉丁名"
    if cjk_ok and lm & lb:
        return 95, "单字中文+拉丁"
    r = fuzz.ratio(nm, nb)
    return (int(r), "整体相似") if r >= 90 else (0, "")


def _split_loc(s) -> list[str]:
    return [x.strip() for x in re.split(r"[/、,，;；|]", str(s or "")) if x.strip().lower() not in ("", "none", "null")]


def clean_loc(s: str) -> str:
    """分店、商场写法归一：去楼层门牌、去"深圳/南山"前缀和"店"类后缀。"""
    s = re.sub(r"[A-Za-z]?\s*\d+\s*[层楼F].*$|[一二三四五六七八九十]+[层楼].*$|\d+号.*$|\s*[BLF]\d+$", "", (s or "").strip())
    s = re.sub(r"^(广东省?)?(深圳市?(?!湾))?(南山区?)?", "", s)
    s = re.sub(r"(分店|总店|旗舰店|首店|概念店|门店|店)$", "", s)
    return s.strip(" ·-")


def is_foreign(s: str) -> bool:
    return "南山" not in s and any(d in s for d in SZ_OTHER + OTHER_CITY)


def malls(s: str) -> set:
    s, out = norm(s), set()
    for canon, aliases in MALLS:
        for a in aliases:
            if a in s:
                out.add(canon)
                s = s.replace(a, "|")
    return out


def same_mall(a: set, b: set) -> bool:
    return bool(a & b) or any(len(a & p) and len(b & p) for p in NEAR_MALLS)


def loc_check(m: dict, shop: dict, amap: "Amap | None") -> tuple[bool, str, float]:
    """笔记里的位置和这家门店是否一致 → (是否接受, 说明, 强度)。强度用来在同名多家里挑：分店字面或商场一致 2，
    地理编码在范围内 1 减距离，区域字面一致 0.5，没有位置信息 0。
    分店（branch）优先，每个分店写法依次看：外区外市 → 否；和门店分店名或地址字面一致 → 是；两边都认出商场 → 同商场是、
    不同商场否；都认不出 → 地理编码比距离（编码不出来或太粗的不算）。任一分店写法为"是"就接受，都不是而有"否"就拒。
    没有可用的分店信息时看区域（area）：写的全是南山以外的区或外市就拒。"""
    branches, areas = _split_loc(m.get("branch")), _split_loc(m.get("area"))
    pb = norm(clean_loc((re.search(r"[（(](.*?)[)）]", shop.get("name") or "") or [None, ""])[1]))
    addr = norm(shop.get("address") or "")
    pm = malls(pb + "|" + addr)
    bad = ""
    for raw in branches:
        if is_foreign(raw):
            bad = bad or f"分店写的是外区或外市：{raw}"
            continue
        seg = clean_loc(raw)
        ns = norm(seg)
        if len(ns) < 2:
            continue
        if (len(pb) >= 2 and (ns in pb or pb in ns)) or (len(ns) >= 3 and ns in addr):
            return True, f"分店一致：{raw}", 2.0
        bm = malls(ns)
        if bm and pm:
            if same_mall(bm, pm):
                return True, f"同商场：{raw}", 2.0
            bad = bad or f"商场不同：笔记 {raw}，门店在{'/'.join(sorted(pm))}"
            continue
        q = LOC_ALIAS.get(seg, seg)
        g = amap.geo(q) if amap and shop.get("lng") else None
        if not g:
            continue
        if g.get("city") and "深圳" not in g["city"]:
            bad = bad or f"分店 {raw} 编码到 {g['city']}"
            continue
        if g.get("level") in BAD_GEO_LEVEL and q not in BROAD_M:  # "南山科技园"编码级别是"未知"，但点位可用
            continue
        d = dist_m(g["lng"], g["lat"], shop["lng"], shop["lat"])
        if d <= BROAD_M.get(q, BRANCH_NEAR_M):
            return True, f"分店 {raw} 距门店 {d:.0f}m", 1 - d / 10000
        bad = bad or f"分店 {raw} 距门店 {d:.0f}m"
    if bad:
        return False, bad, 0
    if areas and not any("南山" in a for a in areas + branches) and any(is_foreign(a) for a in areas):
        return False, f"区域写的是外区或外市：{m.get('area')}", 0
    for raw in areas:
        na = norm(clean_loc(raw))
        if len(na) >= 2 and ((len(pb) >= 2 and (na in pb or pb in na)) or (len(na) >= 3 and na in addr)):
            return True, f"区域一致：{raw}", 0.5
    return True, "", 0


def align(mention: dict, shops: list[dict], amap: "Amap | None" = None, kwd: str = ""):
    """→ (门店或 None, 分数, 说明)。名字有证据的候选里去掉位置对不上的，按分数、位置强度挑。
    同名多家时再看笔记搜索词里的锚点（kwd，如"海岸城 火锅"）：离锚点 1.5 公里内的加 0.25。都一样取门店库里靠前的。"""
    hint = None
    if amap:
        for k, v in KWD_ANCHOR.items():
            if k in (kwd or ""):
                hint = amap.geo(v)
                break
    best, best_key, why_no = None, None, ""
    for s in shops:
        sc, rule = name_match(mention.get("shop"), s["name"])
        if not sc:
            continue
        ok, why, strength = loc_check(mention, s, amap)
        if not ok:
            why_no = why_no or f"{s['name']}：{why}"
            continue
        if hint and s.get("lng") and dist_m(hint["lng"], hint["lat"], s["lng"], s["lat"]) <= 1500:
            strength += 0.25
        if best_key is None or (sc, strength) > best_key:
            best, best_key = (s, sc, rule + (f"；{why}" if why else "")), (sc, strength)
    return best if best else (None, 0, why_no)


def has_content(m: dict) -> bool:
    """有原话或任一事实字段。全空的提及进 KB 只剩一行前缀，不写。"""
    return any(m.get(k) for k in ("quote", "dishes", "scenes", "queue", "deal", "cons", "avg_price"))


def kb_eligible(rec: dict, shops: dict) -> bool:
    """进 KB-review 的条件：对上了门店库里的店，且有内容。load_kb 和 audit_mentions 共用。"""
    return rec.get("amap_id") in shops and has_content(rec["mention"])


def mention_key(rec: dict) -> str:
    """来源 + 笔记 + 店名 + 分店 + 原话前 20 字。只用店名时同一篇笔记里同名店的两条提及会撞。"""
    m = rec["mention"]
    raw = f"{rec['source']}|{rec['doc_id']}|{m.get('shop')}|{m.get('branch')}|{(m.get('quote') or '')[:20]}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="深圳 海岸城 美食")
    ap.add_argument("--sources", default="xhs,dp,dy")
    ap.add_argument("--per-source", type=int, default=2, help="每个来源最多抓几页")
    a = ap.parse_args()
    shops = [json.loads(l) for l in SHOPS.read_text(encoding="utf-8").splitlines()] if SHOPS.exists() else []
    out = OUT.open("a", encoding="utf-8")
    amap_c = Amap()
    stats = {}
    for src in a.sources.split(","):
        ids = discover(a.query, src)[: a.per_source]
        n_docs = n_mentions = n_aligned = 0
        for pid in ids:
            html = fetch(src, pid)
            if not html:
                continue
            docs = PARSERS[src](html)
            for doc in docs[:5]:  # 抖音聚合页一页多条视频，小规模只取前 5 条
                n_docs += 1
                for m in extract(doc):
                    n_mentions += 1
                    shop, score, why = align(m, shops, amap_c)
                    n_aligned += bool(shop)
                    rec = {"source": src, "page_id": pid, "doc_id": doc["doc_id"] or pid, "ts": doc["ts"],
                           "likes": doc["likes"], "collects": doc["collects"], "mention": m,
                           "amap_id": shop["amap_id"] if shop else None, "align_score": score, "align_why": why,
                           "dp_hint": doc["shop_hint"]}
                    rec["key"] = mention_key(rec)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        stats[src] = {"pages": len(ids), "docs": n_docs, "mentions": n_mentions, "aligned": n_aligned}
        print(f"[{src}] {stats[src]}")
    out.close()
    amap_c.save()
    print("汇总", json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
