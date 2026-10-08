# -*- coding: utf-8 -*-
"""用游戏本体的文案表刷新仓库内的卡牌表（默认只报告，加 --apply 才写回）。

数据来源：tools/fetch_game_texts.py 导出的 assets/data/game_texts.json
  STRCard 的键 = 卡号 → 牌名
  STRCard 的键 = 卡号 * 10 + 序号 → 效果文字（模板形式，形如「指定{range=10}格…造成{damage=2}点伤害」）

模板里的占位符自带取值，去掉 {name=} 外壳即为游戏内显示文本。

字段策略：
  名称 / 效果文字 —— 一律以游戏为准（权威）
  类型 / 对象 / 模式 / 来源 —— 表里已有的保留；新卡按号段推断模式（20xxx=PVP、21xxx=PVE、1xxxx=通用）

用法：
    python tools/sync_cards_from_game.py            # 只报告差异
    python tools/sync_cards_from_game.py --apply    # 写回 cards.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TEXTS = HERE / 'assets' / 'data' / 'game_texts.json'
CARDS = HERE / 'assets' / 'data' / 'cards.json'

PH = re.compile(r'\{[A-Za-z_]+=([^}]*)\}')
IMG = re.compile(r"<img[^>]*>", re.I)
WIKIFILE = re.compile(r'\[\[file:[^\]]*\]\]', re.I)
TAG = re.compile(r'<[^>]+>')


def resolve_desc(tpl):
    """游戏文案 → 界面可用文本。

    「指定{range=10}格内的一个怪物，造成{damage=2}点伤害」→「指定10格内的一个怪物，造成2点伤害」
    同时清掉游戏富文本 <img src='ui://…'> 和旧 wiki 的 [[file:…]] 标记。
    """
    s = PH.sub(lambda m: m.group(1), tpl)
    s = IMG.sub('', s)
    s = WIKIFILE.sub('', s)
    s = TAG.sub('', s)
    s = s.replace('\u3000', ' ')
    return re.sub(r'[ \t]{2,}', ' ', s).strip()


def series_mode(cid):
    if 20000 <= cid < 21000:
        return 'PVP'
    if 21000 <= cid < 22000:
        return 'PVE'
    return '通用'


def main():
    ap = argparse.ArgumentParser(description='用游戏文案表刷新卡牌表')
    ap.add_argument('--apply', action='store_true', help='写回 assets/data/cards.json')
    ap.add_argument('--texts', default=str(TEXTS))
    ap.add_argument('--cards', default=str(CARDS))
    args = ap.parse_args()

    tp = Path(args.texts)
    cp = Path(args.cards)
    if not tp.exists():
        print('✗ 缺 %s，先跑 tools/fetch_game_texts.py' % tp)
        return 2
    texts = json.loads(tp.read_text(encoding='utf-8')).get('tables', {})
    scard = texts.get('STRCard')
    if not scard:
        print('✗ 文案表里没有 STRCard')
        return 2

    names, descs = {}, {}
    for k, v in scard.items():
        try:
            kk = int(k)
        except ValueError:
            continue
        if 10000 <= kk <= 99999:
            names[kk] = v
        else:
            base, seq = kk // 10, kk % 10
            if 10000 <= base <= 99999:
                descs.setdefault(base, {})[seq] = v

    cards = json.loads(cp.read_text(encoding='utf-8'))
    byid = {int(c['id']): c for c in cards}

    renamed, retexted, added, extra = [], [], [], []
    for cid in sorted(names):
        gname = names[cid]
        gdesc = resolve_desc(descs.get(cid, {}).get(1, '')) if descs.get(cid) else ''
        c = byid.get(cid)
        if c is None:
            added.append((cid, gname, gdesc))
            continue
        if c.get('name') != gname:
            renamed.append((cid, c.get('name'), gname))
        if gdesc and c.get('desc') != gdesc:
            retexted.append((cid, c.get('name'), c.get('desc', ''), gdesc))
    for cid in sorted(set(byid) - set(names)):
        extra.append((cid, byid[cid].get('name')))

    print('游戏 STRCard：名字 %d 张 / 有描述的 %d 张' % (len(names), len(descs)))
    print('仓库 cards.json：%d 张\n' % len(cards))

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

    order = ['id', 'name', 'type', 'target', 'desc', 'mode', 'from']
    for c in cards:
        cid = int(c['id'])
        if cid in names:
            c['name'] = names[cid]
            if descs.get(cid) and descs[cid].get(1):
                c['desc'] = resolve_desc(descs[cid][1])
    for cid, nm, ds in added:
        row = {'id': cid, 'name': nm, 'type': 'Effect', 'target': 'Entity',
               'desc': ds, 'mode': series_mode(cid), 'from': '基础卡牌'}
        cards.append({k: row[k] for k in order})
    cards.sort(key=lambda c: int(c['id']))
    cp.write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n✓ 已写回 %s（%d 张）' % (cp.relative_to(HERE), len(cards)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
