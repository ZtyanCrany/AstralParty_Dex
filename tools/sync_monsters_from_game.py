# -*- coding: utf-8 -*-
"""用游戏本体的文案表生成怪物名字表 `assets/data/monsters.json`。

数据来源：`tools/fetch_game_texts.py` 导出的文案表 `STRMonster`。
键惯例：`<种类id>*10+0/1/2` → 名字；`*10+3` → 图鉴描述（<10000 的是角色台词，跳过）。

生成规则在 `astral/tablegen.py`（与运行时数据层共用）。

用法：
    python tools/sync_monsters_from_game.py            # 只报告
    python tools/sync_monsters_from_game.py --apply    # 写回 monsters.json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from astral import tablegen            # noqa: E402

TEXTS = HERE / 'assets' / 'data' / 'game_texts.json'
OUT = HERE / 'assets' / 'data' / 'monsters.json'


def main():
    ap = argparse.ArgumentParser(description='生成怪物名字表 monsters.json')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--texts', default=str(TEXTS))
    args = ap.parse_args()

    tp = Path(args.texts)
    if not tp.exists():
        print('✗ 缺 %s，先跑 tools/fetch_game_texts.py' % tp)
        return 2
    tables = json.loads(tp.read_text(encoding='utf-8')).get('tables', {})
    if not tables.get('STRMonster'):
        print('✗ 文案表里没有 STRMonster')
        return 2

    rows, rep = tablegen.gen_monsters(tables)
    print('STRMonster 共 %d 键 → 解出 %d 个怪物（%d 个有名字，%d 个有图鉴描述）'
          % (rep['keys'], rep['rows'], rep['named'], rep['described']))
    print('   跳过 <10000 的角色台词键 %d 个' % rep['chatter'])
    for k in list(rows)[:10]:
        print('   %-6s %-14s %s' % (k, rows[k]['name'] or '（无名）', rows[k]['desc'][:52]))

    if not args.apply:
        print('\n（只报告，未写回。加 --apply 生效）')
        return 0
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n✓ 已写出 %s（%d 个怪物）' % (OUT.relative_to(HERE), len(rows)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
