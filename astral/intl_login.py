# -*- coding: utf-8 -*-
"""国际服（AstralParty_INT）自助登录：直接复用本机客户端的登录票。

票由客户端自己写在 mmkv.default 里（不在渠道 SDK 日志里）：

    fl_dft#user_steam = {"u":"u:jp:cn:<账号号>","it":"<票>","ite":<过期时间>,
                         "tk":"<旧票,不能用>","lk":"steam", …}

握手只用 it（JWT，249 字符）；tk 是同一份 JSON 里的旧票，服务器判 err=10000，
读出来只为显示。两个文件都只读，取到的值不打印、不落盘。

设备号取客户端 SDK 上报的 did（FeMooSDK_pushEvent.db）。

握手：auth = Abroad(3) + abroad{source:1, sid, deviceId}，source 只接受 1。
服务器对反复握手会限流，所以一次登录只发一次，且先用 ite 判断票是否过期。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

# ── 国际服客户端的数据目录（Steam 版）──
INT_DIR = Path.home() / 'AppData/LocalLow/feimo/AstralParty_INT'
MMKV = INT_DIR / 'mmkv.default'
PUSHDB = INT_DIR / 'FeMooSDK_pushEvent.db'

# 国际服游戏服（与国服同为 8800 端口）
GAME_HOST = '8.211.150.179'
GAME_PORT = 8800

# JWT 的形状：eyJ 开头 + 两段 base64url。tk 那种随机串不会命中
_JWT = re.compile(rb'eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{10,}')


def _varint(buf: bytes, i: int):
    """MMKV 的长度都是 varint（每字节 7 位，最高位是续位）。"""
    val = shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
        if shift > 28:
            break
    return -1, i


def _decode_val(raw: bytes) -> str:
    """剥掉值前面的类型字节再解。

    类型字节的长度不固定，优先取能解成 JSON 的那一种切法（fl_dft#user_steam
    是 JSON 串），都不行再退回第一个能按 UTF-8 解的。
    """
    first = ''
    for cut in (0, 1, 2, 3):
        try:
            s = raw[cut:].decode('utf-8')
        except UnicodeDecodeError:
            continue
        if not first:
            first = s
        if s[:1] in '{[':
            try:
                json.loads(s)
                return s
            except Exception:
                continue
    return first


def read_mmkv(path: Path | None = None) -> dict:
    """解析 MMKV 文件（键 → str）。记录从偏移 8 开始：varint 键长 | 键 | varint 值长 | 值"""
    p = Path(path or MMKV)
    if not p.exists():
        return {}
    data = p.read_bytes()
    out, i, n = {}, 8, len(data)
    while i < n:
        klen, i = _varint(data, i)
        if klen <= 0 or i + klen > n:
            break
        key = data[i:i + klen]
        i += klen
        vlen, i = _varint(data, i)
        if vlen < 0 or i + vlen > n:
            break
        val = data[i:i + vlen]
        i += vlen
        try:
            out[key.decode('utf-8')] = _decode_val(val)
        except UnicodeDecodeError:
            continue
    return out


def read_user() -> dict:
    """取 fl_dft#user_steam 这个 JSON（票 / 过期时间 / 账号名都在里面）。

    直接解析不出来时，退回在文件里找 JWT 形状的串：mmkv 的布局随版本会变，
    票本身的样子不变。
    """
    out = {'it': '', 'tk': '', 'u': '', 'ite': 0}
    v = read_mmkv().get('fl_dft#user_steam', '') or ''
    try:
        obj = json.loads(v)
    except Exception:
        obj = None
    if isinstance(obj, dict):
        out['it'] = str(obj.get('it') or '')
        out['tk'] = str(obj.get('tk') or '')
        out['u'] = str(obj.get('u') or '')
        try:
            out['ite'] = int(obj.get('ite') or 0)
        except Exception:
            out['ite'] = 0
    if not out['it'] and MMKV.exists():
        hits = _JWT.findall(MMKV.read_bytes())
        if hits:
            # 票最长（文件里其它 JWT 都更短）
            out['it'] = max(hits, key=len).decode('ascii')
    return out


def read_device_id() -> str:
    """取客户端 SDK 上报的设备号 did（只读打开数据库）。"""
    if not PUSHDB.exists():
        return ''
    try:
        con = sqlite3.connect('file:%s?mode=ro' % PUSHDB.as_posix(), uri=True)
    except Exception:
        return ''
    try:
        cur = con.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in tables:
            cols = [r[1] for r in cur.execute('PRAGMA table_info("%s")' % t).fetchall()]
            if not cols:
                continue
            for row in cur.execute('SELECT * FROM "%s" ORDER BY rowid DESC LIMIT 200' % t):
                for c in row:
                    if not isinstance(c, str) or 'did' not in c:
                        continue
                    m = re.search(r'"did"\s*:\s*"([0-9a-fA-F]{20,})"', c)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    finally:
        try:
            con.close()
        except Exception:
            pass
    return ''


def read_session():
    """给登录流程用：返回 (票, 设备号, 不能登录的原因)。

    票拿不到或有原因时，前两项为空 —— 调用方直接把第三项当提示语给用户。
    """
    if not MMKV.exists():
        return '', '', '没找到海外服客户端 —— 请先用 Steam 上的海外服《星趴》登录一次'
    u = read_user()
    tok = u['it']
    if not tok:
        return '', '', '海外服客户端里没有登录票 —— 请先打开海外服登录一次'
    # 过期的票服务器必然拒绝，先看 ite 免得白撞一次（会被限流）
    if u['ite'] and u['ite'] < time.time():
        when = time.strftime('%m-%d %H:%M', time.localtime(u['ite']))
        return '', '', ('海外服登录票已于 %s 过期（该票有效期约 3 小时）—— '
                        '请打开海外服客户端重新登录一次，再回来点这个按钮。' % when)
    return tok, read_device_id(), ''


def account_name() -> str:
    """登录页显示用：mmkv 里记的账号（形如 u:jp:cn:<账号号>）。"""
    u = read_user().get('u') or ''
    return u.split(':')[-1] if u.startswith('u:') else u


def state() -> dict:
    """登录页用：本机海外服客户端有没有可用的登录票（只读，很快）。"""
    if not MMKV.exists():
        return {'ok': False, 'none': True,
                'msg': '没找到海外服客户端 —— 请先用 Steam 上的海外服《星趴》登录一次'}
    tok, did, why = read_session()
    if not tok:
        return {'ok': False, 'need_login': True, 'expired': True, 'msg': why}
    who, when = account_name(), ''
    try:
        days = int((time.time() - MMKV.stat().st_mtime) / 86400)
        when = '（刚登录过）' if days < 1 else '（%d 天前登录）' % days
    except Exception:
        pass
    # 账号取自 mmkv 的 u；读不到就留空（界面会退回显示「检测到…登录票」）。
    return {'ok': True, 'user': who, 'log': MMKV.name, 'device': bool(did),
            'label': 'Steam 海外服 登录 · %s%s' % (who or '本机客户端', when)}
