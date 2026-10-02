# -*- coding: utf-8 -*-
"""统一路径解析：区分「只读资源」与「可写数据」。

打包成 exe 后需注意：
  · `__file__` 指向 PyInstaller 的解包临时目录（onefile 会被清理）
  · 因此登录态/回放缓存不能用 __file__ 推导路径，否则软件关闭后会丢失

规则：
  · DATA_DIR  = exe 旁边的 userdata/      → 可写（token.json / replays / sid.txt）
  · BUNDLE_DIR= 打包时塞进去的只读资源      → ui / assets（wiki 表、协议描述符）
  · asset()   = 优先用 exe 旁边的同名文件（方便用户自己更新），找不到再回退到包里
"""
import sys
from pathlib import Path


def _exe_dir() -> Path:
    """exe 所在目录（可写）；未打包时就是项目根。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _bundle_dir() -> Path:
    """只读资源所在目录：打包后是解包目录，未打包时是项目根。"""
    mp = getattr(sys, '_MEIPASS', None)
    if mp:
        return Path(mp)
    return Path(__file__).resolve().parents[1]


EXE_DIR = _exe_dir()
BUNDLE_DIR = _bundle_dir()
DATA_DIR = EXE_DIR / 'userdata'
REPLAY_DIR = DATA_DIR / 'replays'


def asset(*parts) -> Path:
    """找只读资源：先看 exe 旁边（用户可自行替换），再回退到打包目录。"""
    p = EXE_DIR.joinpath(*parts)
    if p.exists():
        return p
    return BUNDLE_DIR.joinpath(*parts)
