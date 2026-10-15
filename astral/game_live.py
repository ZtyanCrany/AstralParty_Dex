# -*- coding: utf-8 -*-
"""运行时从本机游戏客户端抽配置表，使旧版本的软件也能显示新版本的内容。

内置表（`assets/data/*.json`）是打包时的客户端快照，游戏更新后即过期。
本模块在启动时比对客户端内容指纹，与上次抽表时记录的不一致就重抽一次，
生成小表写入 `userdata/game_live/`；读取优先走该目录（见 `paths.data_file`）。
指纹未变化时不重抽。

只读游戏文件，不写游戏目录，不联网；任一环节失败均回退到内置表，行为与旧版一致。
抽取在后台线程执行，不阻塞界面启动。
"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path

from . import paths, tablegen

# 生成小表所需的表
NEEDED = ('STRCard', 'STRItem', 'STRCharacter', 'STRRelic', 'STRBuff', 'STRMonster',
          'STRMap')

# 定位配置包的标记：卡牌美术资源名前缀
MARKER = b'UT_HandCard_'
FALLBACK_MARKERS = [b'UT_Relic_', b'UT_Skin_', '轨道炮'.encode('utf-8')]

# 扫描包的大小上限：先用小上限快速扫，扫不到再放大重扫
SCAN_CAPS = (8 * 1024 * 1024, 128 * 1024 * 1024)

LIVE_SUBDIR = 'game_live'
SCAN_CACHE = '_scan.json'
STAMP = '_stamp.json'


def live_dir():
    return Path(paths.DATA_DIR) / LIVE_SUBDIR


def _log(msg, log=None):
    for fn in (log, getattr(sys.stderr, 'write', None)):
        if fn:
            try:
                fn('[game_live] %s\n' % msg)
                return
            except Exception:
                continue


# ── 找游戏数据目录 ──────────────────────────────────────────────
def steam_data_dirs(kind='cn'):
    """Steam 库里的游戏数据目录（读 libraryfolders.vdf 定位，不猜盘符）。

    客户端本体在 `<库>/steamapps/common/Astral Party/<子目录>/` 下，
    数据落在 `<子目录>/AstralParty_{CN,INT}_Data`。
    """
    want = 'AstralParty_CN_Data' if kind == 'cn' else 'AstralParty_INT_Data'
    out, roots = [], []
    for vdf in ((os.environ.get('ProgramFiles(x86)') or r'C:\Program Files (x86)'),
                (os.environ.get('ProgramFiles') or r'C:\Program Files')):
        p = Path(vdf) / 'Steam' / 'steamapps' / 'libraryfolders.vdf'
        if p.is_file():
            roots.append(p)
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam'),
                          (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam')):
            try:
                with winreg.OpenKey(hive, key) as k:
                    p = Path(winreg.QueryValueEx(k, 'SteamPath')[0]) / 'steamapps' / 'libraryfolders.vdf'
                    if p.is_file():
                        roots.append(p)
            except OSError:
                pass
    except Exception:
        pass
    libdirs = []
    for vdf in roots:
        try:
            text = vdf.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            libdirs.append(m.group(1).replace('\\\\', '\\'))
    for lib in libdirs:
        common = Path(lib) / 'steamapps' / 'common' / 'Astral Party'
        if not common.is_dir():
            continue
        try:
            subs = list(common.iterdir())
        except OSError:
            continue
        for sub in subs:
            if not sub.is_dir():
                continue
            for d in sub.glob(want):        # 只取本国服/国际服对应的目录，避免两服混用
                if d.is_dir():
                    out.append(d)
    return out


def game_dirs(kind='cn'):
    """候选游戏数据目录：LocalLow 缓存 + Steam 库（默认只给国服）。

    要的都是「国服」那一套：软件的文案表取自国服客户端，混进国际服会串服。
    """
    out = []
    prof = os.environ.get('USERPROFILE') or str(Path.home())
    low = Path(prof) / 'AppData' / 'LocalLow' / 'feimo'
    for sub in (('AstralParty_CN', '吉星派对') if kind == 'cn' else ('AstralParty_INT',)):
        out.append(low / sub)
    out += steam_data_dirs(kind)
    return out


def catalog_version(root):
    """该目录里 catalog_<版本>.json 的最高版本号（元组）；没有返回 None。"""
    base = Path(root) / 'com.unity.addressables'
    if not base.is_dir():
        return None
    best = None
    try:
        names = list(base.glob('catalog_*.json'))
    except OSError:
        return None
    for f in names:
        m = re.match(r'catalog_(\d+(?:\.\d+)*)\.json$', f.name)
        if not m:
            continue
        t = tuple(int(x) for x in m.group(1).split('.'))
        if best is None or t > best:
            best = t
    return best


def client_version(kind='cn'):
    """客户端当前版本：各候选目录里最高的 catalog 版本号（元组）；没有返回 None。

    catalog 与内容包可能不在同一目录（新 catalog 写入 LocalLow 缓存、内容包落在
    安装目录），故取各目录的最高版本。
    """
    best = None
    for c in game_dirs(kind):
        v = catalog_version(c)
        if v is not None and (best is None or v > best):
            best = v
    return best


def bundle_freshness(root):
    """Addressables 包的最新改动时间；没有包返回 0。"""
    base = Path(root) / 'com.unity.addressables' / 'AssetBundles'
    if not base.is_dir():
        return 0.0
    newest = 0.0
    for f in base.rglob('__data'):
        try:
            t = f.stat().st_mtime
        except OSError:
            continue
        if t > newest:
            newest = t
    return newest


def newest_root(kind='cn'):
    """内容最新的游戏目录（返回 `(目录, 版本)`）。

    按内容包的新旧选目录：catalog 与内容包可能分处两个目录，要求「同目录有 catalog」
    会选中 catalog 版本新而内容陈旧的缓存。版本号另取各目录 catalog 的最高值，
    仅用于记录与显示。
    """
    best_root, best_key = None, None
    for c in game_dirs(kind):
        fresh = bundle_freshness(c)
        if fresh <= 0:
            continue
        v = catalog_version(c) or (0,)
        key = (fresh, v)
        if best_key is None or key > best_key:
            best_root, best_key = c, key
    if best_root is None:
        return None, None
    return best_root, client_version(kind)


def version_text(v):
    return '.'.join(str(x) for x in v) if v else ''


# ── 找配置包 / 抽表 ─────────────────────────────────────────────
def find_cfg_bundles(root, cache=True, caps=SCAN_CAPS):
    """在 Addressables 里找出含配置 TextAsset 的包（按内容找，不认哈希名）。

    结果按内容指纹缓存：包名是哈希，重找需遍历整个 AssetBundles。
    """
    root = Path(root)
    # 缓存键含包最新时间：更新后哈希目录会变，仅比目录与版本号会命中残留的旧包
    key = [str(root), version_text(catalog_version(root)), int(bundle_freshness(root))]
    cachef = live_dir() / SCAN_CACHE
    if cache:
        try:
            d = json.loads(cachef.read_text(encoding='utf-8'))
            if d.get('key') == key:
                got = [Path(p) for p in (d.get('bundles') or [])]
                if got and all(p.is_file() for p in got):
                    return got
        except Exception:
            pass

    base = root / 'com.unity.addressables' / 'AssetBundles'
    if not base.is_dir():
        return []
    markers = [MARKER] + FALLBACK_MARKERS
    hits = []
    for cap in caps:
        hits = []
        for f in base.rglob('__data'):
            try:
                if not f.is_file() or f.stat().st_size > cap:
                    continue
                blob = f.read_bytes()
            except OSError:
                continue
            if any(m in blob for m in markers):
                hits.append(f)
        if hits:
            break
    if cache and hits:
        try:
            live_dir().mkdir(parents=True, exist_ok=True)
            cachef.write_text(json.dumps({'key': key,
                                          'bundles': [str(p) for p in hits]},
                                         ensure_ascii=False, indent=1), encoding='utf-8')
        except Exception:
            pass
    return hits


def load_game_tables(root, log=None):
    """找配置包 → 抽文案表，保证关键表齐全（不齐则放大扫描上限重来）。

    返回 (包列表, 表)。
    """
    bundles = find_cfg_bundles(root)
    tables = load_tables(bundles)
    miss = [t for t in NEEDED if not tables.get(t)]
    if miss:
        _log('关键表缺 %s，放大扫描上限重来' % ','.join(miss), log)
        more = find_cfg_bundles(root, cache=False, caps=(SCAN_CAPS[-1],))
        if len(more) > len(bundles):
            t2 = load_tables(more, only=None)
            if len(t2) >= len(tables):
                return more, t2
    return bundles, tables


def _varint(buf, i):
    v, sh = 0, 0
    while i < len(buf):
        c = buf[i]
        i += 1
        v |= (c & 0x7F) << sh
        if not (c & 0x80):
            return v, i
        sh += 7
        if sh > 28:
            return None, i
    return None, i


def parse_records(buf):
    """一份 TextAsset 的字节 → {键: 文本}。

    记录格式：0x0A <记录长 varint> 0x0D <键 小端4字节> 0x12 <串长 varint> <UTF-8 文本>
    """
    out, order = {}, 0
    i, n = 0, len(buf)
    while i < n - 2:
        if buf[i] != 0x0A:
            i += 1
            continue
        ln, j = _varint(buf, i + 1)
        if ln is None or ln < 4 or j + ln > n:
            i += 1
            continue
        body = buf[j:j + ln]
        key, k = None, None
        if body[:1] == b'\x0d' and len(body) >= 5:
            key = int.from_bytes(body[1:5], 'little')
            k = 5
        p = k if k is not None else 0
        while p < len(body):
            if body[p] != 0x12:
                p += 1
                continue
            sl, q = _varint(body, p + 1)
            if sl is None or sl < 1 or q + sl > len(body):
                p += 1
                continue
            try:
                txt = body[q:q + sl].decode('utf-8')
            except UnicodeDecodeError:
                p += 1
                continue
            if txt and not any(ord(c) < 0x20 for c in txt):
                if key is None:
                    order += 1
                    key = order
                out[str(key)] = txt
            break
        i = j + ln
    return out


def load_tables(bundles, only=None):
    """用 UnityPy 只读载入包，把 TextAsset 解析成 {表名: {键: 文本}}。"""
    try:
        import UnityPy
    except ImportError:
        return {}
    tables = {}
    for b in bundles:
        try:
            env = UnityPy.load(str(b))
        except Exception:
            continue
        for o in env.objects:
            if o.type.name != 'TextAsset':
                continue
            d = o.read()
            name = getattr(d, 'm_Name', '') or ''
            if not name or (only and name != only):
                continue
            raw = getattr(d, 'm_Script', b'')
            buf = raw.encode('utf-8', 'surrogateescape') if isinstance(raw, str) else raw
            rec = parse_records(buf)
            if rec:
                tables.setdefault(name, {}).update(rec)
    return tables


# ── stamp 与刷新 ────────────────────────────────────────────────
def read_stamp():
    try:
        return json.loads((live_dir() / STAMP).read_text(encoding='utf-8'))
    except Exception:
        return {}


def write_stamp(info):
    try:
        live_dir().mkdir(parents=True, exist_ok=True)
        (live_dir() / STAMP).write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                        encoding='utf-8')
    except Exception:
        pass


def current_stamp():
    """客户端内容指纹：版本 + 包最新时间，任一变化即需重抽。"""
    root, ver = newest_root()
    if root is None:
        return {}
    return {'version': version_text(ver), 'root': str(root),
            'freshness': int(bundle_freshness(root))}


def is_stale():
    """客户端内容是否比手上的新（或尚未抽过）。"""
    cur = current_stamp()
    if not cur:
        return False                     # 本机没装游戏 / 找不到包 ⇒ 不动
    old = read_stamp()
    return (old.get('version') != cur.get('version')
            or old.get('freshness') != cur.get('freshness'))


def _gen_char_ids(tables, wiki, base):
    """角色 ID → 名字（`assets/data/character_ids.json`）。

    ID 类型与基线保持一致：内置表用整数，而 main.py 会把 HERO 的键同对局中的整数
    角色号一起排序，类型不一致会抛 TypeError。基线读不到时按整数生成。
    """
    want_int = True
    for h in (base or []):
        if isinstance(h, dict) and 'id' in h:
            want_int = isinstance(h['id'], int)
            break
    out = []
    for k, v in sorted(tablegen.char_tables(tables, wiki).items()):
        out.append({'id': k if want_int else str(k), 'name': v.get('name')})
    return out


def refresh(bundled_dir=None, force=False, log=None):
    """抽客户端文案表并重建小表，写入 `userdata/game_live/`。

    返回报告 dict；无需刷新或失败时返回 None。`bundled_dir` 为合并基线，默认内置表。
    """
    cur = current_stamp()
    if not cur:
        _log('本机没找到游戏内容包，跳过', log)
        return None
    if not force and not is_stale():
        return None
    root = Path(cur['root'])
    t0 = time.time()
    bundles, tables = load_game_tables(root, log=log)
    if not bundles:
        _log('没找到配置包：%s' % root, log)
        return None
    if not tables:
        _log('配置包解析不出文案表', log)
        return None
    missing = [t for t in NEEDED if not tables.get(t)]
    if missing:
        _log('缺表 %s（继续，缺的部分沿用内置表）' % ','.join(missing), log)

    out = live_dir()
    out.mkdir(parents=True, exist_ok=True)
    base = Path(bundled_dir) if bundled_dir else paths.asset('assets', 'data')

    def load_base(name, default):
        try:
            return json.loads((base / name).read_text(encoding='utf-8'))
        except Exception:
            return default

    rep = {'version': cur['version'], 'root': str(root), 'bundles': len(bundles),
           'tables': len(tables), 'wrote': [], 'seconds': 0}

    def dump(name, obj):
        (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
        rep['wrote'].append(name)

    # 原始表快照，留档备查
    (out / 'game_texts.json').write_text(
        json.dumps({'source': {'root': str(root), 'version': cur['version'],
                               'bundles': [str(b) for b in bundles]},
                    'tables': tables}, ensure_ascii=False, indent=1), encoding='utf-8')
    rep['wrote'].append('game_texts.json')

    wiki = load_base('skins.json', {})
    jobs = [
        ('cards.json', lambda: tablegen.gen_cards(tables, load_base('cards.json', []))[0]),
        ('chips.json', lambda: tablegen.gen_chips(tables, load_base('chips.json', []))[0]),
        ('buffs.json', lambda: tablegen.gen_buffs(tables)[0]),
        ('monsters.json', lambda: tablegen.gen_monsters(tables)[0]),
        ('missions.json', lambda: tablegen.gen_missions(tables,
                                                       load_base('missions.json', {}))[0]),
        ('heroes.json', lambda: tablegen.gen_heroes(tables, load_base('heroes.json', []), wiki)[0]),
        ('character_ids.json', lambda: _gen_char_ids(tables, wiki,
                                                    load_base('character_ids.json', []))),
        ('skins_live.json', lambda: dict(
            tablegen.gen_skins(tables, wiki)[0],
            source={'tables': ['STRItem', 'STRCharacter'],
                    'note': '运行时由 astral/game_live.py 从客户端文本表生成'})),
    ]
    for name, fn in jobs:
        try:
            dump(name, fn())
        except Exception as e:
            _log('生成 %s 失败：%r' % (name, e), log)
    rep['seconds'] = round(time.time() - t0, 1)
    write_stamp(cur)
    _log('已按客户端 %s 重建 %d 张表（%.1fs）：%s'
         % (cur['version'], len(rep['wrote']), rep['seconds'], ', '.join(rep['wrote'])), log)
    return rep


def clear():
    """删除运行时数据，回到内置表（排查用）。"""
    import shutil
    try:
        shutil.rmtree(live_dir())
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False
