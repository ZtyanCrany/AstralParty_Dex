# -*- coding: utf-8 -*-
"""复盘构建层 —— 回放 → 复盘数据（给前端/文本样张用）。

    from astral.review import build_review
    rv = build_review(path)

    rv['players'][i]['rounds']      [{round, kill, dmg, inj, gold, move, died, deaths}]
    rv['players'][i]['chip_events'] [{round, source, candidates, picked, refresh, ...}]

设计要点：
· cond 是本局累计 → 每轮数据 = 本轮末快照 − 上轮末快照
· 死亡 = 该轮 total_die 增量（可 >1，一轮内死两次不合并）；died 只是「本轮死过」的标记
· selfDie 不计入死亡：结算界面的「死亡」列就是 total_die，两者相加会与官方数字对不上
· 残轮（击败 BOSS 即结束）属正常情况，不产生告警
· 筹码来源三条路：同帧多人=任务 / 单独=升星 / buyRelicNum 增长帧=商店
"""
from __future__ import annotations

import bisect
import struct
from pathlib import Path

from astral.proto_loader import msg_class
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


def _match_offer(offers, cid, f):
    """该筹码出自哪次三选一/商店（±2 帧内、且候选里出现过）—— 取最近的那次"""
    best, bd = None, None
    for o in offers:
        if abs(o['frame'] - f) > 2:
            continue
        if not any(cid in g['cands'] for g in o['chain']):
            continue
        d = abs(o['frame'] - f)
        if bd is None or d < bd:
            best, bd = o, d
    return best


def _chain_slices(offers, chips):
    """{筹码ID: (起始组, 结束组)} —— 同一次 offer 里有多枚筹码时把链切开

    同帧、同 (lv, supLv) 且中间没有 5249 的多次三选一会连成一条链（各次三选一的候选组
    按包序连续排在一起）；不加区分时每枚筹码都会背上整条链，同一串候选在界面上画两遍。

    切开规则：第 n 枚筹码的链 =「上一枚所属组的下一组」至「本枚所属组」，最后一枚取到
    链尾。组号非递增、或某枚的候选不在链里（循环往复额外发的筹码不是选出来的）就不切，
    按原样显示。
    """
    by_offer = {}
    for cid, f in chips.items():
        o = _match_offer(offers, cid, f)
        if o is not None:
            by_offer.setdefault(o['ord'], []).append((f, cid, o))
    out = {}
    for items in by_offer.values():
        if len(items) < 2:
            continue
        rows = []
        for f, cid, o in items:
            hit = [i for i, g in enumerate(o['chain']) if cid in g['cands']]
            if not hit:
                rows = None
                break
            rows.append((hit[-1], f, cid, o))
        if not rows:
            continue
        rows.sort()                 # 链按包序排列 ⇒ 所属组号小的那枚先被选中
        idxs = [r[0] for r in rows]
        if any(idxs[i] >= idxs[i + 1] for i in range(len(idxs) - 1)):
            continue                # 组号非递增 ⇒ 分不清，按原样显示
        start = 0
        for k, (gidx, _f, cid, o) in enumerate(rows):
            end = gidx if k < len(rows) - 1 else len(o['chain']) - 1
            out[cid] = (start, end)
            start = gidx + 1
    return out


def _player_chip_events(rp, uid, chips, all_groups):
    groups = all_groups.get(uid, {})
    stores = _shop_purchases(rp, uid, chips, groups)
    ups = _level_ups(rp, uid)
    flips = _mission_flips(rp)
    all_mids = sorted({m for st in rp.mission_states() for m in st})
    has_cycle = CYCLE_CHIP in chips
    cycle_frame = chips.get(CYCLE_CHIP)
    # 循环往复的原话：「3回合后选择并获得1个强化筹码」—— 只发【一次】。
    #   以前只要筹码没被别的来源认领，就一律记成循环往复，于是第 7 轮（离第 2 轮已 5 轮）
    #   那张也被算进去，看起来像「效果发了两次」。这里按「拿到它的轮次 +3、允许 ±1 轮
    #   的轮界误差、且全程只认一次」来卡死。
    cycle_round = _round_at(rp, uid, cycle_frame) if cycle_frame is not None else None
    cycle_used = False
    offers = _offer_events(rp, uid)
    slices = _chain_slices(offers, chips)
    claimed = {}                      # 已被哪个事件认领（判断循环往复）
    used = {}                         # 已被哪次升级/哪个任务认领（判断循环往复）
    shop_price = 10                   # 第 n 次商店购买的预期价：10,15,20,25…
    task_taken = {}                   # {翻转帧: [已分配出去的任务ID]}（同帧多任务按序分配）
    events = []

    def _ev_ord(cid, f):
        """该筹码所属事件的时间序号 + 组号（用于让分配/展示顺序都跟事件走）

        组号 = 该筹码在自己那次 offer 的链里属于第几组，即玩家选它的先后；
        同帧、同一次 offer 的多个筹码靠它定序。
        """
        o = _match_offer(offers, cid, f)
        if o is None:
            return (9999, 9999)
        hit = [i for i, g in enumerate(o['chain']) if cid in g['cands']]
        return (o['ord'], hit[-1] if hit else 9999)

    def _cycle_ok(rnd):
        """这张筹码能不能算循环往复给的（只发一次 + 落在 +3 轮的窗口里）"""
        if not has_cycle or cycle_used or cycle_round is None or rnd is None:
            return False
        return abs(rnd - (cycle_round + 3)) <= 1

    _order = {cid: _ev_ord(cid, f) for cid, f in chips.items()}
    for cid, f in sorted(chips.items(), key=lambda kv: (kv[1], _order[kv[0]])):
        # ① 出自哪个「事件」（记录帧通常 = 到手帧 或 前后一帧）
        #    轮次一律取【事件所在轮】：轮末的三选一/商店，筹码背包登记会晚一帧
        #    甚至跨到下一轮，若用登记帧的轮次会出现「第7轮下面挂着第6轮的筹码」
        ev = _match_offer(offers, cid, f)
        rnd = _round_at(rp, uid, ev['frame'] if ev is not None else f)
        # ② 刷新链 = 该事件自己的候选组（同一次 offer 里多枚筹码时按 _chain_slices 切开）；
        #    选中的筹码只标在最后那一组（避免刷新前的同名也标为选中）
        chain = []
        if ev is not None:
            lo, hi = slices.get(cid, (0, len(ev['chain']) - 1))
            for g in ev['chain'][lo:hi + 1]:
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
        elif reused and _cycle_ok(rnd):
            source = 'cycle'          # 同一次升级/任务已发过一个 ⇒ 这个是循环往复给的
            arg = None                # 循环往复没有编号，不携带任务号
            key2 = None
        elif n_players >= 2:
            source = 'task'           # 同帧多人拿到（没找到任务翻转也要标任务）
            key2 = None
        elif chain:
            source = 'cycle' if _cycle_ok(rnd) else 'star'
            key2 = None
        else:
            source = 'unknown'
            key2 = None
        if source == 'cycle':
            cycle_used = True         # 只发一次：后面再出现就不许再记成循环往复了
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


# ── 手牌 / 卡牌 ───────────────────────────────────────────
# 卡牌只存在于每帧 Room 快照的 Hero#8 cards（repeated CardInfo）里；回放的协议包日志
# 记不出「用了哪张、给了谁」⇒ 全部靠逐帧手牌差分：
#   · 每轮开局手牌 = 该轮第一帧的非空手牌（已阵亡的玩家该轮为空）
#   · 本轮获得     = 本轮首次出现、且不在开局手牌里的牌
#   · 本轮用掉     = 最后出现帧落在该轮、且早于末帧的牌（末帧还拿在手上的不算出牌）
# 注：牌「怎么来的」（商店/谁发的卡）回放里没有可靠记录，不要猜，界面上也不标。
_CARDS = None
_MSGS = {}


def _msg(full_name):
    """按全名取消息类并缓存（解析回放时反复要用）"""
    if full_name not in _MSGS:
        _MSGS[full_name] = msg_class(full_name)
    return _MSGS[full_name]


def reload_tables():
    """丢弃缓存的表（客户端内容更新后由 main.py 调用）。"""
    global _CARDS
    _CARDS = None


def card_table():
    """{ID: {'name','type','desc','target','mode','from'}}（assets/data/cards.json）"""
    global _CARDS
    if _CARDS is None:
        import json
        from astral.paths import data_file
        try:
            raw = json.loads(Path(data_file('cards.json')).read_text(encoding='utf-8'))
            _CARDS = {int(c['id']): c for c in raw}
        except Exception:
            _CARDS = {}
    return _CARDS


def card_info(cid):
    """卡牌 ID → 展示信息（表里查不到就退回原始 ID，不猜）"""
    c = card_table().get(int(cid))
    if not c:
        return {'id': int(cid), 'name': '卡牌 %d' % cid, 'type': '', 'desc': ''}
    return {'id': int(cid), 'name': c['name'],
            'type': str(c.get('type') or '').strip().lower(),
            'desc': c.get('desc', '')}


def _round_of(rooms, frame, uid):
    """该帧该玩家显示第几轮（1 基）"""
    if 0 <= frame < len(rooms):
        for pl in rooms[frame].players:
            if pl.id == uid:
                return max(pl.hero.round, 0) + 1
    return None


def _read_len(b, i):
    """读 varint（记录头 12 <len> 的长度位）"""
    v, sh = 0, 0
    while i < len(b) and sh < 64:
        c = b[i]
        v |= (c & 0x7f) << sh
        i += 1
        if c < 128:
            return v, i
        sh += 7
    return None, i


def _scan_rooms(rp):
    """顺序切「12 <len>」记录，取出**完整**的 Room 帧（列表下标即帧号）。

    Replay._parse_frames 用锚点匹配（要求 Room 首字段是 09 <房号>），一局只认出 28/387 条，
    且 387 条里多数是残缺快照（有 player 没 hero）。残缺帧会把一张牌的「最后出现帧」
    拉到末帧，导致后半局的出牌全被当成终局截断 —— 所以这里只收 4 个玩家都有 hero 的帧，
    并且独立于既有解析路径，不动筹码那套。
    """
    d, n = rp.data, len(rp.data)
    RoomMsg = _msg('model.Room')
    out, i = [], 18
    while i < n - 1:
        if d[i] != 0x12:
            i += 1
            continue
        ln, j = _read_len(d, i + 1)
        if not ln or j + ln > n:
            i += 1
            continue
        if d[j] == 0x09:
            try:
                rm = RoomMsg()
                rm.ParseFromString(d[j:j + ln])
            except Exception:
                rm = None
            if rm is not None and len(rm.players) and all(pl.hero.hero_id for pl in rm.players):
                out.append(rm)
        i = j + ln
    return out


def _card_life(rooms):
    """{(uid, uniqueId): {'cid','first','last'}} —— 每张牌的存活区间"""
    life = {}
    for fi, rm in enumerate(rooms):
        for pl in rm.players:
            for c in pl.hero.cards:
                k = (pl.id, c.uniqueId)
                if k in life:
                    life[k]['last'] = fi
                else:
                    life[k] = {'cid': c.cardId, 'first': fi, 'last': fi}
    return life


def _hand_snapshots(rooms):
    """{轮次: {uid: [卡牌]}} —— 每轮取该玩家第一帧非空手牌（= 该轮开局手牌）"""
    out = {}
    for rm in rooms:
        for pl in rm.players:
            if not len(pl.hero.cards):
                continue
            r = max(pl.hero.round, 0) + 1
            out.setdefault(r, {}).setdefault(pl.id, [card_info(c.cardId) for c in pl.hero.cards])
    return out


def _card_rounds(rooms, life, last_frame):
    """[{'round': n, 'players': {uid: {'start': [], 'gets': [], 'uses': []}}}]"""
    out = {r: {u: {'start': list(cs), 'gets': [], 'uses': []} for u, cs in snap.items()}
           for r, snap in _hand_snapshots(rooms).items()}

    def slot(r, uid):
        return out.setdefault(0 if r is None else r, {}).setdefault(
            uid, {'start': [], 'gets': [], 'uses': []})

    for (uid, _cu), v in life.items():
        s = slot(_round_of(rooms, v['first'], uid), uid)
        if not any(c['id'] == v['cid'] for c in s['start']):
            s['gets'].append(dict(card_info(v['cid']), frame=v['first']))
        if v['last'] < last_frame:
            slot(_round_of(rooms, v['last'], uid), uid)['uses'].append(
                dict(card_info(v['cid']), frame=v['last']))
    rows = []
    for r in sorted(out):
        row = {'round': r, 'players': {}}
        for uid, d in out[r].items():
            d['gets'].sort(key=lambda e: e['frame'])
            d['uses'].sort(key=lambda e: e['frame'])
            row['players'][str(uid)] = d
        rows.append(row)
    return rows


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
        deaths = max(d('total_die'), 0)
        rows.append({
            'round': rnd,
            'kill': max(kill, 0), 'dmg': max(d('total_damage'), 0),
            'inj': max(d('total_injured'), 0), 'gold': max(gold, 0),
            'move': max(d('movePoint'), 0), 'transfer': max(d('transferGold'), 0),
            'tret': max(d('treatmentScore'), 0), 'boss': bool(v.get('finalKillBoss')),
            'died': deaths > 0, 'deaths': deaths,
        })
        prev = v
    total = {k: sum(r[k] for r in rows) for k in ('kill', 'dmg', 'inj', 'gold', 'move', 'transfer', 'tret')}
    total['died'] = sum(r['deaths'] for r in rows)   # 次数而非「死过的轮数」

    # ── v2 派生指标 ───────────────────────────────────────
    n_rounds = len(rows) or 1
    death_rounds = sum(1 for r in rows if r['died'])
    total['rounds'] = len(rows)
    total['death_rounds'] = death_rounds
    total['surv_rate'] = (len(rows) - death_rounds) / n_rounds          # 存活轮占比
    total['nonfatal_inj'] = sum(r['inj'] for r in rows if not r['died'])  # 不致死的承伤
    total['boss'] = 1 if any(r['boss'] for r in rows) else 0
    # 伤害转化率：拿多少钱打出多少伤害（金币下限 50，避免极小额把比值放大）
    total['eff'] = total['dmg'] / max(total['gold'], 50)
    return rows, total


# ── 局内评分 ─────────────────────────────────────────
#   回答的是「这一局谁贡献大」，不是「这个人强不强」：
#   所有指标都只在本局四人之间做相对比较（与该局队内均值比），换一局队友就重新算。
#   跨局比较需要按段位/地图分桶另建模型，两种口径不可混用。
SCORE_ITEMS_V2 = [
    #  key        标签          相对权重  方向
    ('eff',      '伤害转化率',  18,  1),    # 伤害 / 星币
    ('dmg',      '输出伤害',    15,  1),
    ('kill',     '击杀',        13,  1),
    ('transfer', '转账',        15,  1),    # 比治疗更值钱：治疗自己也在涨治疗分
    ('tret',     '治疗',        10,  1),
    ('surv',     '存活时间',    10,  1),    # 未死亡轮次占比（仍按百分比显示）
    ('ninj',     '有效承伤',     7,  1),
    ('move',     '移动步数',     5,  1),
]

# rating 档位（队内均值严格 = 1.00）
#   ≥1.80 SS · 1.50–1.80 S · 1.25–1.50 A · 1.00–1.25 B（平均） · 0.80–1.00 C · <0.80 D
SCORE_GRADES = ((1.80, 'SS'), (1.50, 'S'), (1.25, 'A'), (1.00, 'B'), (0.80, 'C'))
# 指标名下方的小注（解释这个数怎么来的）
SCORE_NOTES = {'eff': '伤害量/星币数'}
RATING_CAP = 2.60          # 单项比值上限，防止一个极端值把整张卡抬飞

POS_TILT = {
    1: {'tret': 1.45, 'transfer': 1.45, 'dmg': 0.75, 'kill': 0.80},
    2: {'tret': 1.10, 'transfer': 1.15, 'dmg': 1.00, 'kill': 1.00},   # 辅C：输出不再压
    3: {'dmg': 1.15, 'kill': 1.12, 'transfer': 0.55, 'tret': 0.50},   # 主C
    4: {'dmg': 1.15, 'kill': 1.12, 'transfer': 0.55, 'tret': 0.50},   # 主C（与 3 号位一致）
}
# 显示基准：内部 rating 均值恒为 1.00，展示时除以 BASELINE，
# 使 1.00 = 及格线（约队内末位 20~25% 分位），队内平均落在 1.2 上下
RATING_BASELINE = 0.80


def _score_raw(p, key):
    t = p.get('totals') or {}
    alias = {'surv': 'surv_rate', 'ninj': 'nonfatal_inj'}
    return float(t.get(alias.get(key, key), 0) or 0)


def _tilt_of(slot):
    """位置倾斜查询：slot 是 0 基（0 = 1 号位），POS_TILT 的键是 1 基的「号位」。

    两者差一位，直接拿 slot 去查会整体错开：1 号位拿不到倾斜、2 号位吃辅助倾斜、
    3 号位吃辅 C 倾斜。取不到 slot 时返回空表（按无倾斜算），不要默认成 1 号位。
    """
    if slot is None:
        return {}
    try:
        return POS_TILT.get(int(slot) + 1, {})
    except (TypeError, ValueError):
        return {}


def score_players(players, tilt=True):
    """同局四人 rating（队内均值 = 1.00，类似 CS 的 HLTV rating）。

    1. 每项指标取「本人数值 ÷ 队内均值」，得到相对强度（均值 1.00）；
    2. 单项比值截断到 2.6，避免一个爆表值带偏整张卡；
    3. 按位置软加权（1/2 号位转账·治疗↑，3/4 号位输出↑），权重归一化到 1；
    4. rating = Σ(权重 × 比值)。因为每项比值的队内均值都是 1，
       加权和的均值也是 1 —— 也就是「队内平均发挥 = 1.00」。
    """
    if not players:
        return
    n = len(players)
    ratios = [dict() for _ in range(n)]
    for key, _label, _w, direction in SCORE_ITEMS_V2:
        vals = [_score_raw(p, key) for p in players]
        mean = sum(vals) / n
        for i, v in enumerate(vals):
            if mean <= 0:
                r = 1.0                      # 全队都没有这项 → 中性
            else:
                # 加 0.6 个队均做先验：(v + 0.6m) / 1.6m
                # 0 → 0.375、均值 → 1.00、2 倍 → 1.625；队内均值仍严格是 1
                r = (v + 0.6 * mean) / (1.6 * mean)
            r = min(max(r, 0.0), RATING_CAP)
            if direction < 0 and mean > 0:
                r = min(max(2.0 - r, 0.0), RATING_CAP)
            ratios[i][key] = r

    raw_list = []
    for i, p in enumerate(players):
        tilt_map = _tilt_of(p.get('slot')) if tilt else {}
        raw_w = {k: w * tilt_map.get(k, 1.0) for k, _l, w, _d in SCORE_ITEMS_V2}
        wsum = sum(raw_w.values()) or 1.0
        eff_w = {k: v / wsum for k, v in raw_w.items()}
        parts, rating = [], 0.0
        for key, label, _w, _dir in SCORE_ITEMS_V2:
            w = eff_w[key]
            r = ratios[i].get(key, 1.0)
            rating += w * r
            parts.append({'key': key, 'label': label,
                          'note': SCORE_NOTES.get(key, ''),
                          'raw': round(_score_raw(p, key), 3),
                          'ratio': round(r, 3),
                          'norm': round(min(r / RATING_CAP, 1.0), 4),   # 进度条长度
                          'weight': round(w, 4),
                          'contrib': round((r - 1.0) * w * 100, 1)})    # 相对均值的贡献偏差
        raw_list.append(rating)
        p['score'] = {'rating': rating, 'grade': '', 'parts': parts, 'scale': 'rating'}

    # 单项截断会让均值略低于 1，这里按队内实际均值再拉回 1.00
    mean_r = (sum(raw_list) / n) if n else 1.0
    mean_r = mean_r or 1.0
    for p in players:
        sc = p['score']
        sc['rating'] = round(sc['rating'] / mean_r / RATING_BASELINE, 2)
        sc['total'] = sc['rating']
        sc['grade'] = next((g for cut, g in SCORE_GRADES if sc['rating'] >= cut), 'D')

    for p in players:
        p['score']['rank'] = 1 + sum(1 for q in players if q['score']['rating'] > p['score']['rating'])


# ── 主入口 ────────────────────────────────────────────────
def build_review(src, replay_id=None):
    rp = parse_replay(src, replay_id)
    all_groups = {u: _groups_by_frame(rp, u) for u in rp.players}
    nick_of = {u: i.get('nick', '') for u, i in rp.players.items()}
    rooms = _scan_rooms(rp)
    card_rounds = _card_rounds(rooms, _card_life(rooms), max(len(rooms) - 1, 0))
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
    score_players(players)
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
        'card_rounds': card_rounds,
        'players': players,
    }
