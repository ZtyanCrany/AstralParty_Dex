# -*- coding: utf-8 -*-
"""从游戏热更新缓存里提取角色头像 → assets/avatars/<角色ID>.png

背景说明：
  《星引擎 Party》用的是 Unity Addressables，**新资源不在 Steam 安装目录里**，
  而是热更新到用户目录：
      %USERPROFILE%\\AppData\\LocalLow\\feimo\\AstralParty_CN\\com.unity.addressables\\
          AssetBundles\\<hash>\\<hash>\\__data     ← 每个包就是一个 __data 文件
  安装目录（Steam\\steamapps\\common\\Astral Party）只包含基础包版本，
  比它更晚出的角色（如 129 赛克斯 / 305 远野汉娜 / 306 橘雪莉）只存在于热更新缓存里。

用法：
    python tools/fetch_game_avatars.py            # 扫描并导出全部 UT_Platform_<数字>
    python tools/fetch_game_avatars.py 129 305 306  # 只找指定的几个

依赖：UnityPy（pip install UnityPy）
"""
import os
import sys
import time
from pathlib import Path

CACHE = (Path(os.environ.get('USERPROFILE', ''))
         / 'AppData' / 'LocalLow' / 'feimo' / 'AstralParty_CN'
         / 'com.unity.addressables' / 'AssetBundles')
OUT = Path(__file__).resolve().parent.parent / 'assets' / 'avatars'


def bundles():
    """递归找出所有包文件（<hash>\\<hash>\\__data），按修改时间从新到旧"""
    out = []
    for root, _dirs, files in os.walk(CACHE):
        for fn in files:
            if fn == '__info':
                continue
            p = Path(root) / fn
            try:
                out.append((p, p.stat().st_mtime, p.stat().st_size))
            except OSError:
                pass
    out.sort(key=lambda x: -x[1])
    return out


def main():
    import UnityPy
    want = set()
    for a in sys.argv[1:]:
        want.add('UT_Platform_' + a if a.isdigit() else a)
    if not want:
        want = None            # 全部
    if not CACHE.exists():
        print('✗ 没找到热更新缓存目录：%s' % CACHE)
        return 1
    bs = bundles()
    print('热更新缓存: %s' % CACHE)
    print('包文件 %d 个 · %.2f GB · 最新的 %s'
          % (len(bs), sum(b[2] for b in bs) / 1073741824,
             time.strftime('%Y-%m-%d %H:%M', time.localtime(bs[0][1])) if bs else '—'))
    OUT.mkdir(parents=True, exist_ok=True)
    found = {}
    t0 = time.time()
    for i, (p, _mt, _sz) in enumerate(bs):
        try:
            env = UnityPy.load(str(p))
            for o in env.objects:
                if o.type.name not in ('Texture2D', 'Sprite'):
                    continue
                try:
                    d = o.read()
                except Exception:
                    continue
                nm = getattr(d, 'm_Name', '') or ''
                if not nm.startswith('UT_Platform_'):
                    continue
                if want is not None and nm not in want:
                    continue
                key = nm.replace('UT_Platform_', '')
                if key in found:
                    continue
                img = getattr(d, 'image', None)          # Texture2D 直接给 PIL Image
                if img is None and hasattr(d, 'm_RD'):   # Sprite → 取其纹理
                    try:
                        img = d.m_RD.texture.image
                    except Exception:
                        img = None
                if img is None:
                    continue
                dst = OUT / (key + '.png')
                img.save(dst)
                found[key] = (dst.name, img.size, os.path.getsize(dst))
                print('  ✓ %-22s → %s  %sx%s  %.1f KB'
                      % (nm, dst.name, img.size[0], img.size[1], os.path.getsize(dst) / 1024),
                      flush=True)
        except Exception:
            pass
        if (i + 1) % 60 == 0:
            print('  …已扫 %d/%d 包  %.0fs  已找到 %d 个' % (i + 1, len(bs), time.time() - t0, len(found)), flush=True)
        if want is not None and len(found) >= len(want):
            print('  目标已全部找到，提前结束')
            break
    print('\n完成：%d 个 → %s' % (len(found), OUT))
    for k in sorted(found):
        print('   %s' % k, found[k])
    return 0


if __name__ == '__main__':
    sys.exit(main())
