# -*- coding: utf-8 -*-
"""用游戏本体的文案表生成怪物名字表 assets/data/monsters.json。

数据来源：tools/fetch_game_texts.py 导出的 assets/data/game_texts.json 的 STRMonster 表。

STRMonster 的键惯例（与其它表同一套「×10」）：
    <种类id> * 10 + 0/1/2 → 该怪物的名字（不同难度/变体可能各有一条，取第一条非空）
    <种类id> * 10 + 3     → 该怪物的图鉴描述
例：1022 → 10220「果冻巫师」 / 10223「别看他走路晃悠悠的，吃它一发魔法可不是闹着玩的。」

★ 种类 id 从哪来：回放里 `monster.hero.hero_id`（例 1022/1023/1067）。
★ 表里 < 10000 的键是**角色台词**（如 1012），不是怪物，跳过。

用法：
    python tools/sync_monsters_from_game.py            # 只报告
    python tools/sync_monsters_from_game.py --apply    # 写回 monsters.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TEXTS = HERE / 'assets' / 'data' / 'game_texts.json'
OUT = HERE / 'assets' / 'data' / 'monsters.json'

PH = re.compile(r'\{[A-Za-z_0-9]+=([^}]*)\}')
COLOR = re.compile(r'\[/?(?:color|b|i|size|u)(?:=[^\]]*)?\]', re.I)
IMG = re.compile(r"<img[^>]*>", re.I)
TAG = re.compile(r'<[^>]+>')
ASTRAL = re.compile(r'\[/?(?:Astral)=[^\]]*\]', re.I)


def clean(tpl):
    s = PH.sub(lambda m: m.group(1), str(tpl))
    s = IMG.sub('', s)
    s = ASTRAL.sub('', s)
    s = COLOR.sub('', s)
    s = TAG.sub('', s)
    s = s.replace('\u3000', ' ')
    return re.sub(r'[ \t]{2,}', ' ', s).strip()


def main():
    ap = argparse.ArgumentParser(description='生成怪物名字表 monsters.json')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--texts', default=str(TEXTS))
    args = ap.parse_args()

    tp = Path(args.texts)
    if not tp.exists():
        print('✗ 缺 %s，先跑 tools/fetch_game_texts.py' % tp)
        return 2
    sm = {int(k): v for k, v in json.loads(tp.read_text(encoding='utf-8'))['tables'].get('STRMonster', {}).items()}
    if not sm:
        print('✗ 文案表里没有 STRMonster')
        return 2

    rows = {}
    for k, txt in sm.items():
        if k < 10000:               # 角色台词，不是怪物
            continue
        base, tail = k // 10, k % 10
        if base < 1000:
            continue
        r = rows.setdefault(base, {'name': '', 'desc': ''})
        body = clean(txt)
        if not body:
            continue
        if tail == 3:
            if not r['desc']:
                r['desc'] = body
        elif not r['name']:
            r['name'] = body
    rows = {k: v for k, v in rows.items() if v['name'] or v['desc']}
    named = sum(1 for v in rows.values() if v['name'])
    print('STRMonster 共 %d 键 → 解出 %d 个怪物（%d 个有名字，%d 个有图鉴描述）'
          % (len(sm), len(rows), named, sum(1 for v in rows.values() if v['desc'])))
    print('   跳过 <10000 的角色台词键 %d 个' % sum(1 for k in sm if k < 10000))
    for k in sorted(rows)[:10]:
        print('   %-6d %-14s %s' % (k, rows[k]['name'] or '（无名）', rows[k]['desc'][:52]))

    if not args.apply:
        print('\n（只报告，未写回。加 --apply 生效）')
        return 0
    OUT.write_text(json.dumps({str(k): rows[k] for k in sorted(rows)}, ensure_ascii=False, indent=1),
                   encoding='utf-8')
    print('\n✓ 已写出 %s（%d 个怪物）' % (OUT.relative_to(HERE), len(rows)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
