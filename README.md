# memkb 🧠

本地 Markdown 记忆库：给人和 agent 用的「第二大脑」。

终端里 `add / search / recall` 笔记，笔记是纯 Markdown 文件（永远可读、可迁移），
sqlite FTS5 只做关键词索引。**零依赖**：Python 3.10+ 标准库即可。

```bash
memkb add "灵感：播客选题" --tag 想法,播客 --body "做一期讲 FTS5 的节目…"
memkb search "播客 tag:想法"
memkb recall "播客" --n 3 # 给 agent 喂上下文
memkb hook "今天跑了 5 公里" # 一句话记入今日日记
```

## 安装

零依赖，直接用：

```bash
git clone https://github.com/ljiang9/memkb.git
cd memkb
python3 -m memkb --help
# 想全局用：alias memkb="python3 /path/to/memkb -m memkb"
# 或：ln -s $PWD/memkb.py /usr/local/bin/memkb && chmod +x /usr/local/bin/memkb
```

需要 Python 3.10+，且内置 sqlite3 支持 FTS5（官方 CPython 默认都有）。

## 数据存在哪

默认 `~/.memkb/`：

```
~/.memkb/
├── notes/ # 笔记本体，一个文件一条
│ ├── 20261005-143022-播客选题.md
│ └── daily-2026-10-05.md # hook 记的今日日记
└── index.db                # sqlite FTS5 索引（派生数据，删了下次自动重建）
```

换位置：所有命令都支持 `--db PATH`，如 `memkb --db /tmp/test add …`。
换电脑时只拷走 `notes/` 目录就行，Markdown 是开放格式。

## 命令速查

| 命令 | 说明 |
|---|---|
| `add "标题" --tag a,b [--body "正文"]` | 新增笔记；不给 `--body` 则打开 `$EDITOR` 手写，打印笔记 id |
| `search "关键词"` | FTS5 排序 + 关键词高亮 snippet；支持 `tag:xxx` 过滤 |
| `show <id>` | 看全文（id 支持唯一前缀） |
| `ls [--tag x] [--limit n]` | 按时间倒序列出 |
| `rm <id> [--yes]` | 删除文件 + 索引（默认要确认） |
| `export [--tag x]` | 导出成单个 Markdown，打到 stdout |
| `recall "query" --n 5 [--max-chars 4000]` | agent 专用：紧凑上下文块，超字数截断并提示 |
| `hook "一句话"` | 追加时间戳行到今日日记（自动创建） |
| `stats` | 笔记数、标签统计、总字数、最早/最新 |

## 例子

```bash
# 记一条带标签的笔记
$ memkb add "Q4 目标" --tag 工作,规划 --body "1. 上线 memkb 2. 写 5 篇博客"
20261005-091523-q4-目标

# 搜（中文直接搜，tag: 过滤）
$ memkb search "博客 tag:工作"
[20261005-091523-q4-目标] Q4 目标 · 2026-10-05 · tags: 工作,规划
1. 上线 memkb 2. 写 5 篇

# 一句话速记（每天的日记自动归档）
$ memkb hook "灵感：给 memkb 加个 TUI"
已记入今日：2026-10-05

# 给 agent 喂上下文
$ memkb recall "Q4" --n 2 --max-chars 1000
```

## 诚实说明（limitations）

- **只有关键词搜索，没有语义搜索**：搜"开心"找不到写了"快乐"的笔记，不像向量数据库。想要语义请接 embeddings，那是另一个项目。
- **中文分词是"逐字切分"近似**：每个汉字当一个 token，"人工智"能搜到"人工智能"，但"上海自来水来自海上"这种也会误命中。这是零依赖下的 trade-off。
- 单机单用户，没做并发写保护；`~/.memkb` 不在版本库里，备份请自己拷 `notes/`。
- `recall` 的字数是按字符数估算的，不是真实 token 数。

## License

MIT — 详见 [LICENSE](LICENSE)。
