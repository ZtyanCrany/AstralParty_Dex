# -*- coding: utf-8 -*-
"""运行时从「本机游戏资源包」提取美术：角色头像 + 角色立绘头像。

════════ 为什么是运行时提取，而不是打包进 exe ════════
① 游戏出新角色 / 新皮肤后，打包进去的图会立刻过时 → 运行时扫描本机资源包，始终与游戏版本一致；
② 游戏美术资源的版权属官方 → 发行包本身不分发这些图，分发更合规。
（本工具的运行前提是本机已安装游戏。）

════════ 存哪 / 怎么做到「第二次启动瞬间完成」 ════════
· 提取结果 → userdata/assets/{avatars,photos}/<tag>.png
· 扫描进度 → userdata/assets/_scan.json 里的「已扫到哪个 mtime」水位线。
  资源包不会原地改内容（更新=新文件），所以 mtime ≤ 水位线的包一律跳过；
  扫描按 mtime **升序**推进并随时落盘 → 被中断也能续扫，不重复扫描。
  首次全量约 1 分钟；此后每次启动几乎 0 秒；游戏更新后只扫新增的包。

════════ 资源位置 ════════
    热更新缓存  %USERPROFILE%\\AppData\\LocalLow\\feimo\\AstralParty_CN\\
                com.unity.addressables\\AssetBundles\\<哈希>\\<哈希>\\__data
    安装基础包  <Steam 库>\\steamapps\\common\\Astral Party\\<分支>\\AstralParty_CN_Data\\
                StreamingAssets\\aa\\StandaloneWindows64\\*.bundle

找不到游戏 / 提不出图 → 一律不报错、静默跳过，界面用占位头像兜底（见 ui/app.html 的 phAv）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path

from . import paths

PREFIX_AV = 'UT_Platform_'                  # 角色头像（注意同前缀还有地图图标：Start/Shop/...）
PREFIX_PH = 'UT_Hero_ProfilePhoto_'         # 角色立绘头像（含皮肤）
STATE_NAME = '_scan.json'
AV_SIZE = 260                               # 角色头像原图边长（读的时候再裁内容边界）
PH_SIZE = 160                               # 立绘头像：提取时即裁成方形 160


# ══════════════════ 路径 ══════════════════

def avatars_dir() -> Path:
    return paths.DATA_DIR / 'assets' / 'avatars'


def photos_dir() -> Path:
    return paths.DATA_DIR / 'assets' / 'photos'


def _state_path() -> Path:
    return paths.DATA_DIR / 'assets' / STATE_NAME


def _load_state() -> dict:
    try:
        d = json.loads(_state_path().read_text(encoding='utf-8'))
        if isinstance(d, dict):
            d.setdefault('watermark', 0.0)
            d.setdefault('extracted', {})
            d.setdefault('scanned_at', 0)
            d.setdefault('catalog', '')
            return d
    except Exception:
        pass
    return {'watermark': 0.0, 'extracted': {}, 'scanned_at': 0, 'catalog': ''}


def _save_state(st: dict) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


# ══════════════════ 定位游戏 ══════════════════

def find_cache():
    """游戏热更新缓存目录（catalog_*.json 与 AssetBundles 所在处）。找不到返回 None。"""
    cands = []
    up = os.environ.get('USERPROFILE')
    if up:
        cands.append(Path(up) / 'AppData/LocalLow/feimo/AstralParty_CN/com.unity.addressables')
    la = os.environ.get('LOCALAPPDATA')
    if la:
        cands.append(Path(la).parent / 'LocalLow/feimo/AstralParty_CN/com.unity.addressables')
    for c in cands:
        try:
            if (c / 'AssetBundles').is_dir() or next(c.glob('catalog_*.json'), None):
                return c
        except Exception:
            continue
    return None


def _steam_roots():
    """Steam 库根目录：注册表里的 SteamPath + libraryfolders.vdf 里登记的库。"""
    roots = []
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam'),
                          (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam')):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ('SteamPath', 'InstallPath'):
                        try:
                            roots.append(Path(winreg.QueryValueEx(k, name)[0]))
                        except OSError:
                            pass
            except OSError:
                pass
    except Exception:
        pass
    for p in ('C:/Program Files (x86)/Steam', 'C:/Program Files/Steam', 'D:/Steam', 'E:/Steam'):
        roots.append(Path(p))
    # libraryfolders.vdf 里还有别的库（装在别的盘）
    extra = []
    for r in list(roots):
        vdf = r / 'steamapps' / 'libraryfolders.vdf'
        try:
            if vdf.is_file():
                for m in re.finditer(r'"path"\s+"([^"]+)"',
                                     vdf.read_text(encoding='utf-8', errors='ignore')):
                    extra.append(Path(m.group(1).replace('\\\\', '/')))
        except Exception:
            pass
    for r in extra:
        if r not in roots:
            roots.append(r)
    return roots


def find_installs(hint=None):
    """安装目录里的基础资源包目录（StandaloneWindows64）。找不到返回 []。

    两点注意：
      ① 同一目录会以不同大小写出现（注册表给 `d:\\St\\Steam`、libraryfolders 给 `D:\\St\\Steam`）
         → 不按大小写归一会把同一个库扫描两遍，耗时翻倍；
      ② 机器上可能同时装着国服（AstralParty_CN）和国际服（AstralParty_INT）
         → 本工具面向国服，优先只取 CN；没有 CN 才退回其它分支。

    兜底：`userdata/assets/_gamepath.txt` 第一行可手写 Steam 库根目录
    （如 `D:\\St\\Steam`）或**直接写资源包目录**（含 *.bundle 的那层），
    供「装在非注册表 Steam 库 / 绿色版」的用户使用。
    """
    if not hint:
        hp = paths.DATA_DIR / 'assets' / '_gamepath.txt'
        if hp.is_file():
            try:
                hint = hp.read_text(encoding='utf-8').strip().splitlines()[0].strip()
            except Exception:
                hint = None
    if hint:
        hp = Path(hint)
        if hp.is_dir() and next(hp.glob('*.bundle'), None) is not None:
            return [hp]                                       # 直接给的是资源包目录
    roots = [Path(hint)] if hint else []
    roots += _steam_roots()
    all_dirs, seen = [], set()
    for r in roots:
        try:
            for aa in r.glob('steamapps/common/Astral Party/*/*_Data/StreamingAssets/aa/StandaloneWindows64'):
                key = os.path.normcase(os.path.abspath(str(aa)))       # 大小写归一
                if aa.is_dir() and key not in seen:
                    seen.add(key)
                    all_dirs.append(aa)
        except Exception:
            continue
    cn = [d for d in all_dirs if d.parents[2].name.endswith('_CN_Data')]
    return cn or all_dirs


def bundle_files(cache=None, installs=None):
    """全部资源包（热更新 __data + 安装基础包），按 mtime 升序（利于断点续扫）。"""
    out = []
    if cache and (cache / 'AssetBundles').is_dir():
        out += list((cache / 'AssetBundles').rglob('__data'))
    for d in (installs or []):
        try:
            out += list(d.glob('*.bundle'))
        except Exception:
            pass
    keep = []
    for p in out:
        try:
            keep.append((p.stat().st_mtime, p))
        except OSError:
            pass
    keep.sort(key=lambda x: x[0])
    return [(p, mt) for mt, p in keep]


# ══════════════════ 资源清单 ══════════════════

def catalog_keys(cache, prefixes=(PREFIX_AV, PREFIX_PH)):
    """从最新 catalog_*.json 的 m_KeyDataString 里取全部匹配前缀的资源名。"""
    if not cache:
        return '', []
    cats = sorted(cache.glob('catalog_*.json'))
    if not cats:
        return '', []
    cat = cats[-1]
    try:
        data = json.loads(cat.read_text(encoding='utf-8-sig'))
        raw = base64.b64decode(data['m_KeyDataString'])
    except Exception:
        return cat.name, []
    keys = set()
    pat = '|'.join(re.escape(p) for p in prefixes)
    for run in re.findall(rb'[\x20-\x7e]{6,}', raw):
        s = run.decode('ascii', 'ignore')
        for part in re.split(r'(?=(?:%s))' % pat, s):
            for p in prefixes:
                if part.startswith(p):
                    m = re.match(re.escape(p) + r'([0-9A-Za-z_]{1,16})', part)
                    if m:
                        keys.add(p + m.group(1).rstrip('_'))
                    break
    return cat.name, sorted(keys)


def wanted(hero_ids, cache):
    """算出「需要的资源名 → 落盘 tag」。只认角色编号型，避开地图图标等同前缀资源。"""
    ids = {str(int(h)) for h in hero_ids if h}
    _cat, keys = catalog_keys(cache)
    out = {}                                            # key → (子目录, tag)
    for k in keys:
        if k.startswith(PREFIX_AV):
            tail = k[len(PREFIX_AV):]
            if tail in ids:                             # 只有 101/102…/306 这类角色号
                out[k] = ('avatars', tail)
        elif k.startswith(PREFIX_PH):
            tail = k[len(PREFIX_PH):]
            m = re.match(r'^(\d{3})(?:_(\d{2})|_(Max))?$', tail)
            if m and m.group(1) in ids:
                out[k] = ('photos', tail)
    return out


# ══════════════════ 图像处理 ══════════════════

def trim_and_resize(im, size, margin=0.12):
    """找内容边界 → 以内容为中心的**正方形**裁切（外扩 margin）→ 缩放到 size×size。

    必须裁成正方形：圆形头像框里用 object-fit:cover 时，非正方形图会被二次裁切
    （会切掉头发/耳朵）。方框一律以内容为中心、**不许往画布内塞**
    （塞=平移=吃掉余量=实心内容被切），越界交给 PIL 补透明。
    """
    from PIL import Image
    im = im.convert('RGBA')
    bg = im.getpixel((0, 0))
    w, h = im.size
    px = im.load()
    x0, y0, x1, y1 = w, h, -1, -1
    step = max(1, min(w, h) // 200)
    for y in range(0, h, step):
        for x in range(0, w, step):
            p = px[x, y]
            if p[3] <= 12:
                continue
            if abs(p[0] - bg[0]) + abs(p[1] - bg[1]) + abs(p[2] - bg[2]) > 24:
                if x < x0: x0 = x
                if y < y0: y0 = y
                if x > x1: x1 = x
                if y > y1: y1 = y
    if x1 > x0 and y1 > y0:
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        side = max(4, int(max(x1 - x0, y1 - y0) * (1 + 2 * margin)))
        a0 = int(round(cx - side / 2.0))
        b0 = int(round(cy - side / 2.0))
        im = im.crop((a0, b0, a0 + side, b0 + side))
    return im.resize((size, size), Image.LANCZOS)


# ══════════════════ 并行扫描 ══════════════════
#   扫一个资源包 = 读文件 + 解压（UnityPy）。单包约 12ms，全量 6661 个包串行需约
#   75 秒 —— 光靠去重不够，因此按 CPU 核数并行。子进程负责「扫 + 命中就落盘」，
#   只把命中的资源名回传（省 IPC）；水位线按块推进（块内并行、块间串行）→ 可断点续扫。
WORKERS_MAX = 8
CHUNK = 400                 # 每块的包数：中断最多白扫 1 块（几秒）

_W = {}                     # 子进程里的工作上下文（由 _init_worker 注入）


def _init_worker(want, dirs, av_size, ph_size):
    _W['want'] = want
    _W['dirs'] = dirs
    _W['av'] = av_size
    _W['ph'] = ph_size


# ══════════════════ 资源包读取 ══════════════════

def bundle_textures(env):
    """遍历已加载的资源包，逐个产出 (资源名, 数据对象)。

    只取 Texture2D / Sprite；对象读取失败的条目跳过。
    离线工具与运行时提取共用这一份遍历，避免两处实现各自演化。
    """
    for o in env.objects:
        if o.type.name not in ('Texture2D', 'Sprite'):
            continue
        try:
            d = o.read()
            name = getattr(d, 'm_Name', '') or ''
        except Exception:
            continue
        yield name, d


def _scan_one(path):
    """子进程入口：扫一个资源包，命中就裁好落盘，返回命中的资源名。

    必须是模块级函数 —— 多进程在 Windows 上需要能重新 import 到它。
    """
    hits = []
    want = _W.get('want') or {}
    if not want:
        return hits
    try:
        import UnityPy
    except Exception:
        return hits
    try:
        env = UnityPy.load(path)
    except Exception:
        return hits
    for name, d in bundle_textures(env):
        info = want.get(name)
        if not info:
            continue
        try:
            im = d.image
            if im is None:
                continue
            sub, tag = info
            if sub == 'photos':
                im = trim_and_resize(im, _W['ph'])
            elif max(im.size) > _W['av']:
                im = im.resize((_W['av'], _W['av']), 1)
            im.convert('RGBA').save(Path(_W['dirs'][sub]) / (tag + '.png'))
            hits.append(name)
        except Exception:
            continue
    return hits


# ══════════════════ 主流程 ══════════════════

def ensure(hero_ids, progress=None, force=False, budget_s=None, av_size=AV_SIZE, ph_size=PH_SIZE):
    """确保 userdata/assets 里备齐 hero_ids 的角色头像与立绘头像。

    progress(阶段文字, 已完成, 总数) —— 会在扫描过程中被调用（可能很密集，调用方自行节流）。
    budget_s —— 单次最多跑多少秒（到点就停，下次启动接着扫）；None=不限。
    返回统计 dict（ok / game / found / missing / scanned / seconds / done）。
    """
    t0 = time.time()
    st = _load_state()
    if force:
        st = {'watermark': 0.0, 'extracted': {}, 'scanned_at': 0, 'catalog': ''}
    cache = find_cache()
    installs = find_installs()
    game = bool(cache or installs)

    def say(text, i=0, n=0):
        if progress:
            try:
                progress(text, i, n)
            except Exception:
                pass

    if not game:
        say('没找到本机游戏，头像将用占位图', 0, 0)
        _save_state(st)
        return {'ok': False, 'game': False, 'reason': 'no_game', 'found': 0, 'missing': 0,
                'seconds': round(time.time() - t0, 1), 'done': True}

    catname, _keys = catalog_keys(cache)
    need = wanted(hero_ids, cache)
    if not need:
        say('游戏资源清单里没有可用项', 0, 0)
        return {'ok': False, 'game': True, 'reason': 'no_keys', 'found': 0, 'missing': 0,
                'seconds': round(time.time() - t0, 1), 'done': True}

    for d in (avatars_dir(), photos_dir()):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def out_of(sub, tag):
        return (avatars_dir() if sub == 'avatars' else photos_dir()) / (tag + '.png')

    todo = {k: v for k, v in need.items() if not out_of(v[0], v[1]).exists()}
    if not todo:
        st['scanned_at'] = time.time()
        st['catalog'] = catname
        _save_state(st)
        say('头像资源已就绪（%d 项）' % len(need), len(need), len(need))
        return {'ok': True, 'game': True, 'reason': 'cached', 'found': 0,
                'missing': len(need), 'total': len(need),
                'seconds': round(time.time() - t0, 1), 'done': True}

    try:
        import UnityPy
    except Exception:
        say('缺少 UnityPy，跳过资源提取', 0, 0)
        return {'ok': False, 'game': True, 'reason': 'no_unitypy', 'found': 0,
                'missing': len(todo), 'seconds': round(time.time() - t0, 1), 'done': True}

    files = bundle_files(cache, installs)
    wm = float(st.get('watermark') or 0)
    fresh = [(p, mt) for p, mt in files if mt > wm]
    n_total = len(fresh)
    say('准备提取头像资源…', 0, n_total)

    found, stopped, done_n = 0, False, 0

    def absorb(names):
        nonlocal found
        for k in names:
            if k in todo:
                del todo[k]
                found += 1

    dirs = {'avatars': str(avatars_dir()), 'photos': str(photos_dir())}
    # 父进程也初始化一份工作上下文：并行不可用时走的就是同一套代码
    _init_worker(dict(todo), dirs, av_size, ph_size)

    pool = None
    try:
        from concurrent.futures import ProcessPoolExecutor
        workers = max(1, min(WORKERS_MAX, (os.cpu_count() or 4) - 1))
        pool = ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                                   initargs=(dict(todo), dirs, av_size, ph_size))
        say('正在从本机游戏提取头像资源…（%d 进程并行，共 %d 个资源包）' % (workers, n_total), 0, n_total)
    except Exception:
        pool = None
        say('并行不可用，改用单进程扫描…', 0, n_total)

    for i in range(0, n_total, CHUNK):
        if not todo:
            break
        if budget_s and (time.time() - t0) > budget_s:
            stopped = True
            break
        chunk = fresh[i:i + CHUNK]
        if pool is not None:
            try:
                for hits in pool.map(_scan_one, [str(p) for p, _ in chunk]):
                    absorb(hits)
            except Exception:
                pool.shutdown(wait=False)
                pool = None
                say('并行扫描异常，改用单进程继续…', done_n, n_total)
                for p, _mt in chunk:
                    absorb(_scan_one(str(p)))
        else:
            for p, _mt in chunk:
                absorb(_scan_one(str(p)))
        done_n = min(i + len(chunk), n_total)
        st['watermark'] = max(wm, chunk[-1][1])          # 只把「整块扫完」的进度落盘
        wm = st['watermark']
        _save_state(st)
        say('正在从本机游戏提取头像资源… %d/%d（已找到 %d 张）'
            % (done_n, n_total, found), done_n, n_total)
    if pool is not None:
        try:
            pool.shutdown(wait=False)
        except Exception:
            pass
    _save_state(st)

    secs = round(time.time() - t0, 1)
    say('头像资源：新增 %d 张，剩余 %d 张待补' % (found, len(todo)), n_total, n_total)
    return {'ok': True, 'game': True, 'reason': 'scanned', 'found': found,
            'missing': len(todo), 'total': len(need), 'scanned': n_total,
            'seconds': secs, 'done': (not todo) and (not stopped),
            'stopped': stopped}


# ══════════════════ 诊断（打包后 --windowed 没有控制台，异常只能落盘） ══════════════════
def log(msg):
    """往 userdata/log.txt 追加一行。失败就算了，绝不抛（日志不能反过来搞挂程序）。"""
    try:
        d = paths.DATA_DIR
        d.mkdir(parents=True, exist_ok=True)
        with open(d / 'log.txt', 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), msg))
    except Exception:
        pass


def _probe(n):
    """多进程探针 —— 必须是模块级函数（Windows spawn 要能 import 到它）。"""
    return n * 2


def selftest():
    """逐项检查运行环境与提取能力，写进 userdata/log.txt，返回问题清单。

    用法（打包后排查，没有控制台）：
        dist\\星趴档案\\星趴档案.exe --selftest   →  看 dist\\星趴档案\\userdata\\log.txt
    """
    import sys
    import traceback
    log('=== selftest ===')
    log('frozen=%s  exe=%s' % (getattr(sys, 'frozen', False), sys.executable))
    log('EXE_DIR=%s' % paths.EXE_DIR)
    log('DATA_DIR=%s' % paths.DATA_DIR)
    bad = []
    for mod in ('numpy', 'PIL', 'lz4', 'etcpak', 'texture2ddecoder', 'UnityPy', 'astral.gameart'):
        try:
            m = __import__(mod, fromlist=['x'])
            log('  导入 %-18s OK   %s' % (mod, getattr(m, '__file__', '')))
        except Exception as e:
            bad.append('import:' + mod)
            log('  导入 %-18s 失败 %r' % (mod, e))
            log(traceback.format_exc())
    try:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=2) as ex:
            log('  多进程探针: %s' % list(ex.map(_probe, [1, 2])))
    except Exception:
        bad.append('multiprocessing')
        log('  多进程探针 失败\n' + traceback.format_exc())
    try:
        c = find_cache()
        ins = find_installs()
        files = bundle_files(c, ins)
        cat, keys = catalog_keys(c, (PREFIX_AV, PREFIX_PH))
        log('  缓存目录 = %s' % c)
        log('  安装目录 = %s' % (ins,))
        log('  资源包数 = %d' % len(files))
        log('  catalog  = %s   keys=%d' % (cat, len(keys)))
        ok, dec, dec_err = 0, None, None
        for f, _mt in files[:3]:
            try:
                import UnityPy
                env = UnityPy.load(str(f))
                n = sum(1 for o in env.objects if o.type.name in ('Texture2D', 'Sprite'))
                log('    试扫 %-26s → %d 个贴图对象' % (f.name[:26], n))
                ok += 1
                if dec is None:
                    # 只有真正取 image 才会解压贴图 —— 这一步才会用到
                    #   texture2ddecoder / etcpak / lz4，仅 import UnityPy 无法覆盖
                    for o in env.objects:
                        if o.type.name != 'Texture2D':
                            continue
                        try:
                            d = o.read()
                            im = d.image
                            dec = '%s %s' % (getattr(d, 'm_Name', ''), (im.size if im is not None else None))
                        except Exception as e:
                            dec_err = traceback.format_exc()
                            dec = None
                            log('    首图解码失败: %r' % e)
                        break
            except Exception:
                log('    试扫 %-26s 失败\n%s' % (f.name[:26], traceback.format_exc()))
        log('  试扫成功 %d/3 ｜ 首图解码 = %s' % (ok, dec if dec else '✗ 失败'))
        if dec_err:
            log(dec_err)
        if ok == 0:
            bad.append('scan')
        if dec_err:
            bad.append('decode')
    except Exception:
        bad.append('scan-env')
        log('  扫描环境 失败\n' + traceback.format_exc())
    log('=== selftest 结束 ｜ 问题: %s ===' % (bad or '无'))
    return bad
