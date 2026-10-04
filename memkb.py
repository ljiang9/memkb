#!/usr/bin/env python3
"""memkb - 本地 Markdown 记忆库：给人和 agent 用的"第二大脑"。

零依赖（只用标准库），sqlite3 FTS5 做关键词索引，笔记以 Markdown 文件存放在数据目录。
数据目录默认为 ~/.memkb，可用 --db PATH 指定。

设计上诚实：只有关键词搜索，没有语义/向量搜索。
中文分词用"逐字切分"近似（把每个汉字当作一个 token），能搜但可能有误命中。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

VERSION = "0.1.0"
DEFAULT_DIR = os.path.expanduser("~/.memkb")
NOTES_DIRNAME = "notes"
DB_FILENAME = "index.db"
CJK = "\u4e00-\u9fff"


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def cjk_space(text: str) -> str:
    """把连续的汉字逐字隔开，让 FTS5 能按单字做关键词匹配。

    例："AI创业笔记" -> "AI 创 业 笔 记"。英文/数字不受影响。
    """
    text = re.sub("([" + CJK + "])", r" \1 ", text)
    return re.sub(r"\s+", " ", text).strip()


def sanitize_tokens(query: str) -> list:
    """从查询串提取安全的 token（单词/汉字串），去掉 FTS5 特殊字符。"""
    return re.findall(r"[\w" + CJK + "]+", query, flags=re.UNICODE)


def slugify(title: str) -> str:
    slug = re.sub(r"[^\w" + CJK + "]+", "-", title.lower(), flags=re.UNICODE).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:40].strip("-")
    return slug or "note"


def data_paths(db_dir: str):
    notes_dir = os.path.join(db_dir, NOTES_DIRNAME)
    os.makedirs(notes_dir, exist_ok=True)
    return notes_dir, os.path.join(db_dir, DB_FILENAME)


def connect(db_path: str) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts "
            "USING fts5(id UNINDEXED, title, body, tags)"
        )
        return conn
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower():
            sys.exit("error: 当前 Python 的 sqlite3 不支持 FTS5，无法建索引")
        raise


def index_note(conn: sqlite3.Connection, note_id: str, title: str,
               body: str, tags: list) -> None:
    conn.execute("DELETE FROM notes_fts WHERE id = ?", (note_id,))
    conn.execute(
        "INSERT INTO notes_fts (id, title, body, tags) VALUES (?, ?, ?, ?)",
        (note_id, cjk_space(title), cjk_space(body),
         cjk_space(" ".join(tags))),
    )
    conn.commit()


def unindex_note(conn: sqlite3.Connection, note_id: str) -> None:
    conn.execute("DELETE FROM notes_fts WHERE id = ?", (note_id,))
    conn.commit()


def build_match(query: str):
    """把用户查询转成 FTS5 MATCH 表达式；支持 tag:xxx 过滤语法。

    返回 (match_expr, tag_filters)。tag:xxx 同时从查询中剥离。
    """
    tag_filters = re.findall(r"tag:([\w" + CJK + r"][\w" + CJK + r"\-.]*)",
                             query, flags=re.UNICODE)
    query = re.sub(r"tag:[\w" + CJK + r"][\w" + CJK + r"\-.]*", " ",
                   query, flags=re.UNICODE)
    parts = []
    for tok in sanitize_tokens(query):
        chars = cjk_space(tok).split(" ")
        parts.append("(" + " AND ".join(chars) + ")")
    for t in tag_filters:
        chars = cjk_space(t).split(" ")
        parts.append("{tags} : (" + " AND ".join(chars) + ")")
    if not parts:
        return "", tag_filters
    return " AND ".join(parts), tag_filters


def parse_tags(tag_str) -> list:
    if not tag_str:
        return []
    return [t.strip() for t in re.split(r"[,\s]+", tag_str) if t.strip()]


def render_note(title: str, tags: list, created: dt.datetime,
                body: str) -> str:
    tag_list = ", ".join(tags)
    return (
        "---\n"
        'title: "%s"\n' % title.replace('"', "'")
        + "tags: [%s]\n" % tag_list
        + "created: %s\n" % created.isoformat(timespec="seconds")
        + "---\n\n"
        + body.rstrip() + "\n"
    )


def parse_note_file(path: str):
    """返回 (title, tags, created, body)。"""
    raw = open(path, encoding="utf-8").read()
    title, tags, created, body = "", [], "", raw
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    if m:
        head, body = m.group(1), m.group(2).lstrip("\n")
        tm = re.search(r'^title:\s*"(.*)"\s*$', head, re.M)
        if tm:
            title = tm.group(1)
        gm = re.search(r"^tags:\s*\[(.*)\]\s*$", head, re.M)
        if gm:
            tags = [t.strip() for t in gm.group(1).split(",") if t.strip()]
        cm = re.search(r"^created:\s*(\S+)\s*$", head, re.M)
        if cm:
            created = cm.group(1)
    if not title:
        title = os.path.basename(path)
    return title, tags, created, body


def resolve_id(notes_dir: str, note_id: str):
    """支持 id 前缀匹配；返回完整 id，无/多匹配返回 None。"""
    exact = os.path.join(notes_dir, note_id + ".md")
    if os.path.isfile(exact):
        return note_id
    cands = [f[:-3] for f in os.listdir(notes_dir)
             if f.endswith(".md") and f[:-3].startswith(note_id)]
    return cands[0] if len(cands) == 1 else None


def unique_id(notes_dir: str, base: str) -> str:
    nid, i = base, 2
    while os.path.exists(os.path.join(notes_dir, nid + ".md")):
        nid = "%s-%d" % (base, i)
        i += 1
    return nid


def cmd_add(args) -> int:
    notes_dir, db_path = data_paths(args.db)
    body = args.body
    if body is None:
        body = edit_in_editor(args.title)
        if body is None:
            print("已取消：正文为空", file=sys.stderr)
            return 1
    tags = parse_tags(args.tag)
    ts = now_local().strftime("%Y%m%d-%H%M%S")
    note_id = unique_id(notes_dir, "%s-%s" % (ts, slugify(args.title)))
    created = now_local()
    path = os.path.join(notes_dir, note_id + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_note(args.title, tags, created, body))
    conn = connect(db_path)
    index_note(conn, note_id, args.title, body, tags)
    conn.close()
    print(note_id)
    return 0


def edit_in_editor(title: str):
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as tf:
        tf.write("\n\n<!-- 在此输入「%s」的正文，保存后退出；内容为空则取消 -->\n" % title)
        tmp = tf.name
    try:
        rc = subprocess.run([editor, tmp]).returncode
        if rc != 0:
            return None
        lines = [ln for ln in open(tmp, encoding="utf-8").read().splitlines()
                 if not ln.strip().startswith("<!--")]
        body = "\n".join(lines).strip()
        return body or None
    finally:
        os.unlink(tmp)


def despace_cjk(text: str) -> str:
    """展示用：把索引时插入的汉字间空格收回去，snippet 更易读。"""
    return re.sub("(?<=[" + CJK + "]) (?=[" + CJK + "])", "", text)


def cmd_search(args) -> int:
    notes_dir, db_path = data_paths(args.db)
    match, _ = build_match(args.query)
    if not match:
        print("error: 请输入搜索词（可用 tag:xxx 过滤）", file=sys.stderr)
        return 1
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, "
            "snippet(notes_fts, 1, '【', '】', '……', 12), "
            "snippet(notes_fts, 2, '【', '】', '……', 24), "
            "bm25(notes_fts) AS rank "
            "FROM notes_fts WHERE notes_fts MATCH ? "
            "ORDER BY rank LIMIT 20",
            (match,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print("error: 查询语法错误：%s" % e, file=sys.stderr)
        return 1
    if not rows:
        print("没有找到匹配的笔记")
        return 0
    for nid, t_snip, b_snip, _rank in rows:
        path = os.path.join(notes_dir, nid + ".md")
        if not os.path.isfile(path):
            continue
        title, tags, created, _body = parse_note_file(path)
        date = created[:10] if created else "?"
        tag_str = ",".join(tags) if tags else "-"
        print("[%s] %s · %s · tags: %s" % (nid, title, date, tag_str))
        snip = (b_snip or "").strip() or (t_snip or "").strip()
        if snip:
            print("    %s" % despace_cjk(snip))
    conn.close()
    return 0


def cmd_show(args) -> int:
    notes_dir, _db = data_paths(args.db)
    nid = resolve_id(notes_dir, args.id)
    if nid is None:
        print("error: 找不到笔记：%s" % args.id, file=sys.stderr)
        return 1
    print(open(os.path.join(notes_dir, nid + ".md"),
               encoding="utf-8").read(), end="")
    return 0


def cmd_ls(args) -> int:
    notes_dir, _db = data_paths(args.db)
    items = []
    for f in os.listdir(notes_dir):
        if not f.endswith(".md"):
            continue
        path = os.path.join(notes_dir, f)
        title, tags, created, _body = parse_note_file(path)
        if args.tag and args.tag not in tags:
            continue
        items.append((created or "", f[:-3], title, tags))
    items.sort(key=lambda x: x[0], reverse=True)
    for created, nid, title, tags in items[: args.limit]:
        date = created[:10] if created else "?"
        tag_str = ",".join(tags) if tags else "-"
        print("[%s] %s · %s · tags: %s" % (nid, title, date, tag_str))
    if not items:
        print("还没有笔记，用 `memkb add` 写第一条吧")
    return 0


def cmd_rm(args) -> int:
    notes_dir, db_path = data_paths(args.db)
    nid = resolve_id(notes_dir, args.id)
    if nid is None:
        print("error: 找不到笔记：%s" % args.id, file=sys.stderr)
        return 1
    path = os.path.join(notes_dir, nid + ".md")
    title, _tags, _created, _body = parse_note_file(path)
    if not args.yes:
        ans = input("确定删除「%s」(%s)吗？[y/N] " % (title, nid)).strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return 0
    os.unlink(path)
    conn = connect(db_path)
    unindex_note(conn, nid)
    conn.close()
    print("已删除：%s" % nid)
    return 0


def cmd_export(args) -> int:
    notes_dir, _db = data_paths(args.db)
    items = []
    for f in sorted(os.listdir(notes_dir)):
        if not f.endswith(".md"):
            continue
        path = os.path.join(notes_dir, f)
        title, tags, created, _body = parse_note_file(path)
        if args.tag and args.tag not in tags:
            continue
        items.append((created or "", open(path, encoding="utf-8").read()))
    items.sort(key=lambda x: x[0])
    out = "\n\n---\n\n".join(raw.rstrip() for _c, raw in items)
    if out:
        print(out)
    return 0


def cmd_recall(args) -> int:
    notes_dir, db_path = data_paths(args.db)
    match, _ = build_match(args.query)
    if not match:
        print("error: 请输入搜索词", file=sys.stderr)
        return 1
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, bm25(notes_fts) AS rank FROM notes_fts "
            "WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, args.n),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print("error: 查询语法错误：%s" % e, file=sys.stderr)
        return 1
    blocks = []
    total = 0
    truncated = False
    shown = 0
    for (nid, _rank) in rows:
        path = os.path.join(notes_dir, nid + ".md")
        if not os.path.isfile(path):
            continue
        title, tags, _created, body = parse_note_file(path)
        block = "[%s] %s\n%s" % (nid, title, body.strip())
        if total + len(block) > args.max_chars:
            truncated = True
            break
        blocks.append(block)
        total += len(block)
        shown += 1
    conn.close()
    print('# memkb recall: "%s"（top %d）\n' % (args.query, shown))
    print("\n\n---\n\n".join(blocks))
    if truncated:
        print("\n…（已截断：超过 --max-chars %d 字，%d 条命中只展示了 %d 条）"
              % (args.max_chars, len(rows), shown))
    return 0


def cmd_hook(args) -> int:
    notes_dir, db_path = data_paths(args.db)
    today = now_local().strftime("%Y-%m-%d")
    nid = "daily-%s" % today
    path = os.path.join(notes_dir, nid + ".md")
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_note(today, ["daily"], now_local(), ""))
    stamp = now_local().strftime("%H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write("- %s %s\n" % (stamp, args.text.rstrip()))
    title, tags, _created, body = parse_note_file(path)
    conn = connect(db_path)
    index_note(conn, nid, title, body, tags)
    conn.close()
    print("已记入今日：%s" % today)
    return 0


def cmd_stats(args) -> int:
    notes_dir, _db = data_paths(args.db)
    n, chars = 0, 0
    tag_counts = {}
    oldest = newest = None
    for f in os.listdir(notes_dir):
        if not f.endswith(".md"):
            continue
        title, tags, created, body = parse_note_file(
            os.path.join(notes_dir, f))
        n += 1
        chars += len(body)
        for t in tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        if created:
            oldest = created if oldest is None or created < oldest else oldest
            newest = created if newest is None or created > newest else newest
    print("笔记总数：%d" % n)
    print("正文字数：%d" % chars)
    print("最早：%s" % (oldest[:16] if oldest else "-"))
    print("最新：%s" % (newest[:16] if newest else "-"))
    print("标签统计：")
    if tag_counts:
        for t, c in sorted(tag_counts.items(), key=lambda x: -x[1]):
            print("  %s: %d" % (t, c))
    else:
        print("  （无标签）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memkb",
        description="本地 Markdown 记忆库：add/search/recall 笔记，零依赖，关键词搜索。",
    )
    p.add_argument("--db", default=DEFAULT_DIR,
                   help="数据目录（默认 %s）" % DEFAULT_DIR)
    p.add_argument("--version", action="version",
                   version="%%(prog)s %s" % VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="新增笔记")
    a.add_argument("title", help="笔记标题")
    a.add_argument("--tag", default=None,
                   help="标签，逗号分隔，如 --tag 工作,想法")
    a.add_argument("--body", default=None,
                   help="正文；不给则打开 $EDITOR 编辑")
    a.set_defaults(func=cmd_add)

    s = sub.add_parser("search", help="关键词搜索（支持 tag:xxx 过滤）")
    s.add_argument("query", help="搜索词，如“周报”或“复盘 tag:工作”")
    s.set_defaults(func=cmd_search)

    sh = sub.add_parser("show", help="查看笔记全文")
    sh.add_argument("id", help="笔记 id（支持唯一前缀）")
    sh.set_defaults(func=cmd_show)

    ls = sub.add_parser("ls", help="列出最近的笔记")
    ls.add_argument("--tag", default=None, help="只看某个标签")
    ls.add_argument("--limit", type=int, default=20, help="条数（默认 20）")
    ls.set_defaults(func=cmd_ls)

    rm = sub.add_parser("rm", help="删除笔记")
    rm.add_argument("id", help="笔记 id（支持唯一前缀）")
    rm.add_argument("--yes", action="store_true", help="跳过确认")
    rm.set_defaults(func=cmd_rm)

    ex = sub.add_parser("export", help="导出笔记为单个 Markdown（输出到 stdout）")
    ex.add_argument("--tag", default=None, help="只导出某个标签")
    ex.set_defaults(func=cmd_export)

    rc = sub.add_parser("recall", help="给 agent 用的召回：输出紧凑上下文块")
    rc.add_argument("query", help="搜索词")
    rc.add_argument("--n", type=int, default=5, help="取前 N 条（默认 5）")
    rc.add_argument("--max-chars", type=int, default=4000,
                    help="总字数上限，超出截断（默认 4000）")
    rc.set_defaults(func=cmd_recall)

    hk = sub.add_parser("hook", help="一句话记入今日日记")
    hk.add_argument("text", help="要记录的内容")
    hk.set_defaults(func=cmd_hook)

    st = sub.add_parser("stats", help="统计：数量 / 标签 / 字数")
    st.set_defaults(func=cmd_stats)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # 索引是派生的：库空但 notes/ 里有文件时自动重建（删 index.db 也没事）
    notes_dir, db_path = data_paths(args.db)
    conn = connect(db_path)
    empty = conn.execute("SELECT COUNT(*) FROM notes_fts").fetchone()[0] == 0
    files = [f for f in os.listdir(notes_dir) if f.endswith(".md")]
    if empty and files:
        for f in files:
            nid = f[:-3]
            title, tags, _c, body = parse_note_file(
                os.path.join(notes_dir, f))
            index_note(conn, nid, title, body, tags)
        print("（已重建索引：%d 条笔记）" % len(files), file=sys.stderr)
    conn.close()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
