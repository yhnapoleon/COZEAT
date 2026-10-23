"""立即对某个用户跑一次记忆整理（经 sidecar /mem/consolidate），打印整理摘要。

用法：python -I -X utf8 bridge/mem_consolidate.py <user_id>
"""
import sys
from pathlib import Path
import os
os.chdir(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ".")
import httpx
from common.env import need
r = httpx.post("http://localhost:8090/mem/consolidate", params={"uid": sys.argv[1]}, headers={"X-Sidecar-Token": need("SIDECAR_TOKEN")}, timeout=300)
d = r.json(); print("error:", d.get("error"), "| data:", d.get("data"))
