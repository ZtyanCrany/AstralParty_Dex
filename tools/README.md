# tools

`assets/data/*.json` 里各张对照表的**溯源脚本**，以及打包、发版与回归校验工具。
都是离线工具，按需手动运行：

```bash
pip install -r requirements.txt
python tools/fetch_wiki_missions.py        # 示例
```

| 脚本 | 用途 |
|---|---|
| `build_exe.py` | 一键打包成 Windows 免安装版（onedir）。 |
| `fetch_game_avatars.py` | 从游戏热更新缓存里提取角色头像 → assets/avatars/<角色ID>.png |
| `fetch_hero_photos.py` | 从游戏热更新缓存里提取角色「立绘头像」UT_Hero_ProfilePhoto_* → assets/photos/ |
| `fetch_wiki_missions.py` | 抓 Wiki 各张地图的「地图任务」列表 → assets/data/missions.json |
| `make_release.py` | 把 dist/星趴档案 打成免安装 zip（vX.Y.Z_win64）。 |
| `verify_missions.py` | 交叉验证：每局回放的地图 + 任务ID个数 是否与 Wiki 的任务列表对上 |
| `verify_review_invariants.py` | 复盘数据不变量校验（覆盖 11 局全部玩家） |

---

`fetch_wiki_missions.py` 抓取原始任务表，`fetch_hero_photos.py` / `fetch_game_avatars.py` 提取美术资源
（运行时仍会从本机游戏重新提取，仓库不分发官方美术），`verify_missions.py` / `verify_review_invariants.py`
用于回归校验，`build_exe.py` / `make_release.py` 负责打包发版。
