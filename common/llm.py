"""经 LiteLLM 网关调模型（OpenAI 协议）。ETL 和评测脚本共用。"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import need  # noqa: E402
from etl.smoke import call  # noqa: E402

GATEWAY = "http://localhost:4000/v1"
DEFAULT_MODEL = "deepseek-flash"  # 10-20 起默认 DeepSeek；Opus 太贵，只在用户明确要求时用 model="claude-opus-5-5"


def chat(prompt: str, system: str = "", model: str = DEFAULT_MODEL, max_tokens: int = 4096, timeout: int = 180) -> str:
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    st, d = call("POST", f"{GATEWAY}/chat/completions", {"Authorization": "Bearer " + need("LITELLM_MASTER_KEY")},
                 {"model": model, "max_tokens": max_tokens, "messages": msgs}, timeout=timeout)
    if st != 200:
        raise RuntimeError(f"gateway http={st} {str(d)[:300]}")
    return d["choices"][0]["message"]["content"] or ""


def chat_vision(prompt: str, images: list[tuple[str, bytes]], model: str = DEFAULT_MODEL,
                max_tokens: int = 4096, timeout: int = 300) -> str:
    """images: [(mime, 原始字节)]，按 OpenAI 协议的 image_url data URL 发给网关。"""
    import base64
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(b).decode()}"}}
        for mime, b in images]
    st, d = call("POST", f"{GATEWAY}/chat/completions", {"Authorization": "Bearer " + need("LITELLM_MASTER_KEY")},
                 {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]},
                 timeout=timeout)
    if st != 200:
        raise RuntimeError(f"gateway http={st} {str(d)[:300]}")
    return d["choices"][0]["message"]["content"] or ""


def chat_json(prompt: str, system: str = "", **kw):
    text = chat(prompt, system, **kw)
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    if not m:
        raise ValueError(f"no json: {text[:200]}")
    return json.loads(m.group(0))
