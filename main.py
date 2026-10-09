# -*- coding: utf-8 -*-
"""星趴档案 · 本地档案查看工具（pywebview + WebView2）

流程：输入本机使用者的手机号 → 短信验证码 → 登录本人账号
→ 本机生成全量档案 → 导出纯战绩文本（不含任何登录信息）

用法：python main.py          （开发）
      打包后双击 星趴档案.exe
"""
import json
import re
import time
import sys
import threading
import urllib.request
import webview
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from astral import paths                      # noqa: E402
from astral.paths import asset, DATA_DIR      # noqa: E402
ROOT = paths.EXE_DIR                          # 打包后 = exe 所在目录（可写）
DATA_DIR.mkdir(parents=True, exist_ok=True)

from astral import client as C                       # noqa: E402
from astral import proto_loader                      # noqa: E402
from astral.sdk_login import (DEFAULT_DEVICE_ID,     # noqa: E402
                              DEFAULT_EXTRA, request_code, phone_login,
                              password_login)
from astral.sdk_login import (patch_token_meta, clear_token,  # noqa: E402
                              load_token_info,)
from astral.sdk_login import (auto_login as _auto,   # noqa: E402
                              save_token, load_token, token_login)
from astral import game_session                      # noqa: E402
from astral import intl_login                        # noqa: E402

GAME_HOST, GAME_PORT = '101.132.186.71', 8800

# ── 静态表 ──────────────────────────────────────────────
def _load_tables():
    hero, title = {}, {}
    f = asset('assets', 'data', 'character_ids.json')
    if f.exists():
        for h in json.loads(f.read_text(encoding='utf-8')):
            hero[h['id']] = h['name']
    f2 = asset('assets', 'data', 'heroes.json')
    if f2.exists():
        for h in json.loads(f2.read_text(encoding='utf-8')):
            try:
                title[int(h['id'])] = h.get('title') or ''
            except (TypeError, ValueError):
                pass
    return hero, title


HERO, TITLE = _load_tables()
MODE = {1: '标准对战', 2: '合作挑战', 3: '限时玩法', 4: '合作挑战'}
MAPS = {81005: '龙宫游乐园（旧）', 81007: '梦想号-上层甲板', 82007: '星趴·梦想号', 82008: '御魂庆典',
        82010: '水乡古镇', 82012: '魔法学院', 82013: '龙宫游乐园',
        82014: '幽魂暗巷', 82015: '园林中庭', 82016: '异变图书馆',
        83001: '海选赛运动场', 83002: '淘汰赛运动场', 83003: '决赛大赛场'}

# 地图 ID 与 wiki 预览图编号之间不存在稳定对应关系（81005 这类老地图可由
# 筹码表的地图限定字段 mapLimit 反查到编号，但并不服从「后三位」规律），
# 因此只收录有官方数据来源的条目；查不到时按「地图8xxxx」如实显示。


def map_name(mid):
    """地图 ID → 名字。仅查已知映射表，查不到时显示「地图8xxxx」。"""
    if not mid:
        return ''
    try:
        mid = int(mid)
    except Exception:
        return ''
    return MAPS.get(mid) or ('地图%d' % mid)


# ── 地图任务名（来自 Wiki「地图任务」段，顺序与任务ID顺序一致，可经 target_num 交叉验证）──
MISSIONS = {}
try:
    _mf = asset('assets', 'data', 'missions.json')
    if _mf.exists():
        MISSIONS = json.loads(_mf.read_text(encoding='utf-8'))
except Exception:
    MISSIONS = {}


def mission_names(mid, mission_ids):
    """{第几个任务(1基): 任务名}"""
    rec = MISSIONS.get(str(mid)) or {}
    tasks = rec.get('normal') or []
    out = {}
    for i, _mid in enumerate(sorted(mission_ids or []), 1):
        if i <= len(tasks):
            out[i] = tasks[i - 1]
    return out


# ══════════ 皮肤 / 装扮（缺位法）══════════
# 服务器为每个物品分配一个**固定槽位**，未拥有的槽位为空洞。
#   空洞数 = 缺少的皮肤套数；缺的是 Wiki 皮肤名顺序里【最前面】那几套
#   （游戏把未拥有的排在列表最后，例如「清凉盛夏」排最后）
POT_WIKI_FILE = asset('assets', 'data', 'potential.json')   # 来自 Wiki 抓取，可能滞后
POT_LIVE_FILE = asset('assets', 'data', 'potential_live.json')   # 来自游戏内存的客户端配置表


def _load_potential():
    """角色「潜能是否已开放」清单。

    来源优先级（游戏会持续开放新角色的潜能，Wiki 数据存在滞后）：
      ① potential_live.json  —— 从游戏进程内存读取的客户端配置表，优先使用，
                                游戏运行时自动同步，见 sync_potential_from_game
      ② potential.json  —— Wiki 抓取，兜底（滞后于游戏更新）
    返回 ({角色名: True/False}, 来源说明)
    """
    for f, tag in ((POT_LIVE_FILE, '游戏'), (POT_WIKI_FILE, 'Wiki')):
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
            out = {}
            for k, v in (d or {}).items():
                if isinstance(v, dict):
                    r = v.get('released')
                else:
                    r = v
                if r is not None:
                    out[k] = bool(r)
            if out:
                return out, tag
        except Exception:
            continue
    return {}, '未知'



# ═══════════════════════════════════════════════════════════════════════
# 复盘数据：从【回放文件】解析
#
#   回放服务器：国服 sereplaycn / 国际服 sereplayjp
#     （按登录区服在 REPLAY_HOSTS 里自动选；两种服的回放号互不相通）
#     · 公开静态文件，无需凭据
#     · 内容 = 压缩后的 protobuf 序列（开头 18 字节自定义头 + 一串 model.Room 快照）
#     · 每帧 = 一个 model.Room，字段：2=房名 5=map_id 6=房主 9=players…
#     · 取【最后一帧】，每个 player 的 hero(字段10) 里：
#         2=hero_id  7=gold  46=standingPainting(皮肤ID)  47=cond
#       cond = protocol.ConditionalInfo ← 一局的全部战绩：
#         1 kill_count 击杀 · 2 total_damage 伤害 · 3 pk_damage_max 最大单击
#         4 total_die 死亡 · 5 total_injured 承伤 · 13 gold 星币
#         20 transferGold 转账 · 21 initGoldCount 初始星币
#         23 kill_pve_monster 击杀怪物 · 24 treatmentScore 治疗 · 25 movePoint 移动
#         26 battleDiceSixCount 骰6 · 27 finalKillBoss 击杀Boss
# ═══════════════════════════════════════════════════════════════════════
VERSION = 'v1.2.3'  # 工具版本号
REPLAY_DIR = DATA_DIR / 'replays'
# 回放服务器按区服分，两服的**回放号互不相通**（拿一边的号查另一边一律 404）。
# 登录时按 profile.region 设一次 REPLAY_REGION，之后全程跟着走 ——
# 不用让用户自己选，存档 / 复盘 / 导出都自动对上。
REPLAY_HOSTS = {'cn': 'https://sereplaycn.feimogames.com/prod/%s',
                'intl': 'https://sereplayjp.feimogames.com/prod/%s'}
REPLAY_REGION = 'cn'


def replay_url(rid):
    """按当前登录的区服拼回放地址（没登录或国服时用国服的）。"""
    return REPLAY_HOSTS.get(REPLAY_REGION, REPLAY_HOSTS['cn']) % rid


def replay_key(rid):
    """缓存键。回放号只在各自服务器内唯一，所以缓存不能只按号存 ——
    万一两边的号撞上，会把另一边的回放/地图当成自己的。"""
    return ('intl_%s' % rid) if REPLAY_REGION == 'intl' else str(rid)


# 上次登录的是哪个区服（登录页显示与开机自动登录都看它）。
# 只存显示用的元信息，**不存票**：海外服的票始终现读客户端 mmkv，
# 国服的票在 token.json 里（由 sdk_login 管理，这边不碰）。
_REGION_FILE = DATA_DIR / 'last_region.json'


def save_region(region, profile=None, account=''):
    d = profile or {}
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _REGION_FILE.write_text(json.dumps({
            'region': region, 'account': account or d.get('account') or '',
            'nick': d.get('nick') or '', 'uid': d.get('uid') or '',
            'saved_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        }, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def load_region():
    """上次登录的区服信息；没有记录就当作国服。"""
    try:
        return json.loads(_REGION_FILE.read_text(encoding='utf-8')) or {}
    except Exception:
        return {}

# ── 列表用的「只抓开头」方案 ──
# 地图 ID 在回放文件开头 300 字节内（字段5 = fixed32 小端），
# 服务器支持 HTTP Range → 不用拉整个 1.2MB。
# 前缀长度按难度字段定：第 1 个 Room 帧（含 #48 difficulty）整条消息要 ~24KB 才完整，
# 16KB 都解不出帧，所以取 32KB（一次请求同时拿到地图名和难度）。
REPLAY_PREFIX = 32768
_MAP_CACHE_FILE = DATA_DIR / 'replay_maps.json'
_map_cache = {}


def _load_map_cache():
    global _map_cache
    try:
        _map_cache = json.loads(_MAP_CACHE_FILE.read_text(encoding='utf-8')) or {}
    except Exception:
        _map_cache = {}


def _save_map_cache():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _MAP_CACHE_FILE.write_text(json.dumps(_map_cache, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def map_from_prefix(buf):
    """从回放前缀字节里提取 map_id（字段5、fixed32 小端，值域 80000~89999）。"""
    i, n = 0, len(buf)
    while i < n - 5:
        if buf[i] == 0x2d:                              # tag: field 5, wiretype 5
            v = int.from_bytes(buf[i + 1:i + 5], 'little')
            if 80000 <= v <= 89999:
                return v
        i += 1
    return 0


def diff_from_prefix(buf):
    """从回放前缀里取难度（第 1 个 Room 帧的 #48 difficulty）。

    取不到（前缀不够长 / 老回放结构）返回 None —— 界面上就不显示难度。
    """
    try:
        from astral import replay as _rp
        rp = _rp.Replay(buf)
        for f in rp.frames:
            room = f.get('room')
            if room is None:
                continue
            if getattr(room, 'difficulty', 0) or getattr(room, 'mapDifficultyId', 0):
                return int(getattr(room, 'difficulty', 0) or 0)
    except Exception:
        pass
    return None


def fetch_replay_map(replay_id):
    """只下载回放开头，返回 {'id','name','diff'}；结果落盘缓存。"""
    rid = str(replay_id or '')
    if not rid:
        return None
    k = replay_key(rid)                     # 缓存键带区服：两种服的回放号互不相通
    if k in _map_cache and 'diff' in (_map_cache.get(k) or {}):
        return _map_cache[k]                # 旧缓存没有 diff 字段 ⇒ 当未命中，重新抓
    try:
        req = urllib.request.Request(
            replay_url(rid), headers={'Range': 'bytes=0-%d' % (REPLAY_PREFIX - 1)})
        with urllib.request.urlopen(req, timeout=15) as r:
            buf = r.read(REPLAY_PREFIX)
        mid = map_from_prefix(buf)
        df = diff_from_prefix(buf)
    except Exception:
        return None
    info = {'id': mid, 'name': map_name(mid) or '未知地图', 'diff': df}
    _map_cache[k] = info
    return info

# ConditionalInfo 字段号 → 展示名
COND_NAME = {1: '击杀', 2: '伤害', 3: '最大单击', 4: '死亡', 5: '承伤', 6: '陷阱',
             7: '用陷阱', 13: '星币', 14: '击杀盗贼', 18: '星币遗物', 20: '转账',
             21: '初始星币', 23: '击杀怪物', 25: '步数', 26: '骰6', 28: '自己死亡',
             30: '龙珠', 35: '出牌', 36: '技能'}
# 界面主要展示的列
STAT_COLS = ['击杀', '伤害', '承伤', '治疗', '星币', '转账']
COND_NAME[24] = '治疗'
COND_NAME[27] = '击杀Boss'


def _rv(d, i):
    """读一个 varint"""
    v = 0
    s = 0
    while i < len(d):
        b = d[i]
        v |= (b & 0x7f) << s
        i += 1
        s += 7
        if not (b & 0x80):
            return v, i
        if s > 70:
            raise ValueError('varint too long')
    raise ValueError('eof')


def _pb(seg):
    """无 schema 的 protobuf 解析 → {字段号: [值…]}（len 型存 bytes）"""
    out = {}
    i = 0
    n = len(seg)
    while i < n:
        try:
            key, j = _rv(seg, i)
        except Exception:
            break
        f, w = key >> 3, key & 7
        if f == 0 or f > 100000:
            break
        if w == 0:
            try:
                v, j = _rv(seg, j)
            except Exception:
                break
            out.setdefault(f, []).append(v)
        elif w == 2:
            try:
                ln, j = _rv(seg, j)
            except Exception:
                break
            if ln < 0 or j + ln > n:
                break
            out.setdefault(f, []).append(seg[j:j + ln])
            j += ln
        elif w == 5:
            # 回放里数值大量用 fixed32（小端 int32），必须解成整数，不能留 bytes
            if j + 4 > n:
                break
            out.setdefault(f, []).append(int.from_bytes(seg[j:j + 4], 'little'))
            j += 4
        elif w == 1:
            if j + 8 > n:
                break
            out.setdefault(f, []).append(int.from_bytes(seg[j:j + 8], 'little'))
            j += 8
        else:
            break
        i = j
    return out


def _int(d, k, default=0):
    v = d.get(k)
    if not v:
        return default
    try:
        return int(v[0])
    except Exception:
        return default


def _str(d, k, default=''):
    v = d.get(k)
    if not v:
        return default
    try:
        return v[0].decode('utf-8', 'replace')
    except Exception:
        return default


def _w(s):
    """字符串显示宽度（中文/全角算 2 格）。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))


def _pad(s, width, right=False):
    """按显示宽度补空格，让中文排版不歪。"""
    s = str(s)
    n = max(0, width - _w(s))
    return ((' ' * n) + s) if right else (s + (' ' * n))


def fetch_replay_stats(replay_id):
    """下载（带本地缓存）并解析回放 → {玩家ID(字符串): {name,heroId,skin,gold,stats}}"""
    if not replay_id:
        return {}
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    cache = REPLAY_DIR / ('%s.bin' % replay_key(replay_id))
    if cache.exists() and cache.stat().st_size > 2000:
        data = cache.read_bytes()
    else:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 直连
        req = urllib.request.Request(replay_url(replay_id),
                                     headers={'User-Agent': 'Mozilla/5.0'})
        data = op.open(req, timeout=90).read()
        cache.write_bytes(data)

    # 从最后一帧往前回扫：最后一个快照里可能有玩家数据被清空
    # （某个玩家 hero=0 / cond 缺失），所以每个玩家要取【最近一帧里非空】的那份统计。
    # 帧起点按房号定位（房名不一定是 "match"）
    from astral.replay import room_starts
    starts = room_starts(data, replay_id)
    merged = {}
    for s in reversed(starts[-12:]):         # 最多回扫 12 帧（自最后一帧往前）
        k = s + 9                            # Room#2 房名：12 <len> <房名>
        room = _pb(data[k:])
        players = room.get(9) or []
        if not players:
            continue
        for pseg in players:
            p = _pb(pseg)
            pid = str(_int(p, 1))
            nick = _str(p, 2)
            cur = merged.get(pid) or {'name': nick, 'heroId': 0, 'skin': 0,
                                      'gold': 0, 'stats': {}}
            if nick and (not cur['name'] or cur['name'] == '—'):
                cur['name'] = nick
            for hseg in (p.get(10) or []):
                h = _pb(hseg)
                if _int(h, 2):
                    cur['heroId'] = _int(h, 2)
                if _int(h, 46):
                    cur['skin'] = _int(h, 46)
                if _int(h, 7):
                    cur['gold'] = _int(h, 7)
                for cseg in (h.get(47) or []):
                    c = _pb(cseg)
                    new = {}
                    for f, vs in c.items():
                        if f in COND_NAME:
                            try:
                                new[COND_NAME[f]] = int(vs[0])
                            except Exception:
                                pass
                    if new and not cur['stats']:
                        cur['stats'] = new      # 只取最近一帧那一份，不跨帧混
            merged[pid] = cur
        if merged and all(v['stats'] for v in merged.values()):
            break                            # 全员都有数据后结束回扫

    # 换算成【游戏结算界面的口径】（与官方结算界面逐项校验一致）：
    #   击杀 = PVP击杀(kill_count) + 击杀怪物(kill_pve_monster) + 击杀盗贼(kill_thief)
    #   星币 = 本局累计(cond#13 gold) + 开局携带(cond#21 initGoldCount)
    for rec in merged.values():
        st = rec['stats']
        if not st:
            continue
        st['击杀'] = (st.get('击杀', 0) + st.get('击杀怪物', 0)
                      + st.get('击杀盗贼', 0))
        st['星币'] = st.get('星币', 0) + st.get('初始星币', 0)
        st.pop('击杀怪物', None)
        st.pop('击杀盗贼', None)
        st.pop('初始星币', None)
    return merged

POTENT, POTENT_SRC = _load_potential()
SKIN_WIKI_FILE = asset('assets', 'data', 'skins.json')


def _load_skin_wiki():
    try:
        return json.loads(SKIN_WIKI_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}
# 皮肤 ID 编码 = 100 + <角色ID 3位> + <序号 3位>（9 位）
#   序号 001 = 初始皮肤        （每个角色都有）
#   序号 002 = 羁绊皮肤        （需先「缔结契约」= roleCard.isBreakThrough）
#   序号 003+k = Wiki 皮肤名第 k+1 套（购买/通行证获得）
#   背包中 100 段物品的条数 = 游戏显示的「拥有皮肤数量」（初始 + 羁绊 + 购买），
#   数量与官方显示逐项校验一致。
#   契约（羁绊皮肤的前置）：roleCard.isBreakThrough = 「缔结契约」，
#   其角色数与拥有羁绊皮肤的角色数一致。




# 联动角色（301~306，主播女孩重度依赖 / VA-11 HALL-A 联动）：
# 没有羁绊皮肤，且背包里没有 <角色>001 这条「初始皮肤」记录（只有 003 起的名皮）。
COLLAB_IDS = {301, 302, 303, 304, 305, 306}
# 上面这个名单只是打底：更可靠的证据来自本机游戏资源 —— 立绘变体里没有 Max 的角色必然
# 没有羁绊皮肤（35 个角色里恰好就是这六个）。两者取并集，下次出新联动角色不用改代码；
# 读不到游戏资源（没装游戏 / 没启动过）时退化成只用上面这张表，行为不变。
_BONDLESS = None


# ── 联动角色的皮肤号段（按批次整体编号）────────────────────────────────────
#
# 同一次联动的多个角色共用一个 1003XX000 号段，排布：先排所有角色的初始皮肤，再排名皮
# （第 k 个角色的初始 = 1+k，第 j 套名皮 = 2+k+j）。
# 所以 100301004 字面上属于「301 的第 4 号」，实际是 302(糖糖) 的休闲日常。
#
# 依据：100301001 / 100301003 分别点亮超天酱的初始与名皮（旧规则恰好命中）；
#       302 的 roleCard.useAdorn 指向 100301004，即该角色当前穿着的那一套；
#       背包里没有 100302xxx，只有 120302xxx（表情/头像这类，不算皮肤）。
# 遇到新联动批次时，按同一规则往这里补一项即可。
COLLAB_SKIN_SLOTS = {
    100301001: (301, 1), 100301002: (302, 1),
    100301003: (301, 2), 100301004: (302, 2),
}


def _bondless():
    """本机游戏资源给出的「没有羁绊皮肤」角色集合；取不到返回空集合。"""
    global _BONDLESS
    if _BONDLESS is None:
        try:
            from astral import gameart
            _BONDLESS = set(gameart.bondless_ids())
        except Exception:
            _BONDLESS = set()
    return _BONDLESS


def build_skins(p):
    """皮肤清单：直接从背包的 100<角色ID><序号> 空间读取，不做推断。

    契约状态取自 roleCard.isBreakThrough（游戏里的「缔结契约」按钮）。
    """
    wiki = _load_skin_wiki()
    by_id = {}
    for nm, v in wiki.items():
        if v.get('id'):
            by_id[int(v['id'])] = (nm, v)

    # ① 背包里 100 + 3位角色ID + 3位序号
    owned = {}
    for it in getattr(p, 'bag_items', []) or []:
        iid = int(getattr(it, 'item_id', 0))
        if iid in COLLAB_SKIN_SLOTS:        # 联动批次号段 ⇒ 按登记表还原到真正的角色
            cid, slot = COLLAB_SKIN_SLOTS[iid]
            owned.setdefault(cid, set()).add(slot)
            continue
        s = str(iid)
        if len(s) == 9 and s.startswith('100'):
            owned.setdefault(int(s[3:6]), set()).add(int(s[6:]))

    out, tot_orig, tot_bond, tot_buy, n_ctr = [], 0, 0, 0, 0
    cards = getattr(p, 'roleCard', {}) or {}
    # 未拥有的角色也一并列出（全清单）：它们没有背包记录 ⇒ 所有格子自然是灰的
    for hid in sorted(set(by_id.keys()) | set(cards.keys()) | set(owned.keys())):
        card = cards.get(hid)
        owns = card is not None
        has = owned.get(hid, set())
        nm, v = by_id.get(hid, ('角色#%d' % hid, {}))
        names = v.get('skins') or []
        contract = bool(getattr(card, 'isBreakThrough', False))
        collab = (hid in COLLAB_IDS) or (hid in _bondless())   # 名单打底 + 本机立绘证据
        # 联动角色没有羁绊皮肤（不显示这一格）；且背包里没有 <角色>001 这条初始皮肤
        # 记录，但「拥有角色」本身就等于拥有其初始外观 ⇒ 已拥有时初始皮肤直接算已拥有。
        # 计数仍按背包（与游戏内显示一致），不为联动角色额外 +1。
        # ── 这一行怎么排格子 ──
        # 按序号顺序拼：001 初始 → 002 羁绊（联动没有立绘就不设这一格）→ 003+ 名皮
        # → 【背包里有、名单里没有的序号一律补一格「皮肤 00X」】。
        # 补格子这条是关键：以前只画"名单里有的"，背包里多出来的条目会被计入统计却看不见，
        # 于是顶部统计与框里亮格数对不上。任何一条都不允许凭空消失。
        # 联动角色没有羁绊皮肤 ⇒ 名皮紧贴初始皮肤排（002 起）；普通角色 002 是羁绊，名皮从 003 起
        _base = 2 if collab else 3
        slots = {1: '初始皮肤'}
        if not collab:
            slots[2] = '羁绊皮肤'
        for i, sname in enumerate(names):
            slots[_base + i] = sname
        rows = []
        for _seq in sorted(set(slots) | set(has)):
            if _seq in slots:
                rows.append({'name': slots[_seq],
                             'has': (_seq in has) or (_seq == 1 and collab and owns)})
            else:
                rows.append({'name': '皮肤 %03d' % _seq, 'has': True})
        have = sum(1 for r in rows if r['has'])
        # 计数严格按「框里亮了几格」来算 ⇒ 顶部统计与列表永远对得上
        _a = 1 if rows[0]['has'] else 0                              # 初始那一格
        _b = 1 if (not collab and 2 in has) else 0                    # 羁绊那一格
        tot_orig += _a
        tot_bond += _b
        tot_buy  += have - _a - _b
        n_ctr += (1 if contract else 0)
        out.append({'id': hid, 'name': nm, 'title': v.get('title', ''),
                    'contract': contract, 'owns': owns,
                    'have': have, 'total': len(rows), 'skins': rows})
    # 已拥有的排前面（按拥有数降序），未拥有的按角色 ID 排在后面
    out.sort(key=lambda x: (0 if x['owns'] else 1, -x['have'], x['id']))
    # 游戏内「全部皮肤」= 每角色(初始+羁绊) + Wiki 命名皮肤；联动角色没有羁绊那一格
    _bl = COLLAB_IDS | _bondless()          # 分母同样用「名单 + 本机立绘证据」
    sk_total = sum((1 if int(v.get('id') or 0) in _bl else 2) + len(v.get('skins') or [])
                   for v in wiki.values())
    return {'list': out, 'orig': tot_orig, 'bond': tot_bond, 'buy': tot_buy,
            'contract': n_ctr, 'named': tot_bond + tot_buy,
            'hero_have': sum(1 for x in out if x['owns']), 'hero_total': len(out),
            'sk_total': sk_total, 'sum': tot_orig + tot_bond + tot_buy}


REVIEW_DIR = DATA_DIR / 'review'


def get_replay_file(replay_id):
    """确保本地有该局回放文件（复用既有缓存目录），返回 Path"""
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    cache = REPLAY_DIR / ('%s.bin' % replay_key(replay_id))
    if cache.exists() and cache.stat().st_size > 2000:
        return cache
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 直连
    req = urllib.request.Request(replay_url(replay_id), headers={'User-Agent': 'Mozilla/5.0'})
    data = op.open(req, timeout=90).read()
    cache.write_bytes(data)
    return cache


#   缓存版本：改动复盘数据结构（新增字段）后必须 +1，否则旧缓存会被直接复用，
#   导致新增字段缺失、功能不生效（例如旧缓存里没有 skin 字段时，
#   头像会全部回落到初始皮肤）。
REVIEW_CACHE_VERSION = 9


def build_review_cached(replay_id):
    """解析一局复盘并落盘缓存（同一局第二次打开秒开）"""
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    cj = REVIEW_DIR / ('%s.json' % replay_id)
    if cj.exists() and cj.stat().st_size > 500:
        try:
            rv = json.loads(cj.read_text(encoding='utf-8'))
            if rv.get('_v') == REVIEW_CACHE_VERSION:
                return rv
        except Exception:
            pass
    from astral.review import build_review
    rv = build_review(get_replay_file(replay_id))
    rv['_v'] = REVIEW_CACHE_VERSION
    if rv.get('players'):                      # 解析不出玩家的局不落缓存，
        try:                                   # 否则下次会拿空缓存当结果
            cj.write_text(json.dumps(rv, ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass
    return rv


# ── 角色头像（UT_Platform 点位头像 → 自动裁掉四周空白 → 96px data URL）──
#   不使用 <img src="../assets/avatars/102.png"> 的原因：
#   pywebview / WebView2 下该 file:// 相对路径无法加载（显示为裂图），
#   同一页面在 Chrome 中却能加载。
#   改为后端读文件转 base64 后，不再依赖 file:// 加载策略。
# ── 美术资源目录解析 ──
#   优先用「运行时从本机游戏提取到 userdata/assets/」的那份（跟随游戏更新），
#   其次才是开发目录里的 assets/（打包后的 exe 里**不再包含**这两份美术资源：
#   游戏新增角色/皮肤时打包的图会立即过时，且游戏美术版权属官方）。
_ART_DIRS = {}


def _art_dir(name, refresh=False):
    p = DATA_DIR / 'assets' / name
    ok = p.is_dir() and next(p.glob('*.png'), None) is not None
    if not ok:
        q = asset('assets', name)
        if q.is_dir() and next(q.glob('*.png'), None) is not None:
            p = q
    if refresh or name not in _ART_DIRS:
        _ART_DIRS[name] = p
    return _ART_DIRS[name]


AVATAR_DIR = _art_dir('avatars')
_AV_CACHE = {}

# ── 角色「立绘头像」（游戏资源 UT_Hero_ProfilePhoto_* 系列）──
#   命名规律（catalog 3.2.0：194 个 key / 35 位角色）：
#     UT_Hero_ProfilePhoto_<角色3位>            初始皮肤
#     UT_Hero_ProfilePhoto_<角色3位>_Max        羁绊皮肤
#     UT_Hero_ProfilePhoto_<角色3位>_<皮肤2位>   皮肤表里的第 N 个皮肤（N = 01、02…）
#   回放里 model.Hero#standingPainting = 100 + <角色3位> + <皮肤序号3位>，
#   而**皮肤序号就是游戏皮肤表的 1-based 下标**：
#       1 = 初始皮肤、2 = 羁绊皮肤、3、4、5… = assets/data/skins.json 里「皮肤」数组的第 1、2、3… 个
#   映射：
#       seq 1 → <角色>          （如 117 seq=1 → 初始皮肤）
#       seq 2 → <角色>_Max      （羁绊皮肤，红白礼服饰）
#       seq ≥3 → <角色>_0(seq-2)（如 102 seq=5 → _03「科学怪探」；
#                                126 seq=3 → _01「冰上舞者」；
#                                119 seq=3 → _01「转角之期」）
#   越界或立绘号与角色不匹配时回落到初始皮肤。
#   素材由 tools/fetch_hero_photos.py 从游戏资源包提取（安装目录基础包 + 热更新缓存）。
PHOTO_DIR = _art_dir('photos')
_PH_CACHE = {}


def _photo_data(hero_id, skin=0):
    """(角色ID, standingPainting) → assets/photos/<角色>[_<皮肤>|_Max].png 的 data URL。"""
    try:
        hid = int(hero_id or 0)
    except Exception:
        return ''
    if not hid:
        return ''
    key = '%d_%d' % (hid, int(skin or 0))
    if key in _PH_CACHE:
        return _PH_CACHE[key]
    url = ''
    try:
        d = Path(str(PHOTO_DIR))
        if d.exists():
            base = str(hid)
            s = str(int(skin or 0)).zfill(9)                 # 100+角色3位+皮肤序号3位
            same_hero = s.startswith('100') and s[3:6] == base
            seq = int(s[6:9]) if same_hero else 0
            cands = []
            if same_hero:
                if seq >= 3:
                    cands.append('%s_%02d' % (base, seq - 2))   # 皮肤表第 (seq-2) 个
                elif seq == 2:
                    cands.append(base + '_Max')                 # 羁绊皮肤
            cands.append(base)                                   # 初始皮肤 / 回落
            for tag in cands:
                f = d / (tag + '.png')
                if f.exists():
                    import base64
                    url = ('data:image/png;base64,'
                           + base64.b64encode(f.read_bytes()).decode('ascii'))
                    break
    except Exception:
        url = ''
    _PH_CACHE[key] = url
    return url


def _avatar_data(tag):
    """assets/avatars/<tag>.png → 裁到内容边界 → 96×96 → data URL（失败返回 ''）"""
    key = str(tag)
    if key in _AV_CACHE:
        return _AV_CACHE[key]
    url = ''
    try:
        import io
        import base64
        from PIL import Image
        f = Path(str(AVATAR_DIR)) / (key + '.png')
        if f.exists():
            im = Image.open(f).convert('RGBA')
            # ① 优先用 alpha 边界；② 整图不透明时，用「与左上角底色不同」找内容边界
            bb = im.getbbox()
            if not bb or bb == (0, 0, im.width, im.height):
                px = im.load()
                bg = px[0, 0]
                xs, ys = [], []
                for y in range(0, im.height, 2):
                    for x in range(0, im.width, 2):
                        p = px[x, y]
                        if abs(p[0]-bg[0]) + abs(p[1]-bg[1]) + abs(p[2]-bg[2]) > 24:
                            xs.append(x); ys.append(y)
                if xs:
                    bb = (min(xs), min(ys), max(xs)+1, max(ys)+1)
            if bb:
                x0, y0, x1, y1 = bb
                cx, cy = (x0+x1)/2.0, (y0+y1)/2.0     # 正方形裁切，留 12% 余量
                side = max(x1-x0, y1-y0) * 1.12
                box = (max(0, int(cx-side/2)), max(0, int(cy-side/2)),
                       min(im.width, int(cx+side/2)), min(im.height, int(cy+side/2)))
                if box[2] > box[0] and box[3] > box[1]:
                    im = im.crop(box)
            im.thumbnail((96, 96), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, 'PNG', optimize=True)
            url = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        url = ''
    _AV_CACHE[key] = url
    return url


# ════════════════════════════════════════════════════════════
# 运行时补全美术资源（角色头像 + 立绘头像）
#   原因：游戏出新角色 / 新皮肤后，打包进 exe 的图会立即过时
#   → 改为启动时从本机游戏资源包中提取，跟随游戏更新；
#     同时软件包本身不分发官方美术资源（版权更干净）。
#   耗时参考（16 核）：首次全量约 26.6 秒 · 后续启动约 0.3 秒（水位线跳过已扫的包）。
#   策略：后台线程运行，**不阻塞登录界面**；进度由前端轮询 art_status() 获取。
# ════════════════════════════════════════════════════════════
ART_STATE = {'running': False, 'text': '正在检查本机游戏的头像资源…', 'i': 0, 'n': 0,
             'done': False, 'found': 0, 'game': True, 'seconds': 0, 'reason': ''}
ART_LOCK = threading.Lock()


def art_scan_start():
    """后台从本机游戏补全头像资源。重复调用只会跑一个。"""
    global AVATAR_DIR, PHOTO_DIR
    with ART_LOCK:
        if ART_STATE.get('running'):
            return
        ART_STATE.update({'running': True, 'done': False, 'found': 0, 'reason': '',
                          'text': '正在检查本机游戏的头像资源…', 'i': 0, 'n': 0})

    def work():
        global AVATAR_DIR, PHOTO_DIR
        try:
            from astral import gameart
            gameart.log('头像扫描开始（角色 %d 位）' % len(HERO))

            def prog(text, i, n):
                ART_STATE.update({'text': text, 'i': i, 'n': n})

            r = gameart.ensure(sorted(HERO), progress=prog)
            gameart.log('头像扫描结束: %s' % r)
            if r.get('found'):
                _AV_CACHE.clear()                      # 新提取的图需要让缓存失效
                _PH_CACHE.clear()
                AVATAR_DIR = _art_dir('avatars', refresh=True)
                PHOTO_DIR = _art_dir('photos', refresh=True)
            ART_STATE.update({'text': '', 'found': r.get('found', 0), 'game': r.get('game', True),
                              'seconds': r.get('seconds', 0), 'reason': r.get('reason', '')})
        except Exception as e:
            # 打包后为 --windowed（无控制台），异常必须落盘记录才能排查
            try:
                import traceback
                from astral import gameart as _g
                _g.log('头像扫描失败: %r\n%s' % (e, traceback.format_exc()))
            except Exception:
                pass
            ART_STATE.update({'text': '', 'reason': 'error'})
        finally:
            ART_STATE['running'] = False
            ART_STATE['done'] = True

    threading.Thread(target=work, daemon=True).start()



def _pairs(p, idx):
    """task.condition1[idx].params → {角色ID: 场次}。"""
    try:
        items = p.task.condition1[idx].params
    except Exception:
        return {}
    out = {}
    for x in items:
        fc_, hid_ = getattr(x, 'param', None), getattr(x, 'param1', None)
        if fc_ is not None and hid_ is not None:
            out[int(hid_)] = int(fc_)
    return out

def _heroes_of(p):
    """角色卡 → 使用排行（含 PVE 等级与潜能三态）。

    未拥有的角色也一并列出（名字取自静态角色表），带 owned=False，界面里发灰显示。
    这样新角色一上线就能看见「有这个人，但我还没有」，而不是整行消失。
    未拥有但服务器记过场次的角色会带上真实场次/胜场（只灰底、潜能留「—」），
    保证排行榜之和能与档案里的「累计场次」对齐。
    """
    fc, wc = _pairs(p, 17), _pairs(p, 18)
    cards = getattr(p, 'roleCard', {}) or {}
    # 场次以**角色卡自带的 fightCount** 为准，不用 task.condition1[17]：
    # 后者是任务口径，个别角色会少记（逐角色相加比「累计参与」少十几局），
    # fightCount 逐角色相加与「累计参与」严格相等。condition1 只兜底（未拥有的角色没有角色卡）。
    fc_srv = dict(fc)          # 服务端 condition1[17] 原值：用来识别计数陈旧的联动角色
    for _hid, _c in list(cards.items()):
        try:
            _v = int(getattr(_c, 'fightCount', 0) or 0)
        except Exception:
            continue
        if _v:
            fc[_hid] = _v
    # 服务器计数里有、但既不在「拥有角色」也不在静态角色表里的角色：必须一并列出。
    # 只遍历 cards|HERO 会把这类角色整个丢掉，正是「累计场次」与「角色场次之和」对不上的原因。
    # 角色卡自带 fightCount（每角色累计场次），与 task.condition1[17] 是两个不同来源；
    # 对不上时逐角色记一笔，便于定位「累计场次 vs 角色场次之和」的差额。
    try:
        from astral import gameart as _ga3
        # 注意：fc 已被上面的 fightCount 覆盖，这里必须用 fc_srv 才能看出服务端原值
        _fc2 = {k: int(getattr(v, 'fightCount', 0) or 0) for k, v in cards.items()}
        _keys = set(cards) | set(fc_srv)
        _diff = {}
        for k in sorted(_keys):
            c = _fc2.get(k, 0) if k in cards else None
            if c != fc_srv.get(k, 0):
                _diff[k] = '%s/%s' % ('-' if c is None else c, fc_srv.get(k, 0))
        _ga3.log('角色场次对账：ΣfightCount=%d  Σcondition1[17]=%d  差=%d  服务端计数陈旧的角色(卡/条件)=%s'
                 % (sum(_fc2.values()), sum(int(v) for v in fc_srv.values()),
                    sum(_fc2.values()) - sum(int(v) for v in fc_srv.values()), _diff))
    except Exception as _e:
        pass
    # 场次对账：累计(task.condition[14]) vs 角色之和(condition1[17]) vs 模式之和(mapModeCount)。
    # 三个数不一致时，一眼就能看出 15 局差在哪一类，不用再猜。
    try:
        from astral import gameart as _ga2
        _cond = dict(getattr(p.task, 'condition', {}) or {})
        _mmc = sum(int(v) for v in (getattr(p, 'mapModeCount', {}) or {}).values())
        _mmw = sum(int(v) for v in (getattr(p, 'mapModeWinCount', {}) or {}).values())
        _ga2.log('对账：累计场次=%d 角色场次之和=%d 差=%d | 累计胜场=%d 角色胜场之和=%d 差=%d | 模式=%d/%d'
                 % (int(_cond.get(14) or 0), sum(int(v) for v in fc.values()),
                    int(_cond.get(14) or 0) - sum(int(v) for v in fc.values()),
                    int(_cond.get(13) or 0), sum(int(v) for v in wc.values()),
                    int(_cond.get(13) or 0) - sum(int(v) for v in wc.values()), _mmc, _mmw))
    except Exception:
        pass
    missed = sorted((set(fc) | set(wc)) - set(cards) - set(HERO))
    if missed:
        try:
            from astral import gameart as _ga
            _ga.log('角色统计：补回 %d 个未登记角色 %s'
                    % (len(missed), {k: fc.get(k, 0) for k in missed}))
        except Exception:
            pass
    heroes = []
    for hid in sorted(set(cards) | set(HERO) | set(fc) | set(wc)):
        card = cards.get(hid)
        # ── 未拥有：只列名字与称号，其余留空 ──
        if card is None:
            # 没这张角色卡：名字回退静态表；但服务器既然记了场次就如实显示，
            # 否则排行榜之和永远对不上「累计场次」。
            heroes.append({'id': hid, 'name': HERO.get(hid, '角色#%d' % hid),
                           'title': TITLE.get(hid, ''), 'n': fc.get(hid, 0),
                           'w': wc.get(hid, 0), 'lv': None, 'brk': '—',
                           'owned': False, 'stale': False})
            continue
        n = fc.get(hid, 0)
        if not n and not card.isBreakThrough:
            n = 0
        # 等级 = pve_strengthen.level（PVE 英雄等级，协议里有 PveHeroUpLv 升级）
        #   角色卡等级 card.lv 与游戏显示不一致（如 105/129 卡等级 5、PVE 等级 6）
        #   pve 等级为 0 时（例如该角色从未培养）回退用角色卡等级
        try:
            _lv = int(getattr(card.pve_strengthen, 'level', 0) or 0)
        except Exception:
            _lv = 0
        # 潜能三态（「已激发」来自数据包；「是否已开放」来自客户端配置表）
        #   talent 非空        → 已激发
        #   已开放 + talent 空 → 未
        #   未开放             → 未开放
        try:
            _has = len(list(getattr(card.pve_strengthen, 'talent', []) or [])) > 0
        except Exception:
            _has = False
        _rel = POTENT.get(HERO.get(hid, ''), None)
        if _rel is False:
            _pot = '未开放'
        elif _has:
            _pot = '已激发'
        else:
            _pot = '未'
        heroes.append({'id': hid, 'name': HERO.get(hid, '角色#%d' % hid),
                       'title': TITLE.get(hid, ''), 'n': n,
                       'w': wc.get(hid, 0), 'lv': _lv,
                       'brk': _pot, 'owned': True,
                       # 角色卡的 fightCount 与服务端条件计数不一致 ⇒ 该角色的服务端胜场统计少算
                       'stale': bool(fc_srv.get(hid, 0) != fc.get(hid, 0))})
    # 已拥有在前（按场次、编号），未拥有在后（按编号）
    heroes.sort(key=lambda h: (0 if h.get('owned') else 1, -h['n'], h['id']))

    return heroes
def _skins_of(p):
    """皮肤清单（缺位法）；失败不拖垮整个档案。"""
    try:                            # 皮肤清单（缺位法）——失败不影响档案
        skins = build_skins(p)
    except Exception as _e:
        skins = {'list': [], 'named': 0, 'bond': 0, 'sum': 0, 'err': str(_e)[:80]}

    return skins
def _recent_of(p, my_uid):
    """最近对局（取自握手包 showPlayer.record[10]）。

    不对服务器发 5153（查自己会被丢弃）；也不走 5155 兜底（该接口限流取不到数据）。
    """
    recent = []
    sp = getattr(p, 'showPlayer', None)
    for r in list(getattr(sp, 'record', []) or []):
        # record 有 10 局；每局的 data 是**4 个玩家**的列表（model.PlayerFightData）
        #   可用字段：playerId/name/lv/gold/heroId/slot/rank/mapType/isGiveUp/
        #             headIcon/background/playerLevel
        #   伤害/承伤/击杀/治疗量/转账不在此处，而在**对局结算包**
        #     GameFinishS2C.NewAchieveEntry → model.PlayerFinishAchieve
        #     （killCount/totalDamage/totalInjured/treatmentScore/pveTransferGold/
        #       totalGold/totalDie/pkDamageMax/relics/finalKillBoss）
        #   接口已预留：将数值填入 players[i]['stats'] 即可显示。
        players = []
        for it in list(getattr(r, 'data', []) or []):
            ph = int(getattr(it, 'heroId', 0))
            players.append({
                'id': int(getattr(it, 'playerId', 0)),
                'name': getattr(it, 'name', '') or '—',
                'heroId': ph,
                'hero': HERO.get(ph, '角色#%d' % ph),
                'title': TITLE.get(ph, ''),
                'lv': int(getattr(it, 'lv', 0) or 0),
                'plv': int(getattr(it, 'playerLevel', 0) or 0),
                'gold': int(getattr(it, 'gold', 0) or 0),
                'rank': int(getattr(it, 'rank', 0) or 0),
                'slot': int(getattr(it, 'slot', 0) or 0),
                'giveUp': bool(getattr(it, 'isGiveUp', False)),
                'me': int(getattr(it, 'playerId', 0)) == my_uid,
                'stats': None,   # ← 结算数值填入此处
            })
        players.sort(key=lambda x: x['slot'])
        d0 = next((x for x in players if x['me']), None)
        if d0 is None:
            continue
        hid = d0['heroId']
        mt = int(getattr(d0, 'mapType', 0) or 0)
        recent.append({
            'time': getattr(r, 'time', 0),
            'rank': d0['rank'],
            'heroId': hid,
            'hero': d0['hero'],
            'title': d0['title'],
            'mapType': mt,
            'map': MODE.get(mt, '') if mt in MODE else ('模式%d' % mt if mt else ''),
            'replayId': getattr(r, 'replayId', ''),
            'version': getattr(r, 'version', ''),
            'gold': d0['gold'],
            'giveUp': d0['giveUp'],
            'players': players,
        })
        # 不使用 5153 查询：查自己会被服务端丢弃，且置于循环中会阻塞约 10×12 秒；
        #   最近对局 / 获赞数据握手包里已包含。

    return recent
def _maps_of(p):
    """地图胜场排行。"""
    return sorted(({'name': MAPS.get(int(k), '模式%s' % k), 'n': v}
                   for k, v in p.winMap.items()),
                  key=lambda m: -m['n'])


def _friendly_err(err):
    """把底层的握手/超时错误翻译成使用者看得懂的话。"""
    s = str(err or '')
    if 'timeout' in s.lower() or '超时' in s:
        return ('查询超时：游戏服务器把这个请求丢掉了（短时间内查太频繁会被限流）。'
                '等十几秒再试一次；也可以改用回放号查，回放号那条路不需要登录。')
    return '查询失败：%s' % s[:110]


class Api:
    def __init__(self):
        self.phone = ''
        self.sid = ''
        self.profile = None
        self._sess = None           # 复用的游戏连接（见 _session）

    # ── ① 发验证码 ──
    def send_code(self, phone: str):
        phone = re.sub(r'\D', '', phone or '')
        if not re.fullmatch(r'1\d{10}', phone):
            return {'ok': False, 'msg': '手机号格式不对'}
        self.phone = phone
        try:
            ok = request_code(phone)
        except Exception as e:
            return {'ok': False, 'msg': '发送失败：%s' % str(e)[:80]}
        return ({'ok': True, 'msg': '验证码已发送，请查收短信'}
                if ok else {'ok': False, 'msg': '发送失败，请稍后重试'})

    # ── 重新登录拉取最新档案（对局列表会更新）──
    def refresh_profile(self):
        try:
            r = self.auto_login()
            if r.get('ok'):
                return {'ok': True, 'msg': '已刷新为最新数据'}
            return {'ok': False, 'msg': r.get('msg') or '刷新失败'}
        except Exception as e:
            return {'ok': False, 'msg': '刷新失败：%s' % str(e)[:100]}

    # ── 按需加载某局的回放复盘数据 ──
    def maps_of(self, rids):
        """给一串回放号取地图名（每局只抓 8KB 前缀，并发 5 路，失败回退串行）。"""
        ids = [str(x) for x in (rids or []) if x]
        if not ids:
            return {}
        _load_map_cache()
        out = {}
        try:
            import concurrent.futures as cf
            with cf.ThreadPoolExecutor(max_workers=5) as ex:
                for rid, info in zip(ids, ex.map(fetch_replay_map, ids)):
                    out[rid] = (info or {}).get('name') or ''
        except Exception:
            for rid in ids:
                try:
                    out[rid] = (fetch_replay_map(rid) or {}).get('name') or ''
                except Exception:
                    out[rid] = ''
        _save_map_cache()
        return out

    def diffs_of(self, rids):
        """给一串回放号取难度（0 普通 / 1 困难 / 2 噩梦 / 3 疯狂 / 4 极限）。

        与 maps_of 共用同一份前缀缓存 ⇒ 同一局不会重复下载。
        """
        ids = [str(x) for x in (rids or []) if x]
        if not ids:
            return {}
        _load_map_cache()
        out = {}
        try:
            import concurrent.futures as cf
            with cf.ThreadPoolExecutor(max_workers=5) as ex:
                for rid, info in zip(ids, ex.map(fetch_replay_map, ids)):
                    out[rid] = (info or {}).get('diff')
        except Exception:
            for rid in ids:
                try:
                    out[rid] = (fetch_replay_map(rid) or {}).get('diff')
                except Exception:
                    out[rid] = None
        _save_map_cache()
        return out

    def match_maps(self):
        """给「最近对局」列表补地图名（每局只抓 8KB，并发 5 路）。"""
        recs = (self.profile or {}).get('recent') or []
        ids = [str(r.get('replayId') or '') for r in recs]
        ids = [x for x in ids if x]
        if not ids:
            return {'ok': True, 'maps': {}}
        _load_map_cache()
        out = {}
        try:
            import concurrent.futures as cf
            with cf.ThreadPoolExecutor(max_workers=5) as ex:
                for rid, info in zip(ids, ex.map(fetch_replay_map, ids)):
                    if info:
                        out[rid] = info
        except Exception:
            for rid in ids:
                info = fetch_replay_map(rid)
                if info:
                    out[rid] = info
        _save_map_cache()
        return {'ok': True, 'maps': out,
                'diffs': {k: (v or {}).get('diff') for k, v in out.items()}}

    def art_status(self):
        """前端轮询：本机游戏美术资源的补全进度（running/text/i/n/done/found/game）。"""
        return dict(ART_STATE)

    def get_avatars(self):
        """角色头像（data URL）：键 = 角色ID 字符串（'102'…），另含 'Start' 兜底。

        放在后端做（不走 file://），前端拿到的直接是 data URL —— 见 _avatar_data 的注释。
        """
        out = {}
        try:
            d = Path(str(AVATAR_DIR))
            if d.exists():
                for f in sorted(d.glob('*.png')):
                    u = _avatar_data(f.stem)
                    if u:
                        out[f.stem] = u
        except Exception:
            pass
        return out

    # ── 索引 / 回放号 统一解析（复盘查询要用回放号，原「最近对局」用下标）──
    def _resolve_ref(self, ref):
        """把「最近对局的下标」或「回放号」统一解析成 (回放号, 该局记录)。

        回放号 = 12 位以上纯数字（如 1790337873289576）；其余当索引处理。
        索引越界时第二项返回 None，便于调用方区分「没这一局」。
        """
        s = str(ref if ref is not None else '').strip()
        d = re.sub(r'\D', '', s)      # 前端可能传 "'1790…'"（加引号防止 JS 数字精度丢失）
        if len(d) >= 12:
            return d, {}
        try:
            idx = int(d)
        except Exception:
            return '', None
        rec = (self.profile or {}).get('recent') or []
        if idx < 0 or idx >= len(rec):
            return '', None
        one = rec[idx] or {}
        return str(one.get('replayId') or ''), one

    def _my_uid(self):
        try:
            return str((self.profile or {}).get('uid') or '')
        except Exception:
            return ''

    def load_match(self, ref):
        """下载并解析某一局的回放，返回 4 名玩家的完整战绩 + 玩家列表。"""
        try:
            rid, one = self._resolve_ref(ref)
            if one is None:
                return {'ok': False, 'msg': '没有这一局'}
            if not rid:
                return {'ok': False, 'msg': '这一局没有回放号'}
            st = fetch_replay_stats(rid)
            if not st:
                return {'ok': False, 'msg': '回放文件已经损坏，飞魔的问题吧大概'}
            mine = self._my_uid()
            players = [{'id': pid, 'name': (v.get('name') or '—'),
                        'hero': HERO.get(v.get('heroId')) or '',
                        'me': bool(mine and str(pid) == mine)}
                       for pid, v in st.items()]
            out = {'ok': True, 'stats': st, 'players': players, 'replay_id': rid}
            try:
                out['map_name'] = (fetch_replay_map(rid) or {}).get('name') or ''
            except Exception:
                out['map_name'] = ''
            return out
        except Exception as e:
            return {'ok': False, 'msg': '拉取失败：%s' % str(e)[:110]}

    # ── 对局复盘（点玩家名 → 弹窗）：每轮数据 + 筹码三选一 ──
    def load_review(self, ref):
        try:
            rid, one = self._resolve_ref(ref)
            if one is None:
                return {'ok': False, 'msg': '没有这一局'}
            one = one or {}
            if not rid:
                return {'ok': False, 'msg': '没有回放号'}
            rv = build_review_cached(rid)
            if not rv or not rv.get('players'):
                return {'ok': False, 'msg': '回放文件已经损坏，飞魔的问题吧大概'}
            try:
                info = fetch_replay_map(rid) or {}
                rv['map_name'] = info.get('name') or map_name(rv.get('map_id'))
            except Exception:
                rv['map_name'] = map_name(rv.get('map_id'))
            if not rv.get('map_id'):
                try:
                    rv['map_id'] = map_from_prefix(get_replay_file(rid).read_bytes())
                except Exception:
                    pass
            if not rv.get('map_name'):
                rv['map_name'] = map_name(rv.get('map_id'))
            rv['task_names'] = mission_names(rv.get('map_id'), rv.get('missions'))
            rv['time'] = one.get('time')
            meta = {str(x.get('id')): x for x in (one.get('players') or [])}
            for p in rv['players']:
                m = meta.get(str(p['uid'])) or {}
                p['hero_name'] = HERO.get(p['hero_id']) or (m.get('hero') or '')
                p['me'] = bool(m.get('me'))
                # 立绘头像（按角色 + 皮肤）；取不到时返回空串，由前端回落到角色头像
                p['photo'] = _photo_data(p.get('hero_id'), p.get('skin'))
            return {'ok': True, 'review': rv}
        except Exception as e:
            return {'ok': False, 'msg': '复盘解析失败：%s' % str(e)[:110]}

    # ── 复制到剪贴板（WebView 里 navigator.clipboard 在 file:// 下不可靠，走原生 Win32）──
    @staticmethod
    def copy_text(text):
        """把文本写进 Windows 剪贴板。

        64 位下必须声明 restype：ctypes 默认按 32 位 int 收返回值，
        HGLOBAL 句柄会被截断，GlobalLock 直接失败（表现为「内存锁定失败」）。
        """
        try:
            import ctypes
            s = str(text if text is not None else '')
            if not s:
                return {'ok': False, 'msg': '没有可复制的内容'}
            CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
            u, k = ctypes.windll.user32, ctypes.windll.kernel32
            k.GlobalAlloc.restype = ctypes.c_void_p
            k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            k.GlobalLock.restype = ctypes.c_void_p
            k.GlobalLock.argtypes = [ctypes.c_void_p]
            k.GlobalUnlock.argtypes = [ctypes.c_void_p]
            k.GlobalFree.argtypes = [ctypes.c_void_p]
            u.SetClipboardData.restype = ctypes.c_void_p
            u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            buf = ctypes.create_unicode_buffer(s)
            size = ctypes.sizeof(buf)
            h = k.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h:
                return {'ok': False, 'msg': '内存分配失败'}
            p = k.GlobalLock(h)
            if not p:
                k.GlobalFree(h)
                return {'ok': False, 'msg': '内存锁定失败'}
            ctypes.memmove(p, buf, size)
            k.GlobalUnlock(h)
            if not u.OpenClipboard(None):
                k.GlobalFree(h)
                return {'ok': False, 'msg': '剪贴板被占用'}
            try:
                u.EmptyClipboard()
                if not u.SetClipboardData(CF_UNICODETEXT, h):
                    k.GlobalFree(h)
                    return {'ok': False, 'msg': '写入剪贴板失败'}
            finally:
                u.CloseClipboard()          # 成功后内存归系统所有，不能自己释放
            return {'ok': True, 'text': s}
        except Exception as e:
            return {'ok': False, 'msg': '复制失败：%s' % str(e)[:80]}

    # ── ★ 复盘查询：一个输入框自动识别「回放号 / UID」──
    @staticmethod
    def _classify(text):
        """识别输入文本：('replay', 回放号) / ('uid', UID) / ('', 原样)。"""
        s = re.sub(r'\D', '', str(text or ''))
        if not s:
            return '', ''
        if len(s) >= 12:                 # 回放号是 16 位长数字
            return 'replay', s
        if 4 <= len(s) <= 11:            # UID 是 6~7 位
            return 'uid', s
        return '', s

    def _region(self):
        """当前登录区服：'intl' = Steam 海外服，其余（包括还没有记录）= 国服。

        登录时写进 userdata/last_region.json（只记区服与显示用信息，不存票）。
        """
        return load_region().get('region') or 'cn'

    def _bind_region(self, region):
        """把回放服务器切到当前区服：两服的服务器不同，回放号也互不相通。

        登录路径由 _fetch 调用；查询路径不发登录握手，需要自己调。
        """
        global REPLAY_REGION
        REPLAY_REGION = region or 'cn'
        return REPLAY_REGION

    def _session(self, force_new=False):
        """拿一条已登录的游戏连接（查 UID 用；回放号那条路不需要登录）。

        必须复用：服务器会把短时间内反复登录的请求静默丢弃（登录握手直接超时、
        一个包都收不到），每查一次就重新登录的话，连查两次就会全线超时。

        区服必须跟着登录走：两服的 UID 是各自号段，票也只能打自己的服务器。
        """
        if not force_new and self._sess is not None:
            return self._sess
        if self._region() == 'intl':
            tok, did, why = intl_login.read_session()
            if not tok:
                raise RuntimeError(why or '海外服登录票不可用')
            c = C.AstralClient(intl_login.GAME_HOST, intl_login.GAME_PORT,
                               timeout=20.0)
            c.connect()
            c.login_abroad(tok, device_id=did, client_ver='3.2.0')
            self._sess = c
            return c
        sid, why = _auto()
        if not sid:
            raise RuntimeError(why or '登录态不可用，请先用验证码登录一次')
        c = C.AstralClient(GAME_HOST, GAME_PORT, timeout=20.0)
        c.connect()
        c.login_china(sid, device_id=DEFAULT_DEVICE_ID,
                      extra=DEFAULT_EXTRA, client_ver='3.2.0')
        self._sess = c
        return c

    def _drop_session(self):
        """丢掉当前连接（连接出错或退出登录时调）。"""
        c, self._sess = self._sess, None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _self_uid(self):
        """本机记住的账号 UID（未登录时从 token.json 取，用来识别「查自己」）。"""
        if self.profile and self.profile.get('uid'):
            return str(self.profile['uid'])
        try:
            # 注意：load_token() 只返回 accessToken 字符串，uid 在 load_token_info() 里
            t = load_token_info() or {}
            return str(t.get('uid') or '')
        except Exception:
            return ''

    def _query_self(self, uid, refresh=False):
        """查自己：服务器会丢弃 5153，直接用登录握手包里的档案拼一份结果。

        登录包已含所需的一切：Player.level / showPlayer.record / praiseNum、
        task.condition[14 = 场次, 13 = 胜场]，所以这条路不用再发任何请求。

        refresh=True 时无视手里那份档案、重新握手一次（原因见 refresh_profile）。
        """
        prof = self.profile
        if refresh or not prof or str(prof.get('uid')) != str(uid):
            if self._region() == 'intl':
                # 海外服重新握手走 mmkv 那条路（票不落盘，现读现用）
                r = self._auto_login_intl()
                if not r.get('ok'):
                    return {'ok': False, 'kind': 'uid', 'uid': uid,
                            'msg': r.get('msg') or '海外服登录失败'}
                prof = self.profile
            else:
                sid, why = _auto()
                if not sid:
                    return {'ok': False, 'kind': 'uid', 'uid': uid,
                            'msg': why or '登录态不可用，请先用验证码登录一次'}
                prof = self._fetch(sid)
                self.profile = prof
        recs = []
        for r in list(prof.get('recent') or []):
            rid = str(r.get('replayId') or '')
            if not rid:
                continue
            hid = int(r.get('heroId') or 0)
            recs.append({'replayId': rid, 'time': int(r.get('time') or 0),
                         'rank': int(r.get('rank') or 0), 'heroId': hid,
                         'mapType': int(r.get('mapType') or 0),
                         'hero': r.get('hero') or HERO.get(hid, '')})
        try:                                       # 每局地图名 + 难度（只抓前缀，带缓存）
            rids = [r['replayId'] for r in recs]
            mp = self.maps_of(rids)
            df = self.diffs_of(rids)
            for r in recs:
                r['map_name'] = mp.get(r['replayId']) or ''
                r['difficulty'] = df.get(r['replayId'])
        except Exception:
            pass
        return {'ok': True, 'kind': 'uid', 'uid': uid, 'self': True,
                'nick': prof.get('nick') or '',
                'level': int(prof.get('level') or 0),
                'online': True,                    # 刚握手成功，本人此刻必然在线
                'statistics': {
                    # 档案里 adorn = 皮肤数(skins.sum)、decor = 装饰数，与 5153 命名相反
                    'fightCount': int(prof.get('total') or 0),
                    'winFightCount': int(prof.get('wins') or 0),
                    'roleCardCount': int(prof.get('heroCount') or 0),
                    'adornCount': int(prof.get('decor') or 0),
                    'skinCount': int(prof.get('adorn') or 0),
                    'praiseNum': int(prof.get('praise') or 0)},
                'records': recs}

    def query(self, text, refresh=False):
        """复盘查询：回放号 → 单条对局记录；UID → 最近 10 局记录。

        refresh=True（界面的刷新按钮）时，「查自己」那条会重新握手，取到最新对局。
        """
        kind, s = self._classify(text)
        if not kind:
            return {'ok': False, 'msg': '请输入回放号（16 位长数字）或 UID（6~7 位数字）'}
        # 这条路径不发登录握手，区服要自己绑（登录路径由 _fetch 绑）。
        self._bind_region(self._region())

        if kind == 'replay':
            rid = s
            st = fetch_replay_stats(rid)
            if not st:
                return {'ok': False, 'kind': 'replay', 'replay_id': rid,
                        'msg': '取不到这个回放 —— 回放号可能输错或已过期'}
            mine = self._my_uid()
            players = [{'id': pid, 'name': (v.get('name') or '—'),
                        'hero': HERO.get(v.get('heroId')) or '',
                        'me': bool(mine and str(pid) == mine)}
                       for pid, v in st.items()]
            try:
                mapname = (fetch_replay_map(rid) or {}).get('name') or ''
            except Exception:
                mapname = ''
            diff = None
            try:
                rv = build_review_cached(rid)
                diff = (rv or {}).get('difficulty')
            except Exception:
                pass
            rec = {'replayId': rid, 'time': 0, 'rank': 0, 'hero': '', 'players': players}
            return {'ok': True, 'kind': 'replay', 'replay_id': rid, 'map_name': mapname,
                    'difficulty': diff, 'records': [rec], 'stats': st}

        # kind == 'uid'：需要一条登录会话（回放号那条路不用登录）
        uid = int(s)
        if self._self_uid() and str(uid) == self._self_uid():
            # 「查自己」会被服务器丢弃 5153（见 _fetch 里的注释）。登录与否都一样，
            # 因为这条查询本身就用本机记住的票登录，服务器看到的会话 uid 就是你自己。
            # ⇒ 不发 5153，直接用登录握手包里的档案作答。
            return self._query_self(uid, refresh)
        sp, last = None, ''
        for attempt in (1, 2):
            try:
                c = self._session(force_new=(attempt > 1))
                got = c.get_show_player(uid, timeout=15.0)
                # 校验回包是这个 UID 的：连接复用时，上一次超时留下的残包会被
                # 这次读走（表现为查到别人）。对不上就丢掉连接重试。
                got_id = int(getattr(got.showData, 'player_id', 0) or 0)
                if got_id and got_id != int(uid):
                    raise RuntimeError('回包 uid=%s 与请求 %s 不符' % (got_id, uid))
                sp = got
                break
            except Exception as e:
                last = str(e)
                self._drop_session()            # 这条连接不能用了，下次重建
                if attempt == 1:
                    time.sleep(1.6)             # 被限流时留点间隔再试一次
        if sp is None:
            return {'ok': False, 'kind': 'uid', 'uid': uid,
                    'msg': _friendly_err(last)}
        if not int(getattr(sp.showData, 'player_id', 0) or 0):
            # 查不到时服务器回 player_id=0；两服的 UID 是各自号段，要说明白。
            _rg = '海外服' if self._region() == 'intl' else '国服'
            return {'ok': False, 'kind': 'uid', 'uid': uid,
                    'msg': '这个 UID 在%s上查不到 —— 国服和海外服的 UID 是各自号段，'
                           '两边的号不能混着查' % _rg}
        try:
            sd = sp.showData
            stt = sd.statistics
            recs = []
            for r in (sd.record or []):
                hid = int(getattr(r, 'heroId', 0) or 0)
                recs.append({'replayId': str(getattr(r, 'replayId', '') or ''),
                             'time': int(getattr(r, 'time', 0) or 0),
                             'rank': int(getattr(r, 'rank', 0) or 0),
                             'heroId': hid,
                             'mapType': int(getattr(r, 'mapType', 0) or 0),
                             'hero': HERO.get(hid) or ''})
            recs = [r for r in recs if r['replayId']]
            recs.sort(key=lambda x: x['time'] or 0, reverse=True)
            try:                                   # 每局地图名 + 难度（只抓前缀，带缓存）
                rids = [r['replayId'] for r in recs]
                mp = self.maps_of(rids)
                df = self.diffs_of(rids)
                for r in recs:
                    r['map_name'] = mp.get(r['replayId']) or ''
                    r['difficulty'] = df.get(r['replayId'])
            except Exception:
                pass
            statistics = {
                'fightCount': int(getattr(stt, 'fightCount', 0) or 0),
                'winFightCount': int(getattr(stt, 'winFightCount', 0) or 0),
                'roleCardCount': int(getattr(stt, 'roleCardCount', 0) or 0),
                'adornCount': int(getattr(stt, 'adornCount', 0) or 0),
                'skinCount': int(getattr(stt, 'skinCount', 0) or 0),
                'useHero': int(getattr(stt, 'useHero', 0) or 0),
                'praiseNum': int(getattr(sd, 'praiseNum', 0) or 0),
            }
            out = {'ok': True, 'kind': 'uid', 'uid': uid, 'nick': '',
                   'level': 0, 'online': False, 'statistics': statistics,
                   'records': recs}
            try:                                   # 简况（昵称/等级/在线）拿不到也不影响主流程
                ps = c.get_player_simple(uid, timeout=15.0)
                pi = ps.PlayerInfo
                out['nick'] = str(getattr(pi, 'name', '') or '')
                out['level'] = int(getattr(pi, 'lv', 0) or 0)
                out['online'] = bool(getattr(pi, 'isOnline', False))
            except Exception:
                pass
            return out
        except Exception as e:
            return {'ok': False, 'kind': 'uid', 'uid': uid,
                    'msg': '解析失败：%s' % str(e)[:120]}
        finally:
            try:
                c.close()
            except Exception:
                pass

    def _auto_login_intl(self):
        """海外服免码登录：票一直在客户端 mmkv 里，现读现用（不落盘）。"""
        try:
            tok, did, why = intl_login.read_session()
        except Exception as e:
            return {'ok': False, 'msg': '读取海外服登录票失败：%s' % str(e)[:80]}
        if not tok:
            return {'ok': False, 'msg': why or '海外服登录票不可用'}
        try:
            self._finish_login(tok, None, '', channel='intl', region='intl',
                               device_id=did, account=intl_login.account_name())
        except Exception as e:
            return {'ok': False, 'msg': _friendly_err(str(e))}
        nick = str((self.profile or {}).get('nick') or '').strip()
        return {'ok': True, 'profile': self.profile,
                'msg': '✓ 已用记住的登录态登录：%s（Steam 海外服 登录）'
                       % (nick or '海外服')}

    # ── ② 登录 + 拉档案 ──
    def auto_login(self):
        """用上次记住的登录态自动登录（免验证码）。

        上次登的是哪个区服记在 last_region.json 里：
        国服走 SDK 的 accessToken，海外服现读客户端 mmkv 的票 —— 两边都能免码进。
        """
        if self._region() == 'intl':
            return self._auto_login_intl()
        try:
            sid, why = _auto()
        except Exception as e:
            sid, why = None, str(e)[:60]
        if not sid:
            return {'ok': False,
                    'msg': '免验证码登录没成功（%s）—— 请用短信验证码登录一次，成功后会自动记住 ✓'
                           % (why or '登录态不可用')}
        self.sid = sid
        try:
            self.profile = self._fetch(sid)
        except Exception as e:
            return {'ok': False, 'msg': '登录态已失效，请重新用验证码登录一次（%s）'
                    % str(e)[:80]}
        if isinstance(self.profile, dict):
            # 渠道随 token.json 一起记住；老记录没这字段时按手机号登计算
            self.profile['channel'] = (load_token_info() or {}).get('channel') or 'phone'
        self._remember_id()
        return {'ok': True, 'msg': '✓ 已用记住的登录态登录，免验证码',
                'profile': self.profile}

    def refresh_profile(self):
        """重新登录一次并返回最新档案（「最近对局」的刷新按钮）。

        最近对局只在登录握手包里（showPlayer.record[10]），软件一直开着时不会自己更新，
        所以打完一把要重开软件才看得到；这一步相当于把软件重开一次，但不用真的重开。
        """
        if self._region() == 'intl':
            r = self._auto_login_intl()
            if r.get('ok'):
                return {'ok': True, 'profile': self.profile}
            return {'ok': False, 'msg': r.get('msg') or '海外服登录失败'}
        try:
            sid, why = _auto()
        except Exception as e:
            sid, why = None, str(e)[:60]
        if not sid:
            return {'ok': False, 'msg': why or '登录态不可用，请先用验证码登录一次'}
        try:
            prof = self._fetch(sid)      # 内部先丢掉旧连接，再重新握手
        except Exception as e:
            return {'ok': False, 'msg': _friendly_err(str(e))}
        if isinstance(prof, dict):
            prof['channel'] = (load_token_info() or {}).get('channel') or 'phone'
        self.profile = prof
        return {'ok': True, 'profile': prof}

    def login_state(self):
        """登录页顶部要显示的「上次登录」信息（手机号只给掩码）。

        国服看 token.json；海外服看 last_region.json —— 票在客户端 mmkv 里，
        这边只报「票在不在」和上次的昵称，不碰票本身。
        """
        info = load_token_info() or {}
        prof = self.profile or {}
        lr = load_region()
        reg = lr.get('region') or 'cn'
        base = lr if reg == 'intl' else info
        return {
            'version': VERSION,
            'region': reg,
            'region_label': '海外服' if reg == 'intl' else '国服',
            # 海外服的票寿命很短，过期不等于没记住账号：只要记过就返回 true，
            # 界面照常显示上次登录并试一次自动登录（票过期会把原因说清楚）。
            'has_token': bool(lr.get('nick')) if reg == 'intl'
                         else bool(info.get('accessToken')),
            'tel_masked': '' if reg == 'intl' else (info.get('tel_masked') or ''),
            'account': base.get('account') or '',
            'uid': prof.get('uid') or base.get('uid') or '',
            'nick': prof.get('nick') or base.get('nick') or '',
            'saved_at': (base.get('saved_at') or '').replace('T', ' ')[:16],
        }

    def logout(self):
        """退出登录：只结束当前会话，【本机记住的账号保留】。

        保留的原因：否则「使用上次账号登录」和开机自动登录都会失效，
        使用者只能重新走短信验证码，功能自相矛盾。
        真要抹掉本机凭据请用 forget()。
        """
        self.profile = None
        self.sid = None
        self._drop_session()
        try:
            (DATA_DIR / 'sid.txt').unlink(missing_ok=True)
        except Exception:
            pass
        return {'ok': True, 'msg': '已退出登录（本机仍记住该账号）'}

    def forget(self):
        """彻底清除本机记住的登录态（token.json）。"""
        self.profile = None
        self.sid = None
        self._drop_session()
        try:
            clear_token()
        except Exception:
            pass
        try:
            _REGION_FILE.unlink(missing_ok=True)   # 「上次登的是海外服」也一并忘掉
        except Exception:
            pass
        return {'ok': True, 'msg': '已清除本机登录态'}

    def _remember_id(self):
        """把 UID/昵称补进 token.json，供登录页显示。"""
        try:
            d = self.profile or {}
            patch_token_meta(uid=d.get('uid'), nick=d.get('nick'),
                             channel=d.get('channel'))
        except Exception:
            pass

    def has_token(self):
        try:
            return bool(load_token())
        except Exception:
            return False

    def _finish_login(self, sid, resp, tel='', channel='phone', region='cn',
                      device_id=None, account=''):
        """登录成功后的公共收尾：记住本机登录态 → 读档案。

        channel：本机这次是用哪种方式登进来的
                 （phone/steam/bilibili/taptap/intl）。界面上的渠道标签就用它 ——
                 服务器给的 platform 一律回 CN_STEAM，认不得。

        region='intl'（Steam 海外服）时**什么都不落盘**：票是客户端 mmkv 里的，
        写进本工具的 token.json 会把国服的登录态顶掉，而且那张票也不该留在磁盘上。
        """
        self.token_saved = False
        self.sid = sid
        if region == 'cn':
            try:
                saved = save_token(resp, tel)
                self.token_saved = bool(saved)
            except Exception:
                self.token_saved = False
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                (DATA_DIR / 'sid.txt').write_text(sid, encoding='utf-8')
            except Exception:
                pass
        self.profile = self._fetch(sid, region=region, device_id=device_id)
        if isinstance(self.profile, dict):
            self.profile['channel'] = channel
            self.profile['region'] = region
        if region == 'cn':
            self._remember_id()
        save_region(region, self.profile, account=account)

    def do_login(self, phone: str, code: str):
        phone = re.sub(r'\D', '', phone or '')
        code = (code or '').strip()
        try:
            sid, resp = phone_login(phone, code)
        except Exception as e:
            return {'ok': False, 'msg': '登录失败：%s' % str(e)[:100]}
        try:
            self._finish_login(sid, resp, phone)
        except Exception as e:
            return {'ok': False, 'msg': '已登录，但读取档案失败：%s' % str(e)[:100]}
        note = ('（已接管登录：游戏客户端被服务器请下线，属正常）'
                if getattr(self, '_took_over', False) else '')
        return {'ok': True, 'msg': '✓ 登录成功，档案已生成' + note, 'profile': self.profile}

    def do_login_pwd(self, phone: str, password: str):
        """手机号 + 密码登录（SDK login_type=1）。

        密码只在本机内存里过一遍：不打印、不写日志、不落盘
        （save_token 只存下发的 accessToken，authorize 存档只记密码长度）。
        """
        phone = re.sub(r'\D', '', phone or '')
        password = str(password or '')
        if not phone or not password:
            return {'ok': False, 'msg': '请把手机号和密码都填上'}
        try:
            sid, resp = password_login(phone, password)
        except Exception as e:
            return {'ok': False, 'msg': '登录失败：%s' % str(e)[:100]}
        try:
            self._finish_login(sid, resp, phone)
        except Exception as e:
            return {'ok': False, 'msg': '已登录，但读取档案失败：%s' % str(e)[:100]}
        note = ('（已接管登录：游戏客户端被服务器请下线，属正常）'
                if getattr(self, '_took_over', False) else '')
        return {'ok': True, 'msg': '✓ 登录成功，档案已生成' + note, 'profile': self.profile}

    # ── 渠道客户端登录（复用本机游戏里已登录的那张票，免验证码）──────
    def _steam_log(self):
        """本机有没有可用的 Steam 客户端登录记录（只读日志，很快）。"""
        try:
            ls = game_session.log_session('steam')
        except Exception:
            return None
        if not ls or not ls.get('token'):
            return None
        ls['age_days'] = int((time.time() - ls['mtime']) / 86400)
        ls['label'] = 'Steam 登录 · %s' % (ls.get('user') or ls.get('steam_id') or ls['masked'])
        return ls

    def steam_state(self):
        """登录页用：本机在跑的渠道客户端 + 有没有 Steam 登录记录（都很快）。"""
        try:
            procs = game_session.list_game_processes()
        except Exception as e:
            return {'ok': False, 'running': False, 'procs': [], 'msg': str(e)[:80]}
        ls = self._steam_log()
        log = ({'user': ls.get('user') or '', 'log': ls.get('log_name', ''),
                'age_days': ls['age_days'], 'label': ls['label']} if ls else None)
        return {'ok': True, 'running': bool(procs), 'procs': procs, 'log': log,
                'labels': '、'.join(sorted({p['label'] for p in procs}))}

    def steam_account(self):
        """登录页用：这次会登成哪个账号。

        有客户端登录记录就直接用它 —— 快，而且**不必让游戏在线**；
        没有记录才退回读进程内存（慢，约 30 秒）。
        """
        ls = self._steam_log()
        if ls:
            when = ('刚登录过' if ls['age_days'] < 1 else '%d 天前登录过' % ls['age_days'])
            return {'ok': True, 'from_log': True,
                    'sessions': [{'label': ls['label'], 'channel': '客户端登录记录',
                                  'masked': ls.get('masked', ''), 'login_type': 'steam',
                                  'time': ''}],
                    'label': ls['label'], 'is_steam': True, 'channel': '客户端登录记录',
                    'note': '（来自 %s，%s）' % (ls.get('log_name', '日志'), when)}
        try:
            sess, note = game_session.find_sessions()
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:80]}
        if not sess:
            return {'ok': False, 'none': True, 'msg': note or '没读到游戏登录态'}
        items = [{'label': game_session.account_label(s) or '未知账号',
                  'channel': s.get('label', ''), 'masked': s.get('masked', ''),
                  'login_type': game_session.kind_of(s), 'time': s.get('time', '')}
                 for s in sess]
        # 与 steam_login 的取票口径保持一致：有 Steam 会话就以它为准
        # （渠道会话不带时间戳，不能只靠"最新"排序挑，否则会挑到旧的手机号会话）
        top = next((x for x in items if x['login_type'] == 'steam'), items[0])
        return {'ok': True, 'sessions': items, 'label': top['label'],
                'is_steam': top['login_type'].lower() == 'steam',
                'channel': top['channel'], 'note': note}

    def steam_login(self):
        """用本机 Steam 渠道的登录态登录（免验证码）。

        取票顺序：
          ① 客户端日志里的长效票 —— 约 30 天有效，**不必让游戏在线** —— 首选
          ② 运行中客户端的进程内存 —— 需要游戏在线，票是当前会话 —— 兜底
        """
        # ① 先试日志：不用开游戏，也不会把游戏挤下线
        ls = self._steam_log()
        log_failed = False
        if ls:
            sid = ''
            try:
                sid, resp = token_login(ls['token'])
            except Exception:
                sid = ''
                log_failed = True            # 记录还在、票却被服务器拒 ⇒ 已失效
            if sid:
                try:
                    self._finish_login(sid, resp, '', channel='steam')
                except Exception as e:
                    return {'ok': False, 'msg': '已登录，但读取档案失败：%s' % str(e)[:100]}
                nick = str((self.profile or {}).get('nick') or '').strip()
                return {'ok': True, 'profile': self.profile, 'account': ls['label'],
                        'nick': nick, 'from_log': True,
                        'msg': '✓ 已登录：%s%s'
                               % (nick or ls['label'],
                                  ('（%s）' % ls['label']) if nick else '')}
        # ② 日志不可用（或票失效）才回退读内存：游戏正开着时还能救回来
        try:
            sess, note = game_session.find_sessions()
        except Exception as e:
            return {'ok': False, 'msg': '读取本机游戏登录态失败：%s' % str(e)[:80]}
        if not sess:
            if log_failed:
                return {'ok': False, 'expired': True, 'need_login': True,
                        'msg': '已失效，请重新登录一次'}
            return {'ok': False, 'need_login': True,
                    'msg': '没读到游戏登录态 —— 请先在游戏里登录一次，并保持游戏在线。'
                           + (('（%s）' % note) if note else '')}
        # 有 Steam 会话就优先用它（按钮写的就是"用 Steam 登录"）
        s = next((x for x in sess if game_session.kind_of(x) == 'steam'), sess[0])
        who = game_session.account_label(s) or '未知账号'
        try:
            sid, resp = token_login(s['token'], tel=str(s.get('phone') or ''))
        except Exception as e:
            if log_failed:
                return {'ok': False, 'expired': True, 'need_login': True,
                        'msg': '已失效，请重新登录一次'}
            return {'ok': False, 'need_login': True, 'account': who,
                    'msg': '登录失败（%s）：%s' % (who, str(e)[:110])}
        try:
            self._finish_login(sid, resp, str(s.get('phone') or ''),
                               channel='steam')
        except Exception as e:
            return {'ok': False, 'msg': '已登录，但读取档案失败：%s' % str(e)[:100]}
        warn = ('' if game_session.kind_of(s) == 'steam'
                else '　注意：游戏当前不是 Steam 登录。')
        # 登录成功后服务器会返回该号的昵称，比 Steam ID 更好认，直接显示
        nick = str((self.profile or {}).get('nick') or '').strip()
        note = ('（已接管登录：游戏客户端被服务器请下线，属正常）'
                if getattr(self, '_took_over', False) else '')
        return {'ok': True, 'profile': self.profile, 'account': who, 'nick': nick,
                'msg': '✓ 已登录：%s%s%s%s'
                       % (nick or who, ('（%s）' % who) if nick else '', note, warn)}

    # ── B站 / TapTap 渠道登录（票都取自客户端日志，免验证码、也不必让游戏在线）──
    def _chan_log_state(self, kind, name):
        """登录页用：本机有没有可用的长效票（只读日志，很快）。"""
        try:
            s = game_session.log_session(kind)
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:80]}
        if not s:
            return {'ok': False, 'none': True, 'msg': '没有找到 %s 登录记录' % name}
        days = int((time.time() - s['mtime']) / 86400)
        when = ('（%d 天前登录）' % days) if days >= 1 else '（刚登录过）'
        return {'ok': True, 'masked': s['masked'], 'user': s['user'], 'log': s['log_name'],
                'ttl_days': int(s['ttl'] / 86400) if s['ttl'] else 30, 'age_days': days,
                'label': '%s 登录 · %s%s' % (name, s['user'] or s['masked'], when)}

    def _chan_log_login(self, kind, name, platform):
        """用客户端日志里的长效票登录（免验证码；游戏在不在线都行）。"""
        try:
            s = game_session.log_session(kind)
        except Exception as e:
            return {'ok': False, 'msg': '读取 %s 登录记录失败：%s' % (name, str(e)[:80])}
        if not s:
            return {'ok': False, 'need_login': True,
                    'msg': '没有找到 %s 登录记录 —— 请先用 %s 客户端登录一次《星趴》，'
                           '登录记录会自动保存在本机。' % (name, name)}
        who = '%s 登录 · %s' % (name, s['user'] or s['masked'])
        try:
            sid, resp = token_login(s['token'])
        except Exception:
            return {'ok': False, 'expired': True, 'need_login': True,
                    'msg': '已失效，请重新登录一次'}
        try:
            self._finish_login(sid, resp, '', channel=kind)
        except Exception as e:
            return {'ok': False, 'msg': '已登录，但读取档案失败：%s' % str(e)[:100]}
        # 档案里的 platform 是服务器给的默认值（往往写成 CN_STEAM），按渠道纠正
        if isinstance(self.profile, dict):
            self.profile['platform'] = platform
        nick = str((self.profile or {}).get('nick') or '').strip()
        note = ('（已接管登录：游戏客户端被服务器请下线，属正常）'
                if getattr(self, '_took_over', False) else '')
        return {'ok': True, 'profile': self.profile, 'account': who, 'nick': nick,
                'msg': '✓ 已登录：%s%s%s'
                       % (nick or who, ('（%s）' % who) if nick else '', note)}

    def bili_state(self):
        """B站：本机有没有可用的长效票。"""
        return self._chan_log_state('bilibili', 'Bilibili')

    def bili_login(self):
        """用 B站客户端日志里的票登录。"""
        return self._chan_log_login('bilibili', 'Bilibili', 'CN_BILIBILI')

    def taptap_state(self):
        """TapTap：本机有没有可用的长效票。"""
        return self._chan_log_state('taptap', 'TapTap')

    def taptap_login(self):
        """用 TapTap 客户端日志里的票登录。"""
        return self._chan_log_login('taptap', 'TapTap', 'CN_TAPTAP')

    # ── Steam 海外服（国际服）登录 ──────────────────────────────
    # 票由客户端自己存在 mmkv.default 的 fl_dft#user_steam.it，
    # 直接拿它进国际服游戏服握手，不需要 SDK 换票。
    def intl_state(self):
        """登录页用：本机海外服客户端有没有可用的登录票（只读，很快）。

        客户端 mmkv 里只记着一个账号号；以前登录过的话优先显示记下的角色昵称。
        """
        try:
            st = intl_login.state()
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:80]}
        try:
            lr = load_region()
            if st.get('ok') and (lr.get('region') or '') == 'intl' and lr.get('nick'):
                st['account'] = st.get('user') or ''
                st['user'] = lr['nick']
        except Exception:
            pass
        return st

    def intl_login(self):
        """用海外服客户端里的票登录（免验证码、免 SDK 换票）。

        只发一次握手：国际服服务器对连续握手敏感（会被限流），失败就失败，
        不做自动重试；也不在启动时自动登录 —— 登进去会把海外服客户端挤下线。
        """
        try:
            tok, did, why = intl_login.read_session()
        except Exception as e:
            return {'ok': False, 'msg': '读取海外服登录票失败：%s' % str(e)[:80]}
        if not tok:
            return {'ok': False, 'need_login': True, 'expired': True,
                    'msg': why or '没读到海外服的登录票'}
        try:
            self._finish_login(tok, None, '', channel='intl', region='intl',
                               device_id=did, account=intl_login.account_name())
        except Exception as e:
            return {'ok': False, 'expired': True, 'need_login': True,
                    'msg': str(e)[:160] or '登录失败'}
        nick = str((self.profile or {}).get('nick') or '').strip()
        return {'ok': True, 'profile': self.profile, 'nick': nick,
                'account': 'Steam 海外服 登录',
                'msg': '✓ 已登录：%s（Steam 海外服 登录）' % (nick or '本机客户端')}

    def _handshake_china(self, c, sid):
        """国服握手（带「被挤下线」自动重试）。返回 (是否接管, ConnectS2C, 连接)。

        ★ 账号已经在别处（游戏客户端 / 上一次会话）在线时，服务器会先把旧会话
          踢掉、并回一个 err=10020 的"登录被拒"；隔一两秒重发就通了 ——
          用户手动"再点一次登录"走的就是这条路。这里替他自动重试，
          顺手把那句"被拒"翻译成人话（err=10020 太抽象了）。
        """
        for _try in range(3):
            try:
                s2c = c.login_china(sid, device_id=DEFAULT_DEVICE_ID,
                                    extra=DEFAULT_EXTRA, client_ver='3.2.0')
                return _try > 0, s2c, c
            except C.ProtocolError as e:
                if '10020' not in str(e):
                    raise
                if _try >= 2:
                    raise C.ProtocolError(
                        'err=10020：这个账号正在别处在线（游戏客户端或上次的会话）。'
                        '同一个账号只能在线一处 —— 把游戏客户端退掉，再点一次登录就好。')
                time.sleep(1.2 + _try)
                try:
                    c.close()     # 被踢之后连接可能已经废了，重连再发
                except Exception:
                    pass
                c = C.AstralClient(c.host, c.port, timeout=20.0)
                c.connect()

    def _fetch(self, sid, region='cn', device_id=None):
        """登录握手 → 档案字典（登录成功后连接留着，给复盘查询复用）。

        region='cn'   国服：SDK 换来的票 + China 段握手
        region='intl' 国际服：客户端 mmkv 里的票 + Abroad 段握手
        握手之后的档案结构两边一致，下面全部共用。
        """
        self._bind_region(region)   # 回放服务器跟着区服走（见 REPLAY_HOSTS）
        self._drop_session()        # 换新连接前先把旧的关掉
        host = intl_login.GAME_HOST if region == 'intl' else GAME_HOST
        port = intl_login.GAME_PORT if region == 'intl' else GAME_PORT
        c = C.AstralClient(host, port, timeout=20.0)
        c.connect()
        ok = False
        self._took_over = False       # 是否替用户接管了"被挤下线"的会话
        if region == 'intl':
            # 国际服不做循环重试（反复握手会被服务器限流）。只有 err=10020
            # 「账号正在别处在线」补一次：服务器会踢掉旧会话，隔两秒即可重连。
            for _try in range(2):
                try:
                    s2c = c.login_abroad(sid, device_id=device_id or '',
                                         client_ver='3.2.0')
                    break
                except C.ProtocolError as e:
                    msg = str(e)
                    if '10000' in msg:
                        raise C.ProtocolError(
                            'err=10000：这张票已经失效了。在海外服客户端里重新登录一次，'
                            '回来再点这个按钮就好。')
                    if '10020' in msg:
                        if _try >= 1:
                            raise C.ProtocolError(
                                'err=10020：这个账号正在别处在线（海外服客户端或上一次的会话）。'
                                '把海外服客户端退掉，过一会儿再点一次就好。')
                        time.sleep(2.0)
                        try:
                            c.close()     # 被踢之后连接可能已经废了，重连再发
                        except Exception:
                            pass
                        c = C.AstralClient(c.host, c.port, timeout=20.0)
                        c.connect()
                        continue
                    raise
            p = s2c.player
        else:
            # 国服：China 段握手（内部会处理"被挤下线"的重试）
            ok, s2c, c = self._handshake_china(c, sid)
            p = s2c.player
        try:
            # 结构（已对照离线握手包复核）：
            #   task.condition      = map<int,int>   → dict() 直接就是 {condType: 值}
            #   task.condition1[17] = map<int,ConditionData>
            #        └ .params = repeated ConditionParams{param:场次, param1:角色ID}
            cond = dict(getattr(p.task, 'condition', {}) or {})

            total, wins = cond.get(14, 0), cond.get(13, 0)

            # 最近对局的记录包含在登录握手包里：
            #   Player.showPlayer (model.ShowPlayerInfo) 含 record[10] / praiseNum，
            #   无需向服务器发查询（查自己的 5153 会被服务端丢弃）。
            my_uid = int(getattr(p, 'id', 0))
            sp = getattr(p, 'showPlayer', None)
            praise = int(getattr(sp, 'praiseNum', 0) or cond.get(22, 0))
            # 拥有换装 = 拥有的**皮肤**数（与游戏 txt_SkinCount 一致）
            #   注意：背包 70000~76999 段是「装饰」（头像 / 名片 等），
            #         属另一项数据，不能计入皮肤。
            skins = _skins_of(p)
            adorn = skins['sum']
            _bagids = [int(getattr(x, 'item_id', 0)) for x in (getattr(p, 'bag_items', []) or [])]
            decor = len([i for i in _bagids if 70000 <= i < 77000])   # 装饰（备用）

            self._sess = c          # 留给复盘查询复用：短时间内再登一次会被服务器丢包
            ok = True
            return {'nick': getattr(p, 'nick', '?'), 'uid': getattr(p, 'id', '?'),
                    'region': region,
                    'platform': (getattr(p, 'platform', '')
                                 or getattr(s2c, 'platform', '') or 'CN_STEAM'),
                    'server': getattr(p, 'serverId', '?'),
                    'total': total, 'wins': wins,
                    'winrate': round(100.0 * wins / total, 1) if total else 0,
                    'heroCount': len(p.roleCard), 'praise': praise,
                    'level': int(getattr(p, 'level', 0) or 0),
                    'adorn': adorn, 'decor': decor,
                    'heroes': _heroes_of(p), 'skins': skins, 'maps': _maps_of(p),
                    # 必须与界面一样按从新到旧排序：界面点第 i 行会调 load_match(i)，
                    #   后端也按同一顺序取，否则「看到的是 A 局、下载的是 B 局」
                    'recent': sorted(_recent_of(p, my_uid),
                                     key=lambda x: int(x.get('time') or 0),
                                     reverse=True)}
        finally:
            if not ok:              # 只有失败才关：成功那条留着给复盘查询复用
                try:
                    c.close()
                except Exception:
                    pass

    # ── ③ 导出 ──
    def export_text(self, with_matches=False):
        """档案导出（Markdown 格式）。"""
        d = self.profile
        if not d:
            return {'ok': False, 'msg': '还没生成档案，请先登录'}
        sk = d.get('skins') or {}
        L = ['# 星趴档案', '',
             '- **昵称**：%s' % d.get('nick', ''),
             '- **UID**：%s' % d.get('uid', ''),
             # 不展示「服务器 6688」：那是服务端下发的区服编号字符串（model.Player#59），
             # 客户端里只用于对外上报（日志/支付订单），没有可读名字，容易误读成国服/国际服
             '- **平台**：%s' % d.get('platform', ''),
             '- **累计参与**：%s 局' % d.get('total', 0),
             '- **累计胜利**：%s 局' % d.get('wins', 0),
             '- **胜率**：%s%%' % d.get('winrate', 0),
             '- **拥有角色**：%s 个' % d.get('heroCount', 0),
             '- **拥有皮肤**：%s 套' % sk.get('sum', 0),
             '- **拥有装饰**：%s 件' % d.get('decor', 0),
             '- **缔结契约**：%s 位' % sk.get('contract', 0),
             '- **获赞**：%s' % d.get('praise', 0),
             '',
             '## 角色使用排行', '',
             '| # | 角色 | 称号 | 场次 | 胜场 | 胜率 | 等级 | 潜能 |',
             '|---:|---|---|---:|---:|---:|---:|---|']
        hs = sorted([x for x in (d.get('heroes') or []) if x.get('n')],
                    key=lambda x: -x['n'])
        for i, h in enumerate(hs, 1):
            rate = round(100.0 * h['w'] / h['n']) if h['n'] else 0
            L.append('| %d | %s | %s | %d | %d | %d%% | %s | %s |'
                     % (i, h.get('name', ''), h.get('title') or '—', h['n'], h['w'],
                        rate, h.get('lv', '—'), h.get('brk') or '未'))
        rec = d.get('recent') or []
        L += ['', '## 最近对局', '',
              '| 时间 | 角色 | 称号 | 结果 | 地图 |',
              '|---|---|---|---|---|']
        for r in rec:
            L.append('| %s | %s | %s | %s | %s |'
                     % (time.strftime('%Y-%m-%d %H:%M', time.localtime(r['time'])),
                        r.get('hero', ''), r.get('title') or '—',
                        '胜利' if r.get('rank') == 1 else ('第 %s 名' % r.get('rank')),
                        r.get('map') or '—'))
        L += ['', '## 地图胜场', '', '| 地图 | 胜场 |', '|---|---:|']
        for m in (d.get('maps') or []):
            L.append('| %s | %s |' % (m.get('name', ''), m.get('n', 0)))
        L += ['', '---', '',
              '作者：Nemophila & ZytanCrany　·　版本 %s' % VERSION,
              '生成时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S')]
        return {'ok': True, 'text': '\n'.join(L)}

    def save_export(self):
        """弹系统保存对话框，存成 .md 文件。"""
        r = self.export_text()
        if not r.get('ok'):
            return r
        try:
            nick = ((self.profile or {}).get('nick') or '导出').replace('/', '_')
            default = '星趴档案_%s_%s.md' % (nick, time.strftime('%y%m%d_%H%M'))
            path = None
            try:
                win = webview.windows[0] if getattr(webview, 'windows', None) else None
                if win:
                    # 新版用 FileDialog.SAVE，旧版退回 SAVE_DIALOG
                    dlg = getattr(getattr(webview, 'FileDialog', None), 'SAVE',
                                  getattr(webview, 'SAVE_DIALOG', 30))
                    res = win.create_file_dialog(dlg, save_filename=default)
                    if isinstance(res, (list, tuple)):
                        res = res[0] if res else None
                    path = res
            except Exception:
                path = None
            if not path:
                return {'ok': False, 'msg': '已取消保存'}
            out = Path(path)
            if out.suffix.lower() not in ('.md', '.markdown'):
                out = out.with_suffix('.md')
            out.write_text(r['text'], encoding='utf-8')
            return {'ok': True, 'path': str(out),
                    'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')}
        except Exception as e:
            return {'ok': False, 'msg': '保存失败：%s' % str(e)[:120]}


def _fit_window():
    """按屏幕尺寸自适应开窗。

    比例：屏宽的 62% × 屏高的 76%
    （客户区约 1053x806，对应 1707x1067 逻辑屏的 61.7% x 75.6%）。
    下限保证对局详情 10 列放得下（不然会出横向滚动条），上限不超出屏幕。

    注意：非 DPI 感知的进程调用 GetSystemMetrics 拿到的已经是「逻辑像素」，
    正好等于 pywebview 建窗用的单位，不要再拿 DPI 去缩放一次。
    """
    try:
        import ctypes
        u = ctypes.windll.user32
        sw, sh = int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except Exception:
        sw, sh = 1707, 1067
    if sw < 800 or sh < 600:
        sw, sh = 1707, 1067
    # pywebview 的宽高是「外框」，而目标尺寸是「客户区」（可用区域），
    # 所以把 Windows 边框(约 8px×2)和标题栏(约 38px)补回去，
    # 这样实际可用的客户区才等于上述 62% x 76%。
    w = max(1040, min(int(sw * 0.62), sw - 40)) + 16
    h = max(680, min(int(sh * 0.76), sh - 40)) + 38
    return (w, h)


def _selftest_helpers():
    """开机自检：档案解析的纯函数必须返回数据，不能返回 None。

    这类缺陷（函数体搬移时丢了 return）编译不报错，界面只表现为空白或
    「已登录，但读取档案失败」，而且必须真实登录才会走到这里；用假对象直接
    喂给它们，就能把问题挡在自检阶段。
    """

    class _S:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    card = _S(isBreakThrough=False, lv=5)
    card.pve_strengthen = _S(level=6, talent=[])
    p = _S(nick='自检', id=1, platform='CN_STEAM', serverId=6688,
           task=_S(condition={13: 1, 14: 1, 22: 3}, condition1={}),
           roleCard={104: card},
           showPlayer=_S(praiseNum=3, record=[]),
           winMap={}, bag_items=[])

    bad = []
    got = (('角色排行', _heroes_of(p), list), ('皮肤清单', _skins_of(p), dict),
           ('最近对局', _recent_of(p, 1), list), ('地图排行', _maps_of(p), list),
           ('任务计数', _pairs(p, 17), dict))
    for name, val, want in got:
        if not isinstance(val, want):
            bad.append('%s（%s）返回 %r，应为 %s' % (name, '档案解析',
                                                val, want.__name__))
    if not isinstance(_heroes_of(p), list) or not _heroes_of(p):
        bad.append('角色排行返回空表，角色卡解析可能失效')
    sk = _skins_of(p)
    if isinstance(sk, dict) and 'sum' not in sk:
        bad.append('皮肤清单缺少 sum 字段')

    # 复盘查询的输入识别（纯函数；判定错了「回放号/UID」就整个查不动）
    for txt, want_kind, want_num in (
            ('1790337873289576', 'replay', '1790337873289576'),
            ("'1790337873289576'", 'replay', '1790337873289576'),   # 前端加引号防 JS 精度丢失
            ('1000001', 'uid', '1000001'),
            ('  1000001 ', 'uid', '1000001'),
            ('', '', ''),
            ('123', '', '123')):
        k, n = Api._classify(txt)
        if (k, n) != (want_kind, want_num):
            bad.append('查询输入识别 %r → (%r, %r)，应为 (%r, %r)'
                       % (txt, k, n, want_kind, want_num))

    # 会话复用：每查一次就重新登录，服务器会把请求静默丢掉（真实故障）
    class _FakeCli:
        closed = 0

        def connect(self):
            pass

        def login_china(self, *a, **k):
            pass

        def login_abroad(self, *a, **k):
            pass

        def close(self):
            _FakeCli.closed += 1

    global _auto, load_token_info
    real_cli, real_auto = C.AstralClient, _auto
    real_ti = load_token_info
    try:
        C.AstralClient = lambda *a, **k: _FakeCli()
        _auto = lambda: ('自检-sid', '')
        api = Api()
        if api._session() is not api._session():
            bad.append('游戏连接没有复用：连查两次会各登录一次，会被服务器限流')
        api._drop_session()
        if _FakeCli.closed != 1:
            bad.append('_drop_session 没有关掉旧连接')

        # 「查自己」的识别必须*未登录*也生效：uid 存在 token.json 的 uid 字段，
        # 而 load_token() 只返回 accessToken 字符串，用错函数就会认不出自己
        load_token_info = lambda: {'accessToken': '自检', 'uid': 123456, 'nick': '自检'}
        api2 = Api()
        if api2._self_uid() != '123456':
            bad.append('未登录时读不到本机记住的 uid ⇒ 认不出「查自己」')
        api2.profile = {'uid': 123456, 'nick': '自检', 'level': 9, 'total': 10, 'wins': 5,
                        'heroCount': 3, 'praise': 7, 'adorn': 4, 'decor': 2,
                        'skins': {'sum': 4}, 'recent': []}
        r = api2.query('123456')
        st = (r or {}).get('statistics') or {}
        if not (r or {}).get('ok') or st.get('skinCount') != 4 or st.get('fightCount') != 10:
            bad.append('「查自己」没走本地档案或字段对不上：%r'
                       % ((r or {}).get('msg') or st,))
    except Exception as e:
        bad.append('会话/自查自检异常：%r' % (e,))
    finally:
        C.AstralClient, _auto = real_cli, real_auto
        load_token_info = real_ti
    return bad



def main():
    if '--selftest' in sys.argv:
        # 打包后排查用（--windowed 没有控制台）：写 userdata/log.txt 后直接退出
        try:
            from astral import gameart
            bad = list(gameart.selftest() or []) + _selftest_helpers()
            print('selftest 问题:', bad)
        except Exception as e:
            print('selftest 自身失败:', e)
        return
    api = Api()
    ww, wh = _fit_window()
    webview.create_window('星趴档案', str(asset('ui', 'app.html')),
                          js_api=api, width=ww, height=wh,
                          min_size=(980, 620), background_color='#F0EEE6')
    # 头像扫描交给 webview.start 的回调执行：必须等 GUI 后端（pythonnet → .NET 程序集）
    # 加载完成后再起扫描线程。放在 start() 之前会与 .NET 加载抢同一批原生 DLL，
    # 偶发程序集解析失败并直接崩在启动阶段（表现为「Unhandled exception in script」）。
    webview.start(art_scan_start)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()     # 打包成 exe 后多进程扫描必需（否则子进程会重启整个程序）
    main()
