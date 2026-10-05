# wombat-gate（中文简介）

**一次只放一个进洞。** 让很多个 AI agent（或者人）共用同一个 git 工作区时，排队提交、互不踩脚的小工具。

袋熊被追的时候会钻进洞里，用屁股把洞口堵死——那是一块软骨加骨头的硬板，狐狸咬不动。洞里一次只有一只袋熊，别的都在外面等。（袋熊还会把立方体形状的便便摆在石头上，宣示「这是我的」。我们觉得对待自己的目录就该是这个态度。）

**洞里一次只有一只袋熊 = 一次只有一个 session 往 git 里写。**

```
  agent A ──┐                                      ┌──▶ 只提交 A 自己的路径
  agent B ──┼──▶ 排队 ──▶ 拿锁 ──▶ 写 ─────────────┤
  agent C ──┘   （等）   （本地盘上的 flock）        └──▶ 放锁 ──▶ 下一个
```

## 解决什么问题

十几个 Claude Code session 同时在一个工作区里各自 `git add` / `git commit`：

- 第二个写索引的进程直接报 `index.lock: File exists` 失败；
- `git add -A`、`git commit -a`、不带路径的 `git commit` 会把别人做了一半的东西扫进自己的提交；
- 工作区在慢盘上时（WSL 挂载的 Windows 盘、网络盘），一次提交要几十秒，大家排着队干等。

自测里（本地盘，8 个写者各提交 15 次）：直接用 git，120 次里只成了 1–10 次；用 wombat-gate，120 次全成，排队中位数不到 0.1 秒。

## 它做什么

- **排队**：同一个仓库的所有写操作，都要先拿到本地盘上的同一把 `flock` 锁。持锁的进程死了，内核自动放锁，不留需要手删的锁文件。
- **只提交你点名的路径**：`wombat-gate commit -m "说明" -- 你的文件或目录`。别人暂存的东西照旧暂存，不会进你的提交；`.`、仓库根、通配符、`:` 魔法路径一律拒绝。
- **快**（0.6）：先按 git 自己的规矩拿住索引锁（`index.lock`），把索引复制一份到本地盘，只在副本上暂存你的路径，再把你的路径嫁接到 HEAD 的树上，不去 stat 整个工作区；用带旧值校验的 `update-ref` 移动分支，最后把副本写回索引——和 `git commit` 自己的做法一样，中途别的 git 进程插不进来。实测：2.2 万个文件、放在 WSL 9p 挂载盘上的仓库，每次占锁从 30–70 秒降到几秒。自测里每次都拿真实的 `git commit` 在仓库副本上做对照，提交出来的树、索引和退出码都要一致。
- **退避重试**：没走 wombat-gate 的 git 进程占着 git 自己的锁时，等一等再试；从不删 git 的锁文件。
- **只看自己路径的 status**：`wombat-gate status -- 路径`，不拿 `index.lock`。
- **逃生口只给人用**：`--allow-broad`、`run --unsafe` 在 agent 环境（`CLAUDECODE` 或 `WOMBAT_NO_ESCAPES=1`）里一律拒绝。
- **耗时日志**：每次操作一行 JSON：排队多久、持锁多久、每一步 git 花了多久。

## 在 Claude Code 里用

1. 装好命令行，在 `CLAUDE.md` 里写清规矩：提交用 `wombat-gate commit -m "…" -- <自己的路径>`，推送用 `wombat-gate run -- git push`，不许 `git add -A` / `git commit -a` / `git stash`。
2. 装 Bash 的 `PreToolUse` 守卫 hook（`hooks/guard.py`，或者直接装插件）：按 shell 的规则拆命令、按 git 真实的参数结构判断，拦整库暂存 / 提交、丢弃改动、关掉 agent 检查之类的写法。
3. 在项目的 `.claude/settings.json` 里设 `{"env": {"WOMBAT_NO_ESCAPES": "1"}}`。

也可以作为 Claude Code 插件安装（命令行、守卫 hook 和一段教 agent 规矩的 skill 一起装上）：

```
/plugin marketplace add liyifreddy/wombat-gate
/plugin install wombat-gate@wombat-gate
```

## 局限

- 只有走 wombat-gate 的进程才排队；直接用 git 的进程照样会撞。所有 agent 都要换过来。
- 锁必须放在本地盘上；Windows 侧的 git 看不到 WSL 里的锁。
- 只管提交；你自己的 `git status`、`git log` 在慢盘上还是一样慢。
- 两个 agent 提交同一个路径：先提交的那个会把两边的改动一起带走。给每个 agent 分不重叠的路径。
- WSL + 9p：我们这里 WSL 内存耗尽时，`/mnt/<盘>` 会掉线（之后一直 I/O 错误，直到重新挂载）。这不是 wombat-gate 造成的，但跑得久的 git 命令往往最先撞上。

详细说明见英文 [README](README.md)；设计取舍见 [DESIGN.md](DESIGN.md)；多 session 协作规范见 [PLAYBOOK.md](PLAYBOOK.md)。许可：MIT。
