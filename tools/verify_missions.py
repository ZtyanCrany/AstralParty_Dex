# -*- coding: utf-8 -*-
"""交叉验证：每局回放的地图 + 任务ID个数 是否与 Wiki 的任务列表对上"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('app', ROOT / 'main.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
from astral.replay import parse_replay      # noqa: E402

MISS = json.loads((ROOT / 'assets' / 'data' / 'missions.json').read_text(encoding='utf-8'))
RP = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / '_capture' / 'replays'

print('%-18s %-8s %-8s %-6s %-6s %s' % ('回放', '地图ID', '地图名', 'ID数', 'Wiki数', '任务ID'))
print('-' * 96)
ok = bad = 0
for f in sorted(RP.glob('*.bin'), key=lambda x: x.stem, reverse=True):
    data = f.read_bytes()
    try:
        mid = app.map_from_prefix(data)
    except Exception:
        mid = None
    rp = parse_replay(f)
    ids = sorted({m for st in rp.mission_states() for m in st})
    wiki = MISS.get(str(mid), {})
    wtasks = wiki.get('normal') or []
    name = wiki.get('name') or (app.map_name(mid) if mid else '?')
    flag = ''
    if wtasks:
        flag = '✓' if len(wtasks) == len(ids) else '✗ 对不上'
        ok += (len(wtasks) == len(ids))
        bad += (len(wtasks) != len(ids))
    else:
        flag = '? 无 Wiki 数据'
    print('%-18s %-8s %-10s %-6d %-6s %s  %s' % (
        f.stem, mid, name, len(ids), len(wtasks) or '-', ids, flag))
print('\n一致 %d 局 / 不一致 %d 局' % (ok, bad))
