# -*- coding: utf-8 -*-
"""按游戏本体刷新「角色潜能是否已开放」（默认只报告，加 --apply 才写回）。

判据：客户端 `Character` 资产里，已开放潜能的角色比未开放的多一个 22 号字段（潜能配置）。
该判据与 Wiki 抓来的角色逐一对上，只差 Wiki 滞后的角色（星魅琉华在 3.3.0 开放），
所以按它刷新比 Wiki 抓取准。

只读游戏文件，不动游戏目录。

生成规则在 `astral/tablegen.py`（与运行时数据层共用；运行时还会另写一份
`userdata/game_live/potential_live.json`，主程序优先读它）。

用法：
    python tools/sync_potential_from_game.py            # 只报告
    python tools/sync_potential_from_game.py --apply    # 写回 assets/data/potential.json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from astral import game_live, tablegen        # noqa: E402

OUT = HERE / 'assets' / 'data' / 'potential.json'


def main():
    ap = argparse.ArgumentParser(description='按客户端角色配置刷新潜能开放情况')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--root', help='游戏数据目录（默认自动挑内容最新的那个）')
    args = ap.parse_args()

    if args.root:
        root = Path(args.root)
    else:
        root, _ = game_live.newest_root()
        if root is None:
            root = game_live.game_dirs()[0]
    print('游戏数据目录：%s' % root)
    if not root.exists():
        print('✗ 目录不存在')
        return 2

    bundles, tables, raws = game_live.load_game_tables(root, raw_names=('Character',))
    if not bundles:
        print('✗ 没找到配置包（游戏未下载过配置？）')
        return 2
    if 'Character' not in raws:
        print('✗ 配置包里没有 Character 资产，无法判断潜能开放情况')
        return 2

    base = {}
    if OUT.exists():
        base = json.loads(OUT.read_text(encoding='utf-8'))
    out, rep = tablegen.gen_potential(raws['Character'], tables, base,
                                     json.loads((HERE / 'assets' / 'data' / 'skins.json')
                                                .read_text(encoding='utf-8')))
    notes = rep.get('changes') or []
    opened = sorted(k for k, v in out.items() if v.get('released'))
    closed = sorted(k for k, v in out.items() if not v.get('released'))
    print('角色 %d 个（已开放 %d / 未开放 %d）' % (len(out), len(opened), len(closed)))
    print('变化 %d 处：%s' % (len(notes), '、'.join(notes) or '无'))
    if not notes:
        print('（与现有表一致，无需写回）')
        return 0
    print('已开放：%s' % '、'.join(opened))
    print('未开放：%s' % '、'.join(closed))
    if not args.apply:
        print('\n（只是报告；要写入加 --apply）')
        return 0
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n已写入 %s' % OUT)
    return 0


if __name__ == '__main__':
    sys.exit(main())
