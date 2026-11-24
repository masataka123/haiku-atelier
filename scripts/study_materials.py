"""Register source material and keep study handouts stable across parallel work."""

import fcntl
import random
import re

from atelier import canonical, field, read_array, read_json, write_json


def ledger(run):
    path = run / "materials.json"
    return read_array(path) if path.is_file() else []


def ticket_path(run, block, task):
    return run / "material-tickets" / f"{block}-{task}.json"


def register(run, plan, block_id, source):
    block = next((block for block in plan["blocks"] if block["id"] == block_id), None)
    if block is None:
        raise ValueError(f"{block_id} は課題の一覧にありません")
    item = read_json(source)
    case_id = field(item, "id", source)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", case_id):
        raise ValueError("教材IDには半角英数字、ハイフン、下線を使ってください")
    roles, poems = item.get("roles"), item.get("poems")
    if (
        not isinstance(roles, list) or not roles
        or any(not isinstance(role, str) or role not in block["tasks"] for role in roles)
        or len(roles) != len(set(roles))
    ):
        raise ValueError("roles には当該課題の担当を重複なく指定してください — " + "、".join(block["tasks"]))
    if not isinstance(poems, list) or not poems or any(not isinstance(poem, str) or not poem.strip() for poem in poems):
        raise ValueError("poems には本文を一つ以上指定してください")
    record = {
        "id": case_id, "block": block_id, "roles": sorted(roles),
        "source": field(item, "source", source), "poems": [poem.strip() for poem in poems],
    }
    if "commentary" in item:
        record["commentary"] = field(item, "commentary", source)
    if len(record["poems"]) != len(set(record["poems"])):
        raise ValueError("同一の本文は一つの教材内でも重複して登録できません")

    with (run.parent / ".materials.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = ledger(run)
        previous = next((row for row in rows if row["id"] == case_id), None)
        if previous == record:
            print(f"{case_id} は登録済みです")
            return
        for row in (record, previous):
            if row and any(ticket_path(run, row["block"], role).is_file() for role in row["roles"]):
                raise ValueError(f"{row['block']} の該当する票は配布済みです")
        verse_keys = {canonical(poem) for poem in record["poems"]}
        for row in rows:
            if row["id"] != case_id and verse_keys.intersection(map(canonical, row["poems"])):
                raise ValueError(f"{row['id']}（{row['block']}）と同じ本文が登録済みです")
        for other_ledger in sorted(run.parent.glob("[!.]*/materials.json")):
            if other_ledger.parent == run:
                continue
            for row in read_array(other_ledger):
                if verse_keys.intersection(map(canonical, row["poems"])):
                    raise ValueError(
                        f"別セット {other_ledger.parent.name} の {row['id']}（{row['block']}）と同じ本文が登録済みです"
                    )
        write_json(run / "materials.json", [row for row in rows if row["id"] != case_id] + [record])
    print(f"教材を登録しました — {case_id} / {block_id}")


def inventory(run, plan):
    rows = ledger(run)
    print(f"教材台帳 — {len(rows)}件")
    for block in plan["blocks"]:
        block_id = block["id"]
        cases = [row["id"] for row in rows if row["block"] == block_id]
        holdout = "（未読用の取り置き）" if block.get("holdout") else ""
        print(f"- {block_id}{holdout}　{len(cases)}件")


def handout(run, block_id, task, stage):
    path = ticket_path(run, block_id, task)
    with (run.parent / ".materials.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_file():
            ticket = read_json(path)
        else:
            if stage != "attempt":
                raise ValueError("初読の票が保存されていません")
            rows = [row for row in ledger(run) if row["block"] == block_id and task in row["roles"]]
            if not rows:
                raise ValueError(f"{block_id} / {task} に配れる教材を register-material で用意してください")
            rng = random.Random(f"{run.name}:{block_id}:{task}")
            rows.sort(key=lambda row: row["id"])
            rng.shuffle(rows)
            ticket = []
            for row in rows:
                order = list(range(len(row["poems"])))
                rng.shuffle(order)
                ticket.append({**row, "order": order})
            path.parent.mkdir(exist_ok=True)
            write_json(path, ticket)

    heading = "初読の票" if stage == "attempt" else "出典・選評との照合"
    print(f"# {heading}")
    for number, row in enumerate(ticket, 1):
        print()
        for position, original in enumerate(row["order"], 1):
            poem = row["poems"][original].replace("\n", "\n  ")
            print(f"- **{number:02d}-{position}**　{poem}")
        if stage == "review":
            print(f"  出典　{row['source']}")
            if "commentary" in row:
                print(f"  照合資料　{row['commentary']}")
            if len(row["order"]) > 1:
                print("  登録時の原典順　" + " → ".join(
                    f"{number:02d}-{row['order'].index(original) + 1}" for original in range(len(row["order"]))
                ))
