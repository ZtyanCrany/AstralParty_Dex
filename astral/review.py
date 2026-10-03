# -*- coding: utf-8 -*-
"""复盘构建层 —— 回放 → 复盘数据（给前端/文本样张用）。

    from astral.review import build_review
    rv = build_review(path)

    rv['players'][i]['rounds']      [{round, kill, dmg, inj, gold, move, died}]
    rv['players'][i]['chip_events'] [{round, source, candidates, picked, refresh, ...}]

设计要点：
· cond 是本局累计 → 每轮数据 = 本轮末快照 − 上轮末快照
· 死亡 = 该轮 total_die / selfDie 增量 > 0
· 残轮（击败 BOSS 即结束）属正常情况，不产生告警
· 筹码来源三条路：同帧多人=任务 / 单独=升星 / buyRelicNum 增长帧=商店
"""
from __future__ import annotations

import struct

from astral.replay import (CHIP_MIN, CHIP_MAX, chip_name, chip_quality, pb,
                           pb_int, parse_replay)

RELIC_CMD = 5211        # SelectRelicC2S   #2 = 3 个候选(fixed32) / #5 lv / #6 supLv
BUY_CMD = 5249          # BuyRelicC2S      #2 divinationGold / #3 relicGold
SOURCE_TEXT = {'task': '任务完成', 'star': '升星', 'shop': '商店购买',
               'cycle': '循环往复', 'unknown': '未知'}
SOURCE_ICON = {'task': '🎯', 'star': '⭐', 'shop': '🛒', 'cycle': '🔁', 'unknown': '❓'}


# ── 负载解码 ──────────────────────────────────────────────
def relic_options(payload):
    """SelectRelicC2S 负载 → 3 个候选筹码 ID（#2 = 12 字节 = 3×fixed32 小端）"""
    if not payload:
        return []
    d = pb(payload)
    raw = d.get(2)
    if not raw:
        return []
    b = raw[0]
    if not isinstance(b, (bytes, bytearray)) or len(b) < 12:
        return []
    return [struct.unpack_from('<I', b, i * 4)[0] for i in range(len(b) // 4)]


def buy_cost(payload):
    """BuyRelicC2S 负载 → {'divination': n, 'relic': n}"""
    if not payload:
        return {}
    d = pb(payload)
    return {'divination': pb_int(d, 2), 'relic': pb_int(d, 3)}


# ── 筹码事件 ──────────────────────────────────────────────
def _groups_by_frame(rp, uid):
    """{帧号: [{'cands': [3个候选], 'lv': n, 'supLv': n}]}（去重、保序）"""
    g = {}
    for p in rp.packets:
        if p.cmd != RELIC_CMD or p.uid != uid:
            continue
        opts = relic_options(p.payload)
        if len(opts) != 3 or not all(CHIP_MIN <= c <= CHIP_MAX for c in opts):
            continue
        d = pb(p.payload)
        item = {'cands': opts, 'lv': pb_int(d, 5), 'supLv': pb_int(d, 6)}
        lst = g.setdefault(p.frame, [])
        if not any(x['cands'] == opts for x in lst):
            lst.append(item)
    return g


def _hero_lv(rp, uid):
    """{帧号: 英雄等级}"""
    out = {}
    for fi, fr in enumerate(rp.frames):
        for pl in fr['room'].players:
            if pl.id == uid:
                out[fi] = pl.hero.lv
    return out


def _group_lv(groups, cid, f, lvs):
    """该筹码的候选组里带的 lv（取最接近到手帧的那组）"""
    for cf in (f, f - 1, f - 2):
        for item in groups.get(cf, []):
            if cid in item['cands']:
                return item['lv']
    return None


def _buy_events(rp, uid):
    """[(帧号, 累计 buyRelicNum)] —— buyRelicNum 增长即一次【商店购买】（权威信号）"""
    ev, prev = [], 0
    for fi, fr in enumerate(rp.frames):
        for pl in fr['room'].players:
            if pl.id == uid and pl.hero.buyRelicNum > prev:
                prev = pl.hero.buyRelicNum
                ev.append((fi, prev))
    return ev


_QUALITY_PRICE = {'紫': 10, '金': 15, '蓝': 5}     # 品质售价：紫=10 金=15


def _shop_purchases(rp, uid, chips, groups=None):
    """{筹码ID: 实付价格} —— 把每次 buyRelicNum 增长配对到一个筹码

    帧序：5249(付款) → 下一帧筹码到手。一帧到手多个时用「候选组 lv」
    区分：商店货架的 lv = 当前等级；升星 offer 的 lv = 等级+1。
    """
    out = {}
    lvs = _hero_lv(rp, uid)
    for gframe, _seq in _buy_events(rp, uid):
        price = None
        for bf in (gframe - 1, gframe, gframe - 2):
            ps = rp.packets_at(BUY_CMD, bf)
            if ps:
                price = buy_cost(ps[0].payload).get('relic') or None
                if price:
                    break
        cands = [c for c, f in chips.items() if f in (gframe, gframe + 1) and c not in out]
        if not cands:
            cands = [c for c, f in chips.items() if abs(f - gframe) <= 2 and c not in out]
        if not cands:
            continue
        pick = None
        if len(cands) > 1 and groups:
            # 升星 offer 的组 lv == 当前等级；商店货架的组 lv == 升级前的等级 ⇒ 选后者
            for c in sorted(cands, key=lambda x: chips[x]):
                f = chips[c]
                glv = _group_lv(groups, c, f, lvs)
                cur = lvs.get(f) if f in lvs else lvs.get(f - 1)
                if glv is not None and cur is not None and glv != cur:
                    pick = c
                    break
        if pick is None:
            pick = min(cands, key=lambda c: chips[c])
        out[pick] = price
    return out


CYCLE_CHIP = 50083       # 循环往复：会让玩家单独再拿一个筹码（不是升星/任务）


def _level_ups(rp, uid):
    """{帧号: 升到的新等级} —— 该帧发生了升级（= 第 N 次升星，等级从 0 起）"""
    lvs = _hero_lv(rp, uid)
    out, prev = {}, None
    for f in sorted(lvs):
        if prev is not None and lvs[f] > prev:
            out[f] = lvs[f]
        prev = lvs[f]
    return out


def _mission_flips(rp):
    """{帧号: [该帧完成的任务ID]} —— 任务状态 1(未完成)→2(已完成)"""
    states = rp.mission_states()
    out, prev = {}, {}
    for i, st in enumerate(states):
        for mid, v in st.items():
            if v == 2 and prev.get(mid) != 2:
                out.setdefault(i, []).append(mid)
        prev.update(st)
    return out


def _offer_events(rp, uid):
    """按【包序】切成「事件」= 一次三选一 / 一次商店（含刷新链）

    新事件的条件（任一命中）：
      · (lv, supLv) 变了             —— 同帧可以同时存在商店货架(2,2)与升星 offer(3,3)
      · 换帧了
      · 中间夹了一条 5249 买筹码      —— 帧#42 买(10)→货架A、买(15)→货架B，两组 (lv,supLv) 相同
    并且：5249 的价格 = 紧随其后那个事件的价格（商店来源直接取它，不再猜品质）
    """
    evs, cur, pending = [], None, None
    for p in rp.packets:
        if p.uid != uid:
            continue
        if p.cmd == BUY_CMD:
            pending = (buy_cost(p.payload).get('relic'), p.frame)   # 连帧号一起记
            continue
        if p.cmd != RELIC_CMD:
            continue
        opts = relic_options(p.payload)
        if len(opts) != 3 or not all(CHIP_MIN <= c <= CHIP_MAX for c in opts):
            continue
        d = pb(p.payload)
        key = (pb_int(d, 5), pb_int(d, 6))
        if (cur is None or cur['key'] != key or cur['frame'] != p.frame
                or pending is not None):
            cur = {'frame': p.frame, 'key': key, 'lv': key[0], 'supLv': key[1],
                   'chain': [], 'buy': (pending[0] if pending else None),
                   'buyf': (pending[1] if pending else None), 'ord': len(evs)}
            evs.append(cur)
            pending = None
        if not any(g['cands'] == opts for g in cur['chain']):
            cur['chain'].append({'cands': opts, 'lv': key[0], 'supLv': key[1]})
    return evs


def _round_at(rp, uid, frame):
    """该帧该玩家在第几轮（1 基）"""
    for fr in rp.frames[frame:]:
        for pl in fr['room'].players:
            if pl.id == uid:
                return max(pl.hero.round, 0) + 1
    return None


def _player_chip_events(rp, uid, chips, all_groups):
    groups = all_groups.get(uid, {})
    stores = _shop_purchases(rp, uid, chips, groups)
    ups = _level_ups(rp, uid)
    flips = _mission_flips(rp)
    all_mids = sorted({m for st in rp.mission_states() for m in st})
    has_cycle = CYCLE_CHIP in chips
    cycle_frame = chips.get(CYCLE_CHIP)
    offers = _offer_events(rp, uid)
    claimed = {}                      # 已被哪个事件认领（判断循环往复）
    used = {}                         # 已被哪次升级/哪个任务认领（判断循环往复）
    shop_price = 10                   # 第 n 次商店购买的预期价：10,15,20,25…
    task_taken = {}                   # {翻转帧: [已分配出去的任务ID]}（同帧多任务按序分配）
    events = []

    def _ev_ord(cid, f):
        """该筹码所属事件的时间序号（用于让分配/展示顺序都跟事件走）"""
        best, bd = 9999, None
        for o in offers:
            if abs(o['frame'] - f) > 2:
                continue
            if not any(cid in g['cands'] for g in o['chain']):
                continue
            d = abs(o['frame'] - f)
            if bd is None or d < bd:
                best, bd = o['ord'], d
        return best

    _order = {cid: _ev_ord(cid, f) for cid, f in chips.items()}
    for cid, f in sorted(chips.items(), key=lambda kv: (kv[1], _order[kv[0]])):
        # ① 出自哪个「事件」（记录帧通常 = 到手帧 或 前后一帧）
        #    轮次一律取【事件所在轮】：轮末的三选一/商店，筹码背包登记会晚一帧
        #    甚至跨到下一轮，若用登记帧的轮次会出现「第7轮下面挂着第6轮的筹码」
        ev, dist = None, None
        for o in offers:
            if abs(o['frame'] - f) > 2:
                continue
            if not any(cid in g['cands'] for g in o['chain']):
                continue
            d = abs(o['frame'] - f)
            if dist is None or d < dist:
                ev, dist = o, d
        rnd = _round_at(rp, uid, ev['frame'] if ev is not None else f)
        # ② 刷新链 = 该事件自己的候选组；选中的筹码只标在最后那一组（避免刷新前的同名也标为选中）
        chain = []
        if ev is not None:
            for g in ev['chain']:
                chain.append({'cands': g['cands'],
                              'names': [chip_name(c) for c in g['cands']],
                              'picked': -1})
            for g in reversed(chain):
                if cid in g['cands']:
                    g['picked'] = g['cands'].index(cid)
                    break
        pick_grp = next((g for g in reversed(chain) if g['picked'] >= 0), None)
        opts = pick_grp['cands'] if pick_grp else []
        # ③ 来源判定 + 括号里的参数
        arg = None
        claim = (ev['frame'], ev['key']) if ev is not None else None
        lv_now = ups.get(f) or ups.get(f - 1)
        n_players = sum(1 for u, g2 in all_groups.items() if ev['frame'] in g2) if ev else 0
        if chain and n_players >= 2:
            for mf in (f, f - 1):        # 窗口限于 ±1 帧：放宽会把隔壁事件的筹码也认成这个任务
                hit = [m for m in flips.get(mf, []) if m in all_mids]
                # 同一帧可能一起翻转【多个】任务（如帧#19 = 321100+321110），
                #   必须按顺序分给该帧的多个筹码，否则会出现两条「任务完成（1）」
                taken = task_taken.setdefault(mf, [])
                rest = sorted(m for m in hit if m not in taken)
                if rest:
                    taken.append(rest[0])
                    arg = all_mids.index(rest[0]) + 1
                    break
        # ④ 定来源。优先级：任务翻转（团队事件）> 商店真购买 > 升星 > 循环往复 > 默认升星
        #    任务必须优先于商店：任务那一刻全队都拿筹码，若让个人商店价序列先抢占，
        #    队友的「任务完成（2）（3）」就会整条消失
        cand = ('task', arg) if arg is not None else (('star', lv_now) if lv_now else None)
        reused = bool(cand and used.get(cand)) or bool(claim and claimed.get(claim))
        # 升星 offer 自带 lv=升级后的等级；商店货架的 lv=升级前的等级 ⇒ 用这个区分，
        #   比「先判商店」稳（第1轮那条 5249 会把升星(1) 误判成商店购买）
        lv_up_ok = bool(lv_now and ev is not None and ev.get('lv') == lv_now)
        price = None
        if arg is not None and not reused:
            source = 'task'
            key2 = ('task', arg)
        elif lv_up_ok and not reused:
            source, arg = 'star', lv_now
            key2 = ('star', lv_now)
        elif (ev is not None and ev.get('buy') == shop_price
              and ev.get('buyf') is not None
              and 0 <= ev['frame'] - ev['buyf'] <= 1):
            price = shop_price
            shop_price += 5
            source, arg = 'shop', price
            key2 = ('shop', price)
        elif lv_now and not reused:
            source, arg = 'star', lv_now
            key2 = ('star', lv_now)
        elif reused and has_cycle:
            source = 'cycle'          # 同一次升级/任务已发过一个 ⇒ 这个是循环往复给的
            arg = None                # 循环往复没有编号，不携带任务号
            key2 = None
        elif n_players >= 2:
            source = 'task'           # 同帧多人拿到（没找到任务翻转也要标任务）
            key2 = None
        elif chain:
            source = 'cycle' if (has_cycle and cycle_frame is not None and f > cycle_frame) else 'star'
            key2 = None
        else:
            source = 'unknown'
            key2 = None
        if key2:
            used[key2] = True
        if claim:
            claimed[claim] = True
        events.append({
            'id': cid, 'name': chip_name(cid), 'quality': chip_quality(cid),
            'round': rnd, 'frame': f, 'source': source, 'arg': arg,
            'evi': ev['ord'] if ev is not None else 9999,     # 事件在时间轴上的序号（用于排序）
            'price': price,
            'candidates': opts, 'candidates_name': [chip_name(c) for c in opts],
            'picked_index': (opts.index(cid) if cid in opts else -1),
            'refresh': max(len(chain) - 1, 0), 'chain': chain,
        })
    events.sort(key=lambda e: (e['round'] or 0, e['evi'], e['frame']))
    return events


# ── 每轮统计 ──────────────────────────────────────────────
def _rounds_for(rp, uid):
    snaps = rp.cond_snapshots(uid)
    if not snaps:
        return [], {}
    init_gold = 0
    for rnd in sorted(snaps):
        init_gold = max(init_gold, snaps[rnd][1].get('initGoldCount', 0))
    rows, prev = [], None
    for rnd in sorted(snaps):
        fi, v = snaps[rnd]

        def d(k):
            return v.get(k, 0) - (prev.get(k, 0) if prev else 0)
        kill = d('kill_count') + d('kill_pve_monster') + d('kill_thief')
        gold = d('gold') + (init_gold if rnd == 1 else 0)
        rows.append({
            'round': rnd,
            'kill': max(kill, 0), 'dmg': max(d('total_damage'), 0),
            'inj': max(d('total_injured'), 0), 'gold': max(gold, 0),
            'move': max(d('movePoint'), 0),
            'died': (d('total_die') > 0 or d('selfDie') > 0),
        })
        prev = v
    total = {k: sum(r[k] for r in rows) for k in ('kill', 'dmg', 'inj', 'gold', 'move')}
    total['died'] = sum(1 for r in rows if r['died'])
    return rows, total


# ── 主入口 ────────────────────────────────────────────────
def build_review(src, replay_id=None):
    rp = parse_replay(src, replay_id)
    all_groups = {u: _groups_by_frame(rp, u) for u in rp.players}
    players = []
    for uid, info in rp.players.items():
        chips = rp.chips(uid)
        rows, total = _rounds_for(rp, uid)
        events = _player_chip_events(rp, uid, chips, all_groups)
        players.append({
            'uid': uid,
            'nick': info['nick'],
            'hero_id': info['hero_id'],
            'skin': info.get('skin', 0),
            'slot': info['slot'],
            'level': info.get('level', 0),
            'rounds': rows,
            'totals': total,
            'chip_events': events,
            'chips': [{'id': e['id'], 'name': e['name'], 'quality': e['quality'],
                       'round': e['round'], 'source': e['source']} for e in events],
        })
    players.sort(key=lambda p: p['slot'])
    # 难度与地图：取自 Room 帧（#48 difficulty、#63 mapDifficultyId —— 后者是地图表 ID，
    # 比「回放前 300 字节推测地图」可靠，且离线可读）
    difficulty, map_diff_id = 0, 0
    try:
        for f in (rp.frames or []):
            room = f.get('room') if hasattr(f, 'get') else None
            if room is None:
                continue
            difficulty = int(getattr(room, 'difficulty', 0) or 0)
            map_diff_id = int(getattr(room, 'mapDifficultyId', 0) or 0)
            if difficulty or map_diff_id:
                break
    except Exception:
        pass
    # 任务完成情况（每帧取最后一次状态）
    missions = {}
    for st in reversed(rp.mission_states()):
        for mid, state in st.items():
            missions.setdefault(mid, state)
    return {
        'replay_id': rp.replay_id,
        'map_id': rp.map_id or map_diff_id or None,
        'map_difficulty_id': map_diff_id,
        'difficulty': difficulty,
        'frames': rp.frame_count,
        'packets': len(rp.packets),
        'rounds': rp.round_count(),
        'missions': missions,
        'players': players,
    }
