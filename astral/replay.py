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

ANCHOR = b'\x12\x05match'          # Room#2 = 房名（固定 "match"），用作帧起点特征
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
        """帧起点 = "12 05 'match'" 的位置（Room#2 房名），且其前 9 字节须是 09 <房号 fixed64>"""
        d = self.data
        out = []
        for m in re.finditer(re.escape(ANCHOR), d):
            s = m.start() - 9
            if s >= 1 and d[s] == 0x09:               # Room 以 09 <房号 fixed64> 开头
                out.append(s)
        return out

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


def chip_name(cid):
    return chip_table().get(cid, {}).get('name', str(cid))


def chip_quality(cid):
    return chip_table().get(cid, {}).get('quality', '')
