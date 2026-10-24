"""所有代码节点源码都能编译、定义了 main，并且没有 pyflakes 能查出的错（未定义名等）。

代码节点是字符串，上传到 Coze 后才会执行；这里在本机先挡住语法错和拼错的变量名。
"""
import shutil
import subprocess

import pytest

import build_memory
from conftest import flow_code


def _nodes():
    out = []
    for mod in (flow_code, build_memory):
        for k, v in vars(mod).items():
            if k.startswith("CODE_") and isinstance(v, str):
                out.append(pytest.param(v, id=mod.__name__ + "." + k))
    return out


NODES = _nodes()


def test_found_nodes():
    assert len(NODES) >= 27


@pytest.mark.parametrize("src", NODES)
def test_compiles_and_defines_main(src):
    ns = {"Args": dict, "Output": dict}
    exec(compile(src, "<code-node>", "exec"), ns)
    assert callable(ns.get("main"))


@pytest.mark.skipif(shutil.which("ruff") is None, reason="没装 ruff")
@pytest.mark.parametrize("src", NODES)
def test_pyflakes_clean(src):
    r = subprocess.run(["ruff", "check", "--isolated", "--select", "E9,F", "--output-format", "concise", "--stdin-filename", "node.py", "-"],
                       input="Args = Output = dict\n" + src, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout
