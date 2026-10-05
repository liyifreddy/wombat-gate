# 🕳️ wombat-gate（中文简介）

<p align="center">
  <img src="https://raw.githubusercontent.com/liyifreddy/wombat-gate/main/docs/img/wombat-gate-banner.jpg" width="100%" alt="wombat-gate 横幅插图">
</p>

**一次只放一个进洞。** 让很多个 AI agent（或者人）共用同一个 git 工作区时，排队提交、互不踩脚的小工具。

## 😩 痛点

几个 Claude Code session（或别的 agent）共用**一棵**工作树：

- 🧺 **提交卷走别人的文件**：`git add -A`、`git commit -a`、不带路径的 `git commit` 会把邻居做了一半、已经暂存的东西一起提交走。写这个工具之前，我们两天里出过两次。
- 🥊 **几个 agent 抢 git「打架」**：每次写索引都要拿 `.git/index.lock`，第二个写的直接报 `index.lock: File exists`，各自按自己的节奏重试，重试时又互相踩对方的索引。自测里 8 个直接用 git 的写者各提交 15 次，120 次只成了 1–10 次。
- 🐢 **每次提交都扫整棵树**：`git commit` 先刷新索引，每个被跟踪的文件 `lstat()` 一次。2.2 万个文件、放在 WSL 9p 挂载的 Windows 盘上的仓库，一次就是几万个 9p 请求、30–70 秒；一阵提交高峰里 agent 排队最长等了 6 分钟。每个 9p 请求都要一块连续的内核内存，内存一紧，最先失败的就是这种扫全树的请求——然后整个挂载掉线，所有 session 一起受影响（我们 G 盘掉线的根因就是这一类）。
- 💣 **agent 会顺手用整树命令**：`git add -A`、`git stash`、`git reset --hard`、`git checkout -- .`，把别的 session 的改动扔掉或卷走。

## ✨ 你得到什么

- ✅ **只提交自己的路径**：`wombat-gate commit -m "说明" -- 路径…` 只提交这些路径；别人暂存的照旧暂存，不进你的提交。
- ✅ **不再打架**：所有写者排队等同一把本地 `flock`，不再报错、重试、互相踩索引。自测 120 次提交全成；空闲机器上排队中位 0.14 秒、持锁中位 0.026 秒（另有 10 个 agent session 在跑时是 0.9 秒 / 0.16 秒）。
- ✅ **不扫整棵树**：在私有的索引副本里建提交，只把你的路径嫁接到 `HEAD` 上，不去 stat 其余文件：上面那个 2.2 万文件的 9p 仓库，切换后第一次真实提交持锁 3.07 秒（原来 30–70 秒；只测了这一次，一般是「几秒」）。
- ✅ **守卫 hook**：给 Claude Code 用，拦整树的 git 命令（暂存、提交、stash、reset、checkout、clean……）；自测 197 条命令逐条核该拦还是该放。
- ✅ **从不删 git 的锁文件**：队列外的 git 进程占着锁时，退避重试。
- ✅ **报告读真实提交**：打印的 sha 和文件列表来自它真正做出的那个提交，不是 `HEAD`。
- ✅ **计时日志**：每次操作一行 JSON：排队多久、持锁多久、每一步 git 多久。
- ✅ **Claude Code 一键装**：插件里有命令行、守卫 hook 和教 agent 规矩的 skill。自测每次都拿真实的 `git commit` 做对照，另有 36 个重新植入的历史 bug，每个都必须让某项检查变红。

## 🕳️ 怎么做到的

袋熊被追的时候会钻进洞里，用屁股把洞口堵死——那是一块软骨加骨头的硬板，狐狸咬不动。洞里一次只有一只袋熊，别的都在外面等。（袋熊还会把立方体形状的便便摆在石头上，宣示「这是我的」。我们觉得对待自己的目录就该是这个态度。）

**洞里一次只有一只袋熊 = 一次只有一个 session 往 git 里写。**

```
  agent A ──┐                                      ┌──▶ 只提交 A 自己的路径
  agent B ──┼──▶ 排队 ──▶ 拿锁 ──▶ 写 ─────────────┤
  agent C ──┘   （等）   （本地盘上的 flock）        └──▶ 放锁 ──▶ 下一个
```

<p align="center">
  <img src="https://raw.githubusercontent.com/liyifreddy/wombat-gate/main/docs/img/wombat-gate-how-it-works-captioned.jpg" width="100%"
       alt="四格：机器人在洞口排队；一个跟袋熊进洞；在洞里只把自己的箱子放上自己的格子；出来，下一个进去">
</p>

1. **wait in queue（排队）**——要写 git 的 agent 都在洞外排队。
2. **take the lock (flock, local disk)（拿锁，锁在本地盘）**——一次只进一个；锁是 `~/.local/state` 下的一个文件、在仓库之外，这个目录必须在本地盘上（不在时 wombat-gate 会警告）。
3. **commit only your own paths（只提交自己的路径）**——在洞里只放下自己的箱子，也就是自己点名的路径。
4. **release, next in line（放锁，下一个）**——出来、放锁，下一个进去。

<p align="center"><sub>Illustrations generated with Google Gemini and edited by the author.</sub></p>

## 🧭 它做什么（细节）

- **排队**：同一个仓库的所有写操作，都要先拿到本地盘上的同一把 `flock` 锁。持锁的进程死了，内核自动放锁，不留需要手删的锁文件。
- **只提交你点名的路径**：`wombat-gate commit -m "说明" -- 你的文件或目录`。别人暂存的东西照旧暂存，不会进你的提交；`.`、仓库根、通配符、`:` 魔法路径一律拒绝。
- **快**（0.6）：先按 git 自己的规矩拿住索引锁（`index.lock`），把索引复制一份到本地盘，只在副本上暂存你的路径，再把你的路径嫁接到 HEAD 的树上，不去 stat 整个工作区；用带旧值校验的 `update-ref` 移动分支，最后把副本写回索引——和 `git commit` 自己的做法一样，中途别的 git 进程插不进来。实测：2.2 万个文件、放在 WSL 9p 挂载盘上的仓库，每次占锁从 30–70 秒降到几秒。自测里每次都拿真实的 `git commit` 在仓库副本上做对照，提交出来的树、索引和退出码都要一致。
- **退避重试**：没走 wombat-gate 的 git 进程占着 git 自己的锁时，等一等再试；从不删 git 的锁文件。
- **只看自己路径的 status**：`wombat-gate status -- 路径`，不拿 `index.lock`。
- ⛔ **逃生口只给人用**：`--allow-broad`、`run --unsafe` 在 agent 环境（`CLAUDECODE` 或 `WOMBAT_NO_ESCAPES=1`）里一律拒绝。
- **耗时日志**：每次操作一行 JSON：排队多久、持锁多久、每一步 git 花了多久。

## 🤖 在 Claude Code 里用

1. 装好命令行，在 `CLAUDE.md` 里写清规矩：✅ 提交用 `wombat-gate commit -m "…" -- <自己的路径>`，推送用 `wombat-gate run -- git push`；⛔ 不许 `git add -A` / `git commit -a` / `git stash`。
2. 装 Bash 的 `PreToolUse` 守卫 hook（`hooks/guard.py`，或者直接装插件）：按 shell 的规则拆命令、按 git 真实的参数结构判断，拦整库暂存 / 提交、丢弃改动、关掉 agent 检查之类的写法。
3. 在项目的 `.claude/settings.json` 里设 `{"env": {"WOMBAT_NO_ESCAPES": "1"}}`。

也可以作为 Claude Code 插件安装（命令行、守卫 hook 和一段教 agent 规矩的 skill 一起装上）：

```
/plugin marketplace add liyifreddy/wombat-gate
/plugin install wombat-gate@wombat-gate
```

## ⚠️ 局限

- 只有走 wombat-gate 的进程才排队；直接用 git 的进程照样会撞。所有 agent 都要换过来。
- 锁必须放在本地盘上；Windows 侧的 git 看不到 WSL 里的锁。
- 只管提交；你自己的 `git status`、`git log` 在慢盘上还是一样慢。
- 两个 agent 提交同一个路径：先提交的那个会把两边的改动一起带走。给每个 agent 分不重叠的路径。
- WSL + 9p：我们这里 WSL 内存耗尽时，`/mnt/<盘>` 会掉线（之后一直 I/O 错误，直到重新挂载）。这不是 wombat-gate 造成的，但跑得久的 git 命令往往最先撞上。

详细说明见英文 [README](README.md)；设计取舍见 [DESIGN.md](DESIGN.md)；多 session 协作规范见 [PLAYBOOK.md](PLAYBOOK.md)。许可：MIT。
