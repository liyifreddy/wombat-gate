# 🐨 wombat-gate

<p align="center">
  <img src="https://raw.githubusercontent.com/liyifreddy/wombat-gate/main/docs/img/wombat-gate-banner.jpg" width="100%" alt="wombat-gate 横幅插图">
</p>

**一次只放一个进洞。** 几个 AI agent 共用一个 git 仓库时，让它们排队提交，而且每个只提交自己的文件。

🌐 [English](https://github.com/liyifreddy/wombat-gate/blob/main/README.md) · **中文**

## 🚀 快速上手

```sh
pipx install git+https://github.com/liyifreddy/wombat-gate

wombat-gate commit -m "修 parser" -- src/parser   # 只提交这些路径，排队轮流来
wombat-gate run -- git push                       # 推送
wombat-gate who                                   # 现在谁在洞里
```

一个文件，Python ≥ 3.8，只用标准库。Linux、macOS、WSL 都能用。

在 Claude Code 里装上插件（命令行 + 守卫 hook + 给 agent 的规矩）：

```
/plugin marketplace add liyifreddy/wombat-gate
/plugin install wombat-gate@wombat-gate
```

## 😩 遇到的问题

几个 Claude Code（或别的 agent）在同一个仓库里干活，很快就会碰到这些事：

- 🧺 **提交带走了别人的文件。** `git add -A`、`git commit -a`，或者不带路径的 `git commit`，会把别人暂存了一半的改动也一起提交。我们两天里碰到过两次。
- 🥊 **抢锁报错。** git 写索引前要先拿 `.git/index.lock`。两个进程同时来，后到的直接报 `index.lock: File exists`。自测里 8 个进程各提交 15 次，120 次只成功了 1–10 次。
- 🐢 **每次提交都把整个仓库检查一遍。** 我们的仓库有 2.2 万个文件，放在 WSL 挂载的 Windows 盘上，一次提交要 30–70 秒，高峰时排队等了 6 分钟。机器内存一紧，这种大量的读盘请求最先出错，整个盘跟着掉线。
- 💣 **agent 会用影响整个仓库的命令**，比如 `git stash`、`git reset --hard`、`git checkout -- .`，把别人的改动弄丢。

<p align="center">
  <img src="https://raw.githubusercontent.com/liyifreddy/wombat-gate/main/docs/img/wombat-gate-before-after.jpg" width="100%"
       alt="之前：三个机器人在洞里撞成一团，箱子摔坏；之后：袋熊守在洞口一次放一个进去，它把箱子放上自己的格子，别的在外面等">
</p>
<p align="center"><sub>之前：大家一起挤进洞。之后：一次一个，各放各的格子。</sub></p>

## ✨ 用了之后

- ✅ **只提交你点名的路径。** 别人暂存的东西原样留着，不会进你的提交。
- ✅ **排队，不报错。** 大家等同一把锁，轮到谁谁提交。自测 120 次全部成功；机器空闲时排队时间的中位数是 0.14 秒。
- ✅ **不再检查整个仓库。** 只处理你给的路径。上面那个 2.2 万文件的仓库，换用之后第一次提交只用了 3 秒（原来 30–70 秒）。
- ✅ **守卫 hook。** 在 Claude Code 里拦下影响整个仓库的 git 命令，用 197 条命令测过。
- ✅ **不删 git 的锁文件。** 碰到别的 git 进程占着锁，就等一会再试。
- ✅ **每次操作记一行日志**：排了多久、拿锁多久、每一步花了多久。
- ✅ **装起来简单。** Claude Code 插件一条命令装好，带守卫 hook 和教 agent 规矩的说明。

## 🕳️ 为什么叫袋熊

袋熊遇到危险会钻进洞里，用屁股把洞口堵住。它的屁股是一块很硬的骨板，狐狸咬不动。洞里一次只待一只袋熊。wombat-gate 也是这样：一次只让一个 agent 进 git。（袋熊的便便是方的，它会把便便摆在石头上标地盘。我们觉得自己的目录也该这么守。）

```
  agent A ──┐                                      ┌──▶ 只提交 A 自己的路径
  agent B ──┼──▶ 排队 ──▶ 拿锁 ──▶ 写 ─────────────┤
  agent C ──┘   （等）   （本地盘上的锁）           └──▶ 放锁 ──▶ 下一个
```

<p align="center">
  <img src="https://raw.githubusercontent.com/liyifreddy/wombat-gate/main/docs/img/wombat-gate-how-it-works-captioned.jpg" width="100%"
       alt="四格：机器人在洞口排队；一个跟袋熊进洞；在洞里只把自己的箱子放上自己的格子；出来，下一个进去">
</p>

1. **排队**：要提交的 agent 在外面排队。
2. **拿锁**：一次只进一个。锁放在 `~/.local/state` 下面，不在仓库里；这个目录要在本地盘上，不在的话 wombat-gate 会提醒。
3. **只提交自己的路径**：进去的那个只放下自己的箱子。
4. **放锁，下一个**：出来，下一个进去。

<p align="center"><sub>Illustrations generated with Google Gemini and edited by the author.</sub></p>

## 🧭 细节

- **锁不会卡死**：拿着锁的进程死了，系统会自动放锁，不会留下要手动删的锁文件。
- **路径检查**：`.`、仓库根目录、通配符、`:` 开头的特殊路径都不接受，免得一不小心提交整个仓库。
- **为什么快**：在索引的一份副本里只暂存你的路径，再把这些路径接到当前提交上；移动分支时先确认没有别人动过它；整个过程拿着 git 自己的索引锁，别的 git 进程插不进来。每次自测都和真正的 `git commit` 对比结果。
- **只看自己的状态**：`wombat-gate status -- 路径`。
- **给人用的开关**：`--allow-broad`、`run --unsafe` 在 agent 里一律拒绝。

一步一步的过程见 [REFERENCE.md](REFERENCE.md)（英文）。

## 🛠️ 用法

```sh
wombat-gate commit -m "parser: 处理空输入" -- src/parser tests/test_parser.py
wombat-gate commit -n -m "..." -- src/parser   # 只看会提交什么，不真提交
wombat-gate status -- src/parser               # 只看自己路径的状态
wombat-gate run -- git push                    # push、fetch、tag、log 等（白名单）
wombat-gate who                                # 现在谁在洞里
wombat-gate log -n 50                          # 最近的耗时记录
```

设 `WOMBAT_SESSION=agent-7`（或加 `--session`），`who` 里就能看到是谁。

## 🤖 在 Claude Code 里用

1. 在 `CLAUDE.md` 里写清楚：提交用 `wombat-gate commit -m "说明" -- 你的路径`，推送用 `wombat-gate run -- git push`；不要用 `git add -A`、`git commit -a`、`git stash`。
2. 装上守卫 hook（装插件就自带，见上面「快速上手」）。
3. 在项目的 `.claude/settings.json` 里加 `{"env": {"WOMBAT_NO_ESCAPES": "1"}}`。

## ⚠️ 局限

- 只有用 wombat-gate 的进程才会排队，所以所有 agent 都要换过来。
- 锁要放在本地盘上；Windows 那边的 git 看不到 WSL 里的锁。
- 只加快提交；`git status`、`git log` 在慢盘上还是慢。
- 两个 agent 改同一个文件，先提交的会把两边的改动一起带走。最好给每个 agent 分开的目录。
- WSL 内存用光时，Windows 盘可能掉线。这不是 wombat-gate 引起的，但耗时长的 git 命令最容易碰上。

## ✅ 自测

```sh
python3 selftest_wombat_gate.py               # 约 1 分钟，在本地盘上建临时仓库
python3 selftest_wombat_gate.py --mutants     # 把 36 个修过的 bug 放回去，每个都必须被抓到
bash hooks/selftest_guard.sh                  # 197 条命令，逐条核守卫该拦还是该放
```

每次快速提交的结果，都会和在仓库副本上直接跑 `git commit` 的结果对比。

退出码、全部设置、每一条会被拒的命令见 [REFERENCE.md](REFERENCE.md)（英文）；设计思路见 [DESIGN.md](DESIGN.md)；多 session 怎么协作见 [PLAYBOOK.md](PLAYBOOK.md)。许可：MIT。
