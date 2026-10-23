"""把演示脚本自动回放并录成视频（Playwright 录屏 → ffmpeg 转 mp4）。要先启动 demo/server.py。

每一步：按 → 放下一句，等回答出来；滚到这一轮的开头，长回答再慢慢往下滚；找店回答依次点第 2、3 家卡片，展示地图联动。
回放用压缩等待（2–4 秒），真实一轮找店 18–33 秒。

  python -I -X utf8 demo/make_video.py default            # 输出 demo/videos/default_<时间>.mp4
  python -I -X utf8 demo/make_video.py default --headed   # 弹出浏览器窗口录（无头模式地图出不来时用）
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8095"
W, H = 1600, 900

# 把这一轮（用户那句 + 回答）的开头滚到可见区顶部
SCROLL_TO_TURN = """() => {
  const l = document.querySelector('.list'); const ms = [...document.querySelectorAll('.list > .msg, .list > .sys')];
  let i = ms.length - 1; while (i > 0 && !ms[i - 1].classList.contains('me')) i--; const el = ms[Math.max(0, i - 1)];
  const top = l.scrollTop + el.getBoundingClientRect().top - l.getBoundingClientRect().top - 14;
  l.scrollTo({ top, behavior: 'smooth' });
  return Math.max(0, l.scrollHeight - l.clientHeight - top);
}"""


def main():
    name = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "default"
    headed = "--headed" in sys.argv
    script = json.load(urllib.request.urlopen(f"{BASE}/api/scripts/{name}"))
    n = len(script["steps"])
    raw_dir = HERE / "videos" / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=not headed, args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        ctx = b.new_context(viewport={"width": W, "height": H}, record_video_dir=str(raw_dir), record_video_size={"width": W, "height": H})
        page = ctx.new_page()
        page.goto(f"{BASE}/?mode=demo&script={name}&wait=fast")
        page.wait_for_selector(".amap-layers", timeout=30000)
        page.wait_for_timeout(4000)  # 首页停一会儿
        for i in range(n):
            st = script["steps"][i]
            nbot = page.locator(".msg.bot").count()
            page.keyboard.press("ArrowRight")
            page.wait_for_function(
                f"document.querySelectorAll('.msg.bot').length > {nbot} && !document.querySelector('.bubble.wait')", timeout=120000)
            page.wait_for_timeout(900)
            rest = page.evaluate(SCROLL_TO_TURN)
            text = st["resp"]["markdown"]
            page.wait_for_timeout(1800 + min(len(text), 300) * 12)
            # 长回答慢慢往下滚
            while rest > 0:
                step = min(140, rest)
                page.evaluate(f"document.querySelector('.list').scrollBy({{top: {step}, behavior: 'smooth'}})")
                rest -= step
                page.wait_for_timeout(1100)
            # 找店回答：依次点第 2、3 家，地图跟着切；再展开第 1 家的评分明细和评价来源，慢慢滚过去，收起
            last = page.locator(".msg.bot").last
            shops = last.locator(".shop")
            for k in range(1, min(shops.count(), 3)):
                shops.nth(k).scroll_into_view_if_needed()
                page.wait_for_timeout(400)
                shops.nth(k).click()
                page.wait_for_timeout(1800)
            if shops.count():
                more = last.locator(".more").first
                more.scroll_into_view_if_needed()
                more.click()
                page.wait_for_timeout(1500)
                for sel in (".dims", ".aspects", ".note"):
                    el = last.locator(sel).first
                    if el.count():
                        el.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'center'})")
                        page.wait_for_timeout(2600)
                more.click()
                page.wait_for_timeout(600)
            # 记住口味之后、最后一步：打开"我的口味"看一眼
            if "记住" in st["say"] or i == n - 1:
                page.locator("header .ghost", has_text="我的口味").click()
                page.wait_for_timeout(5000)
                page.locator(".drawer-b").evaluate("e => e.scrollTo({top: e.scrollHeight, behavior: 'smooth'})")
                page.wait_for_timeout(2500)
                page.locator(".drawer-h button[aria-label='关闭']").click()
                page.wait_for_timeout(600)
            page.wait_for_timeout(1500)
            print(f"{i + 1}/{n} {st['say']}", flush=True)
        page.wait_for_timeout(3000)
        video = page.video.path()
        ctx.close()
        b.close()
    out = HERE / "videos" / f"{name}_{time.strftime('%m%d_%H%M')}.mp4"
    ff = shutil.which("ffmpeg")
    if not ff:
        print("没找到 ffmpeg，原始视频在", video)
        return
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(video), "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], check=True)
    Path(video).unlink(missing_ok=True)
    print("视频：", out)


if __name__ == "__main__":
    main()
