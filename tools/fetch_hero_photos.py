# -*- coding: utf-8 -*-
"""从游戏热更新缓存里提取角色「立绘头像」UT_Hero_ProfilePhoto_* → assets/photos/

背景：
  《星引擎 Party》用 Unity Addressables，新资源不在 Steam 目录，而在
      %USERPROFILE%\\AppData\\LocalLow\\feimo\\AstralParty_CN\\com.unity.addressables\\AssetBundles\\
       <哈希>\\<哈希>\\__data        ← 真正的资源包（1161 个 · 5.40GB）
  资源名清单在 catalog_<版本>.json 的 m_KeyDataString（base64）里。

命名规律（catalog 3.2.0，共 194 个 key / 35 位角色）：
  UT_Hero_ProfilePhoto_<角色3位>            默认形象
  UT_Hero_ProfilePhoto_<角色3位>_<皮肤2位>   该角色的皮肤（01、02…）
  UT_Hero_ProfilePhoto_<角色3位>_Max         Max 形态
  另有 UT_Hero_ProfilePhoto_<4位数> 64 个（非角色，暂不处理）

与回放的对应（11 局 · 27 个样本）：
  model.Hero#standingPainting = "100" + <角色3位> + <皮肤序号3位>
  例：hero_id=123 & standingPainting=100123001 → 角色 123 的皮肤 01
  皮肤序号只出现 001~006，从不出现 000。

用法：
    python tools/fetch_hero_photos.py                 # 全部（约 1~2 分钟）
    python tools/fetch_hero_photos.py 123 123_01      # 只提取指定 key
    python tools/fetch_hero_photos.py --size 160      # 指定输出边长
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import UnityPy
from PIL import Image

PREFIX = 'UT_Hero_ProfilePhoto_'
ROOT = Path(__file__).resolve().parent.parent          # 项目根
sys.path.insert(0, str(ROOT))                          # 让 astral.* 能导入（裁切逻辑与运行时共用一份）
from astral import gameart                              # 安装目录探测与资源包遍历共用同一份实现
DEFAULT_OUT = ROOT / 'assets' / 'photos'
CACHE_TAIL = Path('AppData/LocalLow/feimo/AstralParty_CN/com.unity.addressables')


def find_cache():
    """定位游戏热更新缓存目录。"""
    cand = []
    up = os.environ.get('USERPROFILE')
    if up:
        cand.append(Path(up) / CACHE_TAIL)
    la = os.environ.get('LOCALAPPDATA')
    if la:
        cand.append(Path(la).parent / 'LocalLow/feimo/AstralParty_CN/com.unity.addressables')
    for c in cand:
        if c.is_dir():
            return c
    raise SystemExit('✗ 找不到游戏热更新缓存：%s' % cand[0])


def catalog_keys(cache):
    """从最新 catalog 的 m_KeyDataString 里取出全部 UT_Hero_ProfilePhoto_* key。"""
    cats = sorted(cache.glob('catalog_*.json'))
    if not cats:
        raise SystemExit('✗ 缓存里没有 catalog_*.json')
    cat = cats[-1]                                     # 文件名有序，取最新
    data = json.loads(cat.read_text(encoding='utf-8-sig'))
    raw = base64.b64decode(data['m_KeyDataString'])
    keys = sorted({r.decode('ascii') for r in re.findall(rb'[\x20-\x7e]{6,}', raw)
                   if r.startswith(PREFIX.encode())})
    return cat.name, keys


def bundle_files(cache, install=None):
    """收集全部资源包：热更新缓存（__data）+ 安装目录基础包（*.bundle），按 mtime 从新到旧。"""
    out = []
    for p in (cache / 'AssetBundles').rglob('__data'):
        try:
            out.append((p, p.stat().st_mtime))
        except OSError:
            pass
    for d in gameart.find_installs(install):
        for p in d.glob('*.bundle'):
            try:
                out.append((p, p.stat().st_mtime))
            except OSError:
                pass
    out.sort(key=lambda x: -x[1])
    return [p for p, _ in out]


def trim_and_resize(im, size, margin=0.12):
    """找内容边界 → 以内容为中心的**正方形**裁切（外扩 margin）→ 缩放到 size×size。

    实现放在 astral/gameart.py，这里**转调** —— 运行时提取（程序启动时后台扫）
      与本离线工具必须用同一份裁切逻辑，避免两份实现各自演化后不一致。
    """
    from astral.gameart import trim_and_resize as _impl
    return _impl(im, size, margin)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('only', nargs='*', help='只提取这些 key（不含前缀），空=全部')
    ap.add_argument('--size', type=int, default=160, help='输出最长边（默认 160）')
    ap.add_argument('--no-crop', action='store_true', help='不裁四周空白')
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    ap.add_argument('--install', default=None, help='Steam 库根目录（默认自动从 libraryfolders.vdf 找）')
    args = ap.parse_args()

    cache = find_cache()
    catname, keys = catalog_keys(cache)
    if args.only:
        want = {PREFIX + k for k in args.only}
        keys = [k for k in keys if k in want]
        if not keys:
            raise SystemExit('✗ 指定的 key 不在 catalog 里：%s' % args.only)
    files = bundle_files(cache, args.install)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    print('热更新缓存: %s' % cache)
    print('安装基础包: %s' % (', '.join(str(d) for d in gameart.find_installs(args.install)) or '（没找到）'))
    print('资源清单  : %s（%d 个 %s* key）' % (catname, len(keys), PREFIX))
    print('资源包    : %d 个' % len(files))
    print('输出      : %s（最长边 %dpx）' % (outdir, args.size))
    print()

    todo = set(keys)
    found = {}
    t0 = time.time()
    for i, f in enumerate(files, 1):
        if not todo:
            break
        try:
            env = UnityPy.load(str(f))
        except Exception:
            continue
        for name, d in gameart.bundle_textures(env):
            if name not in todo:
                continue
            try:
                im = d.image
                if im is None:
                    continue
                if not args.no_crop:
                    im = trim_and_resize(im, args.size)
                elif max(im.size) > args.size:
                    im = im.resize((args.size, args.size), Image.LANCZOS)
                tag = name[len(PREFIX):]
                p = outdir / (tag + '.png')
                im.convert('RGBA').save(p)
                found[name] = (p.name, im.size, p.stat().st_size)
                todo.discard(name)
            except Exception as e:
                print('   ! %s 导出失败: %s' % (name, e))
        if i % 120 == 0:
            print('  …已扫 %d/%d 包  %.0fs  已导出 %d/%d' % (i, len(files), time.time() - t0,
                                                          len(found), len(keys)), flush=True)

    print()
    print('完成：%d/%d 个 → %s  用时 %.0fs' % (len(found), len(keys), outdir, time.time() - t0))
    for name in sorted(found):
        fn, size, nbytes = found[name]
        print('   %-34s → %-14s %sx%s  %.1f KB' % (name, fn, size[0], size[1], nbytes / 1024))
    miss = sorted(todo)
    if miss:
        print()
        print('!! 未找到 %d 个：' % len(miss))
        for k in miss[:20]:
            print('   ', k)
    return 0


if __name__ == '__main__':
    sys.exit(main())
