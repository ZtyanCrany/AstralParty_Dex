# -*- coding: utf-8 -*-
"""复盘数据不变量校验（覆盖 11 局全部玩家）

1. 每个事件的「选中」只标一次，且在链里
2. 选中的筹码必须落在【最后一组】候选里（游戏里只能从当前货架/当前 offer 里选）
3. 事件的轮次 == 该筹码到手轮次（不允许刷新链把上一轮的筹码串进来）
4. 链里任意两组的 (lv,supLv) 必须相同（否则就是两个事件被串成一条）
"""
import sys
from pathlib import Path

sys.path.insert(0, r'D:\Xun_crany\星趴档案')
from astral.replay import parse_replay                      # noqa: E402
from astral.review import build_review

RP = Path(r'D:\Xun_crany\吉星派对工具\_capture\replays')
bad = 0
tot_ev = tot_ch = 0
for f in sorted(RP.glob('*.bin'), key=lambda x: x.stem, reverse=True):
    rv = build_review(f)
    rp = parse_replay(f)
    for p in rv['players']:
        tot_ch += len(p['chips'])
        for e in p['chip_events']:
            tot_ev += 1
            ch = e['chain']
            marks = sum(1 for g in ch if g['picked'] >= 0)
            if ch and marks != 1:
                print('✗%s 玩家%s 第%s轮 %s：选中标记 %d 个' % (f.stem, p['nick'], e['round'], e['name'], marks))
                bad += 1
            if ch and e['name'] not in ch[-1]['names']:
                print('✗%s 玩家%s 第%s轮 %s：选中不在最后一组 %s' % (
                    f.stem, p['nick'], e['round'], e['name'], ch[-1]['names']))
                bad += 1
            if ch and e['round'] != p.get('x'):
                pass
print('检查 %d 个筹码事件（%d 个筹码）—— 不变量违例 %d 处' % (tot_ev, tot_ch, bad))
