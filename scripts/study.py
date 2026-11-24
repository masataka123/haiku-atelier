#!/usr/bin/env python3
"""Keep separate, resumable learning records for the haiku workshop."""

import argparse
import os
import re
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

import study_materials
from atelier import PROJECT, field, read_json, write_json


CORE_ROLES = {"selector": "主選者", "writer": "作句担当"}
EXTRA_ROLES = {"selector-b": "主選者B", "writer-b": "作句担当（現代・多様な形式）"}
SINGLE_READER = {"reader": "独立読み手"}
PAIRED_READERS = {"reader-a": "独立読み手A", "reader-b": "独立読み手B"}
READER_ROLES = SINGLE_READER | PAIRED_READERS
ROLES = CORE_ROLES | EXTRA_ROLES | READER_ROLES
DEFAULT_PLAN = PROJECT / "study-plan.json"
LEGACY_READER_BLOCKS = {
    **dict.fromkeys((
        "first-reading", "similarity", "revision", "ieto-edits", "classics", "voice",
        "tea-award", "seasonal", "haiku-post", "prebato",
    ), "round-1"),
    "follow-up": "transfer",
}
STAGES = {"attempt": "初読・試み", "review": "選評・別の読みと照合"}
HEADINGS = {
    "attempt": ("自分の読みと根拠", "迷い・代案"),
    "review": ("照合した資料と結果", "判断の変化", "次に確かめること"),
}


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise ValueError("学習回と課題のIDには半角英数字、ハイフン、下線を使ってください")
    return value


def read_plan(path):
    plan = read_json(path)
    field(plan, "title", path)
    for flag in ("material_ledger", "synchronized_rounds"):
        if type(plan.get(flag, False)) is not bool:
            raise ValueError(f"{flag} には true または false を指定してください")
    reader_count = plan.get("reader_count", 2)
    if type(reader_count) is not int or reader_count not in (1, 2):
        raise ValueError("reader_count には 1 または 2 を指定してください")
    blocks = plan.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError(f"{path} に学習課題を一つ以上用意してください")
    ids = set()
    expected_tasks = None
    allowed_tasks = CORE_ROLES.keys() | EXTRA_ROLES.keys() | {"reader"}
    for block in blocks:
        field(block, "id", path)
        block_id = identifier(block["id"])
        if block_id in ids:
            raise ValueError(f"学習課題ID {block_id} が重複しています")
        if "after" in block:
            after = block["after"]
            if (
                not isinstance(after, list)
                or any(not isinstance(dep, str) or dep not in ids for dep in after)
                or len(after) != len(set(after))
            ):
                raise ValueError(f"{block_id} の after には、それより前の課題IDを重複なく指定してください")
        if type(block.get("holdout", False)) is not bool or (block.get("holdout") and not block.get("after")):
            raise ValueError(f"{block_id} の未読用の課題には先に終える課題を指定してください")
        ids.add(block_id)
        field(block, "title", path)
        if type(block.get("minutes")) is not int or block["minutes"] < 1:
            raise ValueError(f"{block_id} の minutes は目安を分単位の正の整数で指定してください")
        tasks = block.get("tasks", plan.get("default_tasks"))
        if not isinstance(tasks, dict) or not CORE_ROLES.keys() <= tasks.keys() <= allowed_tasks:
            raise ValueError(f"{block_id} の課題には selector と writer、および必要に応じて selector-b、writer-b、reader を用意してください")
        if expected_tasks is None:
            expected_tasks = tasks.keys()
        elif tasks.keys() != expected_tasks:
            raise ValueError(f"{block_id} の担当は他の課題と揃えてください")
        for role, task in tasks.items():
            for stage in STAGES:
                field(task, stage, f"{block_id} / {role}")
        block["tasks"] = tasks.copy()
    return plan


def readers_for(plan):
    return SINGLE_READER if plan.get("reader_count", 2) == 1 else PAIRED_READERS


def roles_for(plan):
    tasks = plan["blocks"][0]["tasks"]
    roles = CORE_ROLES | EXTRA_ROLES | readers_for(plan)
    return {role: name for role, name in roles.items() if task_for(role) in tasks}


def start(run, plan):
    run.parent.mkdir(parents=True, exist_ok=True)
    if run.exists():
        raise ValueError(f"{run} は開始済みです。status または next で続きを確認してください")
    with TemporaryDirectory(prefix=".study-", dir=run.parent) as folder:
        staging = Path(folder)
        write_json(staging / "plan.json", plan)
        if plan.get("material_ledger"):
            write_json(staging / "materials.json", [])
        for role in roles_for(plan):
            (staging / role).mkdir()
        staging.rename(run)
    print(f"学習回を開始しました — {run}")


def add_readers(run, plan):
    if "reader" in plan["blocks"][0]["tasks"]:
        print(f"独立した読み手の課題は追加済みです — {run}")
        return
    assignments = {
        block["id"]: block["tasks"]["reader"] for block in read_plan(DEFAULT_PLAN)["blocks"]
        if "reader" in block["tasks"]
    }
    for old_id, block_id in LEGACY_READER_BLOCKS.items():
        if block_id in assignments:
            assignments.setdefault(old_id, assignments[block_id])
    if missing := [block["id"] for block in plan["blocks"] if block["id"] not in assignments]:
        raise ValueError("標準計画にない課題には reader の課題を個別に用意してください — " + "、".join(missing))
    readers = readers_for(plan)
    for role in readers:
        (run / role).mkdir(exist_ok=True)
    for block in plan["blocks"]:
        block["tasks"]["reader"] = assignments[block["id"]]
    write_json(run / "plan.json", plan)
    print(f"独立した読み手{len(readers)}人分の課題を追加しました — {run}")


def entry_path(run, role, block_id, stage):
    return run / role / f"{block_id}-{stage}.md"


def review_receipt_path(run, role, block_id):
    return run / "material-tickets" / f"{block_id}-{role}-review.issued"


def task_for(role):
    return "reader" if role in READER_ROLES else role


def reader_waiting(run, plan, role, block_id, attempted):
    return (
        plan.get("material_ledger") and attempted and role in PAIRED_READERS
        and not all(entry_path(run, reader, block_id, "attempt").is_file() for reader in PAIRED_READERS)
    )


def study_progress(run, plan, role):
    saved = {}
    synchronized = plan.get("synchronized_rounds", False)
    peers = [participant for participant in roles_for(plan) if participant != role] if synchronized else []
    # Read later records first so a concurrent submission does not appear to precede its prerequisite.
    for block in reversed(plan["blocks"]):
        block_id = block["id"]
        reviewed = entry_path(run, role, block_id, "review").is_file()
        attempted = entry_path(run, role, block_id, "attempt").is_file()
        round_complete = reviewed and all(entry_path(run, peer, block_id, "review").is_file() for peer in peers)
        saved[block_id] = (attempted, reviewed, round_complete)

    ready, completed = {}, set()
    previous = []
    for block in plan["blocks"]:
        block_id = block["id"]
        attempted, reviewed, round_complete = saved[block_id]
        prerequisites_met = completed.issuperset(block.get("after", previous))
        if (reviewed and not attempted) or (attempted and not prerequisites_met):
            raise ValueError(f"{role} の保存に抜けがあります — {block_id}")
        if reviewed:
            completed.add(block_id)
        elif prerequisites_met and not reader_waiting(run, plan, role, block_id, attempted):
            ready[block_id] = (block, "review" if attempted else "attempt")
        if synchronized and not round_complete:
            break
        previous = [block_id]
    return ready, completed


def show_status(run, plan):
    print(plan["title"])
    for role, name in roles_for(plan).items():
        ready, completed = study_progress(run, plan, role)
        progress = f"{len(completed)}/{len(plan['blocks'])}課題"
        if ready:
            block, stage = next(iter(ready.values()))
            if len(ready) > 1:
                progress += f"｜着手できる課題 {len(ready)}件"
            progress += f"｜次は {block['id']} {block['title']}（{STAGES[stage]}）"
        elif len(completed) == len(plan["blocks"]):
            progress += "｜全課題の記録を保存済み"
        else:
            progress += "｜同じ回の記録待ち" if plan.get("synchronized_rounds") else "｜照合待ち"
        print(f"{name}　{progress}")
    print(f"計画の目安　各担当 {sum(block['minutes'] for block in plan['blocks'])}分")


def show_ready(run, plan, role):
    ready, _ = study_progress(run, plan, role)
    print(f"{ROLES[role]}｜着手できる課題 {len(ready)}件")
    for block, stage in ready.values():
        print(f"- {block['id']} {block['title']}（{STAGES[stage]}）")


def show_next(run, plan, role, block_id):
    ready, completed = study_progress(run, plan, role)
    if block_id is not None:
        if block_id in completed:
            print(f"{ROLES[role]}の {block_id} は保存済みです")
            return
        if block_id not in ready:
            raise ValueError(f"{block_id} に着手できるか ready で確認してください")
        block, stage = ready[block_id]
    elif ready:
        block, stage = next(iter(ready.values()))
    else:
        if len(completed) == len(plan["blocks"]):
            message = "全課題を保存済み"
        else:
            message = "同じ回の記録待ち" if plan.get("synchronized_rounds") else "照合待ち"
        print(f"{ROLES[role]}｜{message}")
        return
    print(f"{ROLES[role]}｜{block['id']} {block['title']}｜{STAGES[stage]}（課題全体の目安 {block['minutes']}分）")
    brief = run / f"{block['id']}-brief.md"
    if brief.is_file():
        print(brief.read_text(encoding="utf-8").strip())
    print(block["tasks"][task_for(role)][stage])
    if stage == "review":
        print(f"保存済みの初読 — {entry_path(run, role, block['id'], 'attempt')}")
    print("\n提出するMarkdownの見出し")
    for heading in record_headings(role, stage):
        print(f"## {heading}")
    print(f"\nrecord に渡す順番 — {run.name} {role} {block['id']} {stage} <記録.md>")


def show_handout(run, plan, role, block_id):
    ready, _ = study_progress(run, plan, role)
    if block_id not in ready:
        raise ValueError(f"{block_id} の教材は着手できる段階になってから配布してください")
    _, stage = ready[block_id]
    study_materials.handout(run, block_id, task_for(role), stage)
    if stage == "review":
        sys.stdout.flush()
        review_receipt_path(run, role, block_id).touch()


def record_headings(role, stage):
    if role in READER_ROLES and stage == "attempt":
        return ("自分の読み",)
    return HEADINGS[stage]


def validate_record(content, role, stage):
    parts = re.split(r"^##[ \t]+(.+?)[ \t]*$", content, flags=re.MULTILINE)
    headings = {heading: body.strip() for heading, body in zip(parts[1::2], parts[2::2])}
    required = record_headings(role, stage)
    if any(not headings.get(heading) for heading in required):
        raise ValueError("指定の見出しと各段落の記録が必要です — " + "／".join(required))


def record(run, plan, role, block_id, stage, source):
    path = entry_path(run, role, identifier(block_id), stage)
    if block_id not in {block["id"] for block in plan["blocks"]}:
        raise ValueError(f"{block_id} は課題の一覧にありません")
    content = source.read_text(encoding="utf-8").rstrip() + "\n"
    if not path.is_file():
        ready, _ = study_progress(run, plan, role)
        pending = ready.get(block_id)
        if (pending is None or pending[1] != stage) and not path.is_file():
            raise ValueError("ready が示す課題と段階の記録から保存してください")
    if path.is_file():
        if path.read_text(encoding="utf-8") == content:
            print(f"保存済み — {path}")
            return
        raise ValueError(f"保存済みの記録は上書きできません — {path}")
    if plan.get("material_ledger"):
        if not study_materials.ticket_path(run, block_id, task_for(role)).is_file():
            raise ValueError("handout で当該課題の教材を配布してから記録してください")
        if stage == "review" and not review_receipt_path(run, role, block_id).is_file():
            raise ValueError("この担当の照合票を handout で出してから照合を記録してください")
    validate_record(content, role, stage)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent) as handle:
        handle.write(content)
        handle.flush()
        try:
            os.link(handle.name, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != content:
                raise ValueError(f"別の記録が先に保存されました — {path}") from None
    print(f"保存 — {path}")


def main():
    parser = argparse.ArgumentParser(description="俳句工房の学習回を開始し、課題ごとの初読と照合を保存する")
    parser.add_argument("--studies-dir", type=Path, default=PROJECT / "studies")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("start", help="学習計画をコピーして開始する")
    create.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    enable = commands.add_parser("add-readers", help="既存の学習回に独立した読み手の課題を加える")
    status = commands.add_parser("status", help="担当別の進み具合を確認する")
    ready = commands.add_parser("ready", help="担当が着手できる課題を一覧にする")
    resume = commands.add_parser("next", help="担当の課題と提出見出しを表示する")
    resume.add_argument("--block", help="指定した課題を表示する")
    materials = commands.add_parser("materials", help="教材台帳の件数と取り置きを表示する")
    register = commands.add_parser("register-material", help="外部教材を課題に登録する")
    handout = commands.add_parser("handout", help="いま配布できる段階の教材を表示する")
    save = commands.add_parser("record", help="該当段階のMarkdownを保存する")
    for command in (create, enable, status, ready, resume, materials, register, handout, save):
        command.add_argument("session")
    for command in (ready, resume, handout, save):
        command.add_argument("role", choices=ROLES)
    register.add_argument("block")
    register.add_argument("file", type=Path)
    handout.add_argument("block")
    save.add_argument("block")
    save.add_argument("stage", choices=STAGES)
    save.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        run = args.studies_dir.resolve() / identifier(args.session)
        if args.command == "start":
            start(run, read_plan(args.plan))
            return
        if not (run / "plan.json").is_file():
            raise ValueError(f"{run} の学習回を start で先に用意してください")
        plan = read_plan(run / "plan.json")
        if args.command == "status":
            show_status(run, plan)
        elif args.command == "add-readers":
            add_readers(run, plan)
        elif args.command == "materials":
            study_materials.inventory(run, plan)
        elif args.command == "register-material":
            study_materials.register(run, plan, args.block, args.file)
        else:
            if args.role not in roles_for(plan):
                if args.role in READER_ROLES and "reader" not in plan["blocks"][0]["tasks"]:
                    raise ValueError("この回には独立した読み手の課題がありません。add-readers で追加できます")
                raise ValueError(f"このセットには {args.role} の課題がありません")
            if args.command == "ready":
                show_ready(run, plan, args.role)
            elif args.command == "next":
                show_next(run, plan, args.role, args.block)
            elif args.command == "handout":
                show_handout(run, plan, args.role, args.block)
            else:
                record(run, plan, args.role, args.block, args.stage, args.file)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
