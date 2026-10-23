"""读取 infra/.env，供 bridge、etl、sidecar 共用。不打印任何值。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "infra" / ".env"


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    # 进程环境变量优先（容器里由 compose 注入）
    for k in list(env):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def need(name: str) -> str:
    v = load_env().get(name) or os.environ.get(name)
    if not v:
        raise SystemExit(f"缺少 {name}，先运行 python -I infra/make_env.py 或在 infra/.env 里补上")
    return v
