# -*- coding: utf-8 -*-
"""用游戏本体的文案表刷新仓库内的筹码表（默认只报告，加 --apply 才写回）。

数据来源：`tools/fetch_game_texts.py` 导出的 `assets/data/game_texts.json`
  STRRelic[50000 + n]      → 筹码名
  STRRelic[(50000 + n)*10] → 筹码效果文字

生成规则在 `astral/tablegen.py`（与运行时数据层共用）。

用法：
    python tools/sync_chips_from_game.py            # 只报告
    python tools/sync_chips_from_game.py --apply    # 写回 chips.json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from astral import tablegen            # noqa: E402

TEXTS = HERE / 'assets' / 'data' / 'game_texts.json'
CHIPS = HERE / 'assets' / 'data' / 'chips.json'


def main():
    ap = argparse.ArgumentParser(description='用游戏文案表刷新筹码表')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--texts', default=str(TEXTS))
    ap.add_argument('--chips', default=str(CHIPS))
    args = ap.parse_args()

    tp, cp = Path(args.texts), Path(args.chips)
    if not tp.exists():
        print('✗ 缺 %s，先跑 tools/fetch_game_texts.py' % tp)
        return 2
    tables = json.loads(tp.read_text(encoding='utf-8')).get('tables', {})
    if not tables.get('STRRelic'):
        print('✗ 文案表里没有 STRRelic')
        return 2

    base = json.loads(cp.read_text(encoding='utf-8')) if cp.exists() else []
    chips, rep = tablegen.gen_chips(tables, base)
    renamed, retexted, added, extra = rep['renamed'], rep['retexted'], rep['added'], rep['extra']

    print('游戏 STRRelic：筹码名 %d 个 / 有描述 %d 个'
          % (rep['names'], sum(1 for k in rep['descs'])))
    print('仓库 chips.json：%d 个\n' % len(base))

    print('① 名字不一致（→ 用游戏值）：%d 处' % len(renamed))
    for cid, old, new in renamed:
        print('   %-7d %s → %s' % (cid, old, new))
    print('\n② 效果文字新增/更正（→ 用游戏值）：%d 处' % len(retexted))
    for cid, nm, old, new in retexted[:12]:
        print('   %-7d %-14s %s' % (cid, nm, new[:70]))
    if len(retexted) > 12:
        print('   …（其余 %d 处同理）' % (len(retexted) - 12))
    print('\n③ 游戏有、仓库没有（→ 新增）：%d 个' % len(added))
    for cid, nm, ds, sib in added:
        src = ('继承自 %s' % sib['name']) if sib else '无同族，字段留空'
        print('   %-7d %-16s %s   [%s]' % (cid, nm, ds[:48], src))
    print('\n④ 仓库有、游戏没有（→ 保留不动）：%d 个' % len(extra))
    for cid, nm in extra:
        print('   %-7d %s' % (cid, nm))

    if not args.apply:
        print('\n（只报告，未写回。加 --apply 生效）')
        return 0
    cp.write_text(json.dumps(chips, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n✓ 已写回 %s（%d 个）' % (cp.relative_to(HERE), len(chips)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
