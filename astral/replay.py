# -*- coding: utf-8 -*-
"""回放解析层 —— 把一局回放文件解析成结构化数据。

只读，不依赖主程序。对外：

    from astral.replay import parse_replay
    rp = parse_replay(path_or_bytes)

    rp.replay_id                对局回放号
    rp.map_id                   地图 ID（拿不到为 None）
    rp.players                  {uid: {'nick','hero_id','slot'}}
    rp.frame_count              帧数
    rp.packets                  [Packet(cmd, uid, payload, sn, offset, frame)]
    rp.chips(uid)               {筹码ID: 首次出现帧号}
    rp.cond_snapshots(uid)      {显示轮次(1基): (帧号, {字段: 值})}
    rp.mission_states()         [{mission_id: 状态} 每帧]
    rp.packets_at(cmd, frame)   某帧某命令的记录
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

from astral.proto_loader import msg_class

ANCHOR = b'\x12\x05match'          # 老回放结构的帧起点特征（Room#2 房名恰为 "match"）
CHIP_MIN, CHIP_MAX = 50001, 50093  # 真·筹码 ID 段
COND_FIELDS = (
    'kill_count', 'kill_pve_monster', 'kill_thief',      # 击杀三件套
    'total_damage', 'total_injured', 'total_die', 'selfDie',
    'gold', 'initGoldCount', 'movePoint', 'buyRelicNum', 're_roll_num',
)


# ── protobuf 原语 ──────────────────────────────────────────────
def _rv(b, i):
    """读 varint，返回 (值, 下一位置)"""
    v, sh = 0, 0
    while i < len(b) and sh < 64:
        c = b[i]
        v |= (c & 0x7f) << sh
        i += 1
        if c < 128:
            return v, i
        sh += 7
    return None, i


def pb(b):
    """无 schema 迷你 protobuf 解析 → {字段号: [值...]}（值 = int 或 bytes）"""
    out = {}
    i, n = 0, len(b)
    while i < n - 1:
        tag = b[i]
        f, w = tag >> 3, tag & 7
        if f == 0 or f > 5000:
            i += 1
            continue
        if w == 0:
            v, j = _rv(b, i + 1)
            if v is None:
                break
            out.setdefault(f, []).append(v)
            i = j
        elif w == 2:
            ln, j, sh, ok = 0, i + 1, 0, False
            while j < n and sh < 32:
                ln |= (b[j] & 0x7f) << sh
                if b[j] < 128:
                    ok = True
                    j += 1
                    break
                j += 1
                sh += 7
            if not ok or j + ln > n:
                i += 1
                continue
            out.setdefault(f, []).append(b[j:j + ln])
            i = j + ln
        elif w == 5:
            if i + 5 > n:
                break
            out.setdefault(f, []).append(struct.unpack_from('<I', b, i + 1)[0])
            i += 5
        elif w == 1:
            if i + 9 > n:
                break
            out.setdefault(f, []).append(struct.unpack_from('<Q', b, i + 1)[0])
            i += 9
        else:
            i += 1
    return out


def pb_int(d, k, default=0):
    v = d.get(k)
    if not v:
        return default
    x = v[0]
    return x if isinstance(x, int) else default


class Packet:
    """回放里记录的一次交互（命令号 + 玩家 + 请求负载 + 递增序号）"""

    __slots__ = ('cmd', 'uid', 'payload', 'sn', 'offset', 'frame')

    def __init__(self, cmd, uid, payload, sn, offset, frame=-1):
        self.cmd, self.uid, self.payload, self.sn = cmd, uid, payload, sn
        self.offset, self.frame = offset, frame

    def __repr__(self):
        return '<Packet cmd=%d uid=%d sn=%d frame=%d %dB>' % (
            self.cmd, self.uid, self.sn, self.frame, len(self.payload))


def room_starts(d, replay_id=None):
    """定位所有 Room 帧起点（房号字段 09 <房号 fixed64> 的位置）。

    房名不固定：匹配局是 "match"，自定义房是玩家自取的名字（如「胖摩西」），
    所以帧起点按房号匹配，而不是按房名。
    回放号 = 时间戳(10位) + 房号(6位)，故优先用房号后 6 位；
    拿不到回放号时，取文件里第一个「12 <len> <不含 NUL 的房名>」候选的房号；
    都不行再退回老结构特征 ANCHOR。
    """
    if isinstance(replay_id, str) and replay_id.isdigit():
        replay_id = int(replay_id)
    key = None
    if isinstance(replay_id, int) and replay_id >= 0:
        k0 = (replay_id % 1000000).to_bytes(8, 'little')
        if d.find(b'\x09' + k0 + b'\x12') >= 0:
            key = k0
    if key is None:
        for m in re.finditer(rb'\x09.{8}\x12', d, re.S):
            s = m.start()
            ln = d[s + 10]
            if 1 <= ln <= 64 and s + 11 + ln < len(d) and b'\x00' not in d[s + 11:s + 11 + ln]:
                key = d[s + 1:s + 9]
                break
    if key is not None:
        pat = b'\x09' + key + b'\x12'
        out, i = [], 0
        while True:
            i = d.find(pat, i)
            if i < 0:
                break
            out.append(i)
            i += 1
        if len(out) >= 3:
            return out
    out = []
    for m in re.finditer(re.escape(ANCHOR), d):
        s = m.start() - 9
        if s >= 1 and d[s] == 0x09:               # Room 以 09 <房号 fixed64> 开头
            out.append(s)
    return out


class Replay:
    def __init__(self, src, replay_id=None):
        if isinstance(src, (bytes, bytearray)):
            self.data = bytes(src)
        else:
            p = Path(src)
            self.data = p.read_bytes()
            replay_id = int(p.stem) if p.stem.isdigit() else p.stem
        self.replay_id = replay_id
        self.frames = []           # [{'index','offset','room','hero_round':{uid:round}}]
        self.players = {}          # uid -> {'nick','hero_id','slot'}
        self.packets = []
        self._parse_frames()
        self._parse_packets()

    # ── 帧 ────────────────────────────────────────────────
    def _room_starts(self):
        """帧起点（房号字段位置），房名与房号是否 "match" 无关 —— 见 room_starts()"""
        return room_starts(self.data, self.replay_id)

    def _parse_frames(self):
        RoomMsg = msg_class('model.Room')
        d = self.data
        starts = self._room_starts()
        for idx, s in enumerate(starts):
            ln = None
            for back in (3, 2, 1, 4, 5):
                p = s - back
                if p >= 1 and d[p] == 0x12:
                    v, after = _rv(d, p + 1)
                    if v and after == s:
                        ln = v
                        break
            if not ln:
                continue
            try:
                room = RoomMsg()
                room.ParseFromString(d[s:s + ln])
            except Exception:
                continue
            hround = {}
            for pl in room.players:
                hround[pl.id] = pl.hero.round
                if pl.id not in self.players:
                    self.players[pl.id] = {
                        'nick': getattr(pl, 'nick', '') or '',
                        'hero_id': pl.hero.hero_id,
                        'slot': getattr(pl, 'slot', 0),
                        # 玩家等级：Room 帧 model.Player#25（回放里真实带着，例如 27 级）
                        'level': int(getattr(pl, 'level', 0) or 0),
                        # 立绘/皮肤 ID：100 + <角色3位> + <皮肤序号3位>（例 100123001）
                        'skin': int(getattr(pl.hero, 'standingPainting', 0) or 0),
                    }
            self.frames.append({'index': idx, 'offset': s, 'room': room, 'hero_round': hround})

    @property
    def frame_count(self):
        return len(self.frames)

    def _frame_of(self, offset):
        lo, hi, ans = 0, len(self.frames) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.frames[mid]['offset'] <= offset:
                ans = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    # ── 协议包日志 ─────────────────────────────────────────
    def _parse_packets(self):
        d, n = self.data, len(self.data)
        for i in range(n - 5):
            if d[i] != 0x0d:
                continue
            cmd = struct.unpack_from('<I', d, i + 1)[0]
            if not (1000 <= cmd <= 5399):
                continue
            for back in range(1, 72):
                p = i - back
                if p < 1:
                    break
                if d[p] != 0x12:
                    continue
                ln, after = _rv(d, p + 1)
                if not (ln and after + ln > i and after <= i + 1):
                    continue
                rec = pb(d[after:after + ln])
                pay = rec.get(3)
                self.packets.append(Packet(cmd, pb_int(rec, 2), pay[0] if pay else b'',
                                           pb_int(rec, 4), after, self._frame_of(after)))
                break

    def packets_at(self, cmd, frame):
        return [p for p in self.packets if p.cmd == cmd and p.frame == frame]

    def packets_of(self, uid):
        return [p for p in self.packets if p.uid == uid]

    # ── 地图 ──────────────────────────────────────────────
    @property
    def map_id(self):
        if not self.frames:
            return None
        room = self.frames[0]['room']
        for f in room.DESCRIPTOR.fields:
            if f.number == 5 and f.type == 7:            # fixed32
                v = getattr(room, f.name)
                if v:
                    return v
        return None

    # ── 筹码 ──────────────────────────────────────────────
    def chips(self, uid):
        """{筹码ID: 首次出现的帧号}（buff_id = 500<筹码ID>01）"""
        out = {}
        for fi, fr in enumerate(self.frames):
            for pl in fr['room'].players:
                if pl.id != uid:
                    continue
                for _, b in pl.hero.buffs.items():
                    bid = b.buff_id
                    if 5000101 <= bid <= 5009301 and bid % 100 == 1:
                        cid = (bid - 1) // 100
                        if CHIP_MIN <= cid <= CHIP_MAX:
                            out.setdefault(cid, fi)
        return out

    def all_chips(self):
        return {u: self.chips(u) for u in self.players}

    # ── 战绩快照 ───────────────────────────────────────────
    def cond_snapshots(self, uid):
        """{显示轮次(1基): (帧号, {字段: 值})} —— 每轮取最后一个非空快照

        英雄的 round 从 -1（开局）开始；-1 与 0 都算第 1 轮。
        """
        snaps = {}
        for fi, fr in enumerate(self.frames):
            for pl in fr['room'].players:
                if pl.id != uid:
                    continue
                c = pl.hero.cond
                vals = {k: getattr(c, k) for k in COND_FIELDS if hasattr(c, k)}
                if not any(v for k, v in vals.items() if k != 'initGoldCount'):
                    continue
                rnd = max(pl.hero.round, 0) + 1
                snaps[rnd] = (fi, vals)          # 后面的帧覆盖前面的 → 留最后一个
        return snaps

    def hero_state(self, uid):
        """{显示轮次: {'buyRelicNum': n, 're_roll_num': n}}"""
        out = {}
        for fi, fr in enumerate(self.frames):
            for pl in fr['room'].players:
                if pl.id != uid:
                    continue
                out[max(pl.hero.round, 0) + 1] = {
                    'buyRelicNum': pl.hero.buyRelicNum,
                    're_roll_num': pl.hero.re_roll_num,
                }
        return out

    def mission_states(self):
        """每帧的 {mission_id: 状态}（1=未完成, 2=已完成）"""
        out = []
        for fr in self.frames:
            out.append({m.mission_id: m.mission_state for m in fr['room'].mapMissions})
        return out

    def round_count(self):
        mx = 0
        for fr in self.frames:
            for pl in fr['room'].players:
                mx = max(mx, max(pl.hero.round, 0) + 1)
        return mx


def parse_replay(src, replay_id=None):
    return Replay(src, replay_id)


# ── 筹码名表 ──────────────────────────────────────────────
_CHIPS = None


def chip_table():
    """{ID: {'name','quality','desc','tag'}}（assets/data/chips.json）"""
    global _CHIPS
    if _CHIPS is None:
        import json
        from astral.paths import asset
        try:
            raw = json.loads(Path(asset('assets', 'data', 'chips.json')).read_text(encoding='utf-8'))
            _CHIPS = {int(c['id']): c for c in raw}
        except Exception:
            _CHIPS = {}
    return _CHIPS


_BUFFS = None


def buff_table():
    """{ID: {'name','desc'}}（assets/data/buffs.json，来自游戏 STRBuff 表）"""
    global _BUFFS
    if _BUFFS is None:
        import json
        from astral.paths import asset
        try:
            raw = json.loads(Path(asset('assets', 'data', 'buffs.json')).read_text(encoding='utf-8'))
            _BUFFS = {int(k): v for k, v in raw.items()}
        except Exception:
            _BUFFS = {}
    return _BUFFS


def status_info(bid):
    """状态/增益的显示信息 {name, desc, from}；查不到返回 None。

    筹码自带的状态 buff_id = 筹码ID * 100 + 1（例：筹码 50001 → 5000101），
    这类状态游戏里往往没有单独文案，回到筹码自己的名字与效果。
    其余（技能/被动状态）查游戏 STRBuff 表；没有名字键的用「状态 <id>」兜底。
    """
    try:
        bid = int(bid)
    except (TypeError, ValueError):
        return None
    if 5000101 <= bid <= 5009301 and bid % 100 == 1:
        cid = (bid - 1) // 100
        c = chip_table().get(cid)
        if c:
            return {'name': c.get('name') or ('筹码 %d' % cid),
                    'desc': c.get('desc') or '', 'from': 'chip', 'chip_id': cid}
    b = buff_table().get(bid)
    if b and (b.get('name') or b.get('desc')):
        return {'name': b.get('name') or ('状态 %d' % bid),
                'desc': b.get('desc') or '', 'from': 'buff'}
    return None


_MONSTERS = None


def monster_table():
    """{种类ID: {'name','desc'}}（assets/data/monsters.json，来自游戏 STRMonster 表）"""
    global _MONSTERS
    if _MONSTERS is None:
        import json
        from astral.paths import asset
        try:
            raw = json.loads(Path(asset('assets', 'data', 'monsters.json')).read_text(encoding='utf-8'))
            _MONSTERS = {int(k): v for k, v in raw.items()}
        except Exception:
            _MONSTERS = {}
    return _MONSTERS


def monster_info(mid):
    """怪物的显示信息 {name, desc}；查不到返回 None。

    种类 ID 来自回放里的 monster.hero.hero_id；名字在游戏 STRMonster 表的 <id>*10(+0/1/2)。
    """
    try:
        mid = int(mid)
    except (TypeError, ValueError):
        return None
    return monster_table().get(mid)


def monster_name(mid):
    """怪物名（查不到回退成「怪物 <id>」）"""
    info = monster_info(mid)
    return (info or {}).get('name') or ('怪物 %s' % (mid,))


def chip_name(cid):
    return chip_table().get(cid, {}).get('name', str(cid))


def chip_quality(cid):
    return chip_table().get(cid, {}).get('quality', '')
