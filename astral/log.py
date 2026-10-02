# -*- coding: utf-8 -*-
"""统一落盘日志：把原本直接 print 的信息写进 <exe 旁>/userdata/log.txt。

打包后以 --windowed 运行没有控制台，直接 print 会丢失；本模块与
astral.gameart 的日志写同一个文件、同一格式，便于一起排查。

    log(msg)    —— 始终写盘，用于错误诊断等值得留痕的信息。
    debug(msg)  —— 仅当命令行带 --debug 或 --selftest 时写盘，用于连接收发等调试信息。

文件超过 LOG_MAX_BYTES 时截断重开，避免长期运行无限增长。
"""
from __future__ import annotations

import sys
import time

from . import paths

LOG_NAME = 'log.txt'
LOG_MAX_BYTES = 256 * 1024          # 超过即截断重开


def _debug_enabled() -> bool:
    """命令行是否请求了调试输出。"""
    argv = sys.argv or []
    return ('--debug' in argv) or ('--selftest' in argv)


def _append(msg) -> None:
    """向 userdata/log.txt 追加一行；任何异常都静默忽略（日志不能反过来影响主流程）。"""
    try:
        d = paths.DATA_DIR
        d.mkdir(parents=True, exist_ok=True)
        p = d / LOG_NAME
        try:
            if p.stat().st_size > LOG_MAX_BYTES:
                p.write_text('', encoding='utf-8')
        except OSError:
            pass
        with open(p, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), msg))
    except Exception:
        pass


def log(msg) -> None:
    """始终写入日志文件。"""
    _append(msg)


def debug(msg) -> None:
    """仅在带 --debug / --selftest 时写入日志文件。"""
    if _debug_enabled():
        _append(msg)
