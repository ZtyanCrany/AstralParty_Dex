# AstralParty_Dex

**星趴档案** ｜ 中文 README → [README.md](README.md)

> For 《星引擎 Party》/《Astral Party》 (the CN server is titled 《吉星派对》)<br>
> Authors: **Nemophila & ZtyanCrany**

A small self-service tool: log in with your own account to browse your profile, skins and recent
matches, look a match up by replay id or a player by UID, then open any match to see **each
player's round-by-round summary, relic pickups and per-round hand cards**.
Everything is processed on your own machine; game artwork is read from the copy of the game
installed locally.

---

## 🚀 Quick start

### Option A — from source (recommended)

```bash
git clone https://github.com/ZtyanCrany/AstralParty_Dex.git
python -m pip install -r requirements.txt
python main.py
```

On Windows you can also simply **double-click `run_source.bat`**.

---

### Option B — prebuilt, no installation

1. Download `AstralParty_Dex_vX.Y.Z_win64.zip` from **Releases**
2. Extract it anywhere
3. Run `星趴档案.exe`
4. Pick any login method on the start page. The first launch extracts artwork from the local game,
   so it briefly uses a fair amount of disk
5. Your session is remembered, so later launches go straight to your profile

> **Requirements**: Windows 10 / 11 64-bit.
> The UI needs the system **WebView2 Runtime** (bundled with Win11; on Win10 it usually comes with Edge).
> If the window stays blank, install the WebView2 Runtime from Microsoft.

---

## ✨ Features

| Page | What you get |
| --- | --- |
| **Login** | Phone number with an SMS code or a password, plus Steam / Bilibili / TapTap channel logins; the session can be remembered for automatic login |
| **My profile** | Level, total matches / wins / win-rate, owned characters, skins, likes received, character usage ranking |
| **My skins** | Every skin of every character, marked owned / not owned, with bond status |
| **Recent matches** | The latest 10 matches; expand one for all four players' stats, click a player name for their per-round summary and relic pickups |
| **Replay lookup** | A **replay id** or a **player UID**: a replay id opens that one match, a UID lists that player's latest 10 matches |
| **Profile export** | One-click Markdown archive (character usage ranking + match details); copy it or save it to a file |

### 🎲 Match breakdown (added in v1.1.0, difficulty and level in v1.2.0)

**Click a player name** inside 「Recent matches」 to open that player's breakdown window:

- **Round stats** — per-round **kills / damage / damage taken / gold / steps**; a round in which they
  died is marked right after damage taken as「(×_×;)」
- **Per-round hand cards** — rows are rounds, columns are players, each cell split into
  *starting hand / gained / used*; click a player name to switch to their own round-by-round detail
- **Relic overview** — which relics were picked up in that match
- **Relic pickup log** — grouped by round with the source labelled (quest completion / star-up /
  shop purchase / cycle), showing which relic was taken out of the three choices and what came up
  **before and after a reroll**

### 🔍 Replay lookup (added in v1.2.0)

「Replay lookup」 in the sidebar has a single box that works out what you are asking for:

- **A replay id** — opens that one match in full: all four players' round-by-round stats and relic
  pickups can be expanded
- **A player UID** — lists that player's latest 10 matches; click any of them for the full breakdown

Lookups need **no login**, so you can pull up a friend's matches; every match is tagged with its
difficulty.

---

## 📷 Screenshots

**My profile** — level / matches / win-rate / owned characters and skins / likes / usage ranking

![My profile](screenshots/01-profile.png)

**Recent matches** — the latest 10 matches, expandable to all four players' full stats

![Recent matches](screenshots/02-matches.png)

**Match breakdown** — opened by **clicking a player name** inside 「Recent matches」

![Match breakdown](screenshots/03-replay.png)

![Breakdown detail](screenshots/04-replay-detail.png)

**Replay lookup** — enter a replay id or a player UID for a single match or that player's latest 10

![Replay lookup](screenshots/07-query.png)

**My skins** — every skin of every character, marked owned / not owned, with bond status

![My skins](screenshots/05-skins.png)

**Login**

![Login](screenshots/06-login.png)

---

## 📁 Project layout

```
AstralParty_Dex/
├─ main.py                  Entry point (pywebview UI; data assembly, export, breakdown cache)
├─ run_source.bat           Source launcher (pythonw, no console window left behind)
├─ requirements.txt         Dependencies
├─ astral/                  Core library
│   ├─ paths.py             Path resolution: read-only assets vs writable data (source / frozen)
│   ├─ log.py               Single log file (the frozen build has no console; errors go to userdata/log.txt)
│   ├─ client_ver.py        Auto-detect and cache the client version (the handshake needs clientVer; stale ⇒ err=10012)
│   ├─ sign.py              SDK signature algorithm + send code / login
│   ├─ signkeys.py          Channel signing keys (client-side constants, kept in one file so it can be dropped)
│   ├─ sdk_login.py         Session handling, handshake, full-profile parsing
│   ├─ game_session.py      Read the login ticket from a locally installed channel client (Steam CN / Bilibili)
│   ├─ intl_login.py        Self-service global-server login (reuses the ticket in the local client's mmkv)
│   ├─ client.py            Game server protocol client (framing / send / receive / routing)
│   ├─ frame.py             Protocol frame codec (35-byte big-endian header + protobuf payload)
│   ├─ keytable.py          The client's 256-byte permutation table (decryption)
│   ├─ proto_loader.py      Build protobuf messages from assets/proto/*.pb
│   ├─ replay.py            Replay parsing (room snapshot stream + packet records)
│   ├─ review.py            Breakdown build (per-round diffs, relic source detection, 3-choice chains, hand cards)
│   ├─ tablegen.py          Data-table generation rules (shared by the CLI sync scripts and the runtime)
│   ├─ game_live.py         Runtime data layer: re-extracts tables from the local client at startup
│   └─ gameart.py           Runtime extraction of avatars / portraits from the local game
├─ ui/app.html              Front-end (plain HTML/CSS/JS, no framework)
├─ assets/
│   ├─ app.ico / app.png    App icon
│   ├─ data/                Read-only tables (characters · skins · potentials · map missions · cards · chips · buffs · monsters)
│   └─ proto/               Protocol descriptors (FileDescriptorProto)
├─ tools/                   Data pipeline and packaging scripts
│   ├─ sync_*_from_game.py  Extract each table from the client (reports only by default; --apply writes)
│   ├─ fetch_*.py           Fetch game texts / map mission names / avatars and portraits
│   ├─ verify_*.py          Data-table and breakdown invariant checks
│   ├─ check_ui.js          UI self-check (no emoji, placeholder avatars, etc.)
│   └─ build_exe.py / make_release.py   Build the exe / produce the release zip
├─ screenshots/             Screenshots used by the READMEs
└─ userdata/                Generated at runtime: session, replay and breakdown cache, extracted avatars and tables
```

## ⚠️ Disclaimer

- This project is for **learning and research** purposes only; it is not affiliated with the
  game's developer or publisher in any way.
- All game data, character designs and names are copyrighted by their respective rights holders.
- Please do not use it for commercial purposes or large-scale scraping; any consequences of using
  this project are borne by the user.
- If the official side considers this project inappropriate, please contact us and it will be
  removed.
- `astral/signkeys.py` holds channel signing keys extracted from the game client (client-side
  constants, not any user's credentials). It is kept in a single file so that, should it ever be
  needed, **deleting that file** stops the distribution while the signature algorithm, protocol
  and UI themselves are unaffected.

---

## 🔧 Building

```bash
python -m pip install pyinstaller
python tools/build_exe.py       # → dist/星趴档案/星趴档案.exe (copy it together with _internal/ to run)
python tools/make_release.py    # → AstralParty_Dex_vX.Y.Z_win64.zip (version read from main.py)
```

`make_release.py` **deletes `userdata/` from the artifact** before zipping (it holds the login
state — shipping it would mean shipping the account), then **re-opens the archive to verify** that
no `userdata/` / `token.json` slipped in; it also verifies that **no game artwork made it into the
package** (avatars / portraits are extracted at runtime from the locally installed game, so the
package contains none).
If you have run the exe inside `dist/星趴档案/` by hand, it will have created a `userdata/` —
remember to re-package or delete it manually.

### Diagnostics (the packaged app has no console, so errors are not displayed)

The program writes avatar-extraction logs and exceptions into `<next to the exe>\userdata\log.txt`;
there is also a self-test entry point:

```bash
dist\星趴档案\星趴档案.exe --selftest     # checks dependency imports / multiprocessing / game asset location / test-scan + decode one texture
```

The self-test result is written to `userdata\log.txt` as well (last line `问题: [...]`; empty = all good).
There are two known pitfalls in resource collection at packaging time (both already fixed in
`tools/build_exe.py`, don't revert them): `--collect-all UnityPy` drags in torch/pandas and
inflates the package to GB scale, while **leaf dependency data files** such as `archspec`'s json
and `fmod_toolkit`'s `fmod.dll` will make every single texture fail to decode if not collected.

---

## 📜 Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## 💛 Credits

- Data sources: the official game client and the 吉星派对 Wiki
