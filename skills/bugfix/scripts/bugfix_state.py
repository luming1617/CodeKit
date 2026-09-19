#!/usr/bin/env python3
"""Bugfix 阶段的确定性状态控制器：冻结快照与文档地图、绑定审批、校验写入范围、批量执行验证。

本脚本存在的唯一理由是把「审批前只读、审批绑定快照、修改不越界、验证真实执行」
这四条本来只写在文档里的要求，变成可复现的机器门禁；主 Agent 不再靠自述通过门禁。

子命令：
    preflight       一次调用完成仓库校验、文档发现与有效性判定、快照冻结、GitNexus 探测
    start           冻结仓库快照并创建 RUN_DIR，写 state.json
    check-snapshot  复查当前快照是否与冻结时一致（HEAD / 工作树 / 文档）
    record-proposal 记录提案摘要（全文 digest + 承重字段 gate_digest）、方案 ID 与版本
    verify-approval 校验提案承重字段未变、快照未漂移、审批口令回显了方案 ID 和版本
    verify-gates    实施前一次性连跑 verify-approval + check-snapshot
    freeze-contract 从已批准的 state.json 与提案正文冻结 Implementation Contract 和验证计划
    verify-scope    用 git 计算实际变更文件，与获批允许文件做严格差集
    run-validation  以 shell=False 执行单条验证命令并记录真实退出码与错误指纹
    run-plan        在同一进程内顺序执行整份验证计划，逐条落盘，只回显摘要

审批绑定分两层，缺一不可：`digest` 是提案全文摘要（审计用），`gate_digest` 只覆盖
承重字段——方案 ID/版本、允许修改文件、明确不修改、必须保持的行为、计划修改表、
验证计划表、审批口令与审查模式（顺序无关，按集合比较）。`verify-approval` 对
`gate_digest` 永远严格失败；只有 gate 完全一致、仅叙述文字变化时，才允许用显式的
`--editorial` 放行，并把前后摘要追加进 `editorial_revisions` 留痕。提案缺少承重
字段时 gate_digest 退化为全文摘要，即任何改动都要重新审批。

为控制模型回合数，所有 stdout 默认只回显摘要（计数 + digest + 路径），完整快照需要
`--verbose` 才会打印；文档正文一律不进 stdout，改为写 `RUN_DIR/doc-map.md` 供按需
分区间读取。

产物目录固定为 ``<repo>/.codekit/bugfix/<run-id>/``，该目录已被 .gitignore 排除，
因此审批前保存提案不会产生受版本控制的文件变化。

退出码：0 门禁通过；1 门禁不通过（漂移、越界、验证失败）；2 用法、安全或 IO 错误；
3 同一错误指纹连续超过重试预算。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_bugfix_proposal as proposal_validator  # noqa: E402

SCHEMA_VERSION = 1
STATE_NAME = "state.json"
VALIDATION_NAME = "validation-record.json"
CONTRACT_NAME = "implementation-contract.json"
PLAN_NAME = "validation-plan.json"
DOC_MAP_NAME = "doc-map.md"
INVESTIGATION_NAME = "investigation.md"
FORBIDDEN_ARG = re.compile(r"[;&|`$()<>{}\n\r]")
DIGITS = re.compile(r"\d+")
HEX_RUN = re.compile(r"\b[0-9a-fA-F]{7,}\b")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OUTPUT_TAIL_LINES = 12
DEFAULT_MAX_REPEATS = 3
UNTRACKED_HASH_LIMIT = 500
UNTRACKED_HASH_MAX_BYTES = 2 * 1024 * 1024

# 资料门禁：文件名即种类，有效性由大小与残留占位符判定。
DOC_KINDS = {"product-doc.md": "prd", "technical-doc.md": "tdd", "release-doc.md": "rpd"}
MIN_DOC_BYTES = 256
DOC_PLACEHOLDER_LIMIT = 20
DOC_MAP_MAX_BYTES = 8 * 1024 * 1024
OUTLINE_MAX_ENTRIES = 200
MD_HEADING = re.compile(r"^(#{1,4})[ \t]+(\S.*?)[ \t]*#*[ \t]*$", flags=re.MULTILINE)
CJK_CHAR = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

# 审批承重的提案字段与表格；模板若重命名，record-proposal 会退化为全文绑定（更严，不是更松）。
GATE_LABELS = (
    "方案 ID", "方案版本", "状态", "推荐方案", "绑定快照", "允许修改文件", "明确不修改",
    "必须保持的行为", "备选方案及未选择原因", "回归测试", "人工验收", "审批状态", "审批范围", "审批口令",
)
GATE_TABLE_KEYS = ("文件/符号", "检查")
GATE_CRITICAL_LABELS = ("方案 ID", "方案版本", "允许修改文件")


class StateError(Exception):
    """用法、安全或 IO 层面的错误，对应退出码 2。"""


def _reconfigure_streams() -> None:
    """Windows 上 stdout/stderr 默认跟随 ANSI 代码页；重定向到管道时中文会变成乱码，
    而本脚本的输出正是主 Agent 修正提案的唯一依据，因此强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover
            pass


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0:
        raise StateError(f"git {' '.join(args)} 失败：{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StateError(f"无法读取 {path}: {exc}") from exc
    return "sha256:" + hashlib.sha256(data).hexdigest()


def status_entries(root: Path, exclude: str | None = None) -> list[str]:
    output = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip('"')
        if not path or is_under(path, exclude):
            continue
        if path not in paths:
            paths.append(path)
    return sorted(paths)


def is_under(path: str, prefix: str | None) -> bool:
    return bool(prefix) and (path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/"))


def head_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def ensure_run_dir_ignored(root: Path) -> None:
    """让 RUN_DIR 自我 ignore，避免产物被当成仓库变更而污染快照与越界判定。

    目标仓库的 .gitignore 不在本 Skill 的写入授权内，因此只在 `.codekit/bugfix/`
    下放置一份局部规则；已存在的仓库级 ignore 仍优先。
    """
    directory = root / ".codekit" / "bugfix"
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / ".gitignore"
    if not marker.exists() or marker.read_text(encoding="utf-8").strip() != "*":
        marker.write_text("*\n", encoding="utf-8")


def untracked_entries(root: Path, exclude: str | None) -> list[str]:
    """未跟踪文件的路径 + 内容摘要。

    只看文件清单会让「悄悄改写未跟踪文件」逃过漂移检测，所以这里补上内容摘要；
    文件数或单文件体积超预算时退化为只记清单，并在摘要里留下可审计的降级标记。
    """
    paths = sorted(
        line.strip() for line in git(root, "ls-files", "--others", "--exclude-standard").splitlines()
        if line.strip() and not is_under(line.strip(), exclude)
    )
    if len(paths) > UNTRACKED_HASH_LIMIT:
        return [f"untracked-count-only:{len(paths)}"]
    entries: list[str] = []
    for rel in paths:
        file = root / rel
        try:
            if file.is_file() and file.stat().st_size <= UNTRACKED_HASH_MAX_BYTES:
                digest = hashlib.sha256(file.read_bytes()).hexdigest()
                entries.append(f"{rel}\1sha256:{digest}")
            else:
                entries.append(f"{rel}\1too-large")
        except OSError as exc:
            entries.append(f"{rel}\1unreadable:{type(exc).__name__}")
    return entries


def read_doc_text(path: Path) -> str:
    """文档地图专用的宽容读取：坏字节按 U+FFFD 计入，不因为编码问题中断快照。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise StateError(f"无法读取 {path}: {exc}") from exc


def approx_tokens(text: str) -> int:
    """粗略 token 预算：中日韩字符按 1 token，其余按 4 字符 1 token。

    这个数只用来判断「整篇读入会不会撞上下文墙」，不是计费值，因此刻意取偏保守。
    """
    cjk = len(CJK_CHAR.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


def outline_of(text: str) -> tuple[list[str], bool]:
    """Markdown 标题 → 行区间索引；超过预算时截断并标记，不静默丢弃。"""
    matches = list(MD_HEADING.finditer(text))
    total_lines = text.count("\n") + 1
    entries: list[str] = []
    for index, match in enumerate(matches):
        end = text.count("\n", 0, matches[index + 1].start()) + 1 if index + 1 < len(matches) else total_lines
        level = len(match.group(1))
        title = match.group(2).strip()
        start = text.count("\n", 0, match.start()) + 1
        entries.append(f"L{start}-L{end} {'#' * level} {title}")
    return entries[:OUTLINE_MAX_ENTRIES], len(entries) > OUTLINE_MAX_ENTRIES


def document_meta(document: Path) -> dict:
    """冻结一份文档的定位信息：摘要、规模、估算 token 与分节区间索引。"""
    if not document.is_file():
        raise StateError(f"文档不可读，无法冻结：{document}")
    size = document.stat().st_size
    meta: dict = {
        "path": str(document),
        "kind": DOC_KINDS.get(document.name, "other"),
        "digest": sha256_file(document),
        "size": size,
    }
    if size > DOC_MAP_MAX_BYTES or document.suffix.lower() not in (".md", ".markdown", ".txt", ".rst", ".adoc"):
        meta.update({"lines": None, "approx_tokens": None, "outline": [], "outline_truncated": False, "map": "skipped-large"})
        return meta
    text = read_doc_text(document)
    outline, truncated = outline_of(text)
    meta.update({
        "lines": text.count("\n") + 1,
        "approx_tokens": approx_tokens(text),
        "outline": outline,
        "outline_truncated": truncated,
    })
    return meta


def write_doc_map(run_dir: Path, documents: list[dict]) -> str | None:
    """把区间索引写成按需读取的文件；派发子代理时只传路径，不传文档正文。"""
    if not documents:
        return None
    lines = [
        "# 文档地图（由 bugfix_state.py 生成，请勿手工编辑）",
        "",
        "读取纪律：先在本文件按标题定位区间，再用 `Read` 的 offset/limit 只读命中区间；",
        "禁止把整篇文档读入上下文，禁止为了找一个落点而通读全文。",
        "",
    ]
    for doc in documents:
        lines.append(f"## {doc['path']}")
        lines.append(
            f"- 种类 `{doc['kind']}`，{doc.get('lines') or '未知'} 行，"
            f"{doc['size']} 字节，估算 {doc.get('approx_tokens') or '未知'} tokens，digest `{doc['digest'][:19]}…`"
        )
        if doc.get("outline_truncated"):
            lines.append(f"- 区间索引已截断到前 {OUTLINE_MAX_ENTRIES} 条（`--verbose` 重跑可核对完整清单）")
        lines += [f"- {entry}" for entry in doc.get("outline", [])]
        lines.append("")
    path = run_dir / DOC_MAP_NAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def judge_document(path: Path) -> tuple[bool, str]:
    """资料门禁第 4/5 条的确定性实现：空文件、模板与未填完的草稿都不算有效文档。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"unreadable:{type(exc).__name__}"
    if size < MIN_DOC_BYTES:
        return False, "empty-or-too-small"
    try:
        text = read_doc_text(path)
    except StateError as exc:
        return False, f"unreadable:{exc}"
    placeholders = len(proposal_validator.PLACEHOLDER.findall(text))
    if placeholders >= DOC_PLACEHOLDER_LIMIT:
        return False, f"template-or-draft:{placeholders}-placeholders"
    return True, ""


def discover_documents(root: Path, feature_directory: str | None) -> tuple[list[Path], list[dict], str]:
    """按 SKILL 的资料解析顺序找文档，返回（有效文档, 被拒文档及理由, 扫描范围）。

    `feature-directory` 非空时只在该目录内查找，找不到也不转扫 `docs/`；为空时才递归
    仓库根目录的 `docs/`。两种情况下缺失都只是 `DOCUMENTS: NOT_AVAILABLE`，不是阻塞。
    """
    if feature_directory:
        base = relative_to_root(root, feature_directory)
        scope = str(base)
        candidates = sorted(base.rglob("*")) if base.is_dir() else []
        scope_missing = not base.is_dir()
    else:
        base = root / "docs"
        scope = str(base)
        candidates = sorted(base.rglob("*")) if base.is_dir() else []
        scope_missing = not base.is_dir()
    valid: list[Path] = []
    rejected: list[dict] = []
    for candidate in candidates:
        if not candidate.is_file() or candidate.name not in DOC_KINDS:
            continue
        ok, reason = judge_document(candidate)
        if ok:
            valid.append(candidate)
        else:
            rejected.append({"path": str(candidate), "kind": DOC_KINDS[candidate.name], "reason": reason})
    if scope_missing:
        rejected.append({"path": scope, "kind": "scope", "reason": "directory-missing-or-unreadable"})
    return valid, rejected, scope


def probe_gitnexus(root: Path) -> dict:
    """只做存在性探测，不启动子进程：GitNexus 不可用是正常分支，不该花一个回合去试错。"""
    runner = (root / ".gitnexus" / "run.cjs").is_file()
    cli = bool(shutil.which("gitnexus"))
    index = (root / ".gitnexus").is_dir()
    available = (runner or cli) and index
    return {
        "status": "AVAILABLE" if available else "NOT_AVAILABLE",
        "project_runner": runner,
        "cli_on_path": cli,
        "index_dir": index,
        "note": None if available else "记录 GitNexus: NOT_AVAILABLE 并继续用代码、测试和文本搜索定位调用关系",
    }


def capture_snapshot(root: Path, documents: list[Path], exclude: str | None = None) -> dict:
    """冻结仓库与工作树状态。

    工作树摘要覆盖 `git status` 条目、已跟踪文件的二进制 diff 和未跟踪文件内容；
    写入范围是否越界另由 verify-scope 用 git 精确判定。
    """
    diff = ""
    base = head_sha(root)
    if base:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--binary", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        diff = result.stdout
    return {
        "captured_at": now(),
        "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "head_sha": base,
        "working_tree_digest": sha256_text(
            "\0".join(status_entries(root, exclude))
            + "\1" + "\0".join(untracked_entries(root, exclude))
            + "\2" + diff
        ),
        "preexisting_changes": status_entries(root, exclude),
        "documents": [document_meta(document) for document in documents],
    }


def snapshot_summary(snapshot: dict) -> dict:
    """回显给主 Agent 的最小快照信息：够用于报告与定位，不含任何清单正文。"""
    return {
        "captured_at": snapshot["captured_at"],
        "branch": snapshot["branch"],
        "head_sha": snapshot["head_sha"],
        "working_tree_digest": snapshot["working_tree_digest"],
        "preexisting_changes_count": len(snapshot.get("preexisting_changes", [])),
        "documents": [
            {
                "path": doc["path"],
                "kind": doc["kind"],
                "digest": doc["digest"],
                "size": doc["size"],
                "lines": doc.get("lines"),
                "approx_tokens": doc.get("approx_tokens"),
                "outline_entries": len(doc.get("outline", [])),
                "outline_truncated": doc.get("outline_truncated", False),
            }
            for doc in snapshot.get("documents", [])
        ],
    }


def state_path(value: str | Path) -> Path:
    path = Path(value)
    if path.name != STATE_NAME:
        raise StateError(f"--manifest 必须指向 {STATE_NAME}：{path}")
    return path


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StateError(f"无法读取 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StateError(f"{path} 不是合法 JSON：{exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise StateError(f"{path} 的 schema_version 不受支持：{data.get('schema_version')}")
    return data


def save_state(path: Path, data: dict) -> None:
    data["updated_at"] = now()
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("verdict") in {"OK", "PASS"} else 1


def relative_to_root(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else root / candidate


def bare(value: str) -> str:
    return value.strip().strip("`").strip()


def proposal_labels(proposal_path: Path) -> dict[str, list[str]]:
    text = read_text_file(proposal_path)
    labels: dict[str, list[str]] = {}
    for section in proposal_validator.split_sections(text):
        for label, values in proposal_validator.section_fields(section.body).items():
            labels.setdefault(bare(label), []).extend(bare(value) for value in values)
    return labels


def proposal_gate_payload(proposal_path: Path, review_mode: str) -> dict | None:
    """提取审批承重字段；缺少关键标签时返回 None，由调用方退化为全文绑定。

    比较刻意采用集合语义（排序去重）：重排行、调整同一字段内的顺序都不是实质变更，
    而任何值的新增/删除/改写都会改变 gate_digest。
    """
    text = read_text_file(proposal_path)
    labels: dict[str, list[str]] = {}
    tables: dict[str, list[str]] = {}
    for section in proposal_validator.split_sections(text):
        for label, values in proposal_validator.section_fields(section.body).items():
            key = bare(label)
            if key in GATE_LABELS:
                labels.setdefault(key, []).extend(bare(value) for value in values)
        for header, rows in proposal_validator.iter_tables(section.body):
            first = bare(header[0]) if header else ""
            if first in GATE_TABLE_KEYS:
                tables.setdefault(first, []).extend("|".join(bare(cell) for cell in row) for row in rows)
    if not any(label in labels for label in GATE_CRITICAL_LABELS):
        return None
    return {
        "labels": {key: sorted(values) for key, values in sorted(labels.items())},
        "tables": {key: sorted(values) for key, values in sorted(tables.items())},
        "review_mode": review_mode,
    }


def canonical_digest(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def gate_digest_of(payload: dict | None) -> str | None:
    if payload is None:
        return None
    # 刻意不掺入全文摘要：掺了就等于每次叙述改写都推翻 gate，编辑性豁免这条路会形同虚设。
    return canonical_digest(payload)


def document_shape(text: str) -> dict:
    """章节骨架与篇幅，用来把“编辑性修订”限制在原地润色，而不是整篇换稿。"""
    return {
        "headings": [title for _, title in MD_HEADING.findall(text)],
        "lines": len(text.splitlines()),
    }


def shape_matches(frozen: dict | None, current: dict) -> bool:
    if not frozen:  # 旧 state.json 没有骨架信息，只按 gate 与篇幅之外的检查放行
        return True
    if frozen.get("headings") != current["headings"]:
        return False
    baseline = int(frozen.get("lines") or 0)
    return abs(current["lines"] - baseline) <= max(8, baseline // 4)


def split_paths(value: str) -> list[str]:
    return [token.strip().strip("`") for token in re.split(r"[,，;；\s]+", value.strip()) if token.strip()]


# --------------------------------------------------------------------------- 子命令

def create_run(root: Path, run_id: str, documents: list[Path], force: bool) -> tuple[Path, dict]:
    if not (root / ".git").exists():
        raise StateError(f"{root} 不是 git 仓库根目录")
    if not SAFE_NAME.fullmatch(run_id):
        raise StateError(f"--run-id 只能是目录安全名称：{run_id}")
    run_dir = root / ".codekit" / "bugfix" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ensure_run_dir_ignored(root)
    manifest = run_dir / STATE_NAME
    if manifest.exists() and not force:
        raise StateError(f"{manifest} 已存在；用 --force 覆盖，或换一个 --run-id")
    relative_run_dir = run_dir.relative_to(root).as_posix()
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "root": str(root),
        "run_dir": relative_run_dir,
        "created_at": now(),
        "snapshot": capture_snapshot(root, documents, relative_run_dir),
        "proposal": None,
        "approval": None,
    }
    save_state(manifest, state)
    return manifest, state


def cmd_start(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    documents = [relative_to_root(root, value) for value in (args.documents or [])]
    manifest, state = create_run(root, args.run_id, documents, args.force)
    doc_map = write_doc_map(manifest.parent, state["snapshot"]["documents"])
    payload: dict = {"verdict": "OK", "manifest": str(manifest), "doc_map": doc_map}
    if args.verbose:
        payload["snapshot"] = state["snapshot"]
    else:
        payload["snapshot"] = snapshot_summary(state["snapshot"])
    return emit(payload)


def cmd_preflight(args: argparse.Namespace) -> int:
    """把「参数解析 → 资料门禁 → 快照冻结 → 工具探测」压成一次调用。

    这些步骤的判定全是确定性的，交给模型逐条执行只会多付回合数，而且无法保证它真的
    按第 4/5 条做过有效性判定；脚本化后 `DOCUMENTS: NOT_AVAILABLE` 是一个字段而不是一段自觉。
    """
    root = Path(args.root).resolve()
    valid, rejected, scope = discover_documents(root, args.feature_directory)
    manifest, state = create_run(root, args.run_id, valid, args.force)
    doc_map = write_doc_map(manifest.parent, state["snapshot"]["documents"])
    summary = snapshot_summary(state["snapshot"])
    payload = {
        "verdict": "OK",
        "manifest": str(manifest),
        "run_dir": str(manifest.parent),
        "documents_status": "AVAILABLE" if valid else "NOT_AVAILABLE",
        "suggested_review_mode": "FULL" if valid else "LITE",
        "scan_scope": scope,
        "documents": summary["documents"],
        "rejected_documents": rejected,
        "doc_map": doc_map,
        "gitnexus": probe_gitnexus(root),
        "snapshot": summary,
        "investigation_file": str(manifest.parent / INVESTIGATION_NAME),
    }
    if args.verbose:
        payload["snapshot"] = state["snapshot"]
    return emit(payload)


def cmd_check_snapshot(args: argparse.Namespace) -> int:
    return emit(evaluate_snapshot(args))


def evaluate_snapshot(args: argparse.Namespace) -> dict:
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    reference = state.get("proposal") or state["snapshot"]
    exclude = state.get("run_dir")
    current = capture_snapshot(Path(state["root"]), [], exclude)
    drift: list[str] = []
    if current["head_sha"] != reference["head_sha"]:
        drift.append("head")
    if current["working_tree_digest"] != reference["working_tree_digest"]:
        drift.append("working-tree")
    for document in state["snapshot"].get("documents", []):
        path, digest = Path(document["path"]), document["digest"]
        if not path.is_file():
            drift.append(f"document-missing:{document['path']}")
        elif sha256_file(path) != digest:
            drift.append(f"document-changed:{document['path']}")
    payload: dict = {
        "verdict": "OK" if not drift else "DRIFT",
        "drift": drift,
        "reference": "proposal" if state.get("proposal") else "start",
    }
    if args.verbose:
        payload["current"] = current
    else:
        payload["current"] = {
            "head_sha": current["head_sha"],
            "working_tree_digest": current["working_tree_digest"],
            "preexisting_changes_count": len(current["preexisting_changes"]),
        }
    return payload


def cmd_record_proposal(args: argparse.Namespace) -> int:
    return emit(evaluate_record_proposal(args))


def evaluate_record_proposal(args: argparse.Namespace) -> dict:
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    proposal = Path(args.proposal)
    if not re.fullmatch(r"BF-\d{8}-[A-Za-z0-9][A-Za-z0-9-]*", args.proposal_id):
        raise StateError(f"--proposal-id 必须符合 BF-YYYYMMDD-short-name：{args.proposal_id}")
    if not re.fullmatch(r"\d+", str(args.version)):
        raise StateError(f"--version 必须是整数：{args.version}")
    digest = sha256_file(proposal)
    gate = gate_digest_of(proposal_gate_payload(proposal, args.review_mode))
    state["proposal"] = {
        "path": str(proposal),
        "digest": digest,
        "gate_digest": gate,
        "gate_bound": "fields" if gate else "full-text",
        "shape": document_shape(read_text_file(proposal)),
        "editorial_revisions": [],
        "proposal_id": args.proposal_id,
        "version": str(args.version),
        "review_mode": args.review_mode,
        "recorded_at": now(),
        "head_sha": state["snapshot"]["head_sha"],
        "working_tree_digest": state["snapshot"]["working_tree_digest"],
    }
    save_state(manifest, state)
    return {"verdict": "OK", "proposal": state["proposal"]}


def normalized(text: str) -> str:
    return re.sub(r"[`\s]+", "", text)


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StateError(f"无法读取 {path}: {exc}") from exc


def cmd_verify_approval(args: argparse.Namespace) -> int:
    return emit(evaluate_approval(args))


def evaluate_approval(args: argparse.Namespace) -> dict:
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    record = state.get("proposal")
    if not record:
        raise StateError("尚未执行 record-proposal，无法校验审批")
    proposal_path = Path(args.proposal or record["path"])
    checks: dict[str, object] = {}
    current_digest = sha256_file(proposal_path)
    gate = gate_digest_of(proposal_gate_payload(proposal_path, record.get("review_mode", "FULL")))
    # 旧 state.json 没有 gate_digest 时保持全文绑定语义，否则未改动的提案也会被误判为漂移；
    # 新版提案若缺少承重字段（gate 为 None），退回全文比较，只会更严不会更松。
    frozen_gate = record.get("gate_digest")
    live_gate = (gate or current_digest) if frozen_gate else current_digest
    if not frozen_gate:
        frozen_gate = record["digest"]
    checks["proposal_gate_match"] = live_gate == frozen_gate
    checks["proposal_digest_match"] = current_digest == record["digest"]
    checks["proposal_shape_match"] = shape_matches(record.get("shape"), document_shape(read_text_file(proposal_path)))
    current = capture_snapshot(Path(state["root"]), [], state.get("run_dir"))
    checks["head_sha_match"] = current["head_sha"] == record["head_sha"]
    checks["working_tree_match"] = current["working_tree_digest"] == record["working_tree_digest"]
    text = read_text_file(Path(args.approval_file))
    phrase = normalized(f"批准执行 {record['proposal_id']} v{record['version']}")
    checks["phrase_bound"] = phrase in normalized(text)

    failed = [name for name, ok in checks.items() if not ok]
    editorial_only = failed == ["proposal_digest_match"]
    note = "审批原文必须逐字包含批准执行 <方案 ID> v<版本>；快照或提案摘要漂移时必须重新审批"
    if editorial_only and not args.editorial:
        note = (
            "仅提案叙述文字变化、承重字段（方案 ID/版本、允许修改文件、明确不修改、必须保持的行为、"
            "计划修改表、验证计划、审查模式）与章节骨架均未实质变化。确认确属编辑性修订后加 "
            "--editorial 重跑，否则按新版本提案重新请求审批"
        )
    elif editorial_only and args.editorial:
        record.setdefault("editorial_revisions", []).append({
            "from_digest": record["digest"],
            "to_digest": current_digest,
            "from_gate": record.get("gate_digest"),
            "to_gate": gate,
            "at": now(),
            "note": args.editorial_note or "未提供说明",
        })
        record["digest"] = current_digest
        record["gate_digest"] = gate
        record["editorial_approved"] = True
        failed = [name for name, ok in checks.items() if not ok and name != "proposal_digest_match"]
        note = "编辑性修订已按 --editorial 放行并留痕；承重字段未变化，审批仍绑定同一方案版本"
    state["approval"] = {
        "approval_file": str(args.approval_file),
        "proposal_digest": record["digest"],
        "proposal_gate_digest": record.get("gate_digest"),
        "editorial": bool(editorial_only and args.editorial),
        "approved_snapshot": {"head_sha": current["head_sha"], "working_tree_digest": current["working_tree_digest"]},
        "bound": f"批准执行 {record['proposal_id']} v{record['version']}",
        "approved_at": now(),
    }
    save_state(manifest, state)
    return {
        "verdict": "OK" if not failed else "FAIL",
        "failed_checks": failed,
        "checks": checks,
        "proposal_id": record["proposal_id"],
        "version": record["version"],
        "review_mode": record.get("review_mode"),
        "editorial": bool(editorial_only and args.editorial),
        "note": note,
    }


def cmd_verify_gates(args: argparse.Namespace) -> int:
    """实施前的「重新确认」压成一次调用：审批绑定 + 快照漂移合并报告。

    两项门禁本来就要求连着跑（execution-and-verification §1），分开执行只会让主 Agent
    多付一次模型往返，所以复用同一份进程内的判定函数，语义与单独调用完全一致。
    """
    manifest = state_path(args.manifest)
    approval = evaluate_approval(argparse.Namespace(
        manifest=str(manifest),
        approval_file=args.approval_file,
        proposal=args.proposal,
        editorial=args.editorial,
        editorial_note=args.editorial_note,
    ))
    snapshot = evaluate_snapshot(argparse.Namespace(manifest=str(manifest), verbose=False))
    ok = approval["verdict"] == "OK" and snapshot["verdict"] == "OK"
    return emit({
        "verdict": "OK" if ok else "FAIL",
        "approval": approval,
        "snapshot": snapshot,
        "note": (
            "两项都通过才能进入第二阶段"
            if ok else "任一项失败都必须停止实施：先按 drift 归因漂移，再更新提案并重新审批"
        ),
    })


def cmd_freeze_contract(args: argparse.Namespace) -> int:
    """从 state.json 与提案正文机械冻结实施合同，杜绝手写 start_head_sha/diff digest。"""
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    approval = state.get("approval")
    if not approval:
        raise StateError("verify-approval 尚未通过，无法冻结实施合同")
    record = state.get("proposal") or {}
    proposal_path = Path(args.proposal or record.get("path", ""))
    labels = proposal_labels(proposal_path) if proposal_path.is_file() else {}
    allowed = list(args.allowed or [])
    if args.allowed_file:
        allowed += [line.strip() for line in read_text_file(Path(args.allowed_file)).splitlines() if line.strip() and not line.startswith("#")]
    if not allowed:
        for value in labels.get("允许修改文件", []):
            allowed += split_paths(value)
    if not allowed:
        raise StateError("无法确定允许写入范围：提案缺少『允许修改文件』且未给 --allowed/--allowed-file")
    contract = {
        "approval_record": {
            "proposal_id": record.get("proposal_id"),
            "proposal_version": record.get("version"),
            "proposal_digest": approval.get("proposal_digest"),
            "proposal_gate_digest": approval.get("proposal_gate_digest"),
            "approved_at": approval.get("approved_at"),
        },
        "objective": args.objective or "; ".join(labels.get("推荐方案", [])) or None,
        "repo_root": state["root"],
        "review_mode": record.get("review_mode"),
        "start_head_sha": approval["approved_snapshot"]["head_sha"],
        "start_diff_digest": approval["approved_snapshot"]["working_tree_digest"],
        "preexisting_changes": state["snapshot"].get("preexisting_changes", []),
        "allowed_write_files": sorted(set(allowed)),
        "forbidden_changes": labels.get("明确不修改", []),
        "behavior_to_preserve": labels.get("必须保持的行为", []),
        "planned_changes": planned_changes_from_proposal(proposal_path) if proposal_path.is_file() else [],
        "acceptance_criteria": list(args.acceptance or []) or labels.get("回归测试", []),
        "validation_commands": [],
        "retry": {"fingerprint": None, "used": 0, "remaining": args.retry_budget},
        "run_dir": state["run_dir"],
        "doc_map": str(manifest.parent / DOC_MAP_NAME),
        "frozen_at": now(),
    }
    plan = validation_plan_from_proposal(proposal_path) if proposal_path.is_file() else []
    contract["validation_commands"] = plan
    contract_digest = canonical_digest(contract)
    contract["contract_digest"] = contract_digest
    contract_path = manifest.parent / CONTRACT_NAME
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    plan_path = None
    if plan:
        plan_path = manifest.parent / PLAN_NAME
        plan_path.write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, "contract_digest": contract_digest, "checks": plan},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return emit({
        "verdict": "OK",
        "contract": str(contract_path),
        "contract_digest": contract_digest,
        "plan": str(plan_path) if plan_path else None,
        "allowed_write_files": contract["allowed_write_files"],
        "validation_items": len(plan),
        "note": "把本文件路径交给 Implementer，不要复述其内容；合同里的 SHA 与 digest 不得手写",
    })


def planned_changes_from_proposal(proposal_path: Path) -> list[str]:
    text = read_text_file(proposal_path)
    rows: list[str] = []
    for section in proposal_validator.split_sections(text):
        for header, table_rows in proposal_validator.iter_tables(section.body):
            if not header or bare(header[0]) != "文件/符号":
                continue
            rows += [" | ".join(bare(cell) for cell in row) for row in table_rows]
    return rows


def validation_plan_from_proposal(proposal_path: Path) -> list[dict]:
    """把提案 §5 的验证计划表直接变成可执行计划，省掉一次人工转录与转录错位风险。"""
    text = read_text_file(proposal_path)
    checks: list[dict] = []
    for section in proposal_validator.split_sections(text):
        for header, table_rows in proposal_validator.iter_tables(section.body):
            cells = [bare(cell) for cell in header]
            if not cells or cells[0] != "检查":
                continue
            command_index = cells.index("命令/方法") if "命令/方法" in cells else 1
            proves_index = cells.index("证明内容") if "证明内容" in cells else 2
            required_index = next((index for index, cell in enumerate(cells) if "必需" in cell or cell.lower() == "must"), None)
            for row in table_rows:
                if len(row) <= max(command_index, proves_index):
                    continue
                command = bare(row[command_index])
                required = bare(row[required_index]).lower() in {"yes", "y", "是", "必须", "必需"} if required_index is not None and len(row) > required_index else True
                try:
                    argv = shlex.split(command)
                except ValueError:
                    argv = []
                checks.append({
                    "name": bare(row[0]),
                    "proves": bare(row[proves_index]),
                    "required": required,
                    "command": argv,
                    "command_text": command,
                })
    return checks


def matches_allowed(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/"):
            if path.startswith(pattern) or path == pattern.rstrip("/"):
                return True
        elif pattern == path:
            return True
    return False


def git_names(root: Path, exclude: str | None, *args: str) -> set[str]:
    return {
        line for line in git(root, *args).splitlines()
        if line.strip() and not is_under(line.strip(), exclude)
    }


def cmd_verify_scope(args: argparse.Namespace) -> int:
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    root = Path(state["root"])
    exclude = state.get("run_dir")
    reference = (state.get("approval") or {}).get("approved_snapshot") or state.get("proposal") or state["snapshot"]
    base = reference["head_sha"] or head_sha(root)
    changed = set(status_entries(root, exclude))
    if base:
        tracked = git_names(root, exclude, "diff", "--name-only", base)
        staged = git_names(root, exclude, "diff", "--name-only", "--cached", base)
    else:  # 仓库还没有任何提交，只能以工作树状态为准
        tracked, staged = set(), git_names(root, exclude, "diff", "--name-only", "--cached")
    untracked = git_names(root, exclude, "ls-files", "--others", "--exclude-standard")
    produced = sorted(tracked | staged | (untracked & changed))
    preexisting = set(state["snapshot"]["preexisting_changes"])
    allowed = list(args.allowed or [])
    if args.allowed_file:
        allowed += [
            line.strip() for line in read_text_file(Path(args.allowed_file)).splitlines()
            if line.strip() and not line.startswith("#")
        ]
    contract_integrity = "NOT_APPLICABLE"
    if args.contract:
        contract = json.loads(read_text_file(Path(args.contract)))
        allowed += list(contract.get("allowed_write_files", []))
        declared = contract.get("contract_digest")
        live = canonical_digest({key: value for key, value in contract.items() if key != "contract_digest"})
        contract_integrity = "OK" if declared and declared == live else ("MISSING_DIGEST" if not declared else "TAMPERED")
    if not allowed:
        raise StateError("verify-scope 需要 --allowed、--allowed-file 或 --contract，否则越界判定无意义")
    attributed = [path for path in produced if path not in preexisting]
    out_of_scope = [path for path in attributed if not matches_allowed(path, allowed)]
    # 冻结时就已修改、现在又出现在 diff 里的已跟踪文件：本次改动无法与用户原有修改区分。
    ambiguous = sorted(preexisting & (tracked | staged))
    verdict = "OK" if not out_of_scope and not ambiguous and contract_integrity != "TAMPERED" else "FAIL"
    return emit({
        "verdict": verdict,
        "base_sha": base,
        "changed_files": produced,
        "out_of_scope": out_of_scope,
        "preexisting_excluded": sorted(preexisting & set(produced)),
        "preexisting_touched": ambiguous,
        "contract_integrity": contract_integrity,
        "allowed": list(dict.fromkeys(allowed)),
    })


def validate_command(command: list[str]) -> list[str]:
    if not command:
        raise StateError("run-validation 需要在 `--` 之后给出完整命令")
    for token in command:
        if FORBIDDEN_ARG.search(token):
            raise StateError(f"验证命令包含禁止的 shell 元字符：{token!r}")
        if ".." in Path(token).parts:
            raise StateError(f"验证命令不允许路径上跳：{token!r}")
    return command


def resolve_program(root: Path, program: str) -> str:
    if os.sep in program or program.startswith("./"):
        candidate = relative_to_root(root, program)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise StateError(f"命令不存在或不可执行：{program}")
    resolved = shutil.which(program)
    if not resolved:
        raise StateError(f"命令不在 PATH 上：{program}")
    return resolved


def fingerprint(program: str, exit_code: int | None, output: str) -> str:
    normalized_output = HEX_RUN.sub("#", DIGITS.sub("#", output))[-400:]
    digest = hashlib.sha256(f"{program}\1{exit_code}\1{normalized_output}".encode()).hexdigest()
    return digest[:12]


def tail(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    kept = [line for line in text.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def load_record(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"{path} 损坏，无法追加验证记录：{exc}") from exc
    return {"schema_version": SCHEMA_VERSION, "checks": []}


def save_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run_check(root: Path, command: list[str], proves: str, timeout: float, tail_lines: int) -> dict:
    """执行一条已通过的验证命令并构造记录条目；命令不合法或程序缺失时抛 StateError。"""
    program = resolve_program(root, command[0])
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            [program, *command[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(root), timeout=timeout, check=False,
        )
        exit_code: int | None = completed.returncode
        output = (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        raw = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        output = raw or ""
    except OSError as exc:
        # Windows 上 npm/npx 这类 .cmd 垫片无法被 shell=False 直接启动：报成 NOT_AVAILABLE
        # 比抛栈更好，主 Agent 才能把它归到「需要人工/换入口」而不是读一段 traceback。
        raise StateError(f"命令无法启动：{program}：{exc}") from exc
    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "command": command,
        "cwd": str(root),
        "exit_code": exit_code,
        "result": "TIMEOUT" if timed_out else ("PASS" if exit_code == 0 else "FAIL"),
        "duration_ms": duration_ms,
        "proves": proves,
        "fingerprint": fingerprint(command[0], exit_code, output),
        "output_tail": tail(output, tail_lines),
        "executed_at": now(),
        "source": "executed",
    }


def not_available_entry(root: Path, command: list[str], proves: str, reason: str) -> dict:
    return {
        "command": command,
        "cwd": str(root),
        "exit_code": None,
        "result": "NOT_AVAILABLE",
        "duration_ms": 0,
        "proves": proves,
        "fingerprint": fingerprint(command[0] if command else "", None, reason),
        "output_tail": "",
        "reason": reason,
        "executed_at": now(),
        "source": "executed",
    }


def consecutive_failures(checks: list[dict], entry: dict) -> int:
    if entry["result"] != "FAIL":
        return 1
    count = 1
    for previous in reversed(checks):
        if previous.get("result") != "FAIL" or previous.get("fingerprint") != entry["fingerprint"]:
            break
        count += 1
    return count


def cmd_run_validation(args: argparse.Namespace) -> int:
    if args.manifest:
        manifest = state_path(args.manifest)
        state = load_state(manifest)
        root = Path(state["root"])
        default_record = manifest.parent / VALIDATION_NAME
    else:
        root = Path(args.cwd or Path.cwd())
        default_record = None
    if not args.record and default_record is None:
        raise StateError("run-validation 需要 --record 或 --manifest 才能落盘验证记录")
    record_path = Path(args.record) if args.record else default_record
    command = validate_command(list(args.command or []))
    try:
        entry = run_check(root, command, args.proves, args.timeout, args.tail_lines)
    except StateError as exc:
        payload = {
            "verdict": "NOT_AVAILABLE", "command": command, "cwd": str(root),
            "exit_code": None, "result": "NOT_AVAILABLE", "reason": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    record = load_record(record_path)
    consecutive = consecutive_failures(record["checks"], entry)
    record["checks"].append(entry)
    save_record(record_path, record)

    payload = dict(entry)
    payload["repeat_count"] = consecutive
    payload["record"] = str(record_path)
    if entry["result"] == "FAIL" and consecutive >= args.max_repeats:
        payload["verdict"] = "RETRY_BUDGET_EXCEEDED"
        payload["result"] = "RETRY_BUDGET_EXCEEDED"
        payload["note"] = f"同一错误指纹已连续 {consecutive} 次，超过重试预算 {args.max_repeats}，必须停止并报告"
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 3
    payload["verdict"] = "PASS" if entry["result"] == "PASS" else "FAIL"
    return emit(payload)


def cmd_run_plan(args: argparse.Namespace) -> int:
    """一进程执行整份验证计划：N 条检查付 1 个模型回合，逐条仍然独立落盘。

    与 run-validation 共用同一套 shell=False、元字符黑名单、指纹与重试预算语义；
    单条命令不合法或程序缺失时记为 NOT_AVAILABLE 而不是中断整份计划，
    这样一份带 `&&` 的验证计划会在记录里显式留下不能自动执行的项，而不是静默跳过。
    """
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    root = Path(state["root"])
    record_path = Path(args.record) if args.record else manifest.parent / VALIDATION_NAME
    plan_path = Path(args.plan) if args.plan else manifest.parent / PLAN_NAME
    raw = json.loads(read_text_file(plan_path))
    items = raw.get("checks") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise StateError(f"验证计划为空或结构不受支持：{plan_path}")

    record = load_record(record_path)
    summary: list[dict] = []
    budget_hit: str | None = None
    for item in items:
        command = list(item.get("command") or [])
        proves = item.get("proves") or item.get("name") or ""
        if not command:
            entry = not_available_entry(root, ["<empty>"], proves, f"计划项 {item.get('name')} 没有可执行命令（人工验收项）")
        else:
            try:
                validate_command(command)
                entry = run_check(root, command, proves, args.timeout, args.tail_lines)
            except StateError as exc:
                entry = not_available_entry(root, command, proves, str(exc))
        entry.setdefault("name", item.get("name"))
        entry["required"] = bool(item.get("required", True))
        consecutive = consecutive_failures(record["checks"], entry)
        record["checks"].append(entry)
        save_record(record_path, record)
        summary.append({
            "name": entry["name"], "command": command, "result": entry["result"],
            "exit_code": entry["exit_code"], "fingerprint": entry["fingerprint"],
            "repeat_count": consecutive, "required": entry["required"], "proves": proves,
        })
        if entry["result"] == "FAIL" and consecutive >= args.max_repeats:
            budget_hit = f"同一错误指纹已连续 {consecutive} 次：{command}"
            break

    required = [row for row in summary if row["required"]]
    unsatisfied = [row for row in required if row["result"] != "PASS"]
    payload = {
        "verdict": "PASS" if not unsatisfied and not budget_hit else "FAIL",
        "record": str(record_path),
        "plan": str(plan_path),
        "executed": len(summary),
        "planned": len(items),
        "passed": sum(1 for row in summary if row["result"] == "PASS"),
        "unsatisfied_required": unsatisfied,
        "items": summary,
        "note": (
            budget_hit
            or "全部必需项 PASS；NOT_AVAILABLE/TIMEOUT/NOT_RUN 一律不能满足必需项，必须补跑或改为人工验收并说明原因"
        ),
    }
    if budget_hit:
        payload["result"] = "RETRY_BUDGET_EXCEEDED"
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 3
    return emit(payload)


# ----------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command_name", required=True)

    preflight = sub.add_parser("preflight", help="一次完成参数解析、资料门禁、快照冻结与工具探测")
    preflight.add_argument("--root", required=True)
    preflight.add_argument("--run-id", required=True)
    preflight.add_argument("--feature-directory", default=None, help="当前需求目录；省略时递归扫描仓库根 docs/")
    preflight.add_argument("--force", action="store_true")
    preflight.add_argument("--verbose", action="store_true", help="额外输出完整快照（含全部清单条目）")
    preflight.set_defaults(handler=cmd_preflight)

    start = sub.add_parser("start", help="冻结仓库快照并创建 RUN_DIR")
    start.add_argument("--root", required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--documents", action="extend", nargs="+", default=[],
                       help="要一并冻结摘要的 PRD/TDD/RPD 文件，可重复传入")
    start.add_argument("--force", action="store_true")
    start.add_argument("--verbose", action="store_true", help="额外输出完整快照（含全部清单条目）")
    start.set_defaults(handler=cmd_start)

    check = sub.add_parser("check-snapshot", help="复查当前快照是否相对提案/审批时漂移")
    check.add_argument("--manifest", required=True)
    check.add_argument("--verbose", action="store_true", help="额外输出当前完整快照")
    check.set_defaults(handler=cmd_check_snapshot)

    record = sub.add_parser("record-proposal", help="记录提案摘要、方案 ID 与版本")
    record.add_argument("--manifest", required=True)
    record.add_argument("--proposal", required=True)
    record.add_argument("--proposal-id", required=True)
    record.add_argument("--version", type=int, required=True)
    record.add_argument("--review-mode", choices=("FULL", "LITE", "WAIVED"), default="FULL")
    record.add_argument(
        "--confirm-review-mode", action="store_true",
        help="确认 FULL（无文档）或 WAIVED 这两种降级已经取得用户明确批准",
    )
    record.set_defaults(handler=cmd_proposal_review_mode)

    approval = sub.add_parser("verify-approval", help="校验提案承重字段未变、快照未漂移、审批口令绑定版本")
    approval.add_argument("--manifest", required=True)
    approval.add_argument("--approval-file", required=True)
    approval.add_argument("--proposal")
    approval.add_argument(
        "--editorial", action="store_true",
        help="仅当 gate_digest 完全一致、只有叙述文字变化时放行，并把前后摘要留痕",
    )
    approval.add_argument("--editorial-note", default=None, help="编辑性修订的说明，写入 editorial_revisions")
    approval.set_defaults(handler=cmd_verify_approval)

    gates = sub.add_parser("verify-gates", help="实施前一次性连跑 verify-approval + check-snapshot")
    gates.add_argument("--manifest", required=True)
    gates.add_argument("--approval-file", required=True)
    gates.add_argument("--proposal")
    gates.add_argument("--editorial", action="store_true")
    gates.add_argument("--editorial-note", default=None)
    gates.set_defaults(handler=cmd_verify_gates)

    contract = sub.add_parser("freeze-contract", help="从 state.json 与提案冻结 Implementation Contract 和验证计划")
    contract.add_argument("--manifest", required=True)
    contract.add_argument("--proposal", help="默认取 state.json 里记录的提案路径")
    contract.add_argument("--allowed", action="extend", nargs="+", default=[], help="覆盖提案里的允许写入文件")
    contract.add_argument("--allowed-file")
    contract.add_argument("--objective", default=None)
    contract.add_argument("--acceptance", action="extend", nargs="+", default=[])
    contract.add_argument("--retry-budget", type=int, default=DEFAULT_MAX_REPEATS)
    contract.set_defaults(handler=cmd_freeze_contract)

    scope = sub.add_parser("verify-scope", help="用 git 校验实际变更是否越界")
    scope.add_argument("--manifest", required=True)
    scope.add_argument("--allowed", action="extend", nargs="+", default=[],
                       help="获批仓库相对路径，可重复传入；以 / 结尾表示目录前缀")
    scope.add_argument("--allowed-file", help="每行一个允许路径的文件")
    scope.add_argument("--contract", help="implementation-contract.json，取其 allowed_write_files")
    scope.set_defaults(handler=cmd_verify_scope)

    validation = sub.add_parser("run-validation", help="以 shell=False 执行验证命令并记录证据")
    validation.add_argument("--manifest", help="提供 cwd 与默认记录路径；省略时必须给 --cwd 和 --record")
    validation.add_argument("--record")
    validation.add_argument("--cwd")
    validation.add_argument("--proves", default=None)
    validation.add_argument("--timeout", type=float, default=600.0)
    validation.add_argument("--tail-lines", type=int, default=OUTPUT_TAIL_LINES, help="写入记录的输出尾部行数")
    validation.add_argument("--max-repeats", type=int, default=DEFAULT_MAX_REPEATS)
    validation.add_argument("command", nargs=argparse.REMAINDER)
    validation.set_defaults(handler=cmd_run_validation)

    plan = sub.add_parser("run-plan", help="在同一进程内执行整份验证计划，只回显摘要")
    plan.add_argument("--manifest", required=True)
    plan.add_argument("--plan", help=f"默认取 RUN_DIR/{PLAN_NAME}")
    plan.add_argument("--record")
    plan.add_argument("--timeout", type=float, default=600.0)
    plan.add_argument("--tail-lines", type=int, default=OUTPUT_TAIL_LINES)
    plan.add_argument("--max-repeats", type=int, default=DEFAULT_MAX_REPEATS)
    plan.set_defaults(handler=cmd_run_plan)
    return parser


def cmd_proposal_review_mode(args: argparse.Namespace) -> int:
    """校验审查模式与已冻结资料的一致性，再转入 record-proposal。

    LITE 是由事实决定的（start 时没有冻结到任何有效文档），不要求用户批准；
    FULL 在无文档时、WAIVED 在任何时候都是「对门禁的降级」，必须用户明确批准。
    """
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    has_documents = bool(state["snapshot"].get("documents"))
    if args.review_mode == "LITE" and has_documents:
        raise StateError(
            "快照已冻结到 PRD/TDD/RPD，不得降级为 LITE；请修正 --documents 或改用 FULL"
        )
    if args.review_mode == "WAIVED" and not args.confirm_review_mode:
        raise StateError(
            "审查模式 WAIVED 表示跳过系统 code-review，必须已经取得用户明确批准"
            "（见 SKILL.md 的『审查模式与降级门禁』），确认后才加 --confirm-review-mode"
        )
    if args.review_mode == "FULL" and not has_documents and not args.confirm_review_mode:
        raise StateError(
            "快照没有冻结到任何文档，声称 FULL 意味着依靠仓库规范完成规格追踪，"
            "必须由用户确认后加 --confirm-review-mode，否则使用 --review-mode LITE"
        )
    return emit(evaluate_record_proposal(args))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command_name == "run-validation" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return args.handler(args)


if __name__ == "__main__":
    _reconfigure_streams()
    try:
        raise SystemExit(main())
    except StateError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:  # pragma: no cover
        raise SystemExit(130)
