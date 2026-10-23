"""create / save / test_run / poll / publish wrappers for Coze Studio workflows."""
from __future__ import annotations

import json
import time
import uuid

from coze_client import Coze

ICON = "default_icon/default_workflow_icon.png"


def create_workflow(c: Coze, name: str, desc: str) -> str:
    body = c.post("/api/workflow_api/create", {
        "name": name, "desc": desc, "icon_uri": ICON, "space_id": c.space_id(), "flow_mode": 0})
    return str(body["data"]["workflow_id"])


def save_workflow(c: Coze, workflow_id: str, schema: str) -> dict:
    return c.post("/api/workflow_api/save", {
        "workflow_id": workflow_id, "schema": schema, "space_id": c.space_id(),
        "submit_commit_id": uuid.uuid4().hex, "ignore_status_transfer": True})


def test_run(c: Coze, workflow_id: str, inputs: dict, bot_id: str | None = None, timeout_s: int = 300) -> dict:
    payload = {"workflow_id": workflow_id, "input": {k: str(v) if not isinstance(v, str) else v for k, v in inputs.items()},
               "space_id": c.space_id()}
    if bot_id:
        payload["bot_id"] = bot_id
    body = c.post("/api/workflow_api/test_run", payload)
    execute_id = body["data"]["execute_id"]
    t0 = time.time()
    while True:
        p = c.get("/api/workflow_api/get_process", {
            "workflow_id": workflow_id, "space_id": c.space_id(), "execute_id": execute_id})
        d = p["data"]
        st = d.get("executeStatus")
        if st in (2, 3, 4) or time.time() - t0 > timeout_s:
            return d
        time.sleep(2)


def summarize_process(d: dict) -> str:
    lines = [f"status={d.get('executeStatus')} cost={d.get('workflowExeCost')} reason={d.get('reason') or ''}"]
    for n in d.get("nodeResults", []) or []:
        lines.append(f"  [{n.get('NodeType')}] {n.get('NodeName')} status={n.get('nodeStatus')} "
                     f"err={n.get('errorInfo') or ''}")
        if n.get("output"):
            lines.append("    output: " + str(n["output"])[:600])
    tc = d.get("tokenAndCost") or {}
    if tc:
        lines.append(f"  tokens: {tc}")
    return "\n".join(lines)


def publish_workflow(c: Coze, workflow_id: str, version: str = "v0.0.1", desc: str = "") -> dict:
    return c.post("/api/workflow_api/publish", {
        "workflow_id": workflow_id, "space_id": c.space_id(), "has_collaborator": False,
        "workflow_version": version, "version_description": desc, "force": False})
