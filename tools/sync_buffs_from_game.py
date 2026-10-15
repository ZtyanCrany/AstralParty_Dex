# -*- coding: utf-8 -*-
"""用游戏本体的文案表生成增益（状态）表 `assets/data/buffs.json`。

数据来源：`tools/fetch_game_texts.py` 导出的文案表 `STRBuff`。
键惯例：`<buff_id>` → 效果文字；`<buff_id> * 10 + 1` → 名字。

生成规则在 `astral/tablegen.py`（与运行时数据层共用）。

用法：
    python tools/sync_buffs_from_game.py            # 只报告
    python tools/sync_buffs_from_game.py --apply    # 写回 buffs.json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from astral import tablegen            # noqa: E402

TEXTS = HERE / 'assets' / 'data' / 'game_texts.json'
OUT = HERE / 'assets' / 'data' / 'buffs.json'


def main():
    ap = argparse.ArgumentParser(description='生成增益表 buffs.json')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--texts', default=str(TEXTS))
    args = ap.parse_args()

    tp = Path(args.texts)
    if not tp.exists():
        print('✗ 缺 %s，先跑 tools/fetch_game_texts.py' % tp)
        return 2
    tables = json.loads(tp.read_text(encoding='utf-8')).get('tables', {})
    if not tables.get('STRBuff'):
        print('✗ 文案表里没有 STRBuff')
        return 2

    rows, rep = tablegen.gen_buffs(tables)
    print('STRBuff 共 %d 键 → 解出 %d 条（名字键 %d 个已并入）'
          % (rep['keys'], rep['rows'], rep['name_keys']))
    print('   其中筹码增益（号段 500<筹码ID>01）%d 条' % rep['chip_buffs'])
    print('   只有效果无名字 %d 条 / 只有名字无效果 %d 条（短文本无标点按名字判定）'
          % (rep['only_desc'], rep['only_name']))
    for k in list(rows)[:8]:
        print('   %-9s %-10s %s' % (k, rows[k]['name'] or '（无名）', rows[k]['desc'][:60]))

    if not args.apply:
        print('\n（只报告，未写回。加 --apply 生效）')
        return 0
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n✓ 已写出 %s（%d 条）' % (OUT.relative_to(HERE), len(rows)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
