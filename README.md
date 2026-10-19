# 星趴档案

**AstralParty_Dex** ｜ English README → [README.en.md](README.en.md)

> 适用于游戏作品《星引擎 Party》《Astral Party》（国服又名《吉星派对》）<br>
> 作者：Nemophila & ZytanCrany

一个简洁的自助小工具：登录账号，查看账号信息、皮肤和最近对局战绩，根据uid或回放号查询战局，
点开任意一局可逐位玩家看**逐轮战况简报、筹码拿取明细和每轮次手牌**。
所有数据在本地主机处理，游戏本体美术资源通过本机游戏资源获取。

---

## 🚀 快速开始



### 方式一：源码运行（推荐）

```bash
git clone https://github.com/ZtyanCrany/AstralParty_Dex.git
python -m pip install -r requirements.txt
python main.py
```

Windows可以直接**双击 `run_source.bat`**。

---

### 方式二：下载免安装版

1. Releases 下载 `AstralParty_Dex_vX.Y.Z_win64.zip`
2. 解压到任意位置
3. 双击 `星趴档案.exe`
4. 在主页面选择任意登录方式，第一次启动会在本机获取美术资源，有暂时的较大占用
5. 登录成功后会记住登录态，以后打开直接进入档案页

> **运行环境**：Windows 10 / 11 64 位。
> 界面依赖系统的 **WebView2 Runtime**（Win11 自带；Win10 一般随 Edge 一并安装）。
> 如果启动后白屏，前往微软官网安装 WebView2 Runtime 即可。

---

## ✨ 特性

| 页面 | 内容 |
| --- | --- |
| **登录页面** | 支持手机号+验证码/密码登录，支持Steam、Bilibili和TapTap渠道登录；可记住登录态实现自动登录 |
| **我的信息** | 等级、累计场次 / 胜场 / 胜率、拥有角色、皮肤数、获赞数、角色使用排行 |
| **我的皮肤** | 按角色列出全部皮肤，标注已拥有 / 未拥有、羁绊状态 |
| **最近对局** | 最近 10 局，展开看四位玩家的战绩表，点击玩家名查看每轮战况与拿取筹码明细 |
| **复盘查询** | 输入**回放号**或**玩家 UID** 可查：回放号看单局，UID 看该玩家最近10局对局 |
| **档案导出** | 一键生成 Markdown 档案（含角色使用排行 + 对局明细），可复制或保存为文件 |

### 🎲 对局复盘（v1.1.0 新增，v1.2.0 补上难度与等级）

在「最近对局」里**点击玩家名字**可弹出该玩家的复盘窗口：


- **轮次数据** —— 逐轮的**击杀 / 伤害 / 承伤 / 星币 / 步数**；该轮阵亡会在承伤后标「(×_×;)」
- **每轮手牌** —— 行 = 轮次、列 = 玩家，每格分「开局 / 获得 / 使用」；点玩家名字切到他的逐轮明细
- **筹码总览** —— 这局拿了哪些筹码
- **获取筹码明细** —— 按轮次分块，标明来源（ 任务完成 / 升星 / 商店购买 /  循环往复），
  展示三选一时选了哪件筹码、以及**刷新前后**出了哪些筹码

### 🔍 复盘查询（v1.2.0 新增）

侧边栏的「复盘查询」只有一个输入框，自动识别你要查什么：

- **回放号** —— 打开本局的完整记录，四位玩家的逐轮战况与筹码明细等都能展开
- **玩家UID** —— 列出该玩家最近10局，点任意一局可看复盘信息

---

## 📷 项目截图

**我的信息** —— 等级 / 场次 / 胜率 / 拥有角色皮肤 / 获赞 / 角色使用排行

![我的信息](screenshots/01-profile.png)

**最近对局** —— 最近 10 局，展开看四位玩家的完整战绩表

![最近对局](screenshots/02-matches.png)

**对局复盘** —— 在「最近对局」里**点击玩家名字**弹出

![复盘弹窗](screenshots/03-replay.png)

![复盘明细](screenshots/04-replay-detail.png)

**复盘查询** —— 输入回放号或玩家 UID，查单局或该玩家最近 10 局

![复盘查询](screenshots/07-query.png)

**我的皮肤** —— 按角色列出全部皮肤，标注已拥有 / 未拥有、羁绊状态

![我的皮肤](screenshots/05-skins.png)

**登录页面**

![登录页](screenshots/06-login.png)

---


## 📁 项目结构

```
星趴档案/
├─ main.py                  主程序（pywebview 入口；数据组装、导出、复盘缓存）
├─ run_source.bat           源码运行入口（pythonw 启动，不留控制台窗口）
├─ requirements.txt         依赖清单
├─ astral/                  核心库
│   ├─ paths.py             路径解析：区分只读资源与可写数据（源码 / 打包双模式）
│   ├─ log.py               统一落盘日志（打包后无控制台，异常写 userdata/log.txt）
│   ├─ client_ver.py        客户端版本号自动探测与缓存（握手要带 clientVer，过期会 err=10012）
│   ├─ sign.py              SDK 签名算法 + 发送验证码 / 登录
│   ├─ signkeys.py          渠道签名密钥（客户端内置常量，单独成文件以便移除）
│   ├─ sdk_login.py         登录态管理、握手包获取与全量档案解析
│   ├─ game_session.py      从本机渠道客户端读登录票（Steam 国服 / B站）
│   ├─ intl_login.py        国际服自助登录（复用本机客户端 mmkv 里的票）
│   ├─ client.py            游戏服务器协议客户端（帧封装 / 收发 / 路由）
│   ├─ frame.py             协议帧编解码（35 字节大端头 + protobuf 载荷）
│   ├─ keytable.py          客户端内置的 256 字节置换表（解密用）
│   ├─ proto_loader.py      从 assets/proto/*.pb 构造 protobuf 消息
│   ├─ replay.py            回放文件解析（房间快照流 + 协议包记录）
│   ├─ review.py            复盘构建（逐轮差分、筹码来源判定、三选一链、手牌）
│   ├─ tablegen.py          数据表生成规则（CLI 同步脚本与运行时共用同一份）
│   ├─ game_live.py         运行时数据层：启动时按本机客户端现抽配置表
│   └─ gameart.py           运行时从本机游戏资源包提取头像 / 立绘头像
├─ ui/app.html              前端界面（原生 HTML/CSS/JS，无框架）
├─ assets/
│   ├─ app.ico / app.png    应用图标
│   ├─ data/                只读数据表（角色 · 皮肤 · 潜能 · 地图任务 · 卡牌 · 筹码 · 增益 · 怪物）
│   └─ proto/               协议描述符（FileDescriptorProto）
├─ tools/                   数据管线与打包脚本
│   ├─ sync_*_from_game.py  按客户端现抽各类数据表（默认只报差异，--apply 才写）
│   ├─ fetch_*.py           拉取游戏文案 / 地图任务名 / 头像立绘
│   ├─ verify_*.py          数据表与复盘不变量校验
│   ├─ check_ui.js          界面自检（无 emoji、占位头像等）
│   └─ build_exe.py / make_release.py   打包 exe / 生成发布 zip
├─ screenshots/             README 截图
└─ userdata/                运行时生成：登录态、回放与复盘缓存、抽出的头像和数据表
```

## ⚠️ 免责声明

- 本项目仅供**学习与研究**使用，与游戏开发方、运营方无任何关联。
- 所有游戏数据、角色形象、名称的版权归原权利方所有。
- 请勿用于任何商业用途或大规模数据抓取；因使用本项目产生的任何后果由使用者自行承担。
- 若官方认为本项目不妥，请联系删除。
- `astral/signkeys.py` 是从游戏客户端提取的**渠道签名密钥**（属客户端常量，**不涉及任何用户凭据**）；删除该文件可停止分发。

---

## 🔧 构建

```bash
python -m pip install pyinstaller
python tools/build_exe.py       # → dist/星趴档案/星趴档案.exe（连同 _internal/ 一起拷走即可运行）
python tools/make_release.py    # → AstralParty_Dex_vX.Y.Z_win64.zip（版本号自动读 main.py 里的 VERSION）
```

`make_release.py` 打包 zip 前会**删掉产物里的 `userdata/`**，避免账号泄露。<br>
打包完成后脚本自动**开包复核**一遍，确认没有 `userdata/` / `token.json` 以及美术资源没有进包才放行；<br>
如果你手动在 `dist/星趴档案/` 里跑过 exe，它会生成 `userdata/`，记得重新打包或手动删掉。

### 排查（打包后没有控制台，异常不会显示）

程序会把头像提取的日志与异常写进 `<exe 旁>\userdata\log.txt`；另有一个自检入口：

```bash
dist\星趴档案\星趴档案.exe --selftest     # 逐项检查依赖导入 / 多进程 / 游戏资源定位 / 试扫 + 解码一张贴图
```

自检结果写进 `userdata\log.txt`（末尾一行 `问题: [...]`，为空即全部正常）。

---

## 📜 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

---

## 💛 致谢

- 数据源：官方客户端 吉星派对Wiki
