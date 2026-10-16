# -*- coding: utf-8 -*-
"""从游戏本体导入本地化文本表（只读，不动游戏文件）。

游戏把配置和文案打包成 TextAsset 放在 Addressables 里，本脚本用 UnityPy（只读载入）
把它们整表导出成 JSON，供后续与仓库内的数据表比对 / 刷新。

找目录、找配置包、解析记录都在 `astral/game_live.py` 中（运行时数据层用同一份实现）。

用法：
    python tools/fetch_game_texts.py                    # 全量导出
    python tools/fetch_game_texts.py --table STRCard    # 只导一张表
    python tools/fetch_game_texts.py --list             # 只列出表名与条数
    python tools/fetch_game_texts.py --check            # 只跑校验锚点
    python tools/fetch_game_texts.py --root <游戏数据目录>

输出：assets/data/game_texts.json
      {"source": {...}, "tables": {"STRCard": {"键": "文本", ...}, ...}}

记录格式（STR* 表一致）：
    0x0A <记录长 varint> 0x0D <键 小端4字节> 0x12 <串长 varint> <UTF-8 文本>
卡牌描述表的键 = 卡号 * 10 + 序号（例：20011 的描述键是 200111）。
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from astral import game_live, paths        # noqa: E402

OUT = HERE / 'assets' / 'data' / 'game_texts.json'

# 校验锚点：键 -> 期望文本（这些是人工核对过的权威值）
ANCHORS = {
    'STRCard': {'21012': '对怪激光', '21013': '对怪板砖', '20013': '板砖',
                '20011': '方向抉择', '200111': '自由选择移动方向'},
}


def check(tables):
    ok = True
    for tname, want in ANCHORS.items():
        got = tables.get(tname)
        if got is None:
            print('⚠️ 缺表 %s' % tname)
            ok = False
            continue
        for k, v in want.items():
            actual = got.get(k)
            flag = '✓' if actual == v else '✗'
            if actual != v:
                ok = False
            print('   %s %s[%s] = %r (期望 %r)' % (flag, tname, k, actual, v))
    return ok


def main():
    ap = argparse.ArgumentParser(description='从游戏本体导出本地化文本表（只读）')
    ap.add_argument('--table', help='只导出指定表名（如 STRCard）')
    ap.add_argument('--list', action='store_true', help='只列出表名与条数')
    ap.add_argument('--check', action='store_true', help='只跑校验锚点')
    ap.add_argument('--root', help='游戏数据目录（默认自动挑内容最新的那个）')
    args = ap.parse_args()

    if args.root:
        root = Path(args.root)
    else:
        root, ver = game_live.newest_root()
        if root is None:
            root = game_live.game_dirs()[0]
    print('游戏数据目录：%s' % root)
    if not root.exists():
        print('✗ 目录不存在')
        return 2

    print('定位配置包…')
    if args.table:
        bundles = game_live.find_cfg_bundles(root)
        tables = game_live.load_tables(bundles, only=args.table) if bundles else {}
    else:
        bundles, tables, _ = game_live.load_game_tables(root, log=lambda m: print(m, end=''))
    if not bundles:
        print('✗ 没找到配置包（游戏未下载过配置？）')
        return 2
    for b in bundles:
        print('   %s  (%.1f MB)' % (b.relative_to(root), b.stat().st_size / 1024 / 1024))

    if not args.table:
        print('解析 TextAsset…')
    print('   表数 %d，条目合计 %d' % (len(tables), sum(len(v) for v in tables.values())))

    if args.list:
        for name in sorted(tables, key=lambda t: -len(tables[t])):
            print('   %-28s %6d 条' % (name, len(tables[name])))
        return 0

    if args.check:
        return 0 if check(tables) else 1

    print('校验锚点：')
    ok = check(tables)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'source': {
            'root': str(root),
            'bundles': [str(b) for b in bundles],
            'marker': game_live.MARKER.decode('ascii'),
        },
        'tables': tables,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    print('→ 已写出 %s（%d 张表）' % (OUT.relative_to(HERE), len(tables)))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
