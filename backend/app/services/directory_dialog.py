"""在运行后端的机器上弹出系统目录选择框。

浏览器给不了本地绝对路径，所以「本地源码路径」这类输入由后端代为选择。
对话框放在独立子进程里，避免 tkinter 和服务的线程 / 事件循环互相干扰。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

_TITLE = "选择后端机器上的本地目录"

_SCRIPT = """
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
root.update()
path = filedialog.askdirectory(parent=root, title=sys.argv[1], mustexist=True)
root.destroy()
if path:
    sys.stdout.write(path)
"""

_DIALOG_TIMEOUT = 300.0

_lock = threading.Lock()


class DirectoryDialogError(RuntimeError):
    """目录选择框弹不出来，或已经有一个在等待选择。"""


def pick_directory() -> str | None:
    """返回用户选中的目录；取消时返回 None。"""
    if not _lock.acquire(blocking=False):
        raise DirectoryDialogError("目录选择框已经打开，请先完成上一个选择")

    try:
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _SCRIPT, _TITLE],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                timeout=_DIALOG_TIMEOUT,
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise DirectoryDialogError("目录选择框等待超时，请重试或手动填写路径") from exc
    finally:
        _lock.release()

    if proc.returncode != 0:
        # 无桌面环境（容器 / 无显示服务）时 tkinter 初始化会失败，
        # stderr 对使用者没有意义，统一提示改用手填。
        raise DirectoryDialogError("当前后端环境无法弹出系统目录选择框，请手动填写路径")

    selected = proc.stdout.strip()
    if not selected:
        return None
    return os.path.normpath(selected)
