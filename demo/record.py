"""按场景文件真实跑一遍，把每轮回答存成演示脚本（演示模式回放用，用户 10-22：写死数据减少不可控）。

场景文件 demo/scenarios/<名字>.json：
  {"title": "...", "steps": [{"say": "饿了"}, {"click": 0}, {"say": "...", "new_conv": true}, ...]}
  say 是用户打字发出的话；click 是点上一轮回答里的第几个选项按钮（录制时换成那个选项的文字）；
  new_conv 表示这一句开始前点"新对话"（同一个用户，换会话名，长期记忆还在）；
  set 是这一句之前点的快捷设置，例如 {"area": "海岸城", "budget": 100}（键 area/budget/dist/scene），之后的句子一直生效，
  发送时按 ctx_of 补在句末，规则和 web/src/settings.ts 的 ctxOf 一致。

每次录制用一个新的 user_id，结果写到 demo/scripts/<名字>.json（已有的同名文件先备份成 .bak.json）。
  python -I -X utf8 demo/record.py default             # 全部重录
  python -I -X utf8 demo/record.py default --upto 5    # 只录前 5 步，看一眼再决定
"""
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from coze_chat import chat  # noqa: E402
from memview import profile  # noqa: E402


SKIP = re.compile(r"记住|忘掉|忘记|偏好|团购券|退款|退吗|过期|发票|预约|怎么去|怎么走|路线|第[一二三123]家|哪个好|就它|就定|这家|那家|它怎么样|换一批")


def ctx_of(text: str, s: dict) -> list[str]:
    """快捷设置补成句末条件；和 web/src/settings.ts 的 ctxOf 保持一致。"""
    if SKIP.search(text):
        return []
    parts = []
    if s.get("area") and not re.search(r"附近|周边|一带|[城园湾区路]", text):
        parts.append(f"在{s['area']}附近")
    if s.get("budget") and not re.search(r"人均|预算|以内|块|元", text):
        parts.append(f"人均{s['budget']}以内")
    if s.get("dist") and not re.search(r"步行|走路|公里|米", text):
        parts.append(s["dist"])
    if s.get("scene") and s["scene"] not in text:
        parts.append(s["scene"])
    return parts


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "default"
    upto = int(sys.argv[sys.argv.index("--upto") + 1]) if "--upto" in sys.argv else 999
    sc = json.loads((HERE / "scenarios" / f"{name}.json").read_text(encoding="utf-8"))
    uid = "demo-rec-" + time.strftime("%m%d%H%M%S")
    conv_no, prev_opts, steps, settings = 1, [], [], {}
    for i, st in enumerate(sc["steps"][:upto]):
        if st.get("new_conv"):
            conv_no += 1
        if "click" in st:
            k = int(st["click"])
            if k >= len(prev_opts):
                print(f"第 {i + 1} 步要点第 {k + 1} 个选项，但上一轮只有 {len(prev_opts)} 个选项：{prev_opts}，停止")
                break
            say, via = prev_opts[k], "click"
        else:
            say, via = st["say"], "type"
        conv = f"{uid}-c{conv_no}"
        settings.update(st.get("set") or {})
        ctx = ctx_of(say, settings)
        text = f"{say}（{'，'.join(ctx)}）" if ctx else say
        print(f"== {i + 1}. [{via}] {text}  （{conv}）", flush=True)
        resp = chat(text, uid, conv)
        cards = ",".join(c.get("type", "") for c in resp["cards"])
        print(f"   {resp['elapsed_s']}s 卡片[{cards}] 选项{resp['options']} {resp.get('error') or ''}")
        print("   " + resp["markdown"][:300].replace("\n", "\n   "), flush=True)
        # 这一步之后的画像快照（演示模式的"我的口味"页按步显示）
        steps.append({"say": say, "via": via, "new_conv": bool(st.get("new_conv")), "set": st.get("set") or {}, "ctx": ctx,
                      "resp": resp, "profile": profile(uid)})
        prev_opts = resp["options"]
        if resp.get("error"):
            print("出错，停止")
            break
    out = HERE / "scripts" / f"{name}.json"
    out.parent.mkdir(exist_ok=True)
    if out.exists():
        out.replace(out.with_suffix(".bak.json"))
    out.write_text(json.dumps({"title": sc.get("title") or name, "user_id": uid,
                               "recorded_at": time.strftime("%Y-%m-%d %H:%M"), "steps": steps},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("写入", out, len(steps), "步")


if __name__ == "__main__":
    main()
