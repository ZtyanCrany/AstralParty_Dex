# -*- coding: utf-8 -*-
"""一键打包成 Windows 免安装版（onedir）。

用法:
    python tools/build_exe.py

产物:
    dist/星趴档案/星趴档案.exe  +  _internal/
    整个 dist/星趴档案 文件夹可以拷到任何地方运行（免管理员、免安装）
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CMD = [
    sys.executable, '-m', 'PyInstaller',
    '--noconfirm', '--clean',
    '--name', '星趴档案',
    '--windowed',                      # 不弹控制台
    '--icon', str(ROOT / 'assets' / 'app.ico'),
    # 只读资源：界面 + 数据表 + 协议描述符
    # 文案表（cards/chips/buffs/monsters…）随包分发：别人装了本软件就能直接看到名字与描述，
    #   不需要本机装着游戏；只有 tools/sync_*_from_game.py 这类「从游戏重取文案」的同步工具
    #   才需要本机装游戏（那是开发/维护时跑的事）。
    # 不打包 assets/avatars 与 assets/photos：
    #   美术改为运行时从「本机游戏资源包」提取（见 astral/gameart.py）：
    #     · 游戏以后出新角色/新皮肤，自动跟得上，不会像打进去的图那样过时；
    #     · 软件包本身不分发官方美术资源，版权上更干净。
    '--add-data', 'ui;ui',
    '--add-data', 'assets/data;assets/data',
    '--add-data', 'assets/proto;assets/proto',
    '--add-data', 'assets/app.ico;assets',
    '--add-data', 'assets/app.png;assets',
    # 从本机游戏资源包提取美术要用（UnityPy + 贴图解码，后者含二进制扩展）
    # 不能用 --collect-all UnityPy：它会把可选重依赖（torch / pandas / scipy…）一路拖进来，
    #   打包体积会从 43MB 涨到 GB 级。只收 UnityPy 自己的数据文件（typetree .dat）
    #   + 明确需要的子模块，并把重依赖显式排除兜底。
    '--collect-data', 'UnityPy',
    '--hidden-import', 'UnityPy',
    '--hidden-import', 'UnityPy.classes',
    '--hidden-import', 'UnityPy.files',
    '--hidden-import', 'UnityPy.streams',
    '--hidden-import', 'UnityPy.helpers',
    '--hidden-import', 'UnityPy.resources',
    '--hidden-import', 'UnityPy.export',
    '--hidden-import', 'texture2ddecoder',
    '--hidden-import', 'lz4',
    '--hidden-import', 'etcpak',
    # etcpak 导入时会 `from archspec.cpu import host`，archspec 需要自己的 json 数据文件
    #   （cpu/microarchitectures.json 等）。只收 .py 不收 json → 打包后 import etcpak 直接
    #   FileNotFoundError → 贴图全部解不出来（冻结环境解出 0 张，源码模式 165 张）
    '--collect-data', 'archspec',
    '--hidden-import', 'archspec',
    # UnityPy 的类注册表会连带 import 音频库 fmod_toolkit → pyfmodex，
    #   而 fmod_toolkit 自带一个 1.68MB 的 libfmod/Windows/x64/fmod.dll。
    #   不收这个 DLL：每次读贴图都会失败（3 个样本全部失败）
    '--collect-all', 'fmod_toolkit',
    '--collect-all', 'pyfmodex',
    # 解码相关的 C 扩展：用 collect-all 连数据/DLL 一起收（hidden-import 只保证模块在）
    '--collect-all', 'texture2ddecoder',
    '--collect-all', 'etcpak',
    '--collect-all', 'lz4',
    '--exclude-module', 'torch',
    '--exclude-module', 'pandas',
    '--exclude-module', 'scipy',
    '--exclude-module', 'matplotlib',
    '--exclude-module', 'sklearn',
    '--exclude-module', 'tensorflow',
    '--exclude-module', 'IPython',
    '--exclude-module', 'jupyter',
    # 会被 PyInstaller 连带打进包但本程序用不到的依赖（dist 里 pyarrow 单独约 79MB）
    '--exclude-module', 'pyarrow',
    '--exclude-module', 'cryptography',
    '--exclude-module', 'pydantic',
    '--exclude-module', 'rich',
    '--exclude-module', 'pygments',
    # pywebview 在 Windows 上走 WebView2，需要把这些带上
    '--collect-all', 'webview',
    '--collect-all', 'clr_loader',
    '--collect-all', 'pythonnet',
    '--hidden-import', 'webview.platforms.edgechromium',
    '--hidden-import', 'clr',
    str(ROOT / 'main.py'),
]


def main() -> int:
    print('打包目录:', ROOT)
    r = subprocess.run(CMD, cwd=str(ROOT))
    if r.returncode != 0:
        print('✗ 打包失败')
        return r.returncode
    out = ROOT / 'dist' / '星趴档案'
    # assets/data 是整目录打包的，但 game_texts.json 只是「同步工具用的原始汇总」：
    #   界面运行时只用各小表（cards/chips/buffs/monsters…），把它从产物里删掉，省 ~430KB，
    #   也不必让软件包带着整包游戏原文（要重生成跑 tools/fetch_game_texts.py）。
    for p in sorted(out.rglob('game_texts.json')):
        try:
            p.unlink()
            print('  · 已从产物移除 %s' % p.relative_to(out))
        except OSError as e:
            print('  · 移除失败（可手动删）%s: %s' % (p, e))
    print()
    print('✓ 打包完成:', out)
    print('  可执行文件:', out / '星趴档案.exe')
    print('  提示: 整个文件夹一起拷走才能运行；第一次启动会在旁边生成 userdata/ 保存登录态')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
