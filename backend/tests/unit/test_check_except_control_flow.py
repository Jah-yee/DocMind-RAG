"""检查器 `scripts/check_except_control_flow.py` 的鉴别力测试（issue #98）。

证明这条门禁「有牙」：违规样例必须被检出（能红），放行样例 / 非控制流异常 /
嵌套函数里的 raise / 无 try 的文件不得误报；脚本本体再以子进程做一次端到端
断言（违规树 → 退出码 1，干净树 → 退出码 0）。

用 importlib 按路径加载脚本，不依赖 pytest 的 sys.path 约定（CI hygiene job
用 `--noconftest` 跑本文件时同样成立）。
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = BACKEND_ROOT / "scripts" / "check_except_control_flow.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_except_control_flow", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须登记进 sys.modules：dataclasses 解析字符串注解时会按模块名回查（防回归）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


# ---- 合成样例 ----------------------------------------------------------------

VIOLATION_SOURCE = """\
from app.exceptions import ValidationError


async def parse():
    try:
        raise ValidationError("文件过大")
    except Exception as e:
        raise RuntimeError(f"解析失败: {e}")
"""

RELEASED_SOURCE = """\
from fastapi import HTTPException

from app.exceptions import AppError


async def endpoint():
    try:
        raise AppError("创建失败")
    except (AppError, HTTPException):
        raise
    except Exception as e:
        raise RuntimeError(f"失败: {e}")
"""

NON_CONTROL_SOURCE = """\
async def endpoint():
    try:
        raise ValueError("普通错误")
    except Exception:
        pass
"""

NESTED_SCOPE_SOURCE = """\
from app.exceptions import AppError


async def endpoint():
    try:
        async def inner():
            raise AppError("藏在嵌套函数里")
        await inner()
    except Exception:
        pass
"""

NO_TRY_SOURCE = """\
from app.exceptions import AppError


def boom():
    raise AppError("没有 try")
"""

ALIASED_SOURCE = """\
from app.exceptions import ValidationError as VE


async def endpoint():
    try:
        raise VE("别名")
    except Exception:
        pass
"""

PARTIAL_RELEASE_SOURCE = """\
from fastapi import HTTPException

from app.exceptions import NotFoundError


async def endpoint():
    try:
        raise NotFoundError("不存在")
    except HTTPException:
        raise
    except Exception:
        pass
"""


def _line_of(source: str, needle: str) -> int:
    return next(i for i, line in enumerate(source.splitlines(), 1) if needle in line)


# ---- 纯函数判据（1~4 为任务书指定样例）-----------------------------------------


def test_violation_sample_is_detected():
    """1) 违规样例必须被检出，且指出 try 行号 + 缺席的放行提示。"""
    violations = checker.find_violations(VIOLATION_SOURCE, "sample.py")

    assert len(violations) == 1
    assert violations[0].filename == "sample.py"
    assert violations[0].line == _line_of(VIOLATION_SOURCE, "try:")
    assert (
        f"ValidationError@{_line_of(VIOLATION_SOURCE, 'raise ValidationError')}"
        in violations[0].message
    )
    assert "放行" in violations[0].message


def test_released_sample_is_not_flagged():
    """2) 已有 `except (AppError, HTTPException): raise` 放行的样例不得检出。"""
    assert checker.find_violations(RELEASED_SOURCE, "ok.py") == []


def test_non_control_flow_raise_is_not_flagged():
    """3) 非控制流异常（ValueError）不得检出。"""
    assert checker.find_violations(NON_CONTROL_SOURCE, "plain.py") == []


def test_raise_in_nested_function_is_not_flagged():
    """4) raise 出现在 try 内的嵌套函数里不得检出（执行流不经过外层 try）。"""
    assert checker.find_violations(NESTED_SCOPE_SOURCE, "nested.py") == []


def test_file_without_try_is_not_flagged():
    """5) 没有 try 的文件不得检出。"""
    assert checker.find_violations(NO_TRY_SOURCE, "no_try.py") == []


# ---- 补充判据（别名解析、部分放行）--------------------------------------------


def test_import_alias_is_resolved():
    """`from app.exceptions import ValidationError as VE` 后 raise VE(...) 仍应命中。"""
    violations = checker.find_violations(ALIASED_SOURCE, "alias.py")

    assert len(violations) == 1
    assert f"ValidationError@{_line_of(ALIASED_SOURCE, 'raise VE')}" in violations[0].message


def test_partial_release_still_flagged():
    """只放行了 HTTPException，挡不住被 raise 的 NotFoundError —— 仍算违规。"""
    violations = checker.find_violations(PARTIAL_RELEASE_SOURCE, "partial.py")

    assert len(violations) == 1
    assert "NotFoundError" in violations[0].message


# ---- 端到端（子进程）---------------------------------------------------------


def _make_tree(tmp_path: Path, source: str, name: str) -> Path:
    target = tmp_path / "app" / "api" / "v1" / "endpoints"
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(source, encoding="utf-8")
    return tmp_path


def _run_script(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(root)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def test_script_exits_1_on_violating_tree(tmp_path):
    """6) 把违规源码写进临时文件后，脚本必须以退出码 1 结束并点名该文件。"""
    proc = _run_script(_make_tree(tmp_path, VIOLATION_SOURCE, name="bad.py"))

    assert proc.returncode == 1, proc.stderr
    assert "bad.py" in proc.stdout
    assert "放行" in proc.stdout


def test_script_exits_0_on_clean_tree(tmp_path):
    """对照：放行样例的临时树 → 退出码 0（防「永远红」的假门禁）。"""
    proc = _run_script(_make_tree(tmp_path, RELEASED_SOURCE, name="ok.py"))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "检查通过" in proc.stdout


def test_script_exits_2_on_empty_tree(tmp_path):
    """空树（预期目录不存在）→ 退出码 2：门禁不允许「扫了 0 个文件」的假绿。"""
    proc = _run_script(tmp_path)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "未扫描到任何 .py 文件" in proc.stderr


def test_non_utf8_file_exits_2(tmp_path):
    """非 UTF-8 文件属工具失败：必须 exit 2 且明确报「工具失败」，不得裸崩成 exit 1（评审 Minor-5）。"""
    target = tmp_path / "app" / "api" / "v1" / "endpoints"
    target.mkdir(parents=True, exist_ok=True)
    (target / "latin.py").write_bytes(b'x = "\xe9\xff"\n')

    proc = _run_script(tmp_path)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "工具失败" in proc.stderr


def test_bom_file_is_not_misdetected(tmp_path):
    """带 BOM 的文件是合法 Python（评审 Minor-4）：干净文件必须 exit 0（不得误报语法错误），违规仍照常检出。"""
    clean_root = _make_tree(tmp_path / "clean", "\ufeff" + RELEASED_SOURCE, name="ok.py")
    proc = _run_script(clean_root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "检查通过" in proc.stdout

    bad_root = _make_tree(tmp_path / "bad", "\ufeff" + VIOLATION_SOURCE, name="bad.py")
    proc = _run_script(bad_root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
