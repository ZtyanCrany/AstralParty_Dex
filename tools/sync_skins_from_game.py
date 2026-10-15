# -*- coding: utf-8 -*-
"""从客户端文本表生成皮肤清单（只读 game_texts.json，不碰游戏文件）。

皮肤名取自 Wiki（`assets/data/skins.json`）时滞后于游戏更新，新皮肤只能显示占位名；
客户端的 `STRItem` 表按皮肤 ID 建键，随客户端更新：

    100117005 → 后厨能手-茉莉        100117006 → 钢羽森姬-茉莉
    100119004 → 特别礼物-琉华        100119005 → 仓栗骑士-琉华

名字尾缀「-角色」用于归属角色：联动批次共用一个号段（100301001 是超天酱的
初始皮、100301002 是糖糖的），仅按 ID 会归错。生成规则在 `astral/tablegen.py`。

前置：先跑 tools/fetch_game_texts.py 刷新 game_texts.json（它会自动挑最新的游戏目录）。

用法：
    python tools/sync_skins_from_game.py            # 只看差异，不写文件
    python tools/sync_skins_from_game.py --apply     # 写回 assets/data/skins_live.json
"""
import argparse
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from astral import tablegen            # noqa: E402

TEXTS = HERE / 'assets' / 'data' / 'game_texts.json'
WIKI = HERE / 'assets' / 'data' / 'skins.json'
OUT = HERE / 'assets' / 'data' / 'skins_live.json'


def load_texts():
    try:
        return json.load(io.open(TEXTS, encoding='utf-8'))
    except Exception as e:
        print('✗ 读不到 %s（先跑 tools/fetch_game_texts.py）：%s' % (TEXTS.name, e))
        sys.exit(2)


def load_wiki():
    try:
        return json.load(io.open(WIKI, encoding='utf-8'))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description='从客户端文本表生成皮肤清单（只读）')
    ap.add_argument('--apply', action='store_true',
                    help='写回 assets/data/skins_live.json（不加则只看差异）')
    args = ap.parse_args()

    texts = load_texts()
    payload, rep = tablegen.gen_skins(texts.get('tables') or {}, load_wiki())
    payload['source'] = {'tables': ['STRItem', 'STRCharacter'],
                         'game_texts': TEXTS.name,
                         'note': '由 tools/sync_skins_from_game.py 生成；名字取自客户端文本表'}
    chars = payload['chars']

    old = {}
    if OUT.exists():
        try:
            old = json.load(io.open(OUT, encoding='utf-8')).get('chars') or {}
        except Exception:
            old = {}
    added = []
    for cid, v in chars.items():
        for s, nm in v['slots'].items():
            was = (old.get(cid) or {}).get('slots', {}).get(s)
            if was != nm and was is not None:
                added.append(('改名', cid, s, was, nm))
            elif was is None and (not old or int(s) >= 3):
                added.append(('新增', cid, s, '', nm))

    print('角色 %d 个，命名皮肤 %d 套；共用号段归属 %d 条'
          % (len(chars), rep['named'], len(payload['item_slots'])))
    if payload['unresolved']:
        print('⚠ 归不到角色的条目 %d 条：%s' % (len(payload['unresolved']), payload['unresolved'][:5]))
    if added:
        print('与上一版比：')
        for kind, cid, s, was, nm in added[:40]:
            print('   %s 角色%s 槽%s  %s%s' % (kind, cid, s, (was + ' → ') if was else '', nm))
    if not args.apply:
        print('（未加 --apply：不写文件）')
        return 0
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    print('→ 已写出 %s' % OUT.relative_to(HERE))
    return 0


if __name__ == '__main__':
    sys.exit(main())
