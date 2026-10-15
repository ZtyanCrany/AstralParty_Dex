# -*- coding: utf-8 -*-
"""用游戏本体的文案表补充地图表（默认只报告，加 --apply 才写回）。

数据来源：`tools/fetch_game_texts.py` 导出的文案表 `STRMap`。
键为 `8xxxx`（5 位）时是地图名；`<地图id>0` / `<地图id>1` 是同图的短提示与长描述；
个位数键是难度与说明，不参与。

只补新地图，不动既有条目（任务目标那部分客户端文案表里没有，见 `astral/tablegen.py`）。

生成规则在 `astral/tablegen.py`（与运行时数据层共用）。

用法：
    python tools/sync_missions_from_game.py            # 只报告
    python tools/sync_missions_from_game.py --apply    # 写回 missions.json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from astral import tablegen            # noqa: E402

TEXTS = HERE / 'assets' / 'data' / 'game_texts.json'
OUT = HERE / 'assets' / 'data' / 'missions.json'


def main():
    ap = argparse.ArgumentParser(description='按客户端地图表补充 missions.json')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--texts', default=str(TEXTS))
    args = ap.parse_args()

    tp = Path(args.texts)
    if not tp.exists():
        print('✗ 缺 %s，先跑 tools/fetch_game_texts.py' % tp)
        return 2
    tables = json.loads(tp.read_text(encoding='utf-8')).get('tables', {})
    if not tables.get('STRMap'):
        print('✗ 文案表里没有 STRMap')
        return 2
    base = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}

    rows, rep = tablegen.gen_missions(tables, base)
    print('STRMap 共 %d 键 → 地图条目 %d 个；仓库原有 %d 个' % (rep['keys'], rep['maps'], len(base)))
    if rep['added']:
        print('\n① 客户端有、仓库没有（→ 新增 %d 个）：' % len(rep['added']))
        for k, v in rep['added'].items():
            print('   %-6s %s' % (k, v['name']))
    else:
        print('\n① 客户端有、仓库没有：无')
    if rep['skipped']:
        print('\n② 跳过（占位名 / ID 或名字已占用）：%s' % '、'.join(rep['skipped']))
    print('\n合计 %d 张地图' % rep['total'])

    if not args.apply:
        print('\n（只报告，未写回。加 --apply 生效）')
        return 0
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n✓ 已写出 %s（%d 张地图）' % (OUT.relative_to(HERE), len(rows)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
