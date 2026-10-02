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
                              DEFAULT_EXTRA, request_code, phone_login)
from astral.sdk_login import (patch_token_meta, clear_token,  # noqa: E402
                              load_token_info,)
from astral.sdk_login import (auto_login as _auto,   # noqa: E402
                              save_token, load_token)

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
#   回放服务器：https://sereplaycn.feimogames.com/prod/<replayId>
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
VERSION = 'v1.1.1'        # 工具版本号
REPLAY_DIR = DATA_DIR / 'replays'
REPLAY_URL = 'https://sereplaycn.feimogames.com/prod/%s'

# ── 列表用的「只抓开头」方案 ──
# 地图 ID 在回放文件开头 300 字节内（字段5 = fixed32 小端），
# 服务器支持 HTTP Range → 只下 8KB 就能拿到地图，不用拉整个 1.2MB。
REPLAY_PREFIX = 8192
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


def fetch_replay_map(replay_id):
    """只下载回放开头，返回 {'id':.., 'name':..}；结果落盘缓存。"""
    rid = str(replay_id or '')
    if not rid:
        return None
    if rid in _map_cache:
        return _map_cache[rid]
    try:
        req = urllib.request.Request(
            REPLAY_URL % rid, headers={'Range': 'bytes=0-%d' % (REPLAY_PREFIX - 1)})
        with urllib.request.urlopen(req, timeout=15) as r:
            buf = r.read(REPLAY_PREFIX)
        mid = map_from_prefix(buf)
    except Exception:
        return None
    info = {'id': mid, 'name': map_name(mid) or '未知地图'}
    _map_cache[rid] = info
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
    cache = REPLAY_DIR / ('%s.bin' % replay_id)
    if cache.exists() and cache.stat().st_size > 2000:
        data = cache.read_bytes()
    else:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 直连
        req = urllib.request.Request(REPLAY_URL % replay_id,
                                     headers={'User-Agent': 'Mozilla/5.0'})
        data = op.open(req, timeout=90).read()
        cache.write_bytes(data)

    # 从最后一帧往前回扫：最后一个快照里可能有玩家数据被清空
    # （某个玩家 hero=0 / cond 缺失），所以每个玩家要取【最近一帧里非空】的那份统计。
    merged = {}
    pos = len(data)
    for _ in range(12):                      # 最多回扫 12 帧
        k = data.rfind(b'\x12\x05match', 0, pos)
        if k < 0:
            break
        pos = k
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
        s = str(int(getattr(it, 'item_id', 0)))
        if len(s) == 9 and s.startswith('100'):
            owned.setdefault(int(s[3:6]), set()).add(int(s[6:]))

    out, tot_orig, tot_bond, tot_buy, n_ctr = [], 0, 0, 0, 0
    for hid in sorted(getattr(p, 'roleCard', {}).keys()):
        card = p.roleCard[hid]
        has = owned.get(hid, set())
        nm, v = by_id.get(hid, ('角色#%d' % hid, {}))
        names = v.get('skins') or []
        contract = bool(getattr(card, 'isBreakThrough', False))
        rows = [{'name': '初始皮肤', 'has': 1 in has},
                {'name': '羁绊皮肤', 'has': 2 in has}]
        for i, sname in enumerate(names):
            rows.append({'name': sname, 'has': (3 + i) in has})
        have = sum(1 for r in rows if r['has'])
        tot_orig += (1 if 1 in has else 0)
        tot_bond += (1 if 2 in has else 0)
        tot_buy += len([x for x in has if x >= 3])
        n_ctr += (1 if contract else 0)
        out.append({'id': hid, 'name': nm, 'title': v.get('title', ''),
                    'contract': contract, 'have': have, 'total': len(rows),
                    'skins': rows})
    out.sort(key=lambda x: (-x['have'], x['id']))
    # 游戏内「全部皮肤」= 每角色(初始+羁绊) + Wiki 命名皮肤
    sk_total = sum(2 + len(v.get('skins') or []) for v in wiki.values())
    return {'list': out, 'orig': tot_orig, 'bond': tot_bond, 'buy': tot_buy,
            'contract': n_ctr, 'named': tot_bond + tot_buy,
            'hero_have': len(out), 'hero_total': len(by_id), 'sk_total': sk_total,
            'sum': tot_orig + tot_bond + tot_buy}


REVIEW_DIR = DATA_DIR / 'review'


def get_replay_file(replay_id):
    """确保本地有该局回放文件（复用既有缓存目录），返回 Path"""
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    cache = REPLAY_DIR / ('%s.bin' % replay_id)
    if cache.exists() and cache.stat().st_size > 2000:
        return cache
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 直连
    req = urllib.request.Request(REPLAY_URL % replay_id, headers={'User-Agent': 'Mozilla/5.0'})
    data = op.open(req, timeout=90).read()
    cache.write_bytes(data)
    return cache


#   缓存版本：改动复盘数据结构（新增字段）后必须 +1，否则旧缓存会被直接复用，
#   导致新增字段缺失、功能不生效（例如旧缓存里没有 skin 字段时，
#   头像会全部回落到初始皮肤）。
REVIEW_CACHE_VERSION = 2


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
    try:
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
ART_STATE = {'running': False, 'text': '', 'i': 0, 'n': 0, 'done': True,
             'found': 0, 'game': True, 'seconds': 0, 'reason': ''}
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
    """角色卡 → 使用排行（含 PVE 等级与潜能三态）。"""
    fc, wc = _pairs(p, 17), _pairs(p, 18)
    heroes = []
    for hid, card in p.roleCard.items():
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
                       'brk': _pot})
    heroes.sort(key=lambda h: (-h['n'], h['id']))

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

class Api:
    def __init__(self):
        self.phone = ''
        self.sid = ''
        self.profile = None

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
        return {'ok': True, 'maps': out}

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

    def load_match(self, idx):
        """下载并解析第 idx 局的回放，返回 4 名玩家的完整战绩"""
        try:
            rec = (self.profile or {}).get('recent') or []
            idx = int(idx)
            if idx < 0 or idx >= len(rec):
                return {'ok': False, 'msg': '没有这一局'}
            rid = rec[idx].get('replayId') or ''
            if not rid:
                return {'ok': False, 'msg': '这一局没有回放号'}
            st = fetch_replay_stats(rid)
            if not st:
                return {'ok': False, 'msg': '回放解析不出战绩'}
            return {'ok': True, 'stats': st}
        except Exception as e:
            return {'ok': False, 'msg': '拉取失败：%s' % str(e)[:110]}

    # ── 对局复盘（点玩家名 → 弹窗）：每轮数据 + 筹码三选一 ──
    def load_review(self, idx):
        try:
            rec = (self.profile or {}).get('recent') or []
            idx = int(idx)
            if idx < 0 or idx >= len(rec):
                return {'ok': False, 'msg': '没有这一局'}
            one = rec[idx]
            rid = str(one.get('replayId') or '')
            if not rid:
                return {'ok': False, 'msg': '这一局没有回放号'}
            rv = build_review_cached(rid)
            if not rv or not rv.get('players'):
                return {'ok': False, 'msg': '回放解析不出复盘数据'}
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

    # ── ② 登录 + 拉档案 ──
    def auto_login(self):
        """用上次记住的 accessToken 自动登录（免验证码）。"""
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
        self._remember_id()
        return {'ok': True, 'msg': '✓ 已用记住的登录态登录，免验证码',
                'profile': self.profile}

    def login_state(self):
        """登录页顶部要显示的「上次登录」信息（手机号只给掩码）。"""
        info = load_token_info() or {}
        prof = self.profile or {}
        return {
            'version': VERSION,
            'has_token': bool(info.get('accessToken')),
            'tel_masked': info.get('tel_masked') or '',
            'uid': prof.get('uid') or info.get('uid') or '',
            'nick': prof.get('nick') or info.get('nick') or '',
            'saved_at': (info.get('saved_at') or '').replace('T', ' ')[:16],
        }

    def logout(self):
        """退出登录：只结束当前会话，【本机记住的账号保留】。

        保留的原因：否则「使用上次账号登录」和开机自动登录都会失效，
        使用者只能重新走短信验证码，功能自相矛盾。
        真要抹掉本机凭据请用 forget()。
        """
        self.profile = None
        self.sid = None
        try:
            (DATA_DIR / 'sid.txt').unlink(missing_ok=True)
        except Exception:
            pass
        return {'ok': True, 'msg': '已退出登录（本机仍记住该账号）'}

    def forget(self):
        """彻底清除本机记住的登录态（token.json）。"""
        self.profile = None
        self.sid = None
        try:
            clear_token()
        except Exception:
            pass
        return {'ok': True, 'msg': '已清除本机登录态'}

    def _remember_id(self):
        """把 UID/昵称补进 token.json，供登录页显示。"""
        try:
            d = self.profile or {}
            patch_token_meta(uid=d.get('uid'), nick=d.get('nick'))
        except Exception:
            pass

    def has_token(self):
        try:
            return bool(load_token())
        except Exception:
            return False

    def do_login(self, phone: str, code: str):
        phone = re.sub(r'\D', '', phone or '')
        code = (code or '').strip()
        try:
            sid, resp = phone_login(phone, code)
        except Exception as e:
            return {'ok': False, 'msg': '登录失败：%s' % str(e)[:100]}
        try:
            saved = save_token(resp, phone)
            self.token_saved = bool(saved)
        except Exception:
            self.token_saved = False
        self.sid = sid
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / 'sid.txt').write_text(sid, encoding='utf-8')
        except Exception:
            pass
        try:
            self.profile = self._fetch(sid)
        except Exception as e:
            return {'ok': False, 'msg': '已登录，但读取档案失败：%s' % str(e)[:100]}
        self._remember_id()
        return {'ok': True, 'msg': '✓ 登录成功，档案已生成', 'profile': self.profile}

    def _fetch(self, sid):
        """登录握手 → 档案字典。"""
        c = C.AstralClient(GAME_HOST, GAME_PORT, timeout=20.0)
        c.connect()
        try:
            s2c = c.login_china(sid, device_id=DEFAULT_DEVICE_ID,
                                extra=DEFAULT_EXTRA, client_ver='3.2.0')
            p = s2c.player
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

            return {'nick': getattr(p, 'nick', '?'), 'uid': getattr(p, 'id', '?'),
                    'platform': (getattr(p, 'platform', '')
                                 or getattr(s2c, 'platform', '') or 'CN_STEAM'),
                    'server': getattr(p, 'serverId', '?'),
                    'total': total, 'wins': wins,
                    'winrate': round(100.0 * wins / total, 1) if total else 0,
                    'heroCount': len(p.roleCard), 'praise': praise,
                    'adorn': adorn, 'decor': decor,
                    'heroes': _heroes_of(p), 'skins': skins, 'maps': _maps_of(p),
                    # 必须与界面一样按从新到旧排序：界面点第 i 行会调 load_match(i)，
                    #   后端也按同一顺序取，否则「看到的是 A 局、下载的是 B 局」
                    'recent': sorted(_recent_of(p, my_uid),
                                     key=lambda x: int(x.get('time') or 0),
                                     reverse=True)}
        finally:
            c.close()

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
    art_scan_start()          # 后台从本机游戏补全头像资源（不阻塞登录界面）
    webview.start()


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()     # 打包成 exe 后多进程扫描必需（否则子进程会重启整个程序）
    main()
