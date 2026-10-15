# -*- coding: utf-8 -*-
"""客户端版本号的自动探测与缓存。

登录握手（CMDID 5001 / ConnectC2S）必须带 clientVer，服务器会校验，不匹配返回
err=10012（ClientVerErr，见逆向得到的 errcode code.proto 枚举）。该值每次大版本
更新都会变，写死在代码里必然失效，故在此自动维护。

取版本的三条途径（成本从低到高）：
  1. 本机线索：启动器日志的「游戏最新版本：X」、客户端目录的 pc_package_info.txt
  2. 缓存：userdata/client_ver.json（上次探测成功的值）
  3. 线上探测：换票逐个候选版本试握手

线上探测的判据是错误码：只有 10012 表示版本不对；其它明确带 err 的拒绝
（如 10020 账号别处在线）说明版本已通过校验，可认定命中。网络超时不算命中。
探测本身只是一次普通登录，不写游戏数据、不落盘凭据；服务器对反复握手有限流，
故候选有界、两次之间留间隔、命中即停。
"""
import json
import os
import re
import time
from pathlib import Path

from . import paths

CACHE = Path(paths.DATA_DIR) / 'client_ver.json'

ERR_CLIENT_VER = 10012          # 仅此码表示客户端版本错误

LAUNCHER_SUBPATH = ('bilibili Game', 'AstralParty')


def _launcher_dirs():
    """启动器安装目录（版本信息的本机来源）；找不到返回空。"""
    bases = [os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)')]
    try:
        drives = list(os.listdrives())
    except AttributeError:
        drives = ['%s:/' % c for c in 'CDEFGH']
    bases += [d for d in drives]
    out = []
    for base in bases:
        if not base:
            continue
        d = Path(base).joinpath(*LAUNCHER_SUBPATH)
        if d.is_dir():
            out.append(d)
    return out


def _load_cache():
    try:
        return json.loads(CACHE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def cached(kind='cn', default=''):
    """上次探测成功并缓存的版本。"""
    return str(_load_cache().get(kind) or default)


def save_cache(kind, ver):
    """记住探测成功的版本（只写 client_ver.json 这一个文件）。"""
    data = _load_cache()
    if data.get(kind) == ver:
        return
    data[kind] = ver
    data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception:
        pass


def from_launcher_log():
    """启动器日志里的「游戏最新版本」，取按文件名排序的最后一条。"""
    best = None
    for d in _launcher_dirs():
        logdir = d / 'LauncherLog'
        if not logdir.is_dir():
            continue
        for f in sorted(logdir.glob('*.log')):
            try:
                text = f.read_bytes().decode('gbk', 'ignore')
            except Exception:
                continue
            for m in re.finditer(r'游戏最新版本：([0-9][0-9.]*)', text):
                v = m.group(1).strip('.')
                if v:
                    best = v
    return best


def from_package_info():
    """客户端目录的 pc_package_info.txt 里的 game_version（客户端已更新时有效）。"""
    for d in _launcher_dirs():
        for sub in ('AstralParty Game', ''):
            p = d / sub / 'pc_package_info.txt' if sub else d / 'pc_package_info.txt'
            if not p.is_file():
                continue
            try:
                v = str(json.loads(p.read_text(encoding='utf-8')).get('game_version') or '')
            except Exception:
                continue
            v = v.strip('.')
            if v:
                return v
    return None


def detect_local():
    """本机能读到的版本（启动器日志优先）。"""
    return from_launcher_log() or from_package_info()


def resolve(kind='cn'):
    """当前应使用的版本号：探测缓存 → 本机线索（仅国服）→ 代码内置默认。

    缓存优先，因为它是确实被服务器接受过的值。本机线索取自国服启动器，
    不用于海外服 —— 两服版本不同步，海外服只认缓存与内置默认。
    """
    from .sign import GAME_VERSION, GAME_VERSION_ABROAD
    if kind == 'cn':
        return cached(kind) or detect_local() or GAME_VERSION
    return cached(kind) or GAME_VERSION_ABROAD


def candidates(base):
    """按 base 生成有界候选：同段小版本 +1~+3，再加下一个次版本的前两位。

    候选不能多：每次试探都要换一张一次性票，多了会触发服务器限流。
    """
    out = []

    def add(v):
        if v and v not in out:
            out.append(v)

    m = re.match(r'^(\d+)\.(\d+)\.(\d+)', str(base or '3.2.0'))
    a, b, c = (int(x) for x in m.groups())
    for i in range(1, 4):
        add('%d.%d.%d' % (a, b, c + i))
    add('%d.%d.0' % (a, b + 1))
    add('%d.%d.0' % (a, b + 2))
    return out


def _err_of(exc):
    """从异常文本里取 err 码；取不到返回 None（例如超时）。"""
    m = re.search(r'err=(\d+)', str(exc))
    return int(m.group(1)) if m else None


def probe(host, port, make_session, base, kind='cn', extra='', gap=2.0, verbose=True):
    """逐个候选版本试握手，返回 (已登录的 client, 命中版本, ConnectS2C)。

    make_session() 每次须返回新的 (票, 设备号)：票（authorize_code）一次性，
    每次试探都要重新换一张。kind 为 'cn' 走 login_china，'abroad' 走 login_abroad。
    """
    from . import client as C

    order = []
    for v in [detect_local(), cached(kind), base] + candidates(base):
        if v and v not in order:
            order.append(v)

    def do_login(sid, did, ver):
        c = C.AstralClient(host, port, timeout=20.0)
        c.connect()
        if kind == 'cn':
            s2c = c.login_china(sid, device_id=did, extra=extra, client_ver=ver)
        else:
            s2c = c.login_abroad(sid, device_id=did, client_ver=ver)
        return c, s2c

    for ver in order:
        sid, did = make_session()
        if not sid:
            if verbose:
                print('[client_ver] 没换到票，停止探测')
            return None, None, None
        try:
            c, s2c = do_login(sid, did, ver)
            if verbose:
                print('[client_ver] 版本 %s 通过（登录成功）' % ver)
            save_cache(kind, ver)
            return c, ver, s2c
        except Exception as e:
            err = _err_of(e)
            if err == ERR_CLIENT_VER:
                if verbose:
                    print('[client_ver] 版本 %s 不对（ClientVerErr）' % ver)
                time.sleep(gap)
                continue
            if err is not None:
                # 带 err 但不是版本错误：版本已通过校验，用同一版本重登一次
                if verbose:
                    print('[client_ver] 版本 %s 通过校验（err=%d），重登一次' % (ver, err))
                save_cache(kind, ver)
                time.sleep(gap)
                sid2, did2 = make_session()
                if not sid2:
                    return None, ver, None
                try:
                    c2, s2c2 = do_login(sid2, did2, ver)
                    return c2, ver, s2c2
                except Exception as e2:
                    if verbose:
                        print('[client_ver] 重登仍失败：%s' % str(e2)[:80])
                    return None, ver, None
            # 取不到错误码（超时等）：不能据此判定版本正确
            if verbose:
                print('[client_ver] 版本 %s 尝试失败：%s' % (ver, str(e)[:70]))
            time.sleep(gap)

    if verbose:
        print('[client_ver] 全部候选未通过，请打开一次游戏启动器（它会写下最新版本）后重试')
    return None, None, None
