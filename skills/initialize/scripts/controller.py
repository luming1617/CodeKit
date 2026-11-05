#!/usr/bin/env python3
"""Single deterministic entrypoint for the initialize state machine."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def load_module(filename: str):
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load initialize module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prepare = load_module("prepare.py")
reconcile = load_module("reconcile.py")
validator = load_module("validate-result.py")
gitnexus_step = load_module("gitnexus-step.py")
graphify_step = load_module("graphify-step.py")


class RecoverableRunInputError(validator.ValidationError):
    """An input-placement/content problem that must not abort the active run."""


def lock_status(manifest: dict) -> str:
    """Report the filesystem state of the lock referenced by a manifest."""
    lock_path = manifest.get("lock_path")
    if not isinstance(lock_path, str) or not lock_path:
        return "unknown"
    try:
        return "held" if Path(lock_path).exists() else "absent"
    except OSError:
        return "unknown"


def validate_active_manifest(path: Path) -> dict:
    path = path.resolve()
    manifest = validator.load_json(path)
    if manifest.get("schema_version") != 1:
        raise validator.ValidationError("unsupported run manifest schema_version")
    if Path(manifest.get("manifest_path", "")).resolve() != path:
        raise validator.ValidationError("run manifest path does not match its sealed identity")
    root = Path(manifest.get("root", "")).resolve()
    if prepare.repository_root(root) != root:
        raise validator.ValidationError("run manifest root is not the repository root")
    lock_path = Path(manifest.get("lock_path", ""))
    lock = validator.load_json(lock_path)
    if (
        lock.get("run_id") != manifest.get("run_id")
        or Path(lock.get("root", "")).resolve() != root
        or Path(lock.get("manifest", "")).resolve() != path
    ):
        raise validator.ValidationError("run lock identity does not match the manifest")
    return manifest


def require_run_file(manifest_path: Path, candidate: Path) -> None:
    if candidate.resolve().parent != manifest_path.resolve().parent:
        raise RecoverableRunInputError(
            f"run input must be stored beside the manifest: {candidate}"
        )
    if not candidate.is_file():
        raise RecoverableRunInputError(f"run input does not exist: {candidate}")
    try:
        validator.load_json(candidate)
    except (OSError, json.JSONDecodeError, validator.ValidationError) as exc:
        raise RecoverableRunInputError(f"run input is not valid JSON: {candidate}: {exc}") from exc


def require_prior_step(manifest: dict, filename: str, step: str) -> None:
    """Enforce the order between two controller steps without aborting the run.

    Graphify 排在 GitNexus 之后是设计约定：图谱要建立在索引与 skills 镜像已落盘之后，
    而先跑后跑都会让门禁的基线比较多背一批产物。顺序写错是可修的输入问题，不是真实
    失败，所以这里报 recoverable 并保持当前 run 与锁。
    """
    run_dir = Path(manifest.get("state_run_dir", ""))
    if not (run_dir / filename).is_file():
        raise RecoverableRunInputError(
            f"{filename} not found in {run_dir}: run `controller.py {step}` first"
        )


def report_recoverable_input_error(
    manifest_path: Path, error: Exception, *, hint: str | None = None
) -> int:
    try:
        manifest = validator.load_json(manifest_path)
        phase = manifest.get("phase")
        run_dir = Path(manifest.get("state_run_dir", manifest_path.parent))
        current_lock_status = lock_status(manifest)
    except Exception:
        phase = "unknown"
        run_dir = manifest_path.parent
        current_lock_status = "unknown"
    if hint is None:
        if current_lock_status == "held":
            hint = (
                "repair or copy the JSON input into expected_run_dir and retry this same run; "
                "do not start initialize again"
            )
        elif current_lock_status == "absent":
            hint = "the run lock is absent; inspect the manifest and start a new initialize run"
        else:
            hint = (
                "the run lock status is unknown; inspect the manifest before retrying or "
                "restarting"
            )
    print(json.dumps({
        "status": "input-error",
        "recoverable": True,
        "phase": phase,
        "lock": current_lock_status,
        "message": str(error),
        "expected_run_dir": str(run_dir),
        "hint": hint,
    }, ensure_ascii=False, indent=2), file=sys.stderr)
    return 2


def report_registry_denied(manifest_path: Path, error: "gitnexus_step.RegistryWriteDenied") -> int:
    """宿主拒绝了那次仓库外写入：可恢复，保持 run 与锁，交回宿主提权通道。

    这不是运行失败：注册要么在带提权的重试里成功，要么用户拒绝后按
    ``partial/registry-missing`` 降级。把全部机器可读信息打给主 Agent，尤其是
    ``retry_command``——它就是不变量：首次尝试与升级重试用同一条命令，宿主若弹窗，
    弹的永远是同一条 argv 与同一个目标文件。
    """
    try:
        manifest = validator.load_json(manifest_path)
        phase = manifest.get("phase")
        run_dir = Path(manifest.get("state_run_dir", manifest_path.parent))
        current_lock_status = lock_status(manifest)
    except Exception:
        phase = "unknown"
        run_dir = manifest_path.parent
        current_lock_status = "unknown"
    print(json.dumps({
        "status": "registry-denied",
        "recoverable": True,
        "phase": phase,
        "lock": current_lock_status,
        "message": str(error),
        "registry_path": error.registry_path,
        "exit_code": error.exit_code,
        "retry_command": error.retry_command,
        "justification": error.justification,
        "expected_run_dir": str(run_dir),
        "hint": (
            "宿主拒绝了这次仓库外写入。按 references/tool-adapters.md 的「全局注册」一节，"
            "用宿主提权通道原样重跑 retry_command（与刚才那条命令逐字相同），成功后再重跑 "
            "controller.py analyze 继续同一个 run；重试仍被拒再询问用户，用户拒绝则带 "
            "--allow-partial 接受 partial/registry-missing。"
        ),
    }, ensure_ascii=False, indent=2), file=sys.stderr)
    return gitnexus_step.REGISTRY_DENIED_EXIT


def abort_safely(manifest: Path, reason: str) -> None:
    """Best-effort transition to aborted; never conceal the original failure."""
    try:
        current = validate_active_manifest(manifest)
        if current.get("phase") not in {"finished", "aborted"}:
            validator.abort_command(
                argparse.Namespace(manifest=manifest, reason=reason), emit=False
            )
    except (OSError, validator.ValidationError):
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic CodeKit initialize controller")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="freeze the baseline, lock, and prepare managed files")
    start.add_argument("--root", default=".")
    start.add_argument("--language")
    start.add_argument("--embeddings", action="store_true")
    start.add_argument("--force", action="store_true")
    start.add_argument("--allow-partial", action="store_true")
    start.add_argument("--commit", action="store_true")
    start.add_argument("--allow-global-writes", action="store_true")
    start.add_argument("--no-git-hooks", action="store_true")
    start.add_argument(
        "--strict-tools", action="store_true",
        help="fail the gate when a tool is unavailable (default: degrade to partial)",
    )
    start.add_argument("--run-id", help=argparse.SUPPRESS)

    gate = commands.add_parser("gate", help="validate and seal all worker results")
    gate.add_argument("--manifest", required=True, type=Path)
    gate.add_argument("--result", required=True, action="append", type=Path)

    analyze = commands.add_parser(
        "analyze", help="run the deterministic GitNexus step and write its worker result"
    )
    analyze.add_argument("--manifest", required=True, type=Path)

    register = commands.add_parser(
        "register",
        help="attempt the single out-of-repo GitNexus registry write, and nothing else",
    )
    register.add_argument("--manifest", required=True, type=Path)

    graphify = commands.add_parser(
        "graphify",
        help=(
            "run the deterministic Graphify step and write its worker result "
            "(requires analyze to have run first)"
        ),
    )
    graphify.add_argument("--manifest", required=True, type=Path)

    context = commands.add_parser("reconcile", help="write context from a sealed worker gate")
    context.add_argument("--manifest", required=True, type=Path)
    context.add_argument("--context", required=True, type=Path)
    context.add_argument("--worker-result", required=True, action="append", type=Path)

    finish = commands.add_parser("finish", help="run final gates and release the lock")
    finish.add_argument("--manifest", required=True, type=Path)
    finish.add_argument(
        "--validation-result", action="append", default=[], type=Path,
        help="attested result file; only honoured together with --allow-attested-validations",
    )
    finish.add_argument(
        "--allow-attested-validations", action="store_true",
        help="accept agent-written validation results; blocks any automatic commit",
    )
    finish.add_argument("--validation-timeout", type=int, default=900)

    abort = commands.add_parser("abort", help="record failure and release an owned lock")
    abort.add_argument("--manifest", required=True, type=Path)
    abort.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "start":
        forwarded = ["--root", args.root]
        if args.language is not None:
            forwarded += ["--language", args.language]
        for flag, enabled in (
            ("--embeddings", args.embeddings),
            ("--force", args.force),
            ("--allow-partial", args.allow_partial),
            ("--commit", args.commit),
            ("--allow-global-writes", args.allow_global_writes),
            ("--no-git-hooks", args.no_git_hooks),
            ("--strict-tools", args.strict_tools),
        ):
            if enabled:
                forwarded.append(flag)
        if args.run_id:
            forwarded += ["--run-id", args.run_id]
        return prepare.main(forwarded)
    if args.command == "gate":
        try:
            validate_active_manifest(args.manifest)
            for path in args.result:
                require_run_file(args.manifest, path)
            result = validator.workers_command(
                argparse.Namespace(manifest=args.manifest, results=args.result), emit=False
            )
        except RecoverableRunInputError as error:
            return report_recoverable_input_error(args.manifest, error)
        except Exception:
            abort_safely(args.manifest, "worker gate raised an exception")
            raise
        if result:
            abort_safely(args.manifest, "worker gate failed")
        manifest = validator.load_json(args.manifest)
        print(json.dumps({
            "status": manifest.get("worker_gate", {}).get("status"),
            "phase": manifest.get("phase"),
            "worker_gate": manifest.get("worker_gate"),
            "lock": lock_status(manifest),
        }, ensure_ascii=False, indent=2))
        return result
    if args.command == "analyze":
        try:
            validate_active_manifest(args.manifest)
            return gitnexus_step.main(["--manifest", str(args.manifest)], emit=True)
        except gitnexus_step.RegistryWriteDenied as error:
            return report_registry_denied(args.manifest, error)
        except Exception:
            abort_safely(args.manifest, "gitnexus step raised an exception")
            raise
    if args.command == "register":
        try:
            validate_active_manifest(args.manifest)
            return gitnexus_step.register_main(["--manifest", str(args.manifest)], emit=True)
        except gitnexus_step.RegistryWriteDenied as error:
            return report_registry_denied(args.manifest, error)
        except validator.ValidationError as error:
            # register 是补救命令：前提不满足（manifest 不是 prepared、runner 不可跑、
            # 锁身份不符）是可修的输入问题，绝不能 abort 掉它要救的那个 run。
            # gitnexus_step 抛的 InitializeError 与 validator.ValidationError 是同一个类。
            return report_recoverable_input_error(
                args.manifest,
                error,
                hint=(
                    "register 的前提不满足：manifest 必须仍是 prepared，且 tools.gitnexus 可执行。"
                    "run 与锁保持不动——先核对 manifest，再重试 register，或回到 controller.py "
                    "analyze 继续同一个 run。"
                ),
            )
        except Exception:
            abort_safely(args.manifest, "gitnexus register raised an exception")
            raise
    if args.command == "graphify":
        try:
            manifest = validate_active_manifest(args.manifest)
            require_prior_step(manifest, "gitnexus-result.json", "analyze")
            return graphify_step.main(["--manifest", str(args.manifest)], emit=True)
        except RecoverableRunInputError as error:
            return report_recoverable_input_error(args.manifest, error)
        except Exception:
            abort_safely(args.manifest, "graphify step raised an exception")
            raise
    if args.command == "reconcile":
        try:
            validate_active_manifest(args.manifest)
            require_run_file(args.manifest, args.context)
            for path in args.worker_result:
                require_run_file(args.manifest, path)
            if len(args.worker_result) != len(reconcile.EXPECTED_TASKS):
                raise RecoverableRunInputError(
                    "reconcile requires "
                    f"{len(reconcile.EXPECTED_TASKS)} worker results; "
                    f"received {len(args.worker_result)}"
                )
            result = reconcile.main([
                "--manifest", str(args.manifest),
                "--context", str(args.context),
                *[part for result in args.worker_result for part in ("--worker-result", str(result))],
            ], emit=False)
            manifest = validator.load_json(args.manifest)
            print(json.dumps({
                "status": manifest.get("context_status"),
                "phase": manifest.get("phase"),
                "changed_files": manifest.get("context_changed_files", []),
                "warnings": manifest.get("context_warnings", []),
            }, ensure_ascii=False, indent=2))
            return result
        except RecoverableRunInputError as error:
            return report_recoverable_input_error(args.manifest, error)
        except reconcile.ContextContractError as error:
            return report_recoverable_input_error(args.manifest, error)
        except Exception:
            abort_safely(args.manifest, "context reconciliation failed")
            raise
    if args.command == "finish":
        try:
            validate_active_manifest(args.manifest)
            for path in args.validation_result:
                require_run_file(args.manifest, path)
            result = validator.final_command(
                argparse.Namespace(
                    manifest=args.manifest,
                    validation_result=args.validation_result,
                    allow_attested_validations=args.allow_attested_validations,
                    validation_timeout=args.validation_timeout,
                ),
                emit=False,
            )
            manifest = validator.load_json(args.manifest)
            report = dict(manifest.get("final_report", {}))
            if result == 0:
                try:
                    report["state_cleanup"] = validator.cleanup_state(manifest)
                except OSError as error:
                    report["state_cleanup"] = {
                        "status": "warning",
                        "message": f"successful initialize state could not be removed: {error}",
                    }
                    print(f"initialize warning: {report['state_cleanup']['message']}", file=sys.stderr)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return result
        except RecoverableRunInputError as error:
            return report_recoverable_input_error(args.manifest, error)
        except Exception:
            abort_safely(args.manifest, "final validation raised an exception")
            raise
    validate_active_manifest(args.manifest)
    return validator.abort_command(argparse.Namespace(manifest=args.manifest, reason=args.reason))


if __name__ == "__main__":
    from _common import configure_utf8_streams

    configure_utf8_streams()
    try:
        raise SystemExit(main())
    except (prepare.PrepareError, reconcile.ReconcileError, validator.ValidationError, OSError) as error:
        print(f"initialize controller: {error}", file=sys.stderr)
        raise SystemExit(2)
