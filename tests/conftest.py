"""离线测试的公共部分：把代码节点源码（bridge/flow_code.py 里的 CODE_* 字符串）在本机按 Coze 沙箱的约定跑起来。

沙箱约定：源码里定义 async def main(args: Args) -> Output，入参在 args.params，返回 dict。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT))

import flow_code  # noqa: E402


class Args:
    def __init__(self, params: dict):
        self.params = params


def run_node(code: str, **params) -> dict:
    ns = {"Args": Args, "Output": dict}
    exec(compile(code, "<code-node>", "exec"), ns)
    return asyncio.run(ns["main"](Args(params)))


def card_of(answer: str) -> dict | None:
    """回答末尾的 ```card JSON；没有返回 None。"""
    if "```card" not in answer:
        return None
    body = answer.split("```card", 1)[1].split("```", 1)[0]
    return json.loads(body)


__all__ = ["ROOT", "flow_code", "run_node", "card_of"]
