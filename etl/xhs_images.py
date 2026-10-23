"""小红书纯图笔记读图：下载图片 → Claude 读图转文字 → 写成 xhs_browser 的 JSONL 记录，交给 xhs_ingest 抽取和对齐。

只给"互动高、正文很短"的笔记用。图片地址是 CDN 带时间戳的签名链接，几小时内有效，不带 Cookie 可下载。
输入 JSON：{key: {"title": ..., "urls": [...]}}；图片缓存在 data/raw/ugc/xhs_img/<key>/。
输出记录的 key 是 "<原 key>#img"，和正文那条分开计数。

用法：python -I -X utf8 etl/xhs_images.py <urls.json>
      python -I -X utf8 etl/xhs_images.py --auto   # 从抓取记录自动挑：纯图清单 img_todo.json，
                                                    # 加上赞或藏 ≥ 100、但抽取后没有任何店名的笔记（要先跑 xhs_ingest）
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.llm import chat_vision  # noqa: E402

IMG = ROOT / "data" / "raw" / "ugc" / "xhs_img"
OUT = ROOT / "data" / "raw" / "ugc" / "xhs_browser" / "img_ocr.jsonl"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"

PROMPT = """这些图片来自一篇小红书餐饮笔记，标题是「{title}」。请把图片里和餐厅有关的信息整理成文字：
- 每家店一段：店名、分店或商场、地址或位置提示、人均或价格、推荐菜、评价原话、团购或套餐信息。
- 图片上的文字照抄；没有文字的图片，只写能确定的内容（比如看得出是什么菜），不要猜店名。
- 不是餐厅的信息不要写。只输出整理后的文字。"""


def download(key: str, urls: list[str]) -> list[tuple[str, bytes]]:
    d = IMG / key
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i, u in enumerate(urls, 1):
        f = d / f"{i:02d}.webp"
        if not f.exists():
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": "https://www.xiaohongshu.com/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                f.write_bytes(r.read())
            time.sleep(0.5)
        out.append(("image/webp", f.read_bytes()))
    return out


def auto_spec() -> dict:
    bdir = OUT.parent
    todo_f = bdir / "img_todo.json"
    spec = json.loads(todo_f.read_text(encoding="utf-8")) if todo_f.exists() else {}
    named = set()
    mf = ROOT / "data" / "processed" / "ugc_mentions.jsonl"
    if mf.exists():
        for l in mf.read_text(encoding="utf-8").splitlines():
            r = json.loads(l)
            if r["source"] == "xhs" and (r["mention"].get("shop") or "").lower() not in ("", "none", "null"):
                named.add(r["doc_id"].split("#")[0])
    num = lambda x: int(x) if str(x).isdigit() else (int(float(str(x)[:-1]) * 1e4) if str(x).endswith("万") else 0)  # noqa: E731
    for f in sorted(bdir.glob("crawl_*.jsonl")):
        for l in f.read_text(encoding="utf-8").splitlines():
            n = json.loads(l)
            if n["key"] in spec or n["key"] in named or not n.get("imgs"):
                continue
            if max(num(n.get("likes")), num(n.get("collects"))) >= 100:
                spec[n["key"]] = {"title": n["title"], "ts": n["ts"], "urls": n["imgs"]}
                print(f"[auto] {n['key']} {n['title'][:24]}：互动高但没抽出店名，加入读图")
    return spec


def main():
    spec = auto_spec() if sys.argv[1] == "--auto" else json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    done = set()
    if OUT.exists():
        done = {json.loads(l)["key"] for l in OUT.read_text(encoding="utf-8").splitlines()}
    with OUT.open("a", encoding="utf-8") as out:
        for key, v in spec.items():
            if f"{key}#img" in done:
                print(f"{key} 已读过，跳过")
                continue
            try:
                imgs = download(key, v["urls"])
            except Exception as e:  # 签名链接过期等
                print(f"{key} 图片下载失败 {type(e).__name__}，跳过")
                continue
            text = chat_vision(PROMPT.format(title=v["title"]), imgs, max_tokens=16000)
            print(f"=== {key} {v['title']}（{len(imgs)} 张图）\n{text}\n")
            out.write(json.dumps({"key": f"{key}#img", "kwd": "img", "title": v["title"], "text": text, "ts": v.get("ts", 0),
                                  "likes": "", "collects": "", "comments": [], "nimg": len(imgs)},
                                 ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
