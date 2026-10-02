# -*- coding: utf-8 -*-
"""把 dist/星趴档案 打成免安装 zip（vX.Y.Z_win64）。

用法:
    python tools/make_release.py            # 版本号从 main.py 里的 VERSION 读
    python tools/make_release.py v1.2.0

安全红线：打包前必须确保 userdata/ 不在产物里 ——
  里面的 token.json 是「免密登录钥匙」，混进去等于把账号一起发出去。
  本脚本会主动删掉 dist/星趴档案/userdata 并在打完后**开包复核**（有条目就直接报错）。
"""
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'dist' / '星趴档案'
BANNED = ('userdata/', 'token.json', '_capture', '.hermes')
# 美术资源（角色头像 / 立绘头像）不进包：改为运行时从本机游戏资源包提取
#   （astral/gameart.py）—— 这样游戏出新角色/新皮肤才跟得上，且软件包不分发官方美术资源。
ART_BANNED = ('assets/avatars/', 'assets/photos/')
# 必须真的在包里的只读资源（防手滑改坏 --add-data）
NEEDED = ('ui/app.html', 'assets/data/', 'assets/proto/')


def read_version() -> str:
    s = (ROOT / 'main.py').read_text(encoding='utf-8')
    m = re.search(r"^VERSION\s*=\s*'([^']+)'", s, re.M)
    return m.group(1) if m else 'v0.0.0'


def main() -> int:
    ver = sys.argv[1] if len(sys.argv) > 1 else read_version()
    if not DIST.is_dir():
        print('✗ 没有 %s，先跑 python tools/build_exe.py' % DIST)
        return 1
    exe = DIST / '星趴档案.exe'
    if not exe.is_file():
        print('✗ 找不到 %s' % exe)
        return 1

    # 安全：先删掉产物里的 userdata/（手动在 dist 里跑过 exe 就会生成）
    ud = DIST / 'userdata'
    if ud.exists():
        shutil.rmtree(ud)
        print('  已删除产物里的 userdata/（含免密登录态，绝不能发出去）')

    out = ROOT / ('AstralParty_Dex_%s_win64.zip' % ver)
    if out.exists():
        out.unlink()
    n = 0
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(DIST.rglob('*')):
            if f.is_file():
                # 压缩包顶层多一层「星趴档案/」，解压出来是干净的文件夹
                z.write(f, str(Path('星趴档案') / f.relative_to(DIST)))
                n += 1
    print('  已打包: %s（%d 个文件，%.1f MB）' % (out.name, n, out.stat().st_size / 1048576))

    # 开包复核（重新读取产物内容做校验）
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    bad = [x for x in names if any(b in x for b in BANNED)]
    art = [x for x in names if any(b in x for b in ART_BANNED)]
    lack = [x for x in NEEDED if not any(x in n for n in names)]
    has_exe = any(x.endswith('星趴档案/星趴档案.exe') for x in names)
    has_internal = any('/_internal/' in x for x in names)
    print()
    print('  === 开包复核 ===')
    print('   条目数            : %d' % len(names))
    print('   含 星趴档案.exe    : %s' % ('✓' if has_exe else '✗'))
    print('   含 _internal/      : %s' % ('✓' if has_internal else '✗'))
    print('   含 userdata/token  : %s' % ('✓ 干净' if not bad else '✗ %s' % bad[:3]))
    print('   美术资源进包了吗    : %s' % ('✗ %s' % art[:2] if art else '✓ 没进（运行时从本机游戏提取）'))
    print('   必要资源 ui/data/proto: %s' % ('✓ 齐' if not lack else '✗ 缺 %s' % lack))
    if not (has_exe and has_internal and not bad and not art and not lack):
        print('\n✗ 复核不通过，别发这个包')
        return 1
    print('\n✓ 复核通过: %s' % out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
