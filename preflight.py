"""启动前自检：缺依赖时给中文指引，而不是甩一屏英文堆栈。

存在的理由（客户电脑上真实踩过）：
客户 PATH 里的 python 和自己的项目环境往往不是同一个解释器，
`pip install openpyxl` 装到了 A，运行时用的是 B，报错原文是
`ModuleNotFoundError: No module named 'openpyxl'` —— 客户看不懂，
也不知道该在哪装。这里统一换成一句人话 + 一条能直接复制的命令。

两个入口（run.py 与 web/app.py）都在 import core 之前调用它。
"""

import sys


def _hint():
    return (
        "缺少依赖库 openpyxl，程序起不来。\n"
        "在项目目录下执行这一条即可（只需一次）：\n"
        f"    {sys.executable} -m pip install openpyxl\n"
        f"当前解释器：{sys.version.split()[0]} ｜ {sys.executable}\n"
        "如果刚才明明装过，说明装到了另一个 Python 上——\n"
        "就用上面这一整条命令（带完整路径）再装一次。"
    )


def ensure_deps():
    """缺库就打印中文说明并退出（退出码 3），不抛英文堆栈。"""
    try:
        import openpyxl  # noqa: F401
    except ModuleNotFoundError:
        print(_hint(), file=sys.stderr)
        raise SystemExit(3)
