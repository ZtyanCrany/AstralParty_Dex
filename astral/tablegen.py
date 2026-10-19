# -*- coding: utf-8 -*-
"""把客户端文案表（`{表名: {键: 文本}}`）转成工具用的小表。

卡牌 / 筹码 / 增益 / 怪物 / 角色 / 皮肤六类，生成规则只此一份：
CLI 同步脚本（`tools/sync_*_from_game.py`）与运行时数据层（`astral/game_live.py`）
都调用这里，避免两处规则漂移。

除文案表外可带**基线表**（仓库内的 `assets/data/*.json`）：客户端只提供名字与
文案，类型 / 品质 / 英文名等字段沿用基线；基线没有的条目按号段推断或从同族继承。

`gen_*` 均为纯函数，返回 `(结果, 报告)`；报告供 CLI 打印差异与运行时记录日志。
"""
import re

PH = re.compile(r'\{[A-Za-z_0-9]+=([^}]*)\}')
COLOR = re.compile(r'\[/?(?:color|b|i|size|u)(?:=[^\]]*)?\]', re.I)
ASTRAL = re.compile(r'\[/?(?:Astral)=[^\]]*\]', re.I)
IMG = re.compile(r"<img[^>]*>", re.I)
WIKIFILE = re.compile(r'\[\[file:[^\]]*\]\]', re.I)
TAG = re.compile(r'<[^>]+>')

CARD_LO, CARD_HI = 10000, 99999        # 卡号号段
REL_LO, REL_HI = 50001, 50093          # 筹码号段


def clean(tpl):
    """游戏文案 → 界面纯文本：参数占位填成值（`{damage=2}` → `2`），去掉富文本标记。"""
    s = PH.sub(lambda m: m.group(1), str(tpl))
    s = IMG.sub('', s)
    s = WIKIFILE.sub('', s)
    s = ASTRAL.sub('', s)
    s = COLOR.sub('', s)
    s = TAG.sub('', s)
    s = s.replace('\u3000', ' ')
    return re.sub(r'[ \t]{2,}', ' ', s).strip()


def series_mode(cid):
    """卡牌模式按号段推断：20xxx=PVP、21xxx=PVE、1xxxx=通用。"""
    if 20000 <= cid < 21000:
        return 'PVP'
    if 21000 <= cid < 22000:
        return 'PVE'
    return '通用'


def family_of(name):
    """「手电筒-一般」→「手电筒」；「时间怀表(破碎)」→「时间怀表」"""
    return re.split(r'[-–(（]', str(name or ''), 1)[0].strip()


def _int_keys(table):
    out = {}
    for k, v in (table or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


# ── 卡牌 ────────────────────────────────────────────────────────

# Card 配置表（TextAsset 'Card'）的 field 9 就是卡牌的「大类」，也就是游戏里左上角那个
#   小徽标：攻/防/效/反/咒/建。STRCard 里只有名字和效果文字，类型只在这里。
#   实测：10001 攻击=1、10002 防御=2、20003 保护屏障(反击)=4、
#         21025 罪证嫌疑(诅咒)=8、21026 疾行菇 / 21027 仙品杨桃(建筑)=9。
CARD_KIND = {1: 'attack', 2: 'defend', 3: 'effect', 4: 'counter', 8: 'curse', 9: 'build'}

# 老数据里的写法（大写 / 历史拼法）统一成小写，前端按一份表上色
_CARD_TYPE_NORM = {'attack': 'attack', 'defend': 'defend', 'effect': 'effect',
                   'counter': 'counter', 'curse': 'curse', 'build': 'build'}


def card_kinds(card_bytes):
    """从 Card 表解出 {卡号: 类型}；拿不到就返回空 dict（退化成老做法，不报错）。"""
    out = {}
    if not card_bytes:
        return out
    for cid, body in _pb_records(card_bytes).items():
        for f, wt, v in _pb_fields(body):
            if f == 9 and wt == 0:
                kind = CARD_KIND.get(v)
                if kind:
                    out[cid] = kind
                break
    return out


def gen_cards(tables, base=None, card_bytes=None):
    """STRCard：键=卡号→牌名；键=卡号*10+序号→效果文字（取序号 1）。"""
    scard = _int_keys(tables.get('STRCard'))
    names, descs = {}, {}
    for kk, v in scard.items():
        if CARD_LO <= kk <= CARD_HI:
            names[kk] = v
        else:
            head, seq = kk // 10, kk % 10
            if CARD_LO <= head <= CARD_HI:
                descs.setdefault(head, {})[seq] = v

    kinds = card_kinds(card_bytes)
    # 建筑牌的效果文字不在 STRCard 里：STRBuild 里 <id> 是名字、<id*10+1> 是效果，
    #   只能用名字把两边对上（Card 表里没有直接指向 Build 的外键）。
    build_desc = {}
    sbuild = _int_keys(tables.get('STRBuild'))
    for k, txt in sbuild.items():
        if k % 10 == 1 and (k - 1) // 10 in sbuild:
            nm = str(sbuild[(k - 1) // 10] or '').strip()
            if nm and txt:
                build_desc[nm] = clean(txt)
    cards = [dict(c) for c in (base or [])]
    for c in cards:                      # 老表里是 Attack/Defend/... ⇒ 统一小写
        t = str(c.get('type') or '').strip().lower()
        if t in _CARD_TYPE_NORM:
            c['type'] = t
    byid = {}
    for c in cards:
        try:
            byid[int(c['id'])] = c
        except (KeyError, TypeError, ValueError):
            continue

    renamed, retexted, added = [], [], []
    for cid in sorted(names):
        gname = names[cid]
        gdesc = clean(descs[cid][1]) if descs.get(cid, {}).get(1) else ''
        c = byid.get(cid)
        if c is None:
            row = {'id': cid, 'name': gname, 'type': kinds.get(cid, 'effect'), 'target': 'Entity',
                   'desc': gdesc, 'mode': series_mode(cid), 'from': '基础卡牌'}
            cards.append(row)
            byid[cid] = row
            added.append((cid, gname, gdesc))
            continue
        if c.get('name') != gname:
            renamed.append((cid, c.get('name'), gname))
            c['name'] = gname
        if gdesc and c.get('desc') != gdesc:
            retexted.append((cid, gname, c.get('desc', ''), gdesc))
            c['desc'] = gdesc
        if cid in kinds:
            c['type'] = kinds[cid]
        if not (c.get('desc') or '').strip() and c['type'] == 'build':
            c['desc'] = build_desc.get(str(c.get('name') or '').strip(), '')
    extra = [(cid, byid[cid].get('name')) for cid in sorted(set(byid) - set(names))]
    cards.sort(key=lambda c: int(c['id']))
    return cards, {'names': len(names), 'descs': len(descs), 'renamed': renamed,
                   'retexted': retexted, 'added': added, 'extra': extra,
                   'kinds': len(kinds)}


# ── 筹码 ────────────────────────────────────────────────────────
def gen_chips(tables, base=None):
    """STRRelic：50000+n → 名字；(50000+n)*10 → 效果文字。"""
    sr = _int_keys(tables.get('STRRelic'))
    names = {k: v for k, v in sr.items() if REL_LO <= k <= REL_HI}
    descs = {k: v for k, v in sr.items() if REL_LO * 10 <= k <= (REL_HI + 1) * 10}

    chips = [dict(c) for c in (base or [])]
    byid = {}
    for c in chips:
        try:
            byid[int(c['id'])] = c
        except (KeyError, TypeError, ValueError):
            continue

    renamed, retexted, added = [], [], []
    for cid in sorted(names):
        gname = names[cid]
        raw = descs.get(cid * 10, '')
        gdesc = clean(raw) if raw else ''
        c = byid.get(cid)
        if c is None:
            fam = family_of(gname)
            sib = next((x for x in chips if family_of(x.get('name')) == fam), None)
            row = {'id': cid, 'name': gname,
                   'quality': sib.get('quality', '') if sib else '',
                   'tag': sib.get('tag', '') if sib else '',
                   'tag_full': sib.get('tag_full', '') if sib else '',
                   'desc': gdesc,
                   'style': sib.get('style', '') if sib else '',
                   'map_limit': list(sib.get('map_limit', [])) if sib else []}
            cards_sib = sib
            chips.append(row)
            byid[cid] = row
            added.append((cid, gname, gdesc, cards_sib))
            continue
        if c.get('name') != gname:
            renamed.append((cid, c.get('name'), gname))
            c['name'] = gname
        if gdesc and c.get('desc') != gdesc:
            retexted.append((cid, gname, c.get('desc', ''), gdesc))
            c['desc'] = gdesc
    extra = [(cid, byid[cid].get('name')) for cid in sorted(set(byid) - set(names))]
    chips.sort(key=lambda c: int(c['id']))
    return chips, {'names': len(names), 'descs': descs, 'renamed': renamed,
                   'retexted': retexted, 'added': added, 'extra': extra}


# ── 增益 ────────────────────────────────────────────────────────
def gen_buffs(tables, base=None):
    """STRBuff：<id> → 效果；<id>*10+1 → 名字（筹码增益 id = 筹码ID*100+1）。"""
    sb = _int_keys(tables.get('STRBuff'))

    def is_name_key(k):
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
    out = {str(k): rows[k] for k in sorted(rows)}
    rep = {'keys': len(sb), 'rows': len(rows),
           'name_keys': sum(1 for k in sb if is_name_key(k)),
           'chip_buffs': sum(1 for k in rows if 5000101 <= k <= 5009301),
           'only_desc': sum(1 for v in rows.values() if v['desc'] and not v['name']),
           'only_name': sum(1 for v in rows.values() if v['name'] and not v['desc'])}
    return out, rep


# ── 怪物 ────────────────────────────────────────────────────────
def gen_monsters(tables, base=None):
    """STRMonster：<种类id>*10+0/1/2 → 名字（取第一条非空）；*10+3 → 图鉴描述。

    <10000 的键是角色台词，跳过。
    """
    sm = _int_keys(tables.get('STRMonster'))
    rows = {}
    for k, txt in sm.items():
        if k < 10000:
            continue
        head, tail = k // 10, k % 10
        if head < 1000:
            continue
        r = rows.setdefault(head, {'name': '', 'desc': ''})
        body = clean(txt)
        if not body:
            continue
        if tail == 3:
            if not r['desc']:
                r['desc'] = body
        elif not r['name']:
            r['name'] = body
    rows = {k: v for k, v in rows.items() if v['name'] or v['desc']}
    out = {str(k): rows[k] for k in sorted(rows)}
    rep = {'keys': len(sm), 'rows': len(rows),
           'named': sum(1 for v in rows.values() if v['name']),
           'described': sum(1 for v in rows.values() if v['desc']),
           'chatter': sum(1 for k in sm if k < 10000)}
    return out, rep


# ── 地图 ────────────────────────────────────────────────────────
MAP_MIN, MAP_MAX = 80000, 89999      # 回放前缀里的地图 ID 号段
MAP_PLACEHOLDER = re.compile(r'^(?:Map_\d+|测试地图)$')


def is_map_id(key):
    """STRMap 里的键是不是地图 ID（5 位、8 开头）。"""
    return MAP_MIN <= key <= MAP_MAX


def gen_missions(tables, base=None):
    """STRMap：`8xxxx` → 地图名，补进 `assets/data/missions.json`。

    missions.json 除地图名外还带每张图的任务目标（normal/hard），那部分文案表里没有
    （目标由 STRPVEMission 模板加怪物名拼成），只能沿用基线；本函数只补充客户端新增的
    地图，既有条目原样保留、不改名。

    排除占位名（`Map_001`、`测试地图`）、基线已有的 ID、以及名字已被占用的 ID ——
    地图改版后旧 ID 仍留在表里，加进来会出现同名两行。

    返回 (合并后的表, 报告)。
    """
    smap = _int_keys(tables.get('STRMap'))
    out = dict(base or {})
    used = {str(v.get('name')) for v in out.values() if isinstance(v, dict)}
    skipped, added = [], {}
    for k in sorted(smap):
        if not is_map_id(k):
            continue
        name = clean(smap[k]).strip()
        if not name or MAP_PLACEHOLDER.match(name):
            continue
        if str(k) in out or name in used:
            skipped.append(str(k))
            continue
        added[str(k)] = {'name': name, 'normal': [], 'hard': [], 'title': name}
        used.add(name)
    out.update(added)
    out = {k: out[k] for k in sorted(out, key=lambda x: (len(x), x))}
    rep = {'keys': len(smap), 'maps': sum(1 for k in smap if is_map_id(k)),
           'added': added, 'skipped': skipped, 'total': len(out)}
    return out, rep


# ── 角色 ────────────────────────────────────────────────────────
def char_tables(tables, wiki=None):
    """角色 ID → {name, title}。

    名字 / 称号来自客户端 STRCharacter：`<ID>0` = 名字、`<ID>1` = 称号
    （1170=茉莉 / 1171=家政机器人）。客户端没有的角色用 Wiki 的 title 补。
    """
    out = {}
    for k, v in (tables.get('STRCharacter') or {}).items():
        m = re.fullmatch(r'(\d{3})([01])', str(k))
        if not m:
            continue
        rec = out.setdefault(int(m.group(1)), {})
        if m.group(2) == '0':
            rec['name'] = v
        else:
            rec['title'] = v
    for nm, v in (wiki or {}).items():
        try:
            cid = int(v.get('id') or 0)
        except (TypeError, ValueError):
            continue
        if not cid:
            continue
        rec = out.setdefault(cid, {})
        rec.setdefault('name', nm)
        if v.get('title'):
            rec.setdefault('title', v['title'])
    return out


def skin_ids(tables):
    """{皮肤ID: 文本}（STRItem 里 100+角色3位+序号3位）。"""
    out = {}
    for k, v in (tables.get('STRItem') or {}).items():
        if re.fullmatch(r'100\d{6}', str(k)) and isinstance(v, str) and v:
            out[int(k)] = v
    return out


def resolve_char(suffix, chars):
    """把皮肤名尾缀（如「糖糖」「星魅琉华」「琉华」「枫」）归到角色 ID。

    客户端四种写法都出现过：① 角色称号（糖糖 / 超天酱，联动皮肤用这种）
    ② 角色全名（茉莉 / 姬梦枫）③ 名字简称（枫 / 雪莉）④ 带姓全名（龙崎美咲）。
    按 ①→④ 依次匹配，同一级命中多个时取最长者；仍分不清则返回 None，留空比归错角色好。
    """
    if not suffix:
        return None
    for rule in (0, 1, 2, 3):
        hits = []
        for cid, rec in chars.items():
            nm = rec.get('name') or ''
            ti = rec.get('title') or ''
            if rule == 0 and ti == suffix:
                hits.append((len(ti), cid))
            elif rule == 1 and nm == suffix:
                hits.append((len(nm), cid))
            elif rule == 2 and len(nm) >= 2 and suffix.endswith(nm):
                hits.append((len(nm), cid))
            elif rule == 3 and nm.endswith(suffix):
                hits.append((len(suffix), cid))
        if len(hits) == 1:
            return hits[0][1]
        if len(hits) > 1:
            hits.sort(reverse=True)
            return hits[0][1] if hits[0][0] > hits[1][0] else None
    return None


def gen_skins(tables, wiki=None):
    """皮肤清单：角色 → 槽号 → 名（槽 1=初始、2=羁绊或联动名皮、3+=名皮）。

    另外给出共用号段的归属表 item_slots（联动 100301002 属糖糖）。
    """
    chars = char_tables(tables, wiki)
    ids = skin_ids(tables)

    per, unresolved = {}, []
    for iid, text in sorted(ids.items()):
        suffix = text.rsplit('-', 1)[-1] if '-' in text else ''
        cid = resolve_char(suffix, chars)
        if cid is None:
            if '-' in text:
                unresolved.append((iid, text))
                continue
            cid = int(str(iid)[3:6])        # 没写角色名 ⇒ 按 ID 号段归属
        per.setdefault(cid, {})[iid] = text

    out_chars, item_slots = {}, {}
    for cid, m in sorted(per.items()):
        rec = chars.get(cid, {})
        slots, slot_iid, used = {}, {}, set()
        for iid, text in sorted(m.items()):
            if text.startswith('初始皮肤'):
                slots['1'], slot_iid['1'] = text, iid
                used.add(iid)
        for iid, text in sorted(m.items()):
            if text.startswith('羁绊皮肤'):
                slots['2'], slot_iid['2'] = text, iid
                used.add(iid)
        n = 2 if '2' not in slots else 3
        for iid, text in sorted(m.items()):
            if iid in used:
                continue
            while str(n) in slots:
                n += 1
            slots[str(n)], slot_iid[str(n)] = text, iid
            n += 1
        pretty = {}
        for s, text in slots.items():
            tail = text.rsplit('-', 1)[-1] if '-' in text else ''
            pretty[s] = text.rsplit('-', 1)[0] if (tail and resolve_char(tail, chars) == cid) else text
        out_chars[str(cid)] = {'name': rec.get('name') or ('角色#%d' % cid),
                               'title': rec.get('title') or '',
                               'slots': pretty}
        for s, iid in slot_iid.items():
            if int(str(iid)[3:6]) != cid:
                item_slots[str(iid)] = [cid, int(s)]

    payload = {'chars': out_chars, 'item_slots': item_slots,
               'unresolved': [[i, t] for i, t in unresolved]}
    named = sum(len([s for s in v['slots'] if int(s) >= 3]) for v in out_chars.values())
    rep = {'chars': len(out_chars), 'named': named, 'item_slots': len(item_slots),
           'unresolved': payload['unresolved']}
    return payload, rep


def gen_heroes(tables, base=None, wiki=None):
    """角色表：name / title / id / skins（客户端权威；en 等其它字段沿用基线）。

    skins 与皮肤清单同一套槽号：普通角色从槽 3 起，联动角色从槽 2 起。
    """
    chars = char_tables(tables, wiki)
    skin_payload, _ = gen_skins(tables, wiki)
    base_by_id = {}
    for h in (base or []):
        try:
            base_by_id[int(h['id'])] = h
        except (KeyError, TypeError, ValueError):
            continue
    out = []
    for cid, rec in sorted(chars.items()):
        old = base_by_id.get(cid) or {}
        sl = (skin_payload['chars'].get(str(cid)) or {}).get('slots') or {}
        has_bond = '2' in sl
        named = [sl[s] for s in sorted(sl, key=lambda x: int(x))
                 if int(s) >= (3 if has_bond else 2)]
        row = dict(old)
        row.update({'id': str(cid), 'name': rec.get('name') or old.get('name') or ('角色#%d' % cid),
                    'title': rec.get('title') or old.get('title') or '', 'skins': named})
        out.append(row)
    seen = {int(h['id']) for h in out}
    for h in (base or []):          # 客户端表里没有的角色原样保留
        try:
            if int(h['id']) not in seen:
                out.append(dict(h))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda h: int(h['id']))
    rep = {'chars': len(out), 'with_title': sum(1 for h in out if h.get('title')),
           'with_skins': sum(1 for h in out if h.get('skins'))}
    return out, rep


# ── 潜能 ──────────────────────────────
POTENTIAL_FIELD = 22        # 角色配置里「潜能」那一项


def _varint(buf, i):
    v, sh = 0, 0
    while i < len(buf):
        c = buf[i]
        i += 1
        v |= (c & 0x7F) << sh
        if not (c & 0x80):
            return v, i
        sh += 7
        if sh > 28:
            return None, i
    return None, i


def _pb_fields(buf):
    """protobuf wire 解码 → [(字段号, wire类型, 值)]；0/5 为整数，2 为 bytes。"""
    out, i, n = [], 0, len(buf)
    while i < n:
        tag, i = _varint(buf, i)
        if tag is None or tag == 0:
            break
        f, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(buf, i)
            if v is None:
                break
            out.append((f, 0, v))
        elif wt == 1:
            out.append((f, 1, int.from_bytes(buf[i:i + 8], 'little')))
            i += 8
        elif wt == 2:
            ln, j = _varint(buf, i)
            if ln is None or j + ln > n:
                break
            out.append((f, 2, buf[j:j + ln]))
            i = j + ln
        elif wt == 5:
            out.append((f, 5, int.from_bytes(buf[i:i + 4], 'little')))
            i += 4
        else:
            break
    return out


def _pb_records(buf):
    """记录框架同 parse_records，值不做文本解析 → {键: 键之后的记录体}。

    `Map` / `PVEMission` / `Character` 这类配置资产的值是二进制，parse_records 解不出来。
    """
    out, i, n = {}, 0, len(buf)
    while i < n - 4:
        if buf[i] != 0x0A:
            i += 1
            continue
        ln, j = _varint(buf, i + 1)
        if ln is None or ln < 5 or j + ln > n:
            i += 1
            continue
        body = buf[j:j + ln]
        if body[:1] == b'\x0d':
            out[int.from_bytes(body[1:5], 'little')] = body[5:]
        i = j + ln
    return out


def gen_potential(char_bytes, tables, base=None, wiki=None):
    """角色「潜能是否已开放」小表：{角色名: {'released': bool, ...}}。

    判据来自客户端 `Character` 资产：已开放潜能的角色比未开放的多一个 22 号字段
    （潜能配置）。该判据与 Wiki 抓来的角色逐一对上，只差 Wiki 滞后的角色
    （星魅琉华 119 在 3.3.0 开放）。输出沿用 potential.json 的形状（键 = 角色名），
    Wiki 带的其它字段原样保留。
    """
    out = {k: (dict(v) if isinstance(v, dict) else {'released': bool(v)})
           for k, v in (base or {}).items()}
    if not char_bytes:
        return out, {'chars': 0, 'changes': ['缺 Character 资产，潜能沿用 Wiki 数据']}
    chars = char_tables(tables, wiki)
    notes, hit = [], 0
    for cid, rec in sorted(_pb_records(char_bytes).items()):
        name = (chars.get(cid) or {}).get('name')
        if not name:
            continue
        opened = any(f == POTENTIAL_FIELD and wt == 2 for f, wt, _ in _pb_fields(rec))
        row = out.setdefault(name, {})
        if row.get('released') != opened:
            notes.append('%s %s→%s' % (name, row.get('released'), opened))
        row['released'] = opened
        row['source'] = 'client'
        hit += 1
    return out, {'chars': hit, 'changes': notes}
