# -*- coding: utf-8 -*-
"""用游戏本体的文案表刷新仓库内的筹码表（默认只报告，加 --apply 才写回）。

数据来源：tools/fetch_game_texts.py 导出的 assets/data/game_texts.json
  STRRelic[50000 + n]      → 筹码名（n = 筹码序号，如 50001 起）
  STRRelic[(50000 + n)*10] → 筹码效果文字（与卡表同一套「×10」规则）

文案里带游戏富文本（[color=#94FF46]+1[/color]、<img src='ui://…'>），落表前清成纯文本。

字段策略：
  名称 / 效果文字 —— 以游戏为准
  品质 / 标签 / 风格 / 地图限制 —— 表里已有的保留；新筹码从**同族**（名字前缀相同）的
  既有筹码继承这几个字段，继承不到就留空（会在报告里点名，便于人工补）

用法：
    python tools/sync_chips_from_game.py            # 只报告
    python tools/sync_chips_from_game.py --apply    # 写回 chips.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TEXTS = HERE / 'assets' / 'data' / 'game_texts.json'
CHIPS = HERE / 'assets' / 'data' / 'chips.json'

PH = re.compile(r'\{[A-Za-z_0-9]+=([^}]*)\}')
COLOR = re.compile(r'\[/?(?:color|b|i|size|u)(?:=[^\]]*)?\]', re.I)
IMG = re.compile(r"<img[^>]*>", re.I)
TAG = re.compile(r'<[^>]+>')

REL_LO, REL_HI = 50001, 50093      # 筹码号段


def clean_text(tpl):
    """游戏文案 → 界面可用纯文本（去富文本与占位符外壳）。"""
    s = PH.sub(lambda m: m.group(1), tpl)
    s = IMG.sub('', s)
    s = COLOR.sub('', s)
    s = TAG.sub('', s)
    s = s.replace('\u3000', ' ')
    return re.sub(r'[ \t]{2,}', ' ', s).strip()


def family_of(name):
    """「手电筒-一般」→「手电筒」；「时间怀表(破碎)」→「时间怀表」"""
    return re.split(r'[-–(（]', str(name or ''), 1)[0].strip()


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
    sr = {int(k): v for k, v in json.loads(tp.read_text(encoding='utf-8'))['tables'].get('STRRelic', {}).items()}
    if not sr:
        print('✗ 文案表里没有 STRRelic')
        return 2

    names = {k: v for k, v in sr.items() if REL_LO <= k <= REL_HI}
    descs = {k: v for k, v in sr.items() if REL_LO * 10 <= k <= (REL_HI + 1) * 10}

    chips = json.loads(cp.read_text(encoding='utf-8'))
    byid = {int(c['id']): c for c in chips}

    renamed, retexted, added, extra = [], [], [], []
    for cid in sorted(names):
        gname = names[cid]
        raw = descs.get(cid * 10, '')
        gdesc = clean_text(raw) if raw else ''
        c = byid.get(cid)
        if c is None:
            added.append((cid, gname, gdesc))
            continue
        if c.get('name') != gname:
            renamed.append((cid, c.get('name'), gname))
        if gdesc and c.get('desc') != gdesc:
            retexted.append((cid, gname, c.get('desc', ''), gdesc))
    for cid in sorted(set(byid) - set(names)):
        extra.append((cid, byid[cid].get('name')))

    print('游戏 STRRelic：筹码名 %d 个 / 有描述 %d 个' % (len(names), sum(1 for k in names if k * 10 in descs)))
    print('仓库 chips.json：%d 个\n' % len(chips))

    print('① 名字不一致（→ 用游戏值）：%d 处' % len(renamed))
    for cid, old, new in renamed:
        print('   %-7d %s → %s' % (cid, old, new))
    print('\n② 效果文字新增/更正（→ 用游戏值）：%d 处' % len(retexted))
    for cid, nm, old, new in retexted[:12]:
        print('   %-7d %-14s %s' % (cid, nm, new[:70]))
    if len(retexted) > 12:
        print('   …（其余 %d 处同理）' % (len(retexted) - 12))
    print('\n③ 游戏有、仓库没有（→ 新增）：%d 个' % len(added))
    for cid, nm, ds in added:
        fam = family_of(nm)
        sib = next((c for c in chips if family_of(c.get('name')) == fam), None)
        src = ('继承自 %s' % sib['name']) if sib else '无同族，字段留空'
        print('   %-7d %-16s %s   [%s]' % (cid, nm, ds[:48], src))
    print('\n④ 仓库有、游戏没有（→ 保留不动）：%d 个' % len(extra))
    for cid, nm in extra:
        print('   %-7d %s' % (cid, nm))

    if not args.apply:
        print('\n（只报告，未写回。加 --apply 生效）')
        return 0

    for c in chips:
        cid = int(c['id'])
        if cid in names:
            c['name'] = names[cid]
            raw = descs.get(cid * 10)
            if raw:
                c['desc'] = clean_text(raw)
    for cid, nm, gdesc in added:
        fam = family_of(nm)
        sib = next((c for c in chips if family_of(c.get('name')) == fam), None)
        row = {'id': cid, 'name': nm,
               'quality': sib.get('quality', '') if sib else '',
               'tag': sib.get('tag', '') if sib else '',
               'tag_full': sib.get('tag_full', '') if sib else '',
               'desc': gdesc,
               'style': sib.get('style', '') if sib else '',
               'map_limit': list(sib.get('map_limit', [])) if sib else []}
        chips.append(row)
    chips.sort(key=lambda c: int(c['id']))
    cp.write_text(json.dumps(chips, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n✓ 已写回 %s（%d 个）' % (cp.relative_to(HERE), len(chips)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
