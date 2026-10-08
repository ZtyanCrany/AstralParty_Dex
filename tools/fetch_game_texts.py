# -*- coding: utf-8 -*-
"""从游戏本体导入本地化文本表（只读，不动游戏文件）。

游戏把配置和文案打包成 TextAsset 放在 Addressables 缓存里，本脚本用 UnityPy
（只读载入）把它们整表导出成 JSON，供后续与仓库内的数据表比对 / 刷新。

用法：
    python tools/fetch_game_texts.py                    # 全量导出
    python tools/fetch_game_texts.py --table STRCard    # 只导一张表
    python tools/fetch_game_texts.py --list             # 只列出表名与条数
    python tools/fetch_game_texts.py --check            # 只跑校验锚点
    python tools/fetch_game_texts.py --root <游戏数据目录>

输出：assets/data/game_texts.json
      {"source": {...}, "tables": {"STRCard": {"键": "文本", ...}, ...}}

记录格式（STR* 表一致）：
    0x0A <记录长 varint> 0x0D <键 小端4字节> 0x12 <串长 varint> <UTF-8 文本>
卡牌描述表的键 = 卡号 * 10 + 序号（例：20011 的描述键是 200111）。
"""
import argparse
import io
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / 'assets' / 'data' / 'game_texts.json'

# 定位配置包的标记：卡牌美术资源名前缀，随内容更新极不易变
MARKER = b'UT_HandCard_'
# 兜底标记（万一美术前缀改名）
FALLBACK_MARKERS = [b'UT_Relic_', b'UT_Skin_', b'\xe8\xbd\xa8\xe9\x81\x93\xe7\x82\xae']  # 「轨道炮」

# 校验锚点：键 -> 期望文本（这些是人工核对过的权威值）
ANCHORS = {
    'STRCard': {'21012': '对怪激光', '21013': '对怪板砖', '20013': '板砖',
                '20011': '方向抉择', '200111': '自由选择移动方向'},
}


def game_root(arg=None):
    if arg:
        return Path(arg)
    prof = os.environ.get('USERPROFILE') or str(Path.home())
    return Path(prof) / 'AppData' / 'LocalLow' / 'feimo' / 'AstralParty_CN'


def find_bundles(root, markers, limit_bytes=400 * 1024 * 1024):
    """在 Addressables 缓存里找出含配置 TextAsset 的包文件（按内容找，不认哈希名）。"""
    base = root / 'com.unity.addressables' / 'AssetBundles'
    if not base.exists():
        return []
    hits = []
    for f in base.rglob('__data'):
        try:
            if not f.is_file() or f.stat().st_size > limit_bytes:
                continue
            blob = f.read_bytes()
        except OSError:
            continue
        for m in markers:
            if m in blob:
                hits.append(f)
                break
    return hits


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


def parse_records(buf):
    """把一份 TextAsset 的字节解析成 {键: 文本}。

    结构：0x0A <记录长> 0x0D <键 LE32> 0x12 <串长> <UTF-8 文本>
    键有两种来源：紧随 0x0D 的 4 字节；若记录里没有 0x0D，则用记录出现次序占位。
    """
    out, order = {}, 0
    i, n = 0, len(buf)
    while i < n - 2:
        if buf[i] != 0x0A:
            i += 1
            continue
        ln, j = _varint(buf, i + 1)
        if ln is None or ln < 4 or j + ln > n:
            i += 1
            continue
        body = buf[j:j + ln]
        key, k = None, None
        if body[:1] == b'\x0d' and len(body) >= 5:
            key = int.from_bytes(body[1:5], 'little')
            k = 5
        # 在记录里找 0x12 <串长> <文本>
        p = k if k is not None else 0
        while p < len(body):
            if body[p] != 0x12:
                p += 1
                continue
            sl, q = _varint(body, p + 1)
            if sl is None or sl < 1 or q + sl > len(body):
                p += 1
                continue
            try:
                txt = body[q:q + sl].decode('utf-8')
            except UnicodeDecodeError:
                p += 1
                continue
            if txt and not any(ord(c) < 0x20 for c in txt):
                if key is None:
                    order += 1
                    key = order
                out[str(key)] = txt
            break
        i = j + ln
    return out


def load_tables(bundles, only=None):
    try:
        import UnityPy
    except ImportError:
        print('✗ 缺少 UnityPy：pip install UnityPy')
        sys.exit(2)
    tables = {}
    for b in bundles:
        try:
            env = UnityPy.load(str(b))
        except Exception as e:
            print('  ✗ 载入失败 %s: %s' % (b, e))
            continue
        for o in env.objects:
            if o.type.name != 'TextAsset':
                continue
            d = o.read()
            name = getattr(d, 'm_Name', '') or ''
            if not name or (only and name != only):
                continue
            raw = getattr(d, 'm_Script', b'')
            buf = raw.encode('utf-8', 'surrogateescape') if isinstance(raw, str) else raw
            rec = parse_records(buf)
            if rec:
                tables.setdefault(name, {}).update(rec)
    return tables


def check(tables):
    ok = True
    for tname, want in ANCHORS.items():
        got = tables.get(tname)
        if got is None:
            print('⚠️ 缺表 %s' % tname)
            ok = False
            continue
        for k, v in want.items():
            actual = got.get(k)
            flag = '✓' if actual == v else '✗'
            if actual != v:
                ok = False
            print('   %s %s[%s] = %r (期望 %r)' % (flag, tname, k, actual, v))
    return ok


def main():
    ap = argparse.ArgumentParser(description='从游戏本体导出本地化文本表（只读）')
    ap.add_argument('--table', help='只导出指定表名（如 STRCard）')
    ap.add_argument('--list', action='store_true', help='只列出表名与条数')
    ap.add_argument('--check', action='store_true', help='只跑校验锚点')
    ap.add_argument('--root', help='游戏数据目录（默认 %LOCALAPPDATA%Low/feimo/AstralParty_CN）')
    args = ap.parse_args()

    root = game_root(args.root)
    print('游戏数据目录：%s' % root)
    if not root.exists():
        print('✗ 目录不存在')
        return 2

    print('定位配置包…')
    bundles = find_bundles(root, [MARKER] + FALLBACK_MARKERS)
    if not bundles:
        print('✗ 没找到配置包（游戏未下载过配置？）')
        return 2
    for b in bundles:
        print('   %s  (%.1f MB)' % (b.relative_to(root), b.stat().st_size / 1024 / 1024))

    print('解析 TextAsset…')
    tables = load_tables(bundles, only=args.table)
    print('   表数 %d，条目合计 %d' % (len(tables), sum(len(v) for v in tables.values())))

    if args.list:
        for name in sorted(tables, key=lambda t: -len(tables[t])):
            print('   %-28s %6d 条' % (name, len(tables[name])))
        return 0

    if args.check:
        return 0 if check(tables) else 1

    print('校验锚点：')
    ok = check(tables)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'source': {
            'root': str(root),
            'bundles': [str(b) for b in bundles],
            'marker': MARKER.decode('ascii'),
        },
        'tables': tables,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    print('→ 已写出 %s（%d 张表）' % (OUT.relative_to(HERE), len(tables)))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
