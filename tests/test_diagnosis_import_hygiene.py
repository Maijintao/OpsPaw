"""导入卫生:函数内的导入不许遮蔽模块级名字。

为什么要有这条(真事故,2026-10-15):在 `detect._res_profile` 的 except 分支里写了
`from app.diagnosis import net as _net`,而该模块顶层本来就有 `from ... import net as _net`。
函数内一旦出现同名导入,Python 会把 `_net` 当成**整个函数的局部名**,于是 try 里更早的
`_net.client(...)` 直接 UnboundLocalError——CPU/内存两个维度整条哑掉,单测还全绿(那段
没有覆盖),是靠活靶子回放才发现的。

规则:函数体**第一层**的冗余导入无害(绑定发生在任何使用之前);**嵌在 try/except/if/for
等块里**的导入是条件绑定,遮蔽模块级名字时必炸。这里只拦后者。
"""
import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _module_bound_names(tree: ast.Module) -> set[str]:
    """模块级被绑定的名字:导入别名 + 函数/类名 + 赋值目标。"""
    names: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                names.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def _nested_imports(fn) -> list[tuple[str, int]]:
    """函数里**不在第一层**的导入(嵌在块内 = 条件绑定)。"""
    out: list[tuple[str, int]] = []

    def walk(node) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for a in child.names:
                    out.append(((a.asname or a.name).split(".")[0], child.lineno))
            walk(child)

    for stmt in fn.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue          # 第一层导入:绑定在使用之前,无害
        walk(stmt)
    return out


def test_no_nested_import_shadows_module_global():
    offenders: list[str] = []
    for p in sorted(_APP.rglob("*.py")):
        tree = ast.parse(p.read_text())
        globals_ = _module_bound_names(tree)
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for name, line in _nested_imports(fn):
                if name in globals_:
                    offenders.append(
                        f"{p.relative_to(_APP.parent)}:{line} 函数 {fn.name}() "
                        f"块内 import 遮蔽了模块级名字 {name!r}(会让该名字在本函数里变成"
                        f"局部引用 → UnboundLocalError)")
    assert not offenders, "导入遮蔽:\n" + "\n".join(offenders)
