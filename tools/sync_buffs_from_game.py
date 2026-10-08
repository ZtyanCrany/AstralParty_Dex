# -*- coding: utf-8 -*-
"""用游戏本体的文案表生成增益（状态）表 assets/data/buffs.json。

数据来源：tools/fetch_game_texts.py 导出的 assets/data/game_texts.json 的 STRBuff 表。

STRBuff 的键惯例：
    <buff_id>         → 该增益的效果文字
    <buff_id> * 10 + 1 → 该增益的名字
例：10004「回合开始时恢复治愈层数的生命值…」/ 100041「治愈」

筹码自带的增益：buff_id = 筹码ID * 100 + 1（例：筹码 50001 → 5000101）。

输出：{"<buff_id>": {"name": "...", "desc": "..."}}（文本已去游戏富文本）

用法：
    python tools/sync_buffs_from_game.py            # 只报告
    python tools/sync_buffs_from_game.py --apply    # 写回 buffs.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TEXTS = HERE / 'assets' / 'data' / 'game_texts.json'
OUT = HERE / 'assets' / 'data' / 'buffs.json'

PH = re.compile(r'\{[A-Za-z_0-9]+=([^}]*)\}')
COLOR = re.compile(r'\[/?(?:color|b|i|size|u)(?:=[^\]]*)?\]', re.I)
IMG = re.compile(r"<img[^>]*>", re.I)
TAG = re.compile(r'<[^>]+>')


def clean(tpl):
    s = PH.sub(lambda m: m.group(1), str(tpl))
    s = IMG.sub('', s)
    s = COLOR.sub('', s)
    s = TAG.sub('', s)
    s = s.replace('\u3000', ' ')
    return re.sub(r'[ \t]{2,}', ' ', s).strip()


def main():
    ap = argparse.ArgumentParser(description='生成增益表 buffs.json')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--texts', default=str(TEXTS))
    args = ap.parse_args()

    tp = Path(args.texts)
    if not tp.exists():
        print('✗ 缺 %s，先跑 tools/fetch_game_texts.py' % tp)
        return 2
    sb = {int(k): v for k, v in json.loads(tp.read_text(encoding='utf-8'))['tables'].get('STRBuff', {}).items()}
    if not sb:
        print('✗ 文案表里没有 STRBuff')
        return 2

    def is_name_key(k):
        """名字键 = <base>*10+1 且 base 也在表里（例：100041 的名字属于 10004）"""
        return k % 10 == 1 and (k - 1) // 10 in sb

    rows = {}
    for k, txt in sb.items():
        if is_name_key(k):
            continue
        name = sb.get(k * 10 + 1)
        if name is not None:
            rows[k] = {'name': clean(name), 'desc': clean(txt)}
        else:
            # 没有名字键：短、无标点的文本当名字，其余当效果
            body = clean(txt)
            if len(body) <= 8 and not re.search(r'[，。、；！？,.;!?+\-]', body):
                rows[k] = {'name': body, 'desc': ''}
            else:
                rows[k] = {'name': '', 'desc': body}

    print('STRBuff 共 %d 键 → 解出 %d 条（名字键 %d 个已并入）'
          % (len(sb), len(rows), sum(1 for k in sb if is_name_key(k))))
    chips = sum(1 for k in rows if 5000101 <= k <= 5009301)
    print('   其中筹码增益（号段 500<筹码ID>01）%d 条' % chips)
    onlydesc = sum(1 for v in rows.values() if v['desc'] and not v['name'])
    onlyname = sum(1 for v in rows.values() if v['name'] and not v['desc'])
    print('   只有效果无名字 %d 条 / 只有名字无效果 %d 条（短文本无标点按名字判定）' % (onlydesc, onlyname))
    for k in sorted(rows)[:8]:
        print('   %-9d %-10s %s' % (k, rows[k]['name'] or '（无名）', rows[k]['desc'][:60]))

    if not args.apply:
        print('\n（只报告，未写回。加 --apply 生效）')
        return 0
    OUT.write_text(json.dumps({str(k): rows[k] for k in sorted(rows)}, ensure_ascii=False, indent=1),
                   encoding='utf-8')
    print('\n✓ 已写出 %s（%d 条）' % (OUT.relative_to(HERE), len(rows)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
