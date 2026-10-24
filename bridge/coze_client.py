"""Minimal client for the local Coze Studio internal API (session cookie auth).

Credentials live in ../local-account.txt (key=value lines). Never print them.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import requests

BASE = os.environ.get("COZE_BASE", "http://localhost:8888")
HERE = Path(__file__).resolve().parent
ACCOUNT_FILE = HERE.parent / "local-account.txt"


def load_account() -> dict:
    if not ACCOUNT_FILE.exists():
        return {}
    out = {}
    for line in ACCOUNT_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def save_account(d: dict) -> None:
    ACCOUNT_FILE.write_text(
        "\n".join(f"{k}={v}" for k, v in d.items()) + "\n", encoding="utf-8"
    )


class Coze:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["Content-Type"] = "application/json"
        self.acct = load_account()

    # --- auth -----------------------------------------------------------
    def register(self) -> dict:
        email = f"radar-{secrets.token_hex(3)}@local.test"
        password = secrets.token_urlsafe(12)
        r = self.s.post(
            f"{BASE}/api/passport/web/email/register/v2/",
            data={"email": email, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"register failed: code={body.get('code')} msg={body.get('msg')}")
        self.acct.update({"email": email, "password": password, "user_id": str(body["data"].get("user_id_str", ""))})
        save_account(self.acct)
        return body["data"]

    def login(self) -> dict:
        r = self.s.post(
            f"{BASE}/api/passport/web/email/login/",
            data={"email": self.acct["email"], "password": self.acct["password"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"login failed: code={body.get('code')} msg={body.get('msg')}")
        # cookiejar drops cookies for the bare "localhost" domain; set the header by hand
        sc = r.headers.get("Set-Cookie", "")
        token = sc.split("session_key=", 1)[1].split(";", 1)[0] if "session_key=" in sc else ""
        if not token:
            raise RuntimeError("login ok but no session_key in Set-Cookie")
        self.s.headers["Cookie"] = f"session_key={token}"
        return body["data"]

    # --- generic --------------------------------------------------------
    AUTH_CODES = (401, 700012006)  # missing cookie / "session not exist" (sessions expire after ~20 min)

    def _check(self, path: str, r, retry):
        try:
            body = r.json()
        except Exception:
            raise RuntimeError(f"{path} -> HTTP {r.status_code}: {r.text[:300]}")
        code = body.get("code") if isinstance(body, dict) else None
        if (r.status_code == 401 or code in self.AUTH_CODES) and self.acct.get("email"):
            self.login()
            return retry()
        if r.status_code >= 400 or code not in (0, None):
            raise RuntimeError(f"{path} -> HTTP {r.status_code} code={code} msg={body.get('msg')} {str(body)[:300]}")
        return body

    def post(self, path: str, payload: dict | None = None, _retried: bool = False, **kw) -> dict:
        r = self.s.post(f"{BASE}{path}", data=json.dumps(payload or {}), **kw)
        retry = (lambda: self.post(path, payload, _retried=True, **kw)) if not _retried else (lambda: (_ for _ in ()).throw(RuntimeError(f"{path}: re-login did not help")))
        return self._check(path, r, retry)

    def get(self, path: str, params: dict | None = None, _retried: bool = False) -> dict:
        r = self.s.get(f"{BASE}{path}", params=params)
        retry = (lambda: self.get(path, params, _retried=True)) if not _retried else (lambda: (_ for _ in ()).throw(RuntimeError(f"{path}: re-login did not help")))
        return self._check(path, r, retry)

    def space_id(self) -> str:
        if self.acct.get("space_id"):
            return self.acct["space_id"]
        body = self.post("/api/playground_api/space/list", {})
        spaces = body["data"]["bot_space_list"]
        sid = str(spaces[0]["id"])
        self.acct["space_id"] = sid
        save_account(self.acct)
        return sid


def main():
    c = Coze()
    if not c.acct.get("email"):
        c.register()
        print("registered new local account (credentials saved to local-account.txt)")
    c.login()
    print("login ok")
    print("space_id:", c.space_id())
    try:
        models = c.get("/api/admin/config/model/list")
        names = []
        for p in models.get("data", {}).get("provider_model_list", []) or []:
            for m in p.get("model_list", []) or []:
                names.append(m.get("name") or m.get("model_name"))
        print("models:", names)
    except Exception as e:
        print("model list:", e)


if __name__ == "__main__":
    main()
