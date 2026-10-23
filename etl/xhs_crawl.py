"""小红书低频只读抓取：独立 Chrome profile + 用户扫码登录一次，之后脚本自己跑。

只读：搜索页取笔记 id 和 xsec_token，详情页读 window.__INITIAL_STATE__（正文、互动数、前 10 条评论、图片地址）。
不点赞、不评论、不发布。每篇间隔 --gap 秒（默认 12–20 随机），每天上限 --daily（默认 100 篇）。
出现验证码、登录失效或"无法浏览"就停，不去绕。

用法：
  python -I -X utf8 etl/xhs_crawl.py login                      # 弹出 Chrome，用户扫码，检测到登录后退出
  python -I -X utf8 etl/xhs_crawl.py crawl --kw "海岸城 粤菜" --kw "海岸城 日料" --per-kw 5
输出：data/raw/ugc/xhs_browser/crawl_<日期>.jsonl（每行一篇，格式同 xhs_ingest 的输入）
      纯图笔记（正文 < 80 字且赞或藏 ≥ 100）的图片下载到 data/raw/ugc/xhs_img/<key>/，并写读图清单 img_todo.json
之后：etl/xhs_images.py data/raw/ugc/xhs_browser/img_todo.json → etl/xhs_ingest.py → etl/resolve_mentions.py
"""
import argparse
import json
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "data" / "browser" / "xhs-profile"
OUTDIR = ROOT / "data" / "raw" / "ugc" / "xhs_browser"
IMGDIR = ROOT / "data" / "raw" / "ugc" / "xhs_img"
QUOTA = OUTDIR / "_quota.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"

# 页面里的 Vue ref 要拆一层；键名和浏览器插件实验时一致
JS_STATE = r"""() => {
  const v = x => (x && typeof x === 'object' && ('_value' in x || '_rawValue' in x)) ? (x._value ?? x._rawValue) : x;
  const st = window.__INITIAL_STATE__ || {};
  return { loggedIn: !!v((st.user || {}).loggedIn) };
}"""
JS_FEEDS = r"""() => {
  const v = x => (x && typeof x === 'object' && ('_value' in x || '_rawValue' in x)) ? (x._value ?? x._rawValue) : x;
  const feeds = v(((window.__INITIAL_STATE__ || {}).search || {}).feeds) || [];
  return feeds.filter(f => f.modelType === 'note').map(f => ({ id: f.id, tok: f.xsecToken,
    title: (f.noteCard || {}).displayTitle || '', liked: ((f.noteCard || {}).interactInfo || {}).likedCount }));
}"""
JS_NOTE = r"""(id) => {
  const v = x => (x && typeof x === 'object' && ('_value' in x || '_rawValue' in x)) ? (x._value ?? x._rawValue) : x;
  const map = v(((window.__INITIAL_STATE__ || {}).note || {}).noteDetailMap) || {};
  const ent = map[id] || Object.values(map)[0] || {};
  const n = ent.note || {}; const ii = n.interactInfo || {};
  const comments = ((ent.comments || {}).list || []).slice(0, 10).map(c => ({ t: c.content, like: c.likeCount, ip: c.ipLocation }));
  return { ok: !!(n.title || n.desc), title: n.title || '', text: n.desc || '', ts: n.time || 0, ip: n.ipLocation || '',
    likes: ii.likedCount || '', collects: ii.collectedCount || '', ncomments: ii.commentCount || '',
    tags: (n.tagList || []).map(t => t.name), imgs: (n.imageList || []).map(im => im.urlDefault).filter(Boolean),
    comments, head: document.body.innerText.slice(0, 300) };
}"""
STOP_WORDS = ("安全验证", "验证码", "无法浏览", "安全限制", "请先登录", "扫码登录")
# 标题里出现外地城市名的跳过；"万象城""科技园"各地都有，比"海岸城"更容易混进外地笔记
BAD_CITY = ("无锡", "苏州", "上海", "北京", "广州", "杭州", "南京", "成都", "武汉", "西安", "重庆", "长沙", "天津",
            "厦门", "青岛", "珠海", "东莞", "佛山", "合肥", "郑州", "宁波", "昆明", "南宁", "福州", "沈阳", "济南")  # 不放"香港"：会误伤"香港茶餐厅"这类标题


def safe_eval(page, js, arg=None, tries=4):
    """搜索页加载后会在前端改一次地址（追加 &type=51），刚好撞上时 evaluate 会报上下文被销毁，等一下重读。"""
    for i in range(tries):
        try:
            return page.evaluate(js, arg) if arg is not None else page.evaluate(js)
        except Exception as e:
            if "context was destroyed" not in str(e) and "navigation" not in str(e) or i == tries - 1:
                raise
            page.wait_for_timeout(1500)


def goto_safe(page, url, referer=None) -> bool:
    """打开页面，超时重试一次；两次都失败返回 False，由调用方跳过。"""
    for i in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000, referer=referer)
            return True
        except Exception as e:
            print(f"  打开超时（第 {i + 1} 次）：{type(e).__name__}", flush=True)
            page.wait_for_timeout(5000)
    return False


def fnv(s: str) -> str:
    """和浏览器实验时页面内算的短哈希一致，方便去重。"""
    x = 0x811C9DC5
    for ch in s:
        x ^= ord(ch)
        x = (x * 0x01000193) & 0xFFFFFFFF
    return "x" + format(x, "x")


def open_ctx(p, headless=False):
    PROFILE.mkdir(parents=True, exist_ok=True)
    ctx = p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=headless,
                                               viewport={"width": 1280, "height": 860}, locale="zh-CN")
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


def cmd_login(a):
    with sync_playwright() as p:
        ctx, page = open_ctx(p)
        page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
        print("已打开小红书，请在弹出的窗口里扫码登录（最多等 5 分钟）", flush=True)
        for i in range(100):
            time.sleep(3)
            try:
                if page.evaluate(JS_STATE)["loggedIn"]:
                    print("登录成功，登录态已保存到独立 profile", flush=True)
                    time.sleep(2)
                    break
            except Exception:
                pass  # 扫码后页面会刷新，刷新瞬间读不到
            if i % 10 == 9:
                print(f"还在等待登录…（{(i + 1) * 3} 秒）", flush=True)
        else:
            print("5 分钟内没检测到登录，退出", flush=True)
        ctx.close()


def quota_left(limit: int) -> int:
    q = json.loads(QUOTA.read_text(encoding="utf-8")) if QUOTA.exists() else {}
    return limit - q.get(str(date.today()), 0)


def quota_add(n: int):
    q = json.loads(QUOTA.read_text(encoding="utf-8")) if QUOTA.exists() else {}
    q[str(date.today())] = q.get(str(date.today()), 0) + n
    QUOTA.write_text(json.dumps(q), encoding="utf-8")


def seen_keys() -> set:
    keys = set()
    for f in OUTDIR.glob("*.jsonl"):
        for l in f.read_text(encoding="utf-8").splitlines():
            keys.add(json.loads(l)["key"].split("#")[0])
    return keys


def num(x) -> int:
    return int(x) if str(x).isdigit() else (int(float(str(x)[:-1]) * 10000) if str(x).endswith("万") else 0)


def save_images(key: str, urls: list[str]):
    d = IMGDIR / key
    d.mkdir(parents=True, exist_ok=True)
    for i, u in enumerate(urls[:15], 1):
        f = d / f"{i:02d}.webp"
        if f.exists():
            continue
        req = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": "https://www.xiaohongshu.com/"})
        with urllib.request.urlopen(req, timeout=20) as r:
            f.write_bytes(r.read())
        time.sleep(0.4)


def cmd_crawl(a):
    left = quota_left(a.daily)
    if left <= 0:
        print(f"今天已到上限 {a.daily} 篇，退出")
        return
    seen = seen_keys()
    out = OUTDIR / f"crawl_{date.today():%Y%m%d}.jsonl"
    todo_f = OUTDIR / "img_todo.json"
    todo = json.loads(todo_f.read_text(encoding="utf-8")) if todo_f.exists() else {}
    stats = {"notes": 0, "img_notes": 0, "skipped_seen": 0}
    with sync_playwright() as p:
        ctx, page = open_ctx(p, headless=a.headless)
        try:
            page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            if not safe_eval(page, JS_STATE)["loggedIn"]:
                print("未登录，先运行 login")
                return
            zero_streak = 0
            for ki, kw in enumerate(a.kw):
                if ki:
                    time.sleep(random.uniform(15, 25))  # 搜索之间也留间隔，避免连发
                url = "https://www.xiaohongshu.com/search_result?keyword=" + urllib.parse.quote(kw) + "&source=web_search_result_notes"
                if not goto_safe(page, url):
                    zero_streak += 1
                    if zero_streak >= 2:
                        print("  连续 2 次搜索失败，停止本批")
                        return
                    continue
                feeds = []
                for _ in range(8):  # 结果可能在页面加载后异步填充，最多等约 12 秒
                    page.wait_for_timeout(1500)
                    feeds = safe_eval(page, JS_FEEDS)
                    if feeds:
                        break
                if not feeds:
                    head = safe_eval(page, "() => document.body.innerText.slice(0, 200)")
                    print(f"[{kw}] 搜索 0 条，页面开头：{head!r}", flush=True)
                    if any(w in head for w in STOP_WORDS):
                        print("  遇到验证或登录提示，停止")
                        return
                    zero_streak += 1
                    if zero_streak >= 2:
                        print("  连续 2 个关键词 0 条，疑似限流，停止本批")
                        return
                    continue
                zero_streak = 0
                cands = [f for f in feeds if not any(c in f["title"] for c in BAD_CITY)]
                picked = []
                for f in cands:
                    k = fnv(f["id"])
                    if k in seen:
                        stats["skipped_seen"] += 1
                        continue
                    picked.append((k, f))
                    if len(picked) >= a.per_kw:
                        break
                print(f"[{kw}] 搜索 {len(feeds)} 条，新笔记 {len(picked)} 篇", flush=True)
                for k, f in picked:
                    if left <= 0:
                        print("到达今日上限，停止")
                        return
                    time.sleep(random.uniform(*a.gap))
                    if not goto_safe(page, f"https://www.xiaohongshu.com/explore/{f['id']}?xsec_token="
                                           f"{urllib.parse.quote(f['tok'])}&xsec_source=pc_search", referer=url):
                        print(f"  {k} 打开失败，跳过")
                        continue
                    page.wait_for_timeout(3500)
                    n = safe_eval(page, JS_NOTE, f["id"])
                    if not n["ok"]:
                        if any(w in n["head"] for w in STOP_WORDS):
                            print(f"  遇到验证或登录提示，停止：{n['head'][:60]!r}")
                            return
                        print(f"  {k} 读不到正文，跳过")
                        continue
                    rec = {"key": k, "note_id": f["id"], "kwd": kw, "title": n["title"], "text": n["text"], "ts": n["ts"],
                           "ip": n["ip"], "likes": n["likes"], "collects": n["collects"], "ncomments": n["ncomments"],
                           "tags": n["tags"], "nimg": len(n["imgs"]), "comments": n["comments"],
                           "imgs": n["imgs"][:15]}  # CDN 签名链接几小时内有效，留给抽取后补读图
                    with out.open("a", encoding="utf-8") as fo:
                        fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    seen.add(k)
                    left -= 1
                    quota_add(1)
                    stats["notes"] += 1
                    body = n["text"].split("#")[0].strip()
                    image_heavy = len(body) < 80 and max(num(n["likes"]), num(n["collects"])) >= 100 and n["imgs"]
                    if image_heavy and k not in todo:
                        try:
                            save_images(k, n["imgs"])
                            todo[k] = {"title": n["title"], "ts": n["ts"], "urls": n["imgs"][:15]}
                            stats["img_notes"] += 1
                        except Exception as e:
                            print(f"  {k} 图片下载失败 {type(e).__name__}")
                    print(f"  {k} {n['title'][:24]} | 赞{n['likes']} 藏{n['collects']} 评{n['ncomments']}"
                          + (" | 纯图，已存图" if image_heavy else ""), flush=True)
        finally:
            todo_f.write_text(json.dumps(todo, ensure_ascii=False, indent=1), encoding="utf-8")
            ctx.close()
    print("汇总", json.dumps(stats, ensure_ascii=False), "| 今日剩余", quota_left(a.daily))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    c = sub.add_parser("crawl")
    c.add_argument("--kw", action="append", required=True)
    c.add_argument("--per-kw", type=int, default=5)
    c.add_argument("--daily", type=int, default=100)
    c.add_argument("--gap", type=float, nargs=2, default=(12, 20))
    c.add_argument("--headless", action="store_true")
    a = ap.parse_args()
    {"login": cmd_login, "crawl": cmd_crawl}[a.cmd](a)


if __name__ == "__main__":
    main()
