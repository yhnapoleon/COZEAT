"""从 Remote_Intern/keys/*.txt 生成 infra/.env。只写文件，不打印任何 key。

用法：python -I infra/make_env.py
已存在的 .env 会保留其中不在映射表里的行（例如手工加的可选项）。
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS = HERE.parent.parent / "keys"
ENV = HERE / ".env"

# keys 目录里的文件名 → env 变量名；值是元组时按行依次对应
MAPPING = {
    "gaode.txt": "AMAP_WEB_SERVICE_KEY",
    "huoshan.txt": "DOUBAO_SEARCH_API_KEY",
    "volcano.txt": "ARK_API_KEY",
    "jutuike.com": "JUTUIKE_API_KEY",
    "claude.txt": "ANTHROPIC_API_KEY",
    "openrouters.txt": "OPENROUTER_API_KEY",
    "bocha.txt": "BOCHA_API_KEY",
    "baidu.txt": "BAIDU_QIANFAN_API_KEY",
    "webjs.txt": ("AMAP_JS_KEY", "AMAP_JS_SECURITY_CODE"),
    "ds.txt": "DEEPSEEK_API_KEY",
}


def main():
    old = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                old[k.strip()] = v.strip()
    found = []
    wanted = []
    for fname, names in MAPPING.items():
        names = names if isinstance(names, tuple) else (names,)
        wanted += names
        p = KEYS / fname
        if not p.exists():
            continue
        rows = [r.strip() for r in p.read_text(encoding="utf-8").splitlines() if r.strip()]
        for var, val in zip(names, rows):
            old[var] = val
            found.append(var)
    lines = ["# 由 infra/make_env.py 生成，不要提交"] + [f"{k}={v}" for k, v in old.items()]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    missing = [v for v in wanted if v not in found]
    print("已写入变量:", ", ".join(found))
    print("缺少:", ", ".join(missing) or "无")


if __name__ == "__main__":
    main()
