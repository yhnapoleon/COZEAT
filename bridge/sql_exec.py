"""通过一条单节点维护工作流执行一条 SQL（Coze 没有直接执行 SQL 的接口）。

用法：python -I -X utf8 bridge/sql_exec.py shop "SELECT COUNT(*) AS n FROM shop"
也可以 import run_sql 在脚本里用。
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import canvas as cv  # noqa: E402
from coze_client import Coze  # noqa: E402
from workflow_api import create_workflow, save_workflow, summarize_process, test_run  # noqa: E402

IDS_FILE = HERE.parent / "lifeagent_ids.json"
# Coze 自定义 SQL 的子串黑名单（database_impl.go validateCustomSQL），值里出现也会被拒
BLOCKED = ["UNION", "JOIN", "INFORMATION_SCHEMA", "MYSQL.", "SYS.", "USER()", "LOAD_FILE", "INTO OUTFILE", "SLEEP(", "BENCHMARK("]


def sql_value(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def blocked(text: str) -> str | None:
    up = text.upper()
    for b in BLOCKED:
        if b in up:
            return b
    if re.search(r"\bFROM\b", up):  # 值里的 FROM 会让表名校验误判
        return "FROM"
    return None


def run_sql(c: Coze, table: str, sql: str, timeout_s: int = 120) -> dict:
    ids = json.loads(IDS_FILE.read_text(encoding="utf-8"))
    wid = ids.get("wf_sql_admin")
    if not wid:
        wid = create_workflow(c, "la_sql_admin", "维护用：执行单条 SQL")
        ids["wf_sql_admin"] = wid
        IDS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")
    sid = cv.new_id()
    nodes = [cv.entry([cv.out("noop")]),
             cv.custom_sql(sid, "SQL", ids["tables_online"][table], sql, [], [], pos=(400, 0)),
             cv.exit_node([cv.param("rowNum", cv.ref(sid, "rowNum", "integer")),
                           cv.param("rows", cv.ref(sid, "outputList", "list<object>", schema={"type": "object", "schema": []}))])]
    save_workflow(c, wid, cv.canvas(nodes, [cv.edge(cv.ENTRY_ID, sid), cv.edge(sid, cv.EXIT_ID)]))
    return test_run(c, wid, {"noop": "1"}, bot_id=ids["bot_id"], timeout_s=timeout_s)


def exit_output(d: dict):
    for n in d.get("nodeResults") or []:
        if n.get("NodeType") in ("End", "Exit") or n.get("nodeId") == cv.EXIT_ID:
            try:
                return json.loads(n.get("output") or "{}")
            except ValueError:
                return n.get("output")
    return None


if __name__ == "__main__":
    cli = Coze()
    cli.login()
    res = run_sql(cli, sys.argv[1], sys.argv[2])
    print(summarize_process(res)[-1500:])
