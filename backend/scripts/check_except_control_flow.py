"""静态检查：`try` 体内直接 raise 的控制流异常不得被同层 `except Exception` 吞掉。

检查的缺陷模式（issue #98，2026-10）
-------------------------------------
本仓库的端点 / 服务大量使用「宽 except + 统一包装」的兜底写法：

    try:
        ...
        raise ValidationError("文件过大")   # 自带 400
        ...
    except Exception as e:                  # 兜底
        raise AppError(f"文件解析失败: {e}")  # 被重包装成 500

当 `try` 体内**直接** raise 一个自带 HTTP 状态码的「控制流异常」时，同层的宽
except 会把它捕获并重新包装（或吞成错误字典），于是 4xx 语义被降级成
500 / 200 错误字典，调用方拿到的状态码与真实原因不符。

控制流异常 = `app.exceptions.AppError` 及其全部子类
（`ValidationError` / `AuthenticationError` / `AuthorizationError` /
`NotFoundError` / `ConflictError`），以及 `fastapi.HTTPException`。

同一缺陷模式在本仓库已按同一种病修过多次（issue #82 / #90 / #98 —— 见
`app/api/v1/endpoints/organizations.py` 首个端点的注释）。本脚本把它变成
静态门禁，防止第 8 次复发。

修法（放行处理器必须加在宽 except **之前**）：

    except AppError:      # 或按文件既有风格写 except (AppError, NotFoundError, ...):
        raise
    except Exception as e:
        ...

判据（对每个 `ast.Try` / `ast.TryStar`，含嵌套）
-------------------------------------------------
1. `try` 体内（**不递归进嵌套函数 / 类 / lambda**，但会进入嵌套 `try` 的
   `try` 子句）存在直接 `raise <Name>(...)`，`Name` 按类名判定为控制流异常
   （解析 `from ... import X as Y` 别名与 `mod.X` 属性写法）；
2. 同层存在捕获一切的处理器（裸 `except:` 或 `except Exception:`）；
3. 在该处理器之前，不存在能把「全部被 raise 的控制流异常」放行掉的处理器
   （处理体就是裸 `raise`，且捕获的类名覆盖该异常 —— `AppError` 覆盖其全部
   子类，`HTTPException` 只被同名覆盖）。

判定只按**类名**、不解析真实继承关系：与内建 / 框架同名的本地自定义异常也
会命中，属于有意的保守误报（宁可多报，不可漏报）。

退出码：0 = 无违规；1 = 发现违规；2 = 扫描出错 —— 工具失败（读文件失败 /
非 UTF-8 解码失败 / 语法错误 / 未扫描到任何 .py 文件），**刻意与「发现违规」
的 1 区分**，避免工具故障被误读成一次真实的门禁红灯，反之亦然。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

CONTROL_FLOW_EXCEPTIONS = frozenset(
    {
        "AppError",
        "ValidationError",
        "AuthenticationError",
        "AuthorizationError",
        "NotFoundError",
        "ConflictError",
        "HTTPException",
    }
)

#: 相对被扫描根目录的子目录（任务书 §B：只扫这两个目录）。
SCAN_SUBDIRS = ("app/api/v1/endpoints", "app/services")

#: 不递归进入的作用域节点（issue #98 判据：只看 try 体内的「直接」raise）。
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


@dataclass(frozen=True)
class Violation:
    """一条违规：文件、`try` 语句所在行号、说明。"""

    filename: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.filename}:{self.line}: {self.message}"


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """`from x import y as z` 的别名表：z -> y（含 `from x import y` 的恒等映射）。"""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for name in node.names:
                aliases[name.asname or name.name] = name.name
    return aliases


def _exception_name(node: ast.expr | None, aliases: dict[str, str]) -> str | None:
    """取异常类名：支持 `Name`、`mod.Name` 属性写法与调用形式，并解析 import 别名。"""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _collect_direct_raises(stmts: list[ast.stmt], aliases: dict[str, str]) -> list[tuple[int, str]]:
    """收集这些语句里「直接」出现的 `raise <Name>(...)`（行号, 类名）。

    不递归进入嵌套函数 / 类 / lambda；会进入嵌套 `try` 的语句体。
    裸 `raise`（无 exc）与 `raise Name from ...` 均按规则处理：前者不计，后者计。
    """
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, _SCOPE_NODES):
            return
        if isinstance(node, ast.Raise) and node.exc is not None:
            name = _exception_name(node.exc, aliases)
            if name is not None:
                found.append((node.lineno, name))
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in stmts:
        visit(stmt)
    return found


def _caught_names(handler: ast.ExceptHandler, aliases: dict[str, str]) -> set[str] | None:
    """处理器捕获的类名集合；裸 `except:` 返回 None（捕获一切）。"""
    if handler.type is None:
        return None
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {name for name in (_exception_name(n, aliases) for n in nodes) if name is not None}


def _is_catch_all(handler: ast.ExceptHandler, aliases: dict[str, str]) -> bool:
    """是否捕获一切：裸 `except:` 或 `except Exception:`。"""
    names = _caught_names(handler, aliases)
    return names is None or "Exception" in names


def _is_bare_reraise(handler: ast.ExceptHandler) -> bool:
    """处理体是否就是一条裸 `raise`（即放行）。"""
    body = handler.body
    return len(body) == 1 and isinstance(body[0], ast.Raise) and body[0].exc is None


def _covers(raised: str, caught: set[str]) -> bool:
    """`caught` 里的类名能否放行 `raised` 异常（按类名近似继承：AppError 覆盖子类）。"""
    if raised == "HTTPException":
        return "HTTPException" in caught
    return "AppError" in caught or raised in caught


def _handler_label(handler: ast.ExceptHandler, aliases: dict[str, str]) -> str:
    if handler.type is None:
        return "裸 except:"
    names = _caught_names(handler, aliases) or set()
    return f"except {', '.join(sorted(names))}"


def _check_try(
    node: ast.Try | ast.TryStar, aliases: dict[str, str], filename: str
) -> Violation | None:
    """对一个 try 语句执行判据 1~3，命中则返回 Violation。"""
    control_raises = [
        (lineno, name)
        for lineno, name in _collect_direct_raises(node.body, aliases)
        if name in CONTROL_FLOW_EXCEPTIONS
    ]
    if not control_raises:
        return None

    # 判据 2：同层第一个「捕获一切」的处理器。
    swallow_index = next(
        (i for i, handler in enumerate(node.handlers) if _is_catch_all(handler, aliases)),
        None,
    )
    if swallow_index is None:
        return None

    # 判据 3：更靠前的处理器里，能否把全部控制流异常都放行掉。
    missing = {name for _, name in control_raises}
    for handler in node.handlers[:swallow_index]:
        if not _is_bare_reraise(handler):
            continue
        caught = _caught_names(handler, aliases)
        if caught is None:  # 裸 except: 的裸 raise —— 放行一切
            missing.clear()
            break
        missing = {name for name in missing if not _covers(name, caught)}
        if not missing:
            break
    if not missing:
        return None

    raised_desc = "、".join(f"{name}@{lineno}" for lineno, name in control_raises)
    swallow = node.handlers[swallow_index]
    return Violation(
        filename=filename,
        line=node.lineno,
        message=(
            f"try 体直接 raise 控制流异常（{raised_desc}），"
            f"同层 {_handler_label(swallow, aliases)}@{swallow.lineno} 之前缺少放行处理器"
            f"（{'、'.join(sorted(missing))} 未被任何「捕获后裸 raise」放行，"
            f"4xx 会被降级为 500）；请在宽 except 之前加 `except AppError: raise`"
            f"（HTTPException 同理，按文件既有风格书写）。"
        ),
    )


def find_violations(source: str, filename: str) -> list[Violation]:
    """对一段源码执行全部判据，返回违规列表（按 `try` 行号排序）。"""
    tree = ast.parse(source)
    aliases = _import_aliases(tree)
    try_nodes: tuple[type, ...] = (ast.Try, ast.TryStar)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, try_nodes):
            violation = _check_try(node, aliases, filename)
            if violation is not None:
                violations.append(violation)
    violations.sort(key=lambda v: v.line)
    return violations


def _iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for sub in SCAN_SUBDIRS:
        directory = root / sub
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.py")))
    return files


def main(argv: list[str] | None = None) -> int:
    """扫描入口。argv[0] 可选：被扫描的根目录（默认本仓库 backend/）。"""
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]).resolve() if args else Path(__file__).resolve().parents[1]

    if hasattr(sys.stdout, "reconfigure"):  # 统一 UTF-8 输出（Windows 控制台 / 管道）
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    violations: list[Violation] = []
    scanned = 0
    found_dirs: list[str] = []
    for sub in SCAN_SUBDIRS:
        if (root / sub).is_dir():
            found_dirs.append(sub)
    for path in _iter_python_files(root):
        scanned += 1
        relative = path.relative_to(root).as_posix()
        try:
            # utf-8-sig：带 BOM 的 .py 是合法 Python（CPython 接受 BOM）。用 utf-8
            # 读会把 BOM（U+FEFF）留在源码里，ast.parse 会误报「语法错误」（issue #98 评审 Minor-4）。
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            # 工具失败（含非 UTF-8 编码）必须走 exit 2：与「发现违规」的 exit 1 同码
            # 会把工具故障误读成真实门禁红灯（issue #98 评审 Minor-5）。
            print(f"{relative}: 工具失败：无法以 UTF-8 读取（{exc}）", file=sys.stderr)
            return 2
        try:
            violations.extend(find_violations(source, relative))
        except SyntaxError as exc:
            print(f"{relative}: 语法错误: {exc}", file=sys.stderr)
            return 2

    if scanned == 0:
        # 门禁不允许空转：目录缺失 / 路径错误时若「无违规」退出 0，就是假绿。
        print(
            f"{root}: 未扫描到任何 .py 文件（预期目录: {' + '.join(SCAN_SUBDIRS)}），"
            f"检查无法生效，按失败处理。",
            file=sys.stderr,
        )
        return 2

    for violation in violations:
        print(str(violation))
    if violations:
        print(f"发现 {len(violations)} 处违规（issue #98 缺陷模式），请按上方的放行提示修复。")
        return 1
    print(
        f"检查通过：扫描 {scanned} 个文件（{' + '.join(found_dirs)}），"
        f"未发现「控制流异常被 except Exception 吞掉」的缺陷模式。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
