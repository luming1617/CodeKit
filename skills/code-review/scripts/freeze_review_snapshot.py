#!/usr/bin/env python3
"""Create a deterministic, read-only code-review snapshot manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


class SnapshotError(RuntimeError):
    """Raised when a read-only git command fails during snapshot freezing."""


class HandoffError(SnapshotError):
    """上游 Handoff 与本次冻结不是同一目标，或同一目标上出现了真正互斥的字段。

    CW2-a③ 把旧的"Handoff 任一字段的 raw 值不同即 BLOCKED"收窄成三类可判定的情形，
    只有这里的三类才阻塞；范围大小关系与编码自述不匹配都只登记成 limitation。
    """


class TargetBlocked(RuntimeError):
    """Raised when --auto-target cannot determine a reviewable target.

    与脚本故障区分：这不是环境问题，而是审查目标本身不成立（步骤 02/04 的
    BLOCKED 路径），main 以独立退出码 3 报告，便于 Orchestrator 直接转 BLOCKED。
    """


# CodeKit 运行时产物目录前缀。快照/报告必须落在 <repo>/.codekit/ 下（SKILL.md
# 「产物落点」），而它们本身是未跟踪文件；若不固定排除，任何一次重冻结都会把
# 上一轮产物计入 untracked_digest，造成观测者效应式的自污染。该排除只作用于
# untracked 层，且写死在这里并同步披露进 SKILL.md，不构成藏匿通道。
RUNTIME_ARTIFACT_PREFIXES = (".codekit/",)

# CW1-c：结构指纹字段。git_probe_digest 记录本次冻结实际执行的 git 调用序列
# （含 --end-of-options、基线探测顺序、-z 归一化这类只有本脚本才会产生的细节）。
# 定位要诚实：没有密钥体系就**做不到**密码学意义上的不可伪造，模型只要抄一份
# 同样形状的值就能通过；这一步的成效是把"顺手编一份 manifest"的成本抬到必须
# 精确复现内部实现，并与 --check-worktree 的重放校验（CW1-d）配套使用。
PRODUCED_BY = "codekit:code-review/scripts/freeze_review_snapshot.py"
# 1.3：新增 freeze_index（同一审查目标的第几次冻结，CW4-a 的重冻结预算判据）与
# review_scale（CW4-b 规模分档）。1.4：新增 retry_policy（CW4-e 回滚开关的落盘凭据）。
# 1.5：目标标识拆分（CW2-a）——base/head 移入 committed_range、四个分层摘要移入
# worktree_state，并新增 target_id、digest_scheme、committed_range.range_digest。
MANIFEST_SCHEMA_VERSION = "1.5"
GENERATOR_VERSION = "1.5"
GIT_COMMAND_LOG: list[list[str]] = []
# CW2-a①：三家 SKILL 的摘要编码统一为"裸小写 sha256 hexdigest，不带 `sha256:` 前缀"，
# 并在各自产物里用 digest_scheme 自述这套编码。前缀曾经是跨 SKILL 比对恒为 False 的
# 直接原因之一（bugfix 带前缀、另两家不带）。该字面量由 code-impl 与 bugfix 各自声明，
# 三个值的一致性由 tests 的跨 SKILL 漂移测试钉住，不靠散文约定。
DIGEST_SCHEME = "sha256/hex"
# CW2-a②：两段目标标识。committed_range 只覆盖 base..head 的已提交内容，
# worktree_state 只覆盖工作树三层；两段的成员名单点定义在这里。
COMMITTED_RANGE_FIELDS = ("base_sha", "head_sha", "range_digest")
WORKTREE_STATE_FIELDS = ("staged_digest", "unstaged_digest", "untracked_digest", "diff_digest")
# CW2-a③：Handoff 比对能产出的限制枚举，以及上游未声明 target_id（比本方案早的产物）时
# `target_id_matched` 的取值。写入方与核对方共用这一份，避免"脚本会写什么"和"校验器接受
# 什么"两处各写一遍再互相漂移。
HANDOFF_LIMITATIONS = ("SCOPE_SUPERSET", "HANDOFF_SCHEME_MISMATCH")
TARGET_ID_UNDECLARED = "not_declared"


def take_git_log() -> list[list[str]]:
    """取出并清空已记录的 git 调用序列（main 用于保留目标探测段）。"""
    entries = list(GIT_COMMAND_LOG)
    GIT_COMMAND_LOG.clear()
    return entries



def _reconfigure_streams() -> None:
    # Windows cp936 控制台下 ensure_ascii=False 的 CJK 路径会让 print 直接抛
    # UnicodeEncodeError，快照因此生成失败；统一强制 UTF-8 输出流。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def run_git(repo: Path, *args: str, text: bool = False) -> bytes | str:
    GIT_COMMAND_LOG.append(["git", *args])
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # encoding 只能在文本模式下传：带上它会强制开启文本模式，二进制调用
            # （``text=False``）会因此拿到 str 而不是 bytes，把快照字节流改坏。
            **({"text": True, "encoding": "utf-8", "errors": "replace"} if text else {"text": False}),
        )
    except FileNotFoundError as exc:
        raise SnapshotError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raw_stderr = exc.stderr or (b"" if not text else "")
        detail = raw_stderr.decode("utf-8", "replace") if isinstance(raw_stderr, bytes) else str(raw_stderr)
        detail = detail.strip() or "<no stderr>"
        raise SnapshotError(
            f"git {' '.join(args)} failed with exit status {exc.returncode}: {detail}"
        ) from exc
    return result.stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_text_atomic(path: Path, text: str) -> None:
    """落盘并自举父目录。

    CW1-a：SKILL 步骤 02 原先用 shell 重定向写快照，而 mkdir 只发生在脚本内部
    （--emit-skeleton 的父目录），重定向先于脚本执行，全新仓库上第一步就
    ``No such file or directory``。产物落盘因此收进本函数，命令块不再依赖重定向。
    """
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def manifest_summary(manifest: dict[str, object], manifest_path: Path, skeleton_path: Path | None) -> dict[str, object]:
    """--emit-manifest 模式下打印的紧凑摘要（替代整份 manifest 回灌 stdout）。"""
    summary = {
        "manifest_path": str(manifest_path),
        "skeleton_path": str(skeleton_path) if skeleton_path else None,
        "target_type": manifest["target_type"],
        "target_locator": manifest["target_locator"],
        "target_id": manifest["target_id"],
        "committed_range": manifest["committed_range"],
        "worktree_state": manifest["worktree_state"],
        "freeze_index": manifest["freeze_index"],
        "retry_policy": manifest["retry_policy"],
        "review_scale": manifest["review_scale"],
        "changed_files_count": len(manifest["changed_files"]),
        "changed_files_path": manifest.get("changed_files_path"),
        "preexisting_changes": manifest["preexisting_changes"],
        "documents": manifest["documents"],
        "diff_artifact": manifest.get("diff_artifact"),
        "manifest_digest": manifest["manifest_digest"],
    }
    handoff = manifest.get("handoff")
    if isinstance(handoff, dict):
        # CW2-a③：Orchestrator 只要"比了哪些字段、要披露哪些限制"，两侧摘要原值回灌
        # stdout 没有判断价值，只报条数（完整值仍签在 manifest 里可复核）。
        summary["handoff"] = {
            "sources": handoff["sources"],
            "target_id_matched": handoff["target_id_matched"],
            "compared_fields": handoff["compared_fields"],
            "uncomparable_digests": len(handoff["uncomparable_digests"]),
            "limitations": handoff["limitations"],
        }
    return summary


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def snapshot_field(manifest: dict[str, object], *path: str) -> object:
    """按路径读 manifest 字段（CW2-a② 拆分后报告侧仍按扁平键取值，两侧靠这张路径表对齐）。"""
    current: object = manifest
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def target_id_for(repo: object, head_sha: object) -> str:
    """CW2-a③：跨 SKILL 的目标连接键 = 规范化仓库根 + 目标 head_sha。

    只取两侧都能独立陈述的两项：`base_sha` 与 `target_locator` 不进来，因为上游快照
    可以只覆盖工作树（不含已提交范围），那正是 SCOPE_SUPERSET 要表达的情形，把它塞进
    连接键会让两份同头快照永远对不上。`normcase` 让 Windows 下 `C:\\x` 与 `C:/x`
    落在同一个键上。三家 SKILL 各自实现本函数，逐字相同的输出由跨 SKILL 漂移测试钉住。
    """
    return f"{os.path.normcase(str(Path(str(repo)).resolve()))}@{head_sha}"


def sign_manifest(manifest: dict[str, object]) -> None:
    """对剔除 manifest_digest 后的规范 JSON 自签。

    任何在签名后追加键的路径（如 --emit-diff）都必须重新调用本函数，摘要才继续
    覆盖整份 manifest；这只是内部一致性证明，不声称密码学不可伪造（见 CW1-c）。
    """
    manifest.pop("manifest_digest", None)
    manifest["manifest_digest"] = sha256(canonical_json(manifest))


# CW3-a：canonical diff 产物。此前四段差异只能靠模型逐个转录进 prompt，
# 单角色固定输入里 diff 就占 ≈21k token，而且"分片"在文档里允许却没有任何
# 确定性支持。现在由脚本一次性落盘，派发只传路径 + 摘要。
DIFF_SECTION_MARKERS = ("committed", "staged", "unstaged")
UNTRACKED_CHUNK_PREFIX = "### untracked file: "
# 未跟踪文件按原文附在最后一节；超过该上限的只登记字节数与摘要，避免一个杂物
# 文件把整份产物撑爆——读取方仍能用 untracked_digest 里的逐文件哈希核对全文。
UNTRACKED_INLINE_LIMIT = 256 * 1024


def _chunks_from_diff(text: str) -> list[tuple[str, str]]:
    """把一份 unified diff 按 `diff --git` 边界切成 (路径, 文本) 片段。"""
    lines = text.splitlines(keepends=True)
    chunks: list[tuple[str, str]] = []
    current_path: str | None = None
    buffer: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            if current_path is not None:
                chunks.append((current_path, "".join(buffer)))
            tail = line[len("diff --git ") :].rstrip("\n")
            parts = tail.split(" b/")
            current_path = parts[-1] if len(parts) > 1 else tail
            buffer = [line]
        else:
            buffer.append(line)
    if current_path is not None:
        chunks.append((current_path, "".join(buffer)))
    return chunks


def render_diff_layers(layers: dict[str, str], untracked: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]]:
    """返回 (完整产物文本, [(相对路径, 文本)])，产物文本按四段带段头拼接。"""
    parts: list[str] = []
    chunks: list[tuple[str, str]] = []
    for section in DIFF_SECTION_MARKERS:
        header = f"##### section: {section}{layers[f'{section}_label']} digest={layers[f'{section}_digest']}\n"
        body = layers[section]
        parts.append(header + body + ("\n" if body and not body.endswith("\n") else ""))
        chunks.extend((f"{section}/{path}", text) for path, text in _chunks_from_diff(body))
    parts.append(
        "##### section: untracked (未跟踪文件全文，超过 "
        f"{UNTRACKED_INLINE_LIMIT} 字节只登记摘要) digest={layers['untracked_digest']}\n"
    )
    for path, content in untracked:
        chunk = f"{UNTRACKED_CHUNK_PREFIX}{path}\n{content}"
        parts.append(chunk)
        chunks.append((f"untracked/{path}", chunk))
    return "".join(parts), chunks


def shard_chunks(chunks: list[tuple[str, str]]) -> dict[str, str]:
    """按顶层目录分片：`<layer>/<a>/<b>.py` -> `a`，根目录文件归入 `_root`。"""
    grouped: dict[str, list[str]] = {}
    for path, text in chunks:
        remainder = path.split("/", 1)[1] if "/" in path else path
        top = remainder.split("/", 1)[0] if "/" in remainder else "_root"
        grouped.setdefault(top, []).append(text)
    return {top: "".join(bodies) for top, bodies in sorted(grouped.items())}


def _shard_name(top: str) -> str:
    # 分片名取自 diff 里的路径首段，原样保留以便读者按目录对上仓库路径；
    # 只把会改变落盘位置的片段（分隔符、纯点号）折叠掉。
    cleaned = top.replace("/", "_").replace("\\", "_").strip()
    if not cleaned or set(cleaned) == {"."}:
        return "_root"
    return cleaned


def diff_shard_dir(diff_path: Path) -> Path:
    return diff_path.parent / f"{diff_path.stem}.shards"


def write_diff_shards(diff_path: Path, chunks: list[tuple[str, str]]) -> Path:
    """把分片落盘到 <diff 同名>.shards/，返回可供派发直接读取的清单路径。"""
    shard_dir = diff_shard_dir(diff_path)
    index: list[dict[str, object]] = []
    for top, text in shard_chunks(chunks).items():
        data = text.encode("utf-8")
        shard_path = shard_dir / f"{_shard_name(top)}.diff"
        write_text_atomic(shard_path, text)
        index.append({"top": top, "shard_path": str(shard_path), "bytes": len(data), "digest": sha256(data)})
    index_path = shard_dir / "index.json"
    write_text_atomic(
        index_path, json.dumps({"diff_path": str(diff_path), "shards": index}, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    return index_path


def read_untracked_content(repo: Path, relpath: str) -> str:
    target = repo / relpath
    try:
        data = target.read_bytes()
    except OSError as exc:
        return f"<unreadable: {exc}>\n"
    if len(data) > UNTRACKED_INLINE_LIMIT:
        return f"<large file: {len(data)} bytes digest={sha256(data)}>\n"
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary file: {len(data)} bytes digest={sha256(data)}>\n"


def build_diff_artifact(manifest: dict[str, object]) -> tuple[str, list[tuple[str, str]]]:
    """重放四段差异，产出单一 canonical 文本与其分片片段。

    层头标注的 digest 由本函数对刚读到的字节现算，并与 manifest 已签字的摘要比对：
    不一致说明冻结期间工作树被动过，宁可失败也不要产出一份自相矛盾的读物。
    """
    repo = Path(str(manifest["repository_root"]))
    base_sha = str(snapshot_field(manifest, "committed_range", "base_sha"))
    head_sha = str(snapshot_field(manifest, "committed_range", "head_sha"))
    worktree = manifest["worktree_state"]
    committed = diff_bytes(repo, base_sha, head_sha)
    staged = diff_bytes(repo, "--cached")
    unstaged = diff_bytes(repo)
    untracked_paths, untracked_digest, _ = untracked_manifest(repo)
    layers = {
        "committed": committed.decode("utf-8", "replace"),
        "staged": staged.decode("utf-8", "replace"),
        "unstaged": unstaged.decode("utf-8", "replace"),
        "committed_label": f" {base_sha}..{head_sha}",
        "staged_label": " index vs HEAD",
        "unstaged_label": " working tree vs index",
        "committed_digest": sha256(committed),
        "staged_digest": sha256(staged),
        "unstaged_digest": sha256(unstaged),
        "untracked_digest": untracked_digest,
    }
    for field, label in (("staged_digest", "staged"), ("unstaged_digest", "unstaged")):
        if layers[field] != str(worktree[field]):
            raise SnapshotError(
                f"working tree changed while building the diff artifact: {label} is now {layers[field]}, "
                f"the frozen manifest says {worktree[field]}; re-freeze the snapshot"
            )
    if untracked_digest != str(worktree["untracked_digest"]):
        raise SnapshotError(
            f"working tree changed while building the diff artifact: untracked is now {untracked_digest}, "
            f"the frozen manifest says {worktree['untracked_digest']}; re-freeze the snapshot"
        )
    untracked = [(path, read_untracked_content(repo, path)) for path in untracked_paths]
    return render_diff_layers(layers, untracked)


def attach_diff_artifact(diff_path: Path, shard_mode: str | None, manifest: dict[str, object]) -> None:
    """生成产物、登记摘要并重签 manifest，最后落盘（含可选分片）。"""
    text, chunks = build_diff_artifact(manifest)
    data = text.encode("utf-8")
    resolved = diff_path.resolve()
    manifest["diff_artifact"] = {
        "path": str(resolved),
        "bytes": len(data),
        "digest": sha256(data),
        "sections": [*DIFF_SECTION_MARKERS, "untracked"],
        "shard_index": str(diff_shard_dir(resolved) / "index.json") if shard_mode else None,
    }
    sign_manifest(manifest)
    write_text_atomic(resolved, text)
    if shard_mode:
        write_diff_shards(resolved, chunks)


def resolve_commit(repo: Path, revision: str) -> str:
    return str(run_git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}", text=True)).strip()


def diff_bytes(repo: Path, *args: str) -> bytes:
    return bytes(run_git(repo, "diff", "--binary", "--no-ext-diff", *args))


def parse_name_status(raw: bytes) -> list[str]:
    parts = raw.split(b"\0")
    result: list[str] = []
    index = 0
    while index < len(parts) and parts[index]:
        status = parts[index].decode("utf-8", "surrogateescape")
        index += 1
        if not status:
            break
        path = parts[index].decode("utf-8", "surrogateescape") if index < len(parts) else ""
        index += 1
        if status[0] in {"R", "C"}:
            old_path = path
            new_path = parts[index].decode("utf-8", "surrogateescape") if index < len(parts) else ""
            index += 1
            result.append(f"{status[0]}:{old_path}->{new_path}")
        else:
            result.append(f"{status[0]}:{path}")
    return result


def changed_from_diff(repo: Path, *args: str) -> list[str]:
    raw = bytes(
        run_git(
            repo,
            "diff",
            "--name-status",
            "--find-renames",
            "-z",
            *args,
        )
    )
    return parse_name_status(raw)


def diff_line_stats(repo: Path, *args: str) -> int:
    """一层 tracked diff 的 added+deleted 行数合计（numstat 口径）。

    读 numstat 而不是数 `--binary` 输出的行：二进制补丁的 base85 正文会被行数统计
    放大成假的"规模"，numstat 对二进制条目给出 `-`，这里按 0 计，文件数已经反映它。
    """
    total = 0
    text = run_git(repo, "diff", "--numstat", "--no-ext-diff", *args, text=True)
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        for value in fields[:2]:
            if value.isdigit():
                total += int(value)
    return total


def untracked_manifest(repo: Path) -> tuple[list[str], str, int]:
    """未跟踪层的 (路径清单, 逐文件规范摘要, 行数合计)。

    行数只为 CW4-b 的规模分档服务：全新增文件的变更在 tracked numstat 里是 0，
    不补这一段就会把大改动读成小改动。
    """
    raw = bytes(run_git(repo, "ls-files", "--others", "--exclude-standard", "-z"))
    listed = [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item]
    paths = [item for item in listed if not item.startswith(RUNTIME_ARTIFACT_PREFIXES)]
    entries: list[dict[str, str]] = []
    lines = 0
    for relative in sorted(paths):
        path = repo / relative
        stat = path.lstat()
        if os.path.islink(path):
            entries.append({"path": relative, "kind": "symlink", "value": os.readlink(path)})
        elif stat.st_mode & 0o170000 == 0o100000:
            content = path.read_bytes()
            entries.append({"path": relative, "kind": "file", "value": sha256(content)})
            lines += content.count(b"\n")
        else:
            entries.append({"path": relative, "kind": "other", "value": str(stat.st_mode)})
    return paths, sha256(canonical_json(entries)), lines


DOCUMENT_FIELDS = ("locator", "revision", "content_digest")


def document_specs(documents: list[str], revisions: list[str]) -> list[tuple[str, str | None]]:
    """把 ``--documents`` / ``--document-revision`` 两组参数配成 (路径, 版本标签)。

    不用 ``<path>:<revision>`` 单参数写法：Windows 绝对路径自带 ``C:`` 前缀，POSIX 版本
    标签又常含 ``:``（时间戳），按冒号切分必然歧义；等号只出现在标签侧。
    """
    labels: dict[str, str] = {}
    for entry in revisions:
        locator, separator, revision = entry.partition("=")
        if not separator or not locator.strip():
            raise SnapshotError(f"--document-revision expects PATH=REVISION, got: {entry}")
        labels[locator.strip()] = revision.strip()
    unknown = sorted(set(labels) - set(documents))
    if unknown:
        raise SnapshotError(f"--document-revision without a matching --documents entry: {unknown[0]}")
    return [(document, labels.get(document)) for document in documents]


def document_specs_from_args(args: argparse.Namespace) -> list[tuple[str, str | None]]:
    """CLI 参数配成规格；重放方（validate_review_report.py）直接给 ``document_specs`` 元组，
    避免把 locator 塞进 ``PATH=REVISION`` 字符串再切回来。"""
    preset = getattr(args, "document_specs", None)
    if preset is not None:
        return [(str(locator), revision) for locator, revision in preset]
    return document_specs(
        list(getattr(args, "documents", None) or []),
        list(getattr(args, "document_revision", None) or []),
    )


def collect_documents(repo: Path, specs: list[tuple[str, str | None]]) -> list[dict[str, str]]:
    """按步骤 01 解析到的文档路径与版本冻结内容摘要。

    缺省 revision 以本次读取的内容摘要充当冻结版本（SKILL 步骤 02 的既有规则）。
    清单按 locator 排序，保证同输入重放得到同一签名。
    """
    documents: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw, revision in specs:
        path = Path(raw)
        if not path.is_absolute():
            path = repo / path
        path = path.resolve()
        locator = str(path)
        if locator in seen:
            raise SnapshotError(f"duplicate --documents entry: {locator}")
        seen.add(locator)
        try:
            digest = sha256(path.read_bytes())
        except OSError as exc:
            raise SnapshotError(f"unable to read frozen document {locator}: {exc}") from exc
        documents.append(
            {"locator": locator, "revision": revision or digest, "content_digest": digest}
        )
    return sorted(documents, key=lambda item: item["locator"])


BASELINE_CANDIDATES = ("refs/heads/master", "refs/heads/main")


def resolve_auto_branch_target(repo: Path) -> dict[str, str]:
    """按 SKILL.md 步骤 02 的独立执行规则确定 branch 审查目标。

    原先由 Orchestrator 逐条手工执行的 5 个 git 探测命令（repo root、当前分支、
    HEAD SHA、基线选择、merge-base）全部收进本函数；任何一个 BLOCKED 条件都以
    TargetBlocked 携带机器可读原因返回，不再由模型转述判定。
    """
    try:
        root = Path(str(run_git(repo, "rev-parse", "--show-toplevel", text=True)).strip())
    except SnapshotError as exc:
        raise TargetBlocked(f"not inside a git repository: {exc}") from exc
    try:
        branch = str(run_git(root, "symbolic-ref", "--short", "HEAD", text=True)).strip()
    except SnapshotError as exc:
        raise TargetBlocked(
            "detached HEAD: checkout a feature branch, or freeze with explicit --base-sha/--head-sha"
        ) from exc
    if branch in {"master", "main"}:
        raise TargetBlocked(f"current branch '{branch}' is a baseline branch; no branch diff to review")
    baseline: tuple[str, str] | None = None
    for ref in BASELINE_CANDIDATES:
        try:
            baseline = (ref, resolve_commit(root, ref))
        except SnapshotError:
            continue
        break
    if baseline is None:
        raise TargetBlocked("neither local refs/heads/master nor refs/heads/main exists to base the review on")
    ref, base_tip = baseline
    head_sha = resolve_commit(root, "HEAD")
    try:
        merge_base = str(run_git(root, "merge-base", base_tip, head_sha, text=True)).strip()
    except SnapshotError as exc:
        raise TargetBlocked(f"no common ancestor between {ref} and branch '{branch}': {exc}") from exc
    return {
        "repo": str(root),
        "base_sha": merge_base,
        "head_sha": head_sha,
        "target_type": "branch",
        # locator 记录实际选中的基线（master 与 main 同时存在时固定优先 master）。
        "target_locator": f"{ref[len('refs/heads/'):]}...{branch}",
    }


# CW2-a②：manifest 已按 committed_range / worktree_state 分段，报告负载仍保持扁平形状
# （报告是给审查读者与 code-impl/bugfix 回读门禁看的契约文本，contracts.md 与骨架都按
# 扁平键渲染），所以这里单点声明"报告字段 -> manifest 路径"。骨架预填、校验器的
# 逐字段核对、contracts.md 的复制清单都读这张表，两侧不会各说各话。
SNAPSHOT_REPORT_FIELDS = {
    "base_sha": ("committed_range", "base_sha"),
    "head_sha": ("committed_range", "head_sha"),
    # CW2-a③：连接键进报告负载，下游（bugfix/code-impl 的回读门禁）据此核对
    # "这份审查结论对应的就是我交给它的那个目标"，而不是比三个字面摘要。
    "target_id": ("target_id",),
    "diff_digest": ("worktree_state", "diff_digest"),
    "staged_digest": ("worktree_state", "staged_digest"),
    "unstaged_digest": ("worktree_state", "unstaged_digest"),
    "untracked_digest": ("worktree_state", "untracked_digest"),
    "changed_files": ("changed_files",),
    "preexisting_changes": ("preexisting_changes",),
    "documents": ("documents",),
    "review_scale": ("review_scale",),
    "manifest_digest": ("manifest_digest",),
}
# CW3-c：`changed_files` 用于范围核对，不需要在每个角色上下文里出现全量。超过阈值时
# 骨架只带 count + 全量清单摘要 + 预览，全量另落 changed-files.txt；diff_digest 与
# changed_files_digest 仍覆盖全部条目，截断的只是读物，不是判据。
CHANGE_LIST_INLINE_LIMIT = 60
CHANGE_LIST_PREVIEW = 20
COMPACTED_CHANGED_FIELDS = ("count", "digest", "first", "full_list_path")
# CW4-b：规模分档。E2 的病根是 42 个文件与 800 个文件走完全相同的 9 维 × 全量 diff
# 路径；档位由脚本按机械口径算出、签进 manifest，模型不自选档位（自选的档位总是偏小）。
# 前三档各有 (文件数上限, 变更行上限)，任一超限即升档，末档 XL 无上限。
SCALE_TIERS = ("S", "M", "L", "XL")
SCALE_TIER_LIMITS = ((10, 300), (40, 1500), (150, 6000))
REVIEW_SCALE_FIELDS = ("tier", "changed_files", "changed_lines", "tier_limits")


def review_scale(changed_files: int, changed_lines: int) -> dict[str, object]:
    """`{tier, changed_files, changed_lines, tier_limits}`——口径与结论一起落盘，可复核。"""
    tier = SCALE_TIERS[-1]
    for candidate, (file_limit, line_limit) in zip(SCALE_TIERS, SCALE_TIER_LIMITS):
        if changed_files <= file_limit and changed_lines <= line_limit:
            tier = candidate
            break
    return {
        "tier": tier,
        "changed_files": changed_files,
        "changed_lines": changed_lines,
        "tier_limits": [list(item) for item in SCALE_TIER_LIMITS],
    }


COVERAGE_DIMENSIONS = (
    "correctness",
    "security_access",
    "data_transaction",
    "distributed_reliability",
    "compatibility_contract",
    "performance_capacity",
    "test_adequacy",
    "observability",
    "release_rollback",
)
# 十项发布检查同样由脚本单点定义：骨架预填、校验器核对都读这一份。
RELEASE_CHECK_ITEMS = (
    "deployment_order",
    "dependencies",
    "configuration",
    "feature_flag",
    "rollout",
    "stop_conditions",
    "monitoring",
    "observation_window",
    "data_repair",
    "rollback",
)


# CW2-c：中间契约的磁盘落点。步骤 03/05/07/09/11/16/17 的产出必须各自落成文件，
# 后续派发只传"路径 + 该文件摘要"，不再由主 Agent 从上下文里复述前序结论——
# 上下文一旦被压缩，复述就退化成臆造（审核报告 A3/G1/H1）。
STEPS_DIR_NAME = "steps"
STEP_ARTIFACTS = (
    (3, "context-contract"),
    (5, "traceability-matrix"),
    (7, "impact-surface"),
    (9, "candidate-register"),
    (11, "evidence-verdicts"),
    (16, "failure-attributions"),
    (17, "release-gate"),
)
RUN_DIR_SEGMENTS = (".codekit", "code-review")
# CW4-a：重冻结预算。步骤 22 的 3 轮结构修复上限只管报告，管不住"改标签→重校验→
# 再重冻结"这条放大路径（审核报告 E1）。上限与 code-impl 的 MAX_REVIEW_FREEZES 同值
# 同义：含首次冻结在内，同一审查目标最多冻结 3 份快照，第 4 次由脚本直接拒绝。
MAX_REFREEZE = 3
# 冻结谱系只按 SKILL 固定的产物落点识别（<仓库根>/.codekit/code-review/<run-id>/
# review-snapshot.json）；写在别处的 manifest 不计入，这一边界同步披露进 SKILL。
FREEZE_LINEAGE_PATTERN = "*/review-snapshot.json"
# CW4-e：回滚开关，与 code-impl 的 `--retry-policy gated|prose` 同名同义。`gated`（默认）
# 由脚本机械执行 MAX_REFREEZE；`prose` 恢复该闸门引入前的行为——超限只告警、快照照写，
# 并把策略本身签进 manifest，让"这次是人工放行的"可核对而不是靠事后叙述。
# MAX_DISPATCHES 对应 code-impl 的 policy-independent 绝对预算，两种模式下都是硬上限。
RETRY_POLICIES = ("gated", "prose")
RETRY_POLICY_DEFAULT = "gated"


def run_dir_for(repo: Path, run_id: str) -> Path:
    return repo.joinpath(*RUN_DIR_SEGMENTS, run_id)


def step_artifact_paths(run_dir: Path) -> list[tuple[int, Path]]:
    """本轮应存在的中间契约文件，顺序即步骤顺序。"""
    return [
        (step, run_dir / STEPS_DIR_NAME / f"{step:02d}-{slug}.json")
        for step, slug in STEP_ARTIFACTS
    ]


def freeze_lineage(repo: Path, target_locator: object, base_sha: object, head_sha: object) -> dict[str, int]:
    """同一审查目标在磁盘上已冻结过的 {diff_digest: freeze_index}。

    只认本脚本自己产出的 manifest（`produced_by` 逐字相等）且目标三元组完全一致；
    读不到、JSON 损坏、字段不齐的一律跳过——谱系是预算计数器，不是判据，宁可少计
    也不把无关 run 的产物算进来。
    """
    lineage: dict[str, int] = {}
    root = repo.joinpath(*RUN_DIR_SEGMENTS)
    if not root.is_dir():
        return lineage
    for path in sorted(root.glob(FREEZE_LINEAGE_PATTERN)):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict) or manifest.get("produced_by") != PRODUCED_BY:
            continue
        if (
            manifest.get("target_locator") != target_locator
            or snapshot_field(manifest, "committed_range", "base_sha") != base_sha
            or snapshot_field(manifest, "committed_range", "head_sha") != head_sha
        ):
            continue
        digest = snapshot_field(manifest, "worktree_state", "diff_digest")
        index = manifest.get("freeze_index")
        if isinstance(digest, str) and isinstance(index, int) and not isinstance(index, bool):
            lineage[digest] = max(lineage.get(digest, 0), index)
    return lineage


def build_report_snapshot(manifest: dict[str, object]) -> dict[str, object]:
    """骨架的 snapshot 段：11 个字段按 SNAPSHOT_REPORT_FIELDS 的路径照抄 manifest，超大变更清单改为可核对的摘要形状。"""
    snapshot = {field: snapshot_field(manifest, *path) for field, path in SNAPSHOT_REPORT_FIELDS.items()}
    changed = manifest["changed_files"]
    list_path = manifest.get("changed_files_path")
    if (
        isinstance(changed, list)
        and isinstance(list_path, str)
        and list_path
        and len(changed) > CHANGE_LIST_INLINE_LIMIT
    ):
        snapshot["changed_files"] = {
            "count": len(changed),
            "digest": manifest["changed_files_digest"],
            "first": changed[:CHANGE_LIST_PREVIEW],
            "full_list_path": list_path,
        }
    return snapshot


def build_report_skeleton(manifest: dict[str, object]) -> dict[str, object]:
    """用签名 manifest 机械预填报告骨架的确定性字段。

    snapshot 11 个字段（含 4 个 64-hex digest、documents 清单与 review_scale 档位）、scope 的 target 二元组、
    九个覆盖维度名和十项发布检查名都不允许再由模型手工转录；变更清单超阈值时改为
    build_report_snapshot 的摘要形状，不给随手截断留口子；其余判断性字段一律为
    null——未填写的骨架必然通不过 validate_review_report.py，不可能被当作成品交付。
    """
    return {
        "schema_version": "1.0",
        "review_context": {"review_mode": None, "documents_status": None},
        "verdict": None,
        "verdict_basis": [],
        "blockers": [],
        "scope": {
            "target_type": manifest["target_type"],
            "target_locator": manifest["target_locator"],
            "target_id": manifest["target_id"],
            "included": [],
            "excluded": [],
        },
        "snapshot": build_report_snapshot(manifest),
        "summary": None,
        "findings": [],
        "open_questions": [],
        "traceability": [],
        "coverage": [
            {
                "dimension": dimension,
                "required": True,
                "status": None,
                "owner": None,
                "evidence": [],
                "reason": None,
            }
            for dimension in COVERAGE_DIMENSIONS
        ],
        "validation": [],
        "release_gate": {
            "status": None,
            # 十项检查预填到位：漏项不再是"少写一行"，而是必须逐条改写状态才过关。
            "checks": [
                {
                    "item": item,
                    "status": None,
                    "criticality": None,
                    "evidence": [],
                    "condition_or_gap": None,
                }
                for item in RELEASE_CHECK_ITEMS
            ],
            "blocking_reasons": [],
            "conditions": [],
        },
        "rejected_candidates": [],
        "limitations": [],
        "residual_risks": [],
    }


def attach_changed_files_list(manifest: dict[str, object], run_dir: Path) -> None:
    """超过阈值时把全量清单落到 `$RUN_DIR/changed-files.txt` 并登记进 manifest。"""
    changed = manifest["changed_files"]
    if not isinstance(changed, list) or len(changed) <= CHANGE_LIST_INLINE_LIMIT:
        return
    list_path = (run_dir / "changed-files.txt").resolve()
    write_text_atomic(list_path, "\n".join(str(item) for item in changed) + "\n")
    manifest["changed_files_path"] = str(list_path)
    sign_manifest(manifest)


def handoff_field(entry: dict[str, object], name: str) -> object:
    """按统一 Handoff 形状取值，兼容带分组与未带分组两种上游产物。"""
    group = entry.get("committed_range")
    if isinstance(group, dict) and name in group:
        return group[name]
    group = entry.get("worktree_state")
    if isinstance(group, dict) and name in group:
        return group[name]
    return entry.get(name)


def handoff_producer(entry: dict[str, object]) -> str:
    value = entry.get("produced_by")
    return value if isinstance(value, str) and value else "unknown"


def resolve_handoff_check(path: Path, manifest: dict[str, object], *, committed_present: bool) -> dict[str, object]:
    """CW2-a③：把上游 Handoff 与本次冻结做机械比对，产出签进 manifest 的 `handoff` 段。

    取代旧散文规则"Handoff 的 base_sha/head_sha/diff_digest 与仓库实际状态不一致即
    BLOCKED"——审核报告 A5 实测那条永远为假：三家摘要的 material 本就不同，上游快照还
    常常不含已提交范围，按字面执行只能每次都 BLOCKED。新规则四条，全部机械可判：
    ①同一 `target_id`（同一仓库同一目标头指针）才继续比对，否则 HANDOFF_TARGET；
    ②只比两侧都带且 material 相同的共有字段，material 不同的字段比了也没意义，记成
    HANDOFF_FIELD_UNCOMPARABLE；③摘要编码不同（`digest_scheme` 不等）时不得比摘要，
    记成 HANDOFF_SCHEME_MISMATCH；④上游不含已提交范围而本轮审查确实含（`committed_present`
    由 range_changes 是否为空给出，不看 base!=head，所以一个不改文件的 merge 提交
    不会凭空产出一条限制），是范围超集，记成 SCOPE_SUPERSET 后继续审查，不得 BLOCKED。
    只有"目标不同""上游声明的范围与本轮回放出的范围互斥""工作树漂移"三类抛 HandoffError。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HandoffError(f"--handoff {path} is unreadable: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HandoffError(f"--handoff {path} is not valid JSON: {exc}") from exc
    entries = [payload] if isinstance(payload, dict) else payload if isinstance(payload, list) else None
    if not entries:
        raise HandoffError(f"--handoff {path} must hold a JSON object or a non-empty array of them")
    ours_head = str(snapshot_field(manifest, "committed_range", "head_sha"))
    ours_base = str(snapshot_field(manifest, "committed_range", "base_sha"))
    ours_id = str(manifest["target_id"])
    compared: list[str] = []
    comparable: list[str] = []
    limitations: list[str] = []
    producers: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise HandoffError(f"--handoff {path} holds a non-object entry")
        producer = handoff_producer(entry)
        producers.append(producer)
        their_id = entry.get("target_id")
        if their_id is not None and their_id != ours_id:
            raise HandoffError(
                f"--handoff target_id is {their_id!r} but this freeze is {ours_id!r}: the Handoff and "
                "this snapshot do not describe the same repository and head"
            )
        their_scheme = entry.get("digest_scheme")
        scheme_ok = their_scheme == DIGEST_SCHEME
        if their_scheme is not None and not scheme_ok:
            limitations.append(f"HANDOFF_SCHEME_MISMATCH: {producer}:{their_scheme}")
        their_head = handoff_field(entry, "head_sha")
        if their_head is not None:
            if not isinstance(their_head, str) or their_head != ours_head:
                raise HandoffError(
                    f"--handoff head_sha is {their_head!r} but the frozen target head is {ours_head!r}"
                )
            compared.append("head_sha")
        range_present = isinstance(entry.get("committed_range"), dict)
        if range_present:
            their_base = handoff_field(entry, "base_sha")
            if not isinstance(their_base, str) or their_base != ours_base:
                raise HandoffError(
                    f"--handoff committed_range.base_sha is {their_base!r} but this freeze resolved "
                    f"{ours_base!r}: the upstream declared a different committed range, so the two "
                    "scopes are mutually exclusive rather than a superset"
                )
            compared.append("committed_range.base_sha")
        else:
            # 上游只冻结了工作树。只有本轮真的含已提交内容时，这才是"上游没看过这部分"
            # 的范围超集；两边都没有 committed 段时范围相同，记限制只会凭空压低结论上限。
            if committed_present:
                limitations.append(f"SCOPE_SUPERSET: {producer} carries no committed_range")
        for name in ("staged_digest", "unstaged_digest", "untracked_digest"):
            theirs = handoff_field(entry, name)
            if theirs is None:
                continue
            if not scheme_ok:
                continue
            ours = snapshot_field(manifest, "worktree_state", name)
            if theirs != ours:
                raise HandoffError(
                    f"--handoff {name} is {theirs!r} but the working tree now yields {ours!r}: "
                    "the tree moved after the upstream froze it, so re-freeze upstream first"
                )
            compared.append(name)
        theirs = handoff_field(entry, "diff_digest")
        if theirs is not None:
            ours = snapshot_field(manifest, "worktree_state", "diff_digest")
            if theirs == ours:
                compared.append("diff_digest")
            elif scheme_ok and producer == PRODUCED_BY:
                raise HandoffError(
                    f"--handoff diff_digest is {theirs!r} but this freeze yields {ours!r}: same "
                    "producer and same scheme means the same material, so the two really disagree"
                )
            else:
                # 上游与本技能对 diff_digest 的 material 定义不同（code-impl 只折工作树
                # 条目，bugfix 折拼接文本）。值不等既不是缺陷也不是放行理由，比它没有意义。
                comparable.append(f"{producer}:{theirs}")
    declared_ids = [
        entry.get("target_id") for entry in entries if isinstance(entry, dict) and entry.get("target_id") is not None
    ]
    matched: object = TARGET_ID_UNDECLARED if not declared_ids else all(value == ours_id for value in declared_ids)
    return {
        "path": str(path.resolve()),
        "sources": sorted(set(producers)),
        "digest_scheme": DIGEST_SCHEME,
        "target_id": ours_id,
        "target_id_matched": matched,
        "compared_fields": sorted(set(compared)),
        "uncomparable_digests": sorted(comparable),
        "limitations": sorted(set(limitations)),
    }


def build_snapshot(args: argparse.Namespace, probe_prefix: list[list[str]] | None = None) -> dict[str, object]:
    # 直接以库方式调用（测试、--check-worktree 重放）时也要有确定性起点；
    # main 走 --auto-target 时先 take_git_log() 取出目标探测段再传回来。
    take_git_log()
    repo = Path(args.repo).resolve()
    base_sha = resolve_commit(repo, args.base_sha)
    head_sha = resolve_commit(repo, args.head_sha)
    committed = diff_bytes(repo, base_sha, head_sha)
    staged = diff_bytes(repo, "--cached")
    unstaged = diff_bytes(repo)
    untracked, untracked_digest, untracked_lines = untracked_manifest(repo)
    range_changes = changed_from_diff(repo, base_sha, head_sha)
    worktree_changes = changed_from_diff(repo, "--cached")
    worktree_changes += changed_from_diff(repo)
    worktree_changes += [f"A:{path}" for path in untracked]
    changed_files = sorted(set(range_changes) | set(worktree_changes))
    # CW4-b：规模口径 = tracked 三层的 numstat 增删行数 + 未跟踪文件行数。
    changed_lines = (
        diff_line_stats(repo, base_sha, head_sha)
        + diff_line_stats(repo, "--cached")
        + diff_line_stats(repo)
        + untracked_lines
    )
    preexisting = sorted(set(args.preexisting_change))
    committed_digest = sha256(committed)
    staged_digest = sha256(staged)
    unstaged_digest = sha256(unstaged)
    # CW2-a②：已提交范围与工作组各自可独立核对。base/head 固定端点，range_digest 固定
    # 端点之间的内容（补丁字节 + 范围内文件清单）；跨 SKILL 交接时"上游有没有覆盖已提交
    # 范围"就是看它有没有带这一段，而不是靠比对两个语义不同的 diff_digest。
    range_digest = sha256(canonical_json({
        "base_sha": base_sha,
        "head_sha": head_sha,
        "committed_digest": committed_digest,
        "changed_files": sorted(set(range_changes)),
    }))
    # diff_digest 的 material 只含工作树三层（1.5 起）：committed 段由 range_digest 承担，
    # 两端 SHA 由三元组谱系固定，全量清单由 changed_files_digest 固定，整份 manifest 由
    # manifest_digest 自签。缩小 material 换来的是"同一工作树、不同已提交范围"能在
    # worktree_state 上比对，且重冻结谱系的复用判据仍然成立。
    worktree_digest = sha256(canonical_json({
        "head_sha": head_sha,
        "staged_digest": staged_digest,
        "unstaged_digest": unstaged_digest,
        "untracked_digest": untracked_digest,
        "changed_files": sorted(worktree_changes),
        "preexisting_changes": preexisting,
    }))
    manifest: dict[str, object] = {
        # 1.1：新增 produced_by / generator_version / git_probe_digest 三项来源指纹。
        # 1.2：新增 changed_files_digest（全量清单的独立摘要，CW3-c 的报告侧摘要形状靠它核对）。
        # 1.3：新增 freeze_index（本目标第几次冻结），用于 CW4-a 的重冻结预算。
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "repository_root": str(repo),
        "target_type": args.target_type,
        "target_locator": args.target_locator,
        # CW2-a①②：编码自述 + 跨 SKILL 连接键 + 两段目标标识。
        "digest_scheme": DIGEST_SCHEME,
        "target_id": target_id_for(repo, head_sha),
        "committed_range": {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "range_digest": range_digest,
        },
        "worktree_state": {
            "staged_digest": staged_digest,
            "unstaged_digest": unstaged_digest,
            "untracked_digest": untracked_digest,
            "diff_digest": worktree_digest,
        },
        "changed_files": changed_files,
        "changed_files_digest": sha256(canonical_json(changed_files)),
        # 1.3：新增 review_scale（CW4-b 规模分档），报告侧照抄，档位不由模型自选。
        "review_scale": review_scale(len(changed_files), changed_lines),
        "preexisting_changes": preexisting,
        # 1.2：文档版本进入签名 manifest（CW2-b），报告侧照抄后即为可核对的冻结输入。
        "documents": collect_documents(repo, document_specs_from_args(args)),
    }
    # CW4-a：同一目标同一份差异重复冻结不算新的一次（谱系里已有该 digest 即复用编号），
    # 只有内容真的变了才推进计数器，所以重跑步骤 02 不会白烧预算。同一三元组下的 committed
    # 段由两端 SHA 唯一确定，所以按 worktree 层的 diff_digest 计数与旧的整段计数等价。
    lineage = freeze_lineage(repo, args.target_locator, base_sha, head_sha)
    worktree_fingerprint = manifest["worktree_state"]["diff_digest"]
    manifest["freeze_index"] = lineage.get(worktree_fingerprint, max(lineage.values(), default=0) + 1)
    # CW4-e：策略签进 manifest。--check-worktree 的重放方按 manifest 回填该值，重放结果
    # 才与磁盘上那份逐字段相同（库方式调用与重放 Namespace 都可能不带该属性）。
    manifest["retry_policy"] = getattr(args, "retry_policy", RETRY_POLICY_DEFAULT)
    manifest["produced_by"] = PRODUCED_BY
    manifest["generator_version"] = GENERATOR_VERSION
    manifest["git_probe_digest"] = sha256(canonical_json([*(probe_prefix or []), *GIT_COMMAND_LOG]))
    # CW2-a③：Handoff 比对结果进签名，报告侧的限制条目与结论上限都由校验器核对。
    if getattr(args, "handoff", None) is not None:
        manifest["handoff"] = resolve_handoff_check(
            Path(args.handoff).resolve(), manifest, committed_present=bool(range_changes)
        )
    sign_manifest(manifest)
    return manifest


def read_step_ledger(ledger_path: Path) -> list[dict[str, object]]:
    """续跑时按行读回派发台账；损坏的行直接报错而不是静默跳过。"""
    if not ledger_path.is_file():
        return []
    records: list[dict[str, object]] = []
    for lineno, line in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"{ledger_path}:{lineno} is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise SnapshotError(f"{ledger_path}:{lineno} must contain a JSON object")
        records.append(value)
    return records


def resume_run(repo: Path, run_id: str) -> dict[str, object]:
    """列出一次中断审查的磁盘现状：已冻结什么、缺哪份契约、派发过谁、下一步做什么。

    判据只有文件存在且非空；契约正文是否合规仍由步骤 21 的校验器负责，本函数不做
    任何判断字段的猜测。
    """
    run_dir = run_dir_for(repo.resolve(), run_id)
    if not run_dir.is_dir():
        raise SnapshotError(f"no run directory to resume at {run_dir}")
    manifest_path = run_dir / "review-snapshot.json"
    report_path = run_dir / "review-report.json"
    manifest: dict[str, object] | None = None
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            manifest = None
    completed: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []
    for step, path in step_artifact_paths(run_dir):
        if path.is_file() and path.stat().st_size > 0:
            completed.append({"step": step, "path": str(path), "digest": sha256(path.read_bytes())})
        else:
            missing.append({"step": step, "expected_path": str(path)})
    dispatches = [
        {
            "step": record.get("step"),
            "role": record.get("role"),
            "instance_id": record.get("instance_id"),
            "returned_status": record.get("returned_status"),
        }
        for record in read_step_ledger(run_dir / "dispatch-ledger.jsonl")
    ]
    report_filled: bool | None = None
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report_filled = isinstance(report, dict) and report.get("verdict") is not None
        except (OSError, json.JSONDecodeError):
            report_filled = False
    if manifest is None:
        next_action = f"步骤 02 未完成：先冻结快照到 {manifest_path}"
    elif missing:
        next_action = (
            f"从步骤 {missing[0]['step']:02d} 续跑：写出 {missing[0]['expected_path']}，"
            "再按台账补齐之后的派发"
        )
    elif report_filled is not True:
        next_action = "中间契约齐全：完成步骤 18-20 的判定并填写报告"
    else:
        next_action = "报告已填写：进入步骤 21 校验（建议带 --check-worktree）"
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "manifest_path": str(manifest_path),
        "manifest_frozen": manifest is not None,
        "target_locator": (manifest or {}).get("target_locator"),
        # CW2-a②：续跑时先看目标身份，再决定要不要重冻结——committed 段与工作树段现在
        # 是两段，只看一个 diff_digest 会把"已提交范围变了"和"工作树变了"混成一条。
        "target_id": (manifest or {}).get("target_id"),
        "diff_digest": snapshot_field(manifest, "worktree_state", "diff_digest"),
        "range_digest": snapshot_field(manifest, "committed_range", "range_digest"),
        # CW4-a：续跑时先看还剩几次冻结，别等到第 4 次才发现预算已用完。
        "freeze_index": (manifest or {}).get("freeze_index"),
        "freeze_budget": MAX_REFREEZE,
        "diff_artifact": (manifest or {}).get("diff_artifact"),
        "documents": (manifest or {}).get("documents"),
        "report_path": str(report_path),
        "report_filled": report_filled,
        "steps_completed": completed,
        "steps_missing": missing,
        "dispatches": dispatches,
        "next_action": next_action,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=None, help="repository root; required unless --auto-target branch resolves it")
    parser.add_argument("--base-sha", default=None)
    parser.add_argument("--head-sha", default=None)
    parser.add_argument("--target-type", default=None)
    parser.add_argument("--target-locator", default=None)
    parser.add_argument(
        "--auto-target",
        choices=["branch"],
        default=None,
        help="resolve repo root, current branch, master/main baseline and merge-base deterministically (step 02); exits 3 with 'BLOCKED:' reason when no reviewable branch target exists",
    )
    parser.add_argument(
        "--emit-skeleton",
        type=Path,
        default=None,
        metavar="PATH",
        help="write a report skeleton whose snapshot/scope/coverage fields are prefilled from the manifest",
    )
    parser.add_argument(
        "--force-skeleton",
        action="store_true",
        help="let --emit-skeleton overwrite a file that is not the pristine skeleton of this snapshot",
    )
    parser.add_argument(
        "--emit-manifest",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the signed manifest to PATH (parents auto-created) and print a compact summary; "
        "without it the manifest goes to stdout (step 02 uses this flag so the command needs no shell redirect)",
    )
    parser.add_argument(
        "--retry-policy",
        choices=list(RETRY_POLICIES),
        default=RETRY_POLICY_DEFAULT,
        help="gated (default) enforces the CW4-a re-freeze budget MAX_REFREEZE and refuses the "
        "next snapshot above it; prose is the CW4-e rollback switch: the over-budget snapshot is "
        "written with a warning and the policy is signed into the manifest, so the report must "
        "disclose it. The dispatch budget MAX_DISPATCHES is a policy-independent ceiling and stays "
        "hard in both modes",
    )
    parser.add_argument(
        "--emit-diff",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the four diff layers (committed/staged/unstaged/untracked) into one canonical file, "
        "record its digest in the manifest and print it, so dispatch passes a path instead of pasting the diff",
    )
    parser.add_argument(
        "--diff-shard",
        choices=["by-directory"],
        default=None,
        help="with --emit-diff, also split the artifact into <path>.shards/<top>.diff plus index.json "
        "so a Reviewer reads only the directories it needs",
    )
    parser.add_argument(
        "--handoff",
        default=None,
        metavar="PATH",
        help="JSON file (or array of files' payloads) published by an upstream Skill, e.g. code-impl's "
        "review-snapshot.json or bugfix's Review Handoff; the freeze compares it field by field and "
        "records the outcome into the signed manifest (CW2-a③)",
    )
    parser.add_argument(
        "--preexisting-change",
        action="append",
        default=[],
        help="one `STATUS:path` entry (same notation as changed_files) that is present in the tree but "
        "outside this review target; repeatable",
    )
    parser.add_argument(
        "--documents",
        action="append",
        default=[],
        metavar="PATH",
        help="freeze a step 01 document (PRD/TDD/RPD or repo rule file) into the signed manifest as "
        "{locator, revision, content_digest}; repeatable, relative paths resolve against --repo",
    )
    parser.add_argument(
        "--document-revision",
        action="append",
        default=[],
        metavar="PATH=REVISION",
        help="revision label for a --documents entry (git revision or update time); without it the "
        "content digest read this run is used as the frozen revision",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="RUN_ID",
        help="report on-disk state of an interrupted run (<repo>/.codekit/code-review/<run-id>/) "
        "and print the next step to take instead of freezing a new snapshot",
    )
    args = parser.parse_args()
    if args.resume:
        conflicting = [
            flag
            for flag, value in (
                ("--emit-manifest", args.emit_manifest),
                ("--emit-skeleton", args.emit_skeleton),
                ("--emit-diff", args.emit_diff),
                ("--auto-target", args.auto_target),
                ("--handoff", args.handoff),
            )
            if value
        ]
        if conflicting:
            parser.error(f"--resume only inspects an existing run; drop {' '.join(conflicting)}")
        _reconfigure_streams()
        try:
            report = resume_run(args.repo or Path.cwd(), args.resume)
        except (SnapshotError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if args.diff_shard and not args.emit_diff:
        parser.error("--diff-shard requires --emit-diff")
    _reconfigure_streams()
    take_git_log()
    probe_prefix: list[list[str]] = []
    if args.auto_target == "branch":
        try:
            resolved = resolve_auto_branch_target(args.repo or Path.cwd())
        except TargetBlocked as exc:
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 3
        except SnapshotError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        args.repo = Path(resolved["repo"])
        args.base_sha = resolved["base_sha"]
        args.head_sha = resolved["head_sha"]
        args.target_type = resolved["target_type"]
        args.target_locator = resolved["target_locator"]
        probe_prefix = take_git_log()
    else:
        missing = [
            flag
            for flag, value in (
                ("--repo", args.repo),
                ("--base-sha", args.base_sha),
                ("--head-sha", args.head_sha),
                ("--target-type", args.target_type),
                ("--target-locator", args.target_locator),
            )
            if not value
        ]
        if missing:
            parser.error(f"{' '.join(missing)} required unless --auto-target branch is used")
    try:
        manifest = build_snapshot(args, probe_prefix)
    except HandoffError as exc:
        # CW2-a③：只有"目标不同 / 上游声明的范围与本轮回放出的范围互斥 / 上游冻结后工作树
        # 又动了"这三类才是 BLOCKED（退出码 3，与 --auto-target 的目标不成立同一通道）；
        # 范围超集与编码自述不匹配只登记成 limitation，报告照写。
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    except SnapshotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: unable to read the working tree: {exc}", file=sys.stderr)
        return 2
    if args.emit_manifest is not None and manifest["freeze_index"] > MAX_REFREEZE:
        # CW4-a：无界重冻结在这里截断。超限后不是"换个编号再冻一次"，而是按步骤 22
        # 的兜底收尾；合并后基线 SHA 变化会自然开一条新谱系，不需要放宽本上限。
        budget_note = (
            f"snapshot #{manifest['freeze_index']} for target {manifest['target_locator']!r} "
            f"is above the freeze budget MAX_REFREEZE={MAX_REFREEZE}."
        )
        if args.retry_policy == "prose":
            # CW4-e：回滚开关放行，但放行凭据已签进 manifest，校验器会要求报告披露它。
            print(
                f"WARN: {budget_note} --retry-policy prose is in force, so the snapshot was written "
                "anyway; the report must disclose the over-budget re-freeze in `limitations`.",
                file=sys.stderr,
            )
        else:
            print(
                f"ERROR: {budget_note} Stop re-freezing: finish this run "
                "as a BLOCKED report that lists what stayed unverified (step 22 fallback). Archived or deleted "
                "run directories reset this counter, so treat that as a human decision to start a new review. "
                "Pass --retry-policy prose (the CW4-e rollback switch) only as a deliberate, disclosed decision.",
                file=sys.stderr,
            )
            return 2
    if args.emit_manifest is not None or args.emit_skeleton is not None:
        try:
            attach_changed_files_list(manifest, Path((args.emit_manifest or args.emit_skeleton).parent))
        except OSError as exc:
            print(f"ERROR: unable to write the full change list: {exc}", file=sys.stderr)
            return 2
    if args.emit_diff is not None:
        try:
            attach_diff_artifact(args.emit_diff, args.diff_shard, manifest)
        except SnapshotError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"ERROR: unable to write the diff artifact to {args.emit_diff}: {exc}", file=sys.stderr)
            return 2
    if args.emit_skeleton is not None:
        skeleton_text = json.dumps(
            build_report_skeleton(manifest), ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        try:
            # CW1-b：步骤 02 被文档当作可重入的恢复路径，而无保护的 write_text 会把
            # 步骤 20 已填完的报告静默压回骨架（实测 6941 -> 2941 字节、stderr 为空）。
            if args.emit_skeleton.exists() and not args.force_skeleton:
                existing = args.emit_skeleton.read_text(encoding="utf-8", errors="replace")
                if existing.strip() != skeleton_text.strip():
                    print(
                        f"ERROR: refusing to overwrite {args.emit_skeleton}: it holds {len(existing)} bytes "
                        "that are not the pristine skeleton of this snapshot. Freeze into a new $RUN_DIR, or "
                        "pass --force-skeleton only if the previous report is discarded.",
                        file=sys.stderr,
                    )
                    return 2
            write_text_atomic(args.emit_skeleton, skeleton_text)
        except OSError as exc:
            print(f"ERROR: unable to write report skeleton to {args.emit_skeleton}: {exc}", file=sys.stderr)
            return 2
    manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.emit_manifest is not None:
        try:
            write_text_atomic(args.emit_manifest, manifest_text)
        except OSError as exc:
            print(f"ERROR: unable to write snapshot manifest to {args.emit_manifest}: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(manifest_summary(manifest, args.emit_manifest, args.emit_skeleton), ensure_ascii=False))
        return 0
    print(manifest_text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
