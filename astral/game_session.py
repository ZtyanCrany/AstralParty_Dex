# -*- coding: utf-8 -*-
"""渠道客户端的会话读取（Steam 国服 / B站渠道）。

渠道登录票由渠道 SDK 下发，落盘时是加密的（BnSdk 缓存），但：
  · 客户端运行期间，会话是明文放在进程内存里的；
  · 各家客户端还会把登录返回整段写进自己的日志（含长效票，约 30 天）。
所以本模块有两条取票线：**优先读客户端日志**（免提权、不必让游戏在线、
票能用约 30 天），日志里没有才回退去读**运行中客户端的进程内存**。
这里只读本机、当前用户自己的游戏进程与客户端日志，不写、不改、不联网。

对外能力：
    list_game_processes()   本机正在运行的渠道客户端（用于显示"当前：Steam 国服"）
    log_session(kind)       ★ 从客户端日志里取长效票（kind = 'steam' / 'bilibili'）
    bili_session()          log_session('bilibili') 的快捷入口
    find_sessions()         读出的全部会话（含登录方式/账号/时间），新→旧排序
    find_access_token()     取一张票（默认取最新；可要求优先 Steam 登录的那张）
    account_label()         把会话变成一句人话（"手机号登录 · 手机号 183****56"）

一个客户端内存里可能同时留着**多张历史票**（旧会话不会立刻释放），且它们
各自仍然有效——所以必须按会话时间挑最新的一张，不能随手拿一张。
依赖仅限标准库与 Windows API，避免给打包体积添负担。
"""
import ctypes
import ctypes.wintypes as wt
import re
import subprocess
import time
from pathlib import Path

# 进程名 → (渠道标识, 界面显示名)
WATCH_PROCS = {
    'astralparty_cn.exe': ('steam_cn', 'Steam 国服'),
    'astralparty_int.exe': ('intl', '国际服'),
    'astralparty.exe': ('bili', 'B站渠道'),
    '吉星派对.exe': ('bili', 'B站渠道'),
}

# Steam 里《星趴》的 appid（国服与海外同一 appid，客户端语言决定拉哪个包）
STEAM_APP_ID = '2622000'

# 会话票在内存里的样子（两种拼写都出现过，必须都认）：
#   手机号会话：{"…","AccessToken":"0141gB…","LoginType":"phone","Phone":"183…"}
#   渠道会话：  {"access_token":"07qx…","chUid":"steam_7656…","user_id":"uc:…","expires":2592000}
_TOKEN_RE = re.compile(rb'"[Aa]ccess_?[Tt]oken"\s*:\s*"([A-Za-z0-9_\-\.]{16,400})"')

# 票前后邻域里能捡到的会话字段。
# ★ 键一律写成【带引号的字面量】并显式列出各拼写 —— 用 [Cc]h_?[Uu]id 这类字符类
#   简写在 bytes 模式下实测匹配不上（同段文本里 "chUid" 字面量却能匹配），字面量最稳。
_SESSION_FIELDS = (
    (rb'"plat"', 'plat'),
    (rb'"LoginType"', 'login_type'), (rb'"login_type"', 'login_type'),
    (rb'"chUid"', 'ch_uid'), (rb'"ch_uid"', 'ch_uid'),
    (rb'"sourceUid"', 'source_uid'),
    (rb'"Phone"', 'phone'), (rb'"phone"', 'phone'),
    (rb'"NickName"', 'nick'), (rb'"nickname"', 'nick'),
    (rb'"Time"', 'time'),
    (rb'"UserName"', 'user_name'), (rb'"user_name"', 'user_name'),
    (rb'"UserId"', 'user_id'), (rb'"user_id"', 'user_id'),
)

# plat 字段（游戏自己写的登录平台）→ 登录方式：最权威，优先看它
_PLAT_KIND = {'steam': 'steam', 'bilibili': 'bilibili', 'bili': 'bilibili',
              'taptap': 'taptap', 'tap': 'taptap', 'wechat': 'wechat', 'qq': 'qq',
              'phone': 'phone', 'ios': 'phone', 'android': 'phone'}

# chUid 前缀 → 登录方式（渠道会话不带 LoginType 字段，只能从它推）
_CHUID_KIND = (('steam', 'steam'), ('bili', 'bilibili'), ('tap', 'taptap'),
               ('wechat', 'wechat'), ('qq', 'qq'))

# 登录方式 → 界面用的人话
LOGIN_KIND = {'phone': '手机号登录', 'steam': 'Steam 登录', 'bilibili': 'B站登录',
              'taptap': 'TapTap 登录', 'wechat': '微信登录', 'qq': 'QQ 登录',
              'guest': '游客', 'channel': '渠道登录'}

SCAN_BUDGET_S = 70.0           # 单次扫描时间上限，超时就放弃（界面会提示重试）
REGION_CAP = 96 * 1024 * 1024  # 单个内存区域上限，过大的跳过

k32 = ctypes.WinDLL('kernel32', use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x00000002
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = (0x02, 0x04, 0x20, 0x40)     # RO / RW / EXEC_R / EXEC_RW


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD),
                ('th32ProcessID', wt.DWORD), ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
                ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
                ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long),
                ('dwFlags', wt.DWORD), ('szExeFile', ctypes.c_wchar * 260)]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', wt.DWORD), ('RegionSize', ctypes.c_size_t),
                ('State', wt.DWORD), ('Protect', wt.DWORD), ('Type', wt.DWORD)]


def mask_token(tok):
    """只留头尾，用于日志与界面（票是敏感信息，不进日志）"""
    t = str(tok or '')
    if len(t) <= 12:
        return '…(%d位)' % len(t)
    return '%s…%s(%d位)' % (t[:5], t[-4:], len(t))


def mask_phone(v):
    """手机号掩码：13800138000 → 138****00"""
    d = re.sub(r'\D', '', str(v or ''))
    return (d[:3] + '****' + d[-2:]) if len(d) >= 7 else (v or '')


def _exe_path(pid):
    """取进程完整路径（Unicode，比进程名可靠）"""
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ''
    finally:
        k32.CloseHandle(h)


def list_game_processes():
    """本机在跑的渠道客户端 → [{'pid','name','exe','channel','label'}]"""
    out = []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1 or not snap:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile or ''
            key = name.lower()
            info = WATCH_PROCS.get(key)
            if info is None and name in WATCH_PROCS:
                info = WATCH_PROCS[name]
            if info is not None:
                pid = int(entry.th32ProcessID)
                out.append({'pid': pid, 'name': name, 'exe': _exe_path(pid),
                            'channel': info[0], 'label': info[1]})
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    except Exception:
        pass
    finally:
        k32.CloseHandle(snap)
    return out


def _session_from(data, pos, tok):
    """从票所在位置前后捡账号字段（同一个会话 JSON 通常连在一起）"""
    seg = data[max(0, pos - 1200):pos + 600]
    s = {'token': tok, 'masked': mask_token(tok)}
    for raw, name in _SESSION_FIELDS:
        m = re.search(raw + rb'\s*:\s*"?([^",}\r\n\]\\]{1,80})', seg)
        if m:
            v = m.group(1).decode('utf-8', 'ignore').strip().strip('"')
            if v:
                s.setdefault(name, v)
    s['kind'] = kind_of(s)
    return s


def kind_of(sess):
    """会话的登录方式：依次看 plat（游戏自己写的平台）→ LoginType → chUid 前缀。"""
    s = sess or {}
    plat = str(s.get('plat') or '').strip().lower()
    if plat:
        return _PLAT_KIND.get(plat, plat)
    k = str(s.get('login_type') or '').strip().lower()
    if k:
        return k
    ch = str(s.get('ch_uid') or '').strip().lower()
    for pre, kind in _CHUID_KIND:
        if ch.startswith(pre):
            return kind
    return 'channel' if ch else ''


def _scan_sessions(pid, deadline):
    """扫进程内存 → [会话 dict]（同一张票只留字段最全的那份）"""
    out = {}
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return [], ctypes.get_last_error()
    try:
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            if time.monotonic() > deadline:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if size == 0:
                break
            prot = mbi.Protect or 0
            if (mbi.State == MEM_COMMIT and not (prot & PAGE_GUARD)
                    and not (prot & PAGE_NOACCESS) and (prot & 0xFF) in READABLE
                    and 0 < size <= REGION_CAP):
                buf = ctypes.create_string_buffer(size)
                got = ctypes.c_size_t(0)
                if k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(got)):
                    data = buf.raw[:got.value]
                    for m in _TOKEN_RE.finditer(data):
                        tok = m.group(1).decode('ascii', 'ignore')
                        s = _session_from(data, m.start(), tok)
                        old = out.get(tok)
                        if old is None or len(s) > len(old):
                            out[tok] = s
            addr = base + size
            if addr > 0x7FFFFFFFFFFF:
                break
    except Exception:
        pass
    finally:
        k32.CloseHandle(h)
    return list(out.values()), 0


def _ts(sess):
    try:
        return int(sess.get('time') or 0)
    except (TypeError, ValueError):
        return 0


def find_sessions(pid=None):
    """读本机所有渠道会话（可能同时开着国服与国际服）。

    返回 (会话列表, 扫描情况说明)；列表按会话时间新→旧排序。
    列表项：token/masked/login_type/user_name/phone/time/pid/name/label/channel
    """
    procs = list_game_processes()
    if pid:
        procs = [p for p in procs if p['pid'] == int(pid)] or procs
    if not procs:
        return [], '没有检测到运行中的游戏客户端'
    deadline = time.monotonic() + SCAN_BUDGET_S
    sessions, notes = [], []
    for p in procs:
        if time.monotonic() > deadline:
            notes.append('%s：扫描超时' % p['label'])
            break
        got, err = _scan_sessions(p['pid'], deadline)
        if err:
            notes.append('%s：读不了（err=%d，多半是该客户端以管理员运行）' % (p['label'], err))
            continue
        for s in got:
            s.update({'pid': p['pid'], 'name': p['name'], 'label': p['label'],
                      'channel': p['channel']})
            sessions.append(s)
        if not got:
            notes.append('%s：没读到会话' % p['label'])
    sessions.sort(key=_ts, reverse=True)
    return sessions, '；'.join(notes)


def find_access_token(pid=None, prefer=None):
    """取一张票。

    prefer=None 取**最新的**会话；prefer='steam' 时优先 Steam 登录的那张
    （登录页按钮写的是"用 Steam 登录"，就该优先给 Steam 会话，没有则退回最新）。
    返回会话 dict（含 token / 账号 / 登录方式）或 None。
    """
    sessions, _note = find_sessions(pid=pid)
    if not sessions:
        return None
    if prefer:
        for s in sessions:
            if kind_of(s) == str(prefer).lower():
                return s
    return sessions[0]


def _mask_mid(v, head=11, tail=4):
    """steam_76561198000000000 → steam_76561…0000"""
    v = str(v or '')
    return v if len(v) <= head + tail + 1 else '%s…%s' % (v[:head], v[-tail:])


def account_label(sess):
    """会话 → 一句人话，界面直接显示（例：Steam 登录 · 渠道账号 steam_76561…0970）"""
    if not sess:
        return ''
    k = kind_of(sess)
    kind = LOGIN_KIND.get(k, k)
    who = ''
    if k == 'steam' and sess.get('source_uid'):
        who = 'Steam ID %s' % _mask_mid(str(sess['source_uid']), 6, 4)
    elif sess.get('ch_uid'):
        who = '渠道账号 %s' % _mask_mid(sess['ch_uid'])
    elif sess.get('phone'):
        who = '手机号 %s' % mask_phone(sess['phone'])
    elif sess.get('user_name'):
        who = str(sess['user_name'])[:16]
    if kind and who:
        return '%s · %s' % (kind, who)
    return kind or who or '未知账号'


# ── 渠道客户端的登录记录（日志）：长效票，免提权、也不必让游戏在线 ──────
# 客户端会把 /account/authorize 的整段返回写进自己的日志，其中的 accessToken
# 就是 feimo 长效票（ttl≈2592000 秒 ≈ 30 天）：
#   Steam 国服  LocalLow/feimo/AstralParty_CN/BnSdk/110001933/Log/*.log
#   B站渠道     LocalLow/feimo/吉星派对/BnSdk/110001957/Log/*.log
# ⇒ 只要用该渠道登录过一次，就够工具用很久：既不必提权读内存（B站客户端以管理员
#   运行，OpenProcess 会 err=5），也不必让游戏保持在线。
# ★ 只读本机文件；令牌一律掩码，不外传、不落盘。
_LOG_DIRS = {
    'steam': ('AppData/LocalLow/feimo/AstralParty_CN/BnSdk/110001933/Log',
              'AppData/LocalLow/feimo/AstralParty_INT/BnSdk/110001933/Log'),
    'bilibili': ('AppData/LocalLow/feimo/吉星派对/BnSdk/110001957/Log',
                 'AppData/LocalLow/feimo/AstralParty_CN/BnSdk/110001957/Log'),
    'taptap': ('AppData/LocalLow/feimo/吉星派对/BnSdk/110001958/Log',),
}
_LOG_TOK_RE = re.compile(r'\\?"access_?[Tt]oken\\?"\s*:\s*\\?"([A-Za-z0-9_\-\.]{40,})')
_LOG_STEAM_NAME_RE = re.compile(r'SteamName:\s*([^,\r\n]{1,32})')
_LOG_STEAM_ID_RE = re.compile(r'SteamId:\s*(\d{10,20})')
_LOG_UNAME_RE = re.compile(r'\\?"uname\\?"\s*:\s*\\?"([^"\\]{1,32})')
# TapTap 的昵称在 {"avatar":…,"gender":…,"name":…} 里；裸 name 也可能是客服名
# （日志里 service_info 就有 "name":"微信公众号客服"），所以优先锚 avatar。
_LOG_TAP_NAME_RE = re.compile(
    r'"avatar"\s*:\s*"[^"]*"\s*,\s*"gender"\s*:\s*"[^"]*"\s*,\s*"name"\s*:\s*"([^"]{1,40})"')
_LOG_PLAIN_NAME_RE = re.compile(r'\\?"name\\?"\s*:\s*\\?"([^"\\]{1,40})')
_LOG_TTL_RE = re.compile(r'\\?"ttl\\?"\s*:\s*(\d+)')


def log_session(kind='steam'):
    """从某个渠道客户端的日志里取一张长效票（最近用过的优先）。

    kind: 'steam' / 'bilibili'。返回 dict 或 None：
        token / masked / kind / user（Steam 昵称或渠道用户名）/ steam_id /
        log / log_name / ttl / mtime
    日志按启动次数轮转，带票的那份会被挤到中间，所以全部扫一遍（单份几十 KB）。
    """
    kind = (kind or '').lower()
    cands = []
    for rel in _LOG_DIRS.get(kind, ()):
        d = Path.home() / rel
        if not d.is_dir():
            continue
        try:
            files = sorted(d.glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            continue
        for f in files:
            try:
                txt = f.read_text(encoding='utf-8', errors='ignore')
            except Exception:
                continue
            # B站原生票以 _sh 结尾（B站 OAuth 票，喂给 feimo 会被拒）
            toks = [t for t in _LOG_TOK_RE.findall(txt) if not t.endswith('_sh')]
            if not toks:
                continue
            user, steam_id = '', ''
            if kind == 'steam':
                # 同一个日志目录里也可能有手机号登录的记录 —— 只认真正走 Steam 的那些
                m = _LOG_STEAM_NAME_RE.search(txt)
                if not m:
                    continue
                user = m.group(1).strip()
                sid = _LOG_STEAM_ID_RE.search(txt)
                steam_id = sid.group(1) if sid else ''
            elif kind == 'taptap':
                m = _LOG_TAP_NAME_RE.search(txt) or _LOG_PLAIN_NAME_RE.search(txt)
                user = m.group(1).strip() if m else ''
            else:
                m = _LOG_UNAME_RE.search(txt)
                user = m.group(1).strip() if m else ''
            t = _LOG_TTL_RE.search(txt)
            cands.append({'token': toks[-1], 'masked': _mask_mid(toks[-1], 6, 6),
                          'kind': kind, 'user': user, 'steam_id': steam_id,
                          'log': str(f), 'log_name': f.name,
                          'ttl': int(t.group(1)) if t else 0,
                          'mtime': f.stat().st_mtime})
    if not cands:
        return None
    cands.sort(key=lambda x: x['mtime'], reverse=True)
    return cands[0]


def bili_session():
    """B站渠道的登录记录（log_session 的快捷入口，保留原名字）。"""
    return log_session('bilibili')


if __name__ == '__main__':
    ps = list_game_processes()
    print('在跑的渠道客户端：%d 个' % len(ps))
    for p in ps:
        print('   PID %-7d %-22s %-10s %s' % (p['pid'], p['name'], p['label'], p['exe']))
    t0 = time.monotonic()
    sess, note = find_sessions()
    print('\n读到会话 %d 个（用时 %.1f 秒）%s'
          % (len(sess), time.monotonic() - t0, ('　备注：' + note) if note else ''))
    for s in sess:
        print('   %-26s  %-28s time=%s  (%s)'
              % (s['masked'], account_label(s), s.get('time', '?'), s.get('label')))
