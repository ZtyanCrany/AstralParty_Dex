# -*- coding: utf-8 -*-
"""用游戏本体的文案表刷新仓库内的卡牌表（默认只报告，加 --apply 才写回）。

数据来源：`tools/fetch_game_texts.py` 导出的 `assets/data/game_texts.json`
  STRCard 的键 = 卡号 → 牌名
  STRCard 的键 = 卡号 * 10 + 序号 → 效果文字

生成规则在 `astral/tablegen.py`（与运行时数据层共用）。

用法：
    python tools/sync_cards_from_game.py            # 只报告差异
    python tools/sync_cards_from_game.py --apply    # 写回 cards.json
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from astral import tablegen            # noqa: E402

TEXTS = HERE / 'assets' / 'data' / 'game_texts.json'
CARDS = HERE / 'assets' / 'data' / 'cards.json'


def main():
    ap = argparse.ArgumentParser(description='用游戏文案表刷新卡牌表')
    ap.add_argument('--apply', action='store_true', help='写回 assets/data/cards.json')
    ap.add_argument('--texts', default=str(TEXTS))
    ap.add_argument('--cards', default=str(CARDS))
    args = ap.parse_args()

    tp, cp = Path(args.texts), Path(args.cards)
    if not tp.exists():
        print('✗ 缺 %s，先跑 tools/fetch_game_texts.py' % tp)
        return 2
    tables = json.loads(tp.read_text(encoding='utf-8')).get('tables', {})
    if not tables.get('STRCard'):
        print('✗ 文案表里没有 STRCard')
        return 2

    base = json.loads(cp.read_text(encoding='utf-8')) if cp.exists() else []
    cards, rep = tablegen.gen_cards(tables, base)
    renamed, retexted, added, extra = rep['renamed'], rep['retexted'], rep['added'], rep['extra']

    print('游戏 STRCard：名字 %d 张 / 有描述的 %d 张' % (rep['names'], rep['descs']))
    print('仓库 cards.json：%d 张\n' % len(base))

    print('① 牌名不一致（→ 用游戏值）：%d 处' % len(renamed))
    for cid, old, new in renamed[:20]:
        print('   %-6d %s → %s' % (cid, old, new))
    print('\n② 效果文字不一致（→ 用游戏值）：%d 处' % len(retexted))
    for cid, nm, old, new in retexted[:20]:
        print('   %-6d %s\n        表: %s\n        游戏: %s' % (cid, nm, old[:60], new[:60]))
    print('\n③ 游戏有、仓库没有（→ 新增）：%d 张' % len(added))
    for cid, nm, ds in added:
        print('   %-6d %-8s %s' % (cid, nm, ds[:50]))
    print('\n④ 仓库有、游戏没有（→ 保留不动，请人工确认）：%d 张' % len(extra))
    for cid, nm in extra:
        print('   %-6d %s' % (cid, nm))

    if not args.apply:
        print('\n（只报告，未写回。加 --apply 生效）')
        return 0
    cp.write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n✓ 已写回 %s（%d 张）' % (cp.relative_to(HERE), len(cards)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
