#!/usr/bin/env python3
"""Prepare, validate, and archive the files written during a haiku workshop."""

import argparse
import json
import os
import random
import re
import shutil
import string
import sys
import unicodedata
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


PROJECT = Path(__file__).resolve().parent.parent
SETTING_KEYS = ("writers", "poems_per_writer", "shortlist_limit", "revision_rounds", "final_size")
FREE_READING_PROMPT = "どう読みましたか。票番号を添えてお書きください。"


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: JSON の {error.lineno} 行目を確認してください") from error


def write_text(path, content):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_archive(target, base, files, text_files=()):
    with NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for file in files:
                archive.write(file, file.relative_to(base))
            for name, content in text_files:
                archive.writestr(name, content)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def settings(value):
    if not isinstance(value, dict) or any(type(value.get(key)) is not int or value[key] < 1 for key in SETTING_KEYS):
        raise ValueError("設定値には正の整数を指定してください")
    if not value["writers"] <= len(string.ascii_uppercase):
        raise ValueError("作句担当の人数は26人までです")
    if not value["final_size"] <= value["shortlist_limit"] <= value["writers"] * value["poems_per_writer"]:
        raise ValueError("最終句数、候補の上限、作句総数を小さい順に設定してください")
    if "first_wave_writers" in value:
        first_wave = value["first_wave_writers"]
        if type(first_wave) is not int or not 1 <= first_wave < value["writers"]:
            raise ValueError("first_wave_writers は全担当数より少ない正の整数を指定してください")
    if "round_limits" in value:
        limits = value["round_limits"]
        if (
            not isinstance(limits, list) or len(limits) != value["revision_rounds"]
            or any(type(limit) is not int or limit < value["final_size"] for limit in limits)
            or limits != sorted(limits, reverse=True) or limits[0] != value["shortlist_limit"]
        ):
            raise ValueError("round_limits は初選上限から最終句数以上まで、巡ごとに同数か少ない上限を指定してください")
    feedback = value.get("round_reader_feedback", False)
    if type(feedback) is not bool or (feedback and "round_limits" not in value):
        raise ValueError("round_reader_feedback は巡ごとの上限がある回に true または false で指定してください")
    return value


def start(args):
    brief = args.brief.read_text(encoding="utf-8").strip()
    if not brief:
        raise ValueError("兼題や場面を書いたファイルを指定してください")
    profile = settings(read_json(PROJECT / "profiles.json")[args.profile])
    run = create_run(args.runs_dir.resolve(), args.id, profile, brief)
    print(run)


def create_run(parent, run_id, profile, brief):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", run_id):
        raise ValueError("回の識別名には半角英数字、ハイフン、下線を使ってください")
    run = parent / run_id
    run.mkdir(parents=True, exist_ok=False)
    (run / "drafts").mkdir()
    for turn in range(1, profile["revision_rounds"] + 1):
        (run / "rounds" / f"{turn:02}").mkdir(parents=True)
    write_text(run / "brief.md", brief + "\n")
    write_json(run / "settings.json", profile | {"run_id": run_id})
    return run


def read_array(path):
    value = read_json(path)
    if not isinstance(value, list):
        raise ValueError(f"{path}: JSON の配列を指定してください")
    return value


def field(row, key, source):
    value = row.get(key) if isinstance(row, dict) else None
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{source}: {key} に1行のテキストを指定してください")
    return value.strip()


def canonical(verse):
    return "".join(unicodedata.normalize("NFKC", verse).split())


def validate_poem(row, source):
    poem = {key: field(row, key, source) for key in ("verse", "kigo", "season")}
    reading = row.get("reading")
    if not isinstance(reading, list) or len(reading) != 3 or any(
        not isinstance(part, str) or not re.fullmatch(r"[ぁ-ゖー]+", part) for part in reading
    ):
        raise ValueError(f"{source}: reading に上・中・下の読みをひらがなで指定してください")
    meter = [sum(char not in "ぁぃぅぇぉゃゅょゎ" for char in part) for part in reading]
    poem.update(reading=reading, meter=meter)
    if meter != [5, 7, 5] or "meter_note" in row:
        poem["meter_note"] = field(row, "meter_note", source)
    return poem


def draft_files(run, profile, writer_count=None):
    count = profile["writers"] if writer_count is None else writer_count
    writers = string.ascii_uppercase[:count]
    files = [run / "drafts" / f"{writer}.json" for writer in writers]
    if writer_count is None:
        for path in sorted((run / "supplements").glob("*.json")):
            if not re.fullmatch(rf"[{writers}][0-9]{{2}}", path.stem):
                raise ValueError(f"{path}: 補作のファイル名は担当の英字と数字2桁にしてください（例 A01.json）")
            files.append(path)
    return files


def read_drafts(run, profile, writer_count=None):
    drafts = {}
    seen = {writer: set() for writer in string.ascii_uppercase[:profile["writers"]]}
    for path in draft_files(run, profile, writer_count):
        supplement = path.parent.name == "supplements"
        rows = read_array(path)
        if supplement and not rows:
            raise ValueError(f"{path}: 補作を1句以上指定してください")
        if not supplement and len(rows) != profile["poems_per_writer"]:
            raise ValueError(f"{path}: {profile['poems_per_writer']}句を揃えてください（現在{len(rows)}句）")
        writer_verses = seen[path.stem[0]]
        prefix = f"{path.stem}-" if supplement else path.stem
        for index, row in enumerate(rows, 1):
            poem_id = f"{prefix}{index:02}"
            source = f"{path} / {poem_id}"
            poem = validate_poem(row, source)
            if supplement or "round_limits" in profile:
                field(row, "observation", source)
            verse = canonical(poem["verse"])
            if verse in writer_verses:
                raise ValueError(f"{path}: {poem_id} が同じ担当の句と重複しています")
            writer_verses.add(verse)
            drafts[poem_id] = poem
    return drafts


def display_poem(poem):
    reading = "／".join(poem["reading"])
    meter = "・".join(map(str, poem["meter"]))
    extra = f"・{poem['meter_note']}" if "meter_note" in poem else ""
    return f"{reading}（{meter}） ／ 季語 {poem['kigo']}（{poem['season']}）{extra}"


def pool(run, profile, *, first_wave=False):
    count = profile.get("first_wave_writers") if first_wave else None
    if first_wave and count is None:
        raise ValueError("この回の設定には前半の作句担当数がありません")
    path = run / ("pool-first.md" if first_wave else "pool.md")
    drafts = read_drafts(run, profile, count)
    write_text(path, render_pool(drafts, first_wave=first_wave))
    print(f"原句 {len(drafts)}句 — {path}")


def render_pool(drafts, *, first_wave=False):
    lines = ["# 前半の原句一覧" if first_wave else "# 原句一覧", ""]
    for poem_id, poem in drafts.items():
        lines.append(f"- **{poem_id}　{poem['verse']}** — {display_poem(poem)}")
    return "\n".join(lines) + "\n"


def reopen(run, profile, run_id, reason_file):
    reason = reason_file.read_text(encoding="utf-8").strip()
    if not reason:
        raise ValueError("不足した観察と補作の方針を記録してください")
    read_drafts(run, profile)
    brief = (run / "brief.md").read_text(encoding="utf-8").strip()
    brief += f"\n\n## 初選をやり直す経緯\n\n元の回 {profile['run_id']}\n\n{reason}"
    sources = draft_files(run, profile)
    for name in ("pool-first.md", "first-wave-review.md", "supplements/brief.md"):
        path = run / name
        if path.is_file():
            sources.append(path)
    with TemporaryDirectory(prefix=".atelier-", dir=run.parent) as temporary:
        staged = create_run(Path(temporary), run_id, profile, brief)
        for source in sources:
            destination = staged / source.relative_to(run)
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, destination)
        target = run.parent / run_id
        target.mkdir()
        try:
            staged.replace(target)
        except OSError:
            target.rmdir()
            raise
    print(f"初選のやり直し — {target}")


def read_shortlist(run, profile, drafts):
    ids = read_array(run / "shortlist.json")
    if any(not isinstance(poem_id, str) or poem_id not in drafts for poem_id in ids):
        raise ValueError("shortlist.json に原句一覧の ID を指定してください")
    if len(ids) != len(set(ids)) or not profile["final_size"] <= len(ids) <= profile["shortlist_limit"]:
        raise ValueError("shortlist.json の件数と重複を確認してください")
    if len({canonical(drafts[poem_id]["verse"]) for poem_id in ids}) != len(ids):
        raise ValueError("shortlist.json の同じ句を一つに絞ってください")
    return ids


def indexed_rows(path, expected_ids):
    index = {}
    for row in read_array(path):
        poem_id = field(row, "id", path)
        if poem_id not in expected_ids or poem_id in index:
            raise ValueError(f"{path}: {poem_id} の対象範囲または重複を確認してください")
        index[poem_id] = row
    missing = sorted(set(expected_ids) - index.keys())
    if missing:
        raise ValueError(f"{path}: {', '.join(missing)} の記録がありません")
    return index


def read_round_candidates(path, previous, minimum, maximum):
    ids = read_array(path)
    if any(not isinstance(poem_id, str) or poem_id not in previous for poem_id in ids):
        raise ValueError(f"{path}: 前の巡の候補の ID を指定してください")
    if len(ids) != len(set(ids)) or not minimum <= len(ids) <= maximum:
        raise ValueError(f"{path}: 重複のない {minimum}〜{maximum} 句を指定してください")
    return ids


def read_workshop(run, profile, through, *, proposals_only=False, pending_choice=False):
    if not 1 <= through <= profile["revision_rounds"]:
        raise ValueError(f"改稿の回は 1〜{profile['revision_rounds']} の範囲で指定してください")
    drafts = read_drafts(run, profile)
    ids = read_shortlist(run, profile, drafts)
    versions = {poem_id: [drafts[poem_id]] for poem_id in ids}
    limits = profile.get("round_limits")
    reviews = []
    preferred = dict.fromkeys(ids, 0)
    for turn in range(1, through + 1):
        folder = run / "rounds" / f"{turn:02}"
        if limits and turn > 1:
            ids = read_round_candidates(folder / "candidates.json", ids, profile["final_size"], limits[turn - 1])
        if profile.get("round_reader_feedback"):
            check_round_feedback(run, turn, round_start_entries(ids, versions, preferred))
        critique_path = folder / "critique.json"
        critique = indexed_rows(critique_path, ids)
        checking_proposals = proposals_only and turn == through
        checking_choice = not checking_proposals and not (pending_choice and turn == through)
        for poem_id, row in critique.items():
            source = f"{critique_path} / {poem_id}"
            base = row.get("base")
            if type(base) is not int or not 0 <= base < turn:
                raise ValueError(f"{source}: base に存在する稿番号を指定してください")
            field(row, "issue", source)
            field(row, "suggestion", source)
            if limits:
                if base != preferred[poem_id]:
                    raise ValueError(f"{source}: base は前巡で残した稿番号に合わせてください")
                field(row, "preserve", source)
                if "proposal" not in row:
                    raise ValueError(f"{source}: proposal に添削句、または据え置く場合は null を指定してください")
                if row["proposal"] is not None:
                    row["proposal"] = validate_poem(row["proposal"], source)
                if checking_choice:
                    choice = row.get("preferred")
                    if type(choice) is not int or not 0 <= choice <= turn:
                        raise ValueError(f"{source}: preferred に残す稿番号を指定してください")
                    field(row, "comparison", source)
                    preferred[poem_id] = choice
        reviews.append({poem_id: critique[poem_id] for poem_id in ids})
        if checking_proposals:
            break
        for writer in sorted({poem_id[0] for poem_id in ids}):
            path = folder / f"{writer}.json"
            writer_ids = [poem_id for poem_id in ids if poem_id.startswith(writer)]
            for poem_id, row in indexed_rows(path, writer_ids).items():
                source = f"{path} / {poem_id}"
                edit_note = field(row, "edit_note", source)
                poem = validate_poem(row, source)
                if limits:
                    poem["edit_note"] = edit_note
                versions[poem_id].append(poem)
    return versions, reviews


def read_versions(run, profile, through):
    versions, _ = read_workshop(run, profile, through)
    return versions


def render_comparison(turn, versions, review):
    lines = [f"# 第{turn}巡の読み比べ", ""]
    for poem_id, row in review.items():
        entries = versions[poem_id]
        lines.extend([f"## {poem_id}", ""])
        poems = [("原句", entries[0])]
        if row["base"]:
            poems.append((f"土台の第{row['base']}稿", entries[row["base"]]))
        if row["proposal"] is not None:
            poems.append(("添削案", row["proposal"]))
        poems.append((f"担当の第{turn}稿", entries[turn]))
        for label, poem in poems:
            lines.append(f"- {label} **{poem['verse']}** — {display_poem(poem)}")
        choice = row["preferred"]
        chosen = f"第{choice}稿" if choice else "原句"
        lines.extend([
            f"- 残したい箇所　{row['preserve']}", f"- 診断　{row['issue']}",
            f"- 添削の意図　{row['suggestion']}", f"- 改稿の意図　{entries[turn]['edit_note']}",
            f"- 採用　{chosen} **{entries[choice]['verse']}**。{row['comparison']}", "",
        ])
    return "\n".join(lines)


def review_round(run, profile, number, proposals_only):
    versions, reviews = read_workshop(run, profile, number, proposals_only=proposals_only)
    review = reviews[-1]
    if "round_limits" in profile and not proposals_only:
        path = run / "rounds" / f"{number:02}" / "comparison.md"
        write_text(path, render_comparison(number, versions, review))
    checked = "添削案" if proposals_only else "選評と改稿"
    print(f"第{number}巡の{checked}を確認済み — {len(review)}句")


def reading_signature(poem):
    return canonical(poem["verse"]), tuple(poem["reading"]), poem["kigo"], poem["season"]


def ballot_records(run, profile, *, prepared=None):
    versions, reviews = prepared if prepared is not None else read_workshop(run, profile, profile["revision_rounds"])
    candidates = []
    for poem_id in reviews[-1]:
        seen = set()
        for turn, poem in enumerate(versions[poem_id]):
            signature = reading_signature(poem)
            if signature not in seen:
                verse_data = {key: value for key, value in poem.items() if key != "edit_note"}
                candidates.append({"source": poem_id, "turn": turn, "poem": verse_data})
                seen.add(signature)
    random.Random(profile["run_id"]).shuffle(candidates)
    return {f"K{index:03}": row for index, row in enumerate(candidates, 1)}


def render_ballot(title, rows):
    lines = [f"# {title}", ""]
    for code, row in rows.items():
        poem = row["poem"]
        lines.append(f"- **{code}　{poem['verse']}** — {display_poem(poem)}")
    return "\n".join(lines) + "\n"


def write_blind_ballot(run, profile, label, title, groups, questions):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", label):
        raise ValueError("選句票のラベルには半角英数字、ハイフン、下線を使ってください")
    folder = run / "reviews"
    folder.mkdir(exist_ok=True)
    ballot_path = folder / f"{label}-ballot.md"
    index_path = folder / f"{label}-key.json"
    rng = random.Random(f"{profile['run_id']}:{label}")
    rng.shuffle(groups)
    index = {}
    lines = [f"# {title}", "", *questions, ""]
    for number, group in enumerate(groups, 1):
        rng.shuffle(group)
        for offset, row in enumerate(group, 1):
            code = f"{number:02d}-{offset}" if len(group) > 1 else f"{number:02d}"
            index[code] = row
            lines.append(f"- **{code}**　{row['verse']}")
        lines.append("")
    content = "\n".join(lines)
    if ballot_path.exists() and ballot_path.read_text(encoding="utf-8") != content:
        raise ValueError(f"{label}: 作成済みの選句票と内容が異なります。別のラベルで作成してください")
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(index, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.close()
            try:
                os.link(temporary, index_path)
            except FileExistsError:
                if read_json(index_path) != index:
                    raise ValueError(f"{label}: 作成済みの選句票と内容が異なります。別のラベルで作成してください")
        finally:
            temporary.unlink(missing_ok=True)
    write_text(ballot_path, content)
    print(ballot_path)


def round_start_entries(ids, versions, preferred):
    return {
        poem_id: {
            "source": poem_id, "version": preferred[poem_id], "verse": versions[poem_id][preferred[poem_id]]["verse"],
        }
        for poem_id in ids
    }


def round_ballot(run, profile, number):
    limits = profile.get("round_limits")
    if not limits or not 1 <= number <= len(limits):
        raise ValueError("巡ごとの上限がある回と、有効な巡番号を指定してください")
    if number == 1:
        drafts = read_drafts(run, profile)
        ids = read_shortlist(run, profile, drafts)
        versions = {poem_id: [drafts[poem_id]] for poem_id in ids}
        preferred = dict.fromkeys(ids, 0)
    else:
        versions, reviews = read_workshop(run, profile, number - 1)
        previous = reviews[-1]
        candidates = run / "rounds" / f"{number:02}" / "candidates.json"
        ids = read_round_candidates(candidates, previous, profile["final_size"], limits[number - 1])
        preferred = {poem_id: previous[poem_id]["preferred"] for poem_id in ids}
    entries = round_start_entries(sorted(ids), versions, preferred)
    write_blind_ballot(run, profile, f"round-{number}-start", "独立した読み手の選句票",
                       [[entry] for entry in entries.values()], [FREE_READING_PROMPT])


def check_reader_notes(path):
    content = path.read_text(encoding="utf-8")
    parts = re.split(r"^##[ \t]+(.+?)[ \t]*$", content, flags=re.MULTILINE)
    notes = {heading: body.strip() for heading, body in zip(parts[1::2], parts[2::2])}
    if not notes.get("読み手1") or not notes.get("読み手2"):
        raise ValueError(f"{path}: 「## 読み手1」「## 読み手2」にそれぞれの感想を記録してください")


def check_round_feedback(run, number, expected):
    folder = run / "reviews"
    label = f"round-{number}-start"
    key = folder / f"{label}-key.json"
    rows = checked_blind_ballot(folder / f"{label}-ballot.md", key)
    by_source = {field(row, "source", key): row for row in rows.values()}
    if len(rows) != len(expected) or by_source != expected:
        raise ValueError(f"{key}: 伏せ読みの票が当該巡の候補・採用稿と一致しません")
    check_reader_notes(folder / f"{label}-review.md")


def review_ballot(run, profile, label, ids, turn, baseline):
    if len(ids) != len(set(ids)):
        raise ValueError("伏せ読みでは同じ原句IDを一度だけ指定してください")
    if baseline is not None and (turn is None or not 1 <= baseline < turn or "round_limits" not in profile):
        raise ValueError("--baseline には、確定済みの --round より前の巡を指定してください")
    if turn is None:
        drafts = read_drafts(run, profile)
        choices = {poem_id: [("原句", poem)] for poem_id, poem in drafts.items() if poem_id in ids}
    else:
        versions, reviews = read_workshop(run, profile, turn, pending_choice=baseline is None)
        choices = {}
        for poem_id in ids:
            row = reviews[-1].get(poem_id)
            if row is None:
                continue
            entries = versions[poem_id]
            if baseline is not None:
                original_choice = reviews[baseline - 1][poem_id]["preferred"]
                choices[poem_id] = [(f"第{phase}巡採用", entries[choice]) for phase, choice in (
                    (baseline, original_choice), (turn, row["preferred"]),
                )]
                continue
            options = [("原句", entries[0]), (f"第{turn}稿", entries[turn])]
            if row["base"]:
                options.append((f"第{row['base']}稿", entries[row["base"]]))
            if row.get("proposal") is not None:
                options.append(("添削案", row["proposal"]))
            choices[poem_id] = options
    groups = []
    for poem_id in sorted(ids):
        if poem_id not in choices:
            raise ValueError(f"{poem_id}: 今回の候補にない原句IDです")
        seen = set()
        group = []
        for version, poem in choices[poem_id]:
            verse = canonical(poem["verse"])
            if verse not in seen:
                group.append({"source": poem_id, "version": version, "verse": poem["verse"]})
                seen.add(verse)
        groups.append(group)
    questions = [
        "本文から最初に見えた景と、読み返した箇所を教えてください。",
        "判断が割れそうな箇所では、どの語からどう読んだかをお書きください。",
    ]
    if profile.get("round_reader_feedback"):
        questions = [FREE_READING_PROMPT]
    write_blind_ballot(run, profile, label, "途中の伏せ読み", groups, questions)


def jury_ballot(versions, review, rows):
    preferred = {poem_id: reading_signature(versions[poem_id][row["preferred"]]) for poem_id, row in review.items()}
    return {code: row for code, row in rows.items() if reading_signature(row["poem"]) == preferred[row["source"]]}


def render_jury(profile, rows):
    if not profile.get("round_reader_feedback"):
        return render_ballot("独立選評用の選句票", rows)
    lines = ["# 最終稿の伏せ読み", "", FREE_READING_PROMPT, ""]
    lines.extend(f"- **{code}**　{row['poem']['verse']}" for code, row in rows.items())
    return "\n".join(lines) + "\n"


def ballot(run, profile):
    prepared = read_workshop(run, profile, profile["revision_rounds"])
    rows = ballot_records(run, profile, prepared=prepared)
    index = run / "ballot.json"
    jury_path = run / "jury.md"
    jury = None
    if "round_limits" in profile:
        versions, reviews = prepared
        jury = render_jury(profile, jury_ballot(versions, reviews[-1], rows))
        if (run / "jury-review.md").exists() and (
            not jury_path.exists() or jury_path.read_text(encoding="utf-8") != jury
        ):
            raise ValueError("独立選評後に選句票が変わりました。前の選評を別名で控え、新しい選句票で読み直してください")
    if (run / "selection.json").exists() and (not index.exists() or read_json(index) != rows):
        raise ValueError("句稿が選句票作成後に変わりました。前の選句記録を別名で控え、選句票を作り直してください")
    write_text(run / "ballot.md", render_ballot("最終選句票", rows))
    write_json(index, rows)
    if jury is not None:
        write_text(jury_path, jury)
    print(f"選句票 {len(rows)}稿 — {run / 'ballot.md'}")


def checked_ballot(run, profile):
    prepared = read_workshop(run, profile, profile["revision_rounds"])
    ballot_index = read_json(run / "ballot.json")
    if ballot_index != ballot_records(run, profile, prepared=prepared):
        raise ValueError("選句票と句稿が一致しません。現在の句稿から選句票を作り直してください")
    if (run / "ballot.md").read_text(encoding="utf-8") != render_ballot("最終選句票", ballot_index):
        raise ValueError("最終選句票の本文・読みと対応表が一致しません。ballot で選句票を作り直してください")
    return ballot_index, prepared


def selected_ballot_rows(run, profile, ballot_index):
    path = run / "selection.json"
    selection = read_array(path)
    if len(selection) != profile["final_size"]:
        raise ValueError(f"{path}: 最終選の {profile['final_size']} 句を指定してください")
    sources, verses = set(), set()
    selected = []
    for row in selection:
        code = field(row, "id", path)
        if code not in ballot_index:
            raise ValueError(f"{path}: {code} は選句票にありません")
        candidate = ballot_index[code]
        poem = candidate["poem"]
        verse = canonical(poem["verse"])
        source = candidate["source"]
        if source in sources or verse in verses:
            raise ValueError(f"{path}: {code} は同じ原句または同じ表記の別稿です")
        sources.add(source)
        verses.add(verse)
        comment = field(row, "comment", path)
        selected.append((candidate, comment))
    return selected


def feedback_ballot(run, profile, other, label, count):
    if run == other or count < 1:
        raise ValueError("比較には異なる2回と、1以上の句数を指定してください")
    other_profile = settings(read_json(other / "settings.json"))
    collections = []
    kigo = set()
    for source, source_profile in ((run, profile), (other, other_profile)):
        index, _ = checked_ballot(source, source_profile)
        selected = selected_ballot_rows(source, source_profile, index)
        candidates = [candidate for candidate, _ in selected]
        kigo.update(canonical(candidate["poem"]["kigo"]) for candidate in candidates)
        collections.append((source_profile["run_id"], candidates))
    if len(kigo) != 1:
        raise ValueError("兼題が同じ句を選んでください")
    shared = set.intersection(*(
        {canonical(candidate["poem"]["verse"]) for candidate in candidates}
        for _, candidates in collections
    ))
    groups = []
    for run_id, candidates in collections:
        distinct = [candidate for candidate in candidates if canonical(candidate["poem"]["verse"]) not in shared]
        if count > len(distinct):
            raise ValueError(f"{run_id}: 両回に共通する句を除くと {len(distinct)} 句です。--count を減らしてください")
        for candidate in distinct[:count]:
            groups.append([{"run": run_id, "source": candidate["source"], "version": candidate["turn"],
                            "verse": candidate["poem"]["verse"]}])
    write_blind_ballot(run, profile, label, "同じ兼題の読み比べ", groups, [
        "読み返した句があれば、番号と、どの一語や運びが気になったかを教えてください。",
        "既視感のあった句があれば、番号と、そう感じた箇所を教えてください。",
    ])


def checked_blind_ballot(ballot, key):
    if not ballot.is_file() or not key.is_file():
        raise ValueError(f"{ballot}: 伏せ読みの選句票と対応表を揃えてください")
    rows = read_json(key)
    if not isinstance(rows, dict) or not rows:
        raise ValueError(f"{key}: 選句票の番号と本文を確認してください")
    expected = sorted(f"- **{code}**　{field(row, 'verse', key)}" for code, row in rows.items())
    actual = sorted(line for line in ballot.read_text(encoding="utf-8").splitlines() if line.startswith("- **"))
    if actual != expected:
        raise ValueError(f"{ballot}: 選句票の番号・本文と対応表が一致しません")
    return rows


def review_files(run):
    folder = run / "reviews"
    suffixes = ("-ballot.md", "-key.json", "-review.md")
    labels = {path.name.removesuffix(suffix) for suffix in suffixes for path in folder.glob(f"*{suffix}") if path.is_file()}
    files = []
    for label in sorted(labels):
        ballot, key, review = (folder / f"{label}{suffix}" for suffix in suffixes)
        checked_blind_ballot(ballot, key)
        files.extend((ballot, key))
        if review.is_file():
            files.append(review)
    return files


def finish(run, profile):
    ballot_index, prepared = checked_ballot(run, profile)
    selection = selected_ballot_rows(run, profile, ballot_index)
    if (run / "pool.md").read_text(encoding="utf-8") != render_pool(read_drafts(run, profile)):
        raise ValueError("原句一覧と保存された原句・補作が一致しません。pool で作り直してください")
    brief = (run / "brief.md").read_text(encoding="utf-8").strip()
    lines = [f"# 最終選 {len(selection)}句", "", "## 今回の兼題・素材", "", brief, "", "## 選句と選評", ""]
    for index, (candidate, comment) in enumerate(selection, 1):
        poem = candidate["poem"]
        lines.extend([f"### {index}. {poem['verse']}", "", display_poem(poem), "", comment, ""])
    files = [run / name for name in (
        "brief.md", "settings.json", "pool.md", "shortlist.json", "ballot.md", "ballot.json", "selection.json",
    )]
    submissions = draft_files(run, profile)
    files.extend(submissions)
    supplement_brief = run / "supplements" / "brief.md"
    if len(submissions) > profile["writers"]:
        if not supplement_brief.is_file() or not supplement_brief.read_text(encoding="utf-8").strip():
            raise ValueError("supplements/brief.md に補作が必要になった理由と配題を記録してください")
        files.append(supplement_brief)
    files.extend(review_files(run))
    versions, reviews = prepared
    text_files = [("final.md", "\n".join(lines))]
    if "first_wave_writers" in profile:
        files.extend(run / name for name in ("pool-first.md", "first-wave-review.md"))
    staged = "round_limits" in profile
    screening = run / "screening.md"
    if staged or screening.is_file():
        files.append(screening)
    if staged:
        jury_rows = jury_ballot(versions, reviews[-1], ballot_index)
        jury = render_jury(profile, jury_rows)
        if (run / "jury.md").read_text(encoding="utf-8") != jury:
            raise ValueError("独立選評用の選句票と採用稿が一致しません")
        if profile.get("round_reader_feedback"):
            check_reader_notes(run / "jury-review.md")
            if any(candidate not in jury_rows.values() for candidate, _ in selection):
                raise ValueError("最終選で採る稿を最終巡に残し、最終稿の票と読み手の評を揃えてください")
        files.extend(run / name for name in ("jury.md", "jury-review.md"))
    for turn, review in enumerate(reviews, 1):
        folder = run / "rounds" / f"{turn:02}"
        files.append(folder / "critique.json")
        files.extend(folder / f"{writer}.json" for writer in sorted({poem_id[0] for poem_id in review}))
        if staged:
            if turn > 1:
                files.append(folder / "candidates.json")
            text_files.append((f"rounds/{turn:02}/comparison.md", render_comparison(turn, versions, review)))
    target = run / "workshop.zip"
    write_archive(target, run, files, text_files)
    for name, content in text_files:
        write_text(run / name, content)
    print(f"最終選 — {run / 'final.md'}\n引き継ぎZIP — {target}")


def export_materials():
    source = PROJECT / "reference"
    files = sorted(file for file in source.rglob("*.md") if file.is_file())
    if not files:
        raise ValueError(f"{source}: ZIPに入れる学習資料を確認してください")
    target = PROJECT / "exports" / "伝統俳句_学習と添削_引き継ぎ.zip"
    target.parent.mkdir(exist_ok=True)
    write_archive(target, source, files)
    print(f"学習資料 {len(files)}ファイル — {target}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("start", help="兼題と設定を保存する")
    create.add_argument("id")
    create.add_argument("--brief", required=True, type=Path)
    create.add_argument("--profile", choices=("standard", "rehearsal", "pilot"), default="standard")
    create.add_argument("--runs-dir", type=Path, default=PROJECT / "runs")
    restart = commands.add_parser("reopen", help="元の回を保存したまま、初選をやり直す回を開く")
    restart.add_argument("run", type=Path)
    restart.add_argument("id")
    restart.add_argument("--reason", type=Path, required=True)
    round_vote = commands.add_parser("round-ballot", help="添削前の候補を本文だけの選句票にする")
    round_vote.add_argument("run", type=Path)
    round_vote.add_argument("number", type=int)
    early = commands.add_parser("review-ballot", help="初選または推敲の途中で本文だけの選句票を作る")
    early.add_argument("run", type=Path)
    early.add_argument("label")
    early.add_argument("ids", nargs="+")
    early.add_argument("--round", type=int, dest="turn")
    early.add_argument("--baseline", type=int, help="指定した巡と --round の採用稿だけを比べる")
    feedback = commands.add_parser("feedback-ballot", help="同じ兼題の2回の最終選を伏せて比較する")
    feedback.add_argument("run", type=Path)
    feedback.add_argument("other", type=Path)
    feedback.add_argument("label")
    feedback.add_argument("--count", type=int, default=3)
    commands.add_parser("export-materials", help="学習資料と添削ノートから引き継ぎZIPを作る")
    for action, help_text in (
        ("pool", "原句を確認して一覧にする"), ("round", "指定した回までの選評と改稿を確認する"),
        ("ballot", "作者と稿番号を省いた最終選句票を作る"), ("finish", "最終選の読み物と引き継ぎZIPを作る"),
    ):
        command = commands.add_parser(action, help=help_text)
        command.add_argument("run", type=Path)
        if action == "round":
            command.add_argument("number", type=int)
            command.add_argument("--proposals-only", action="store_true", help="担当者の改稿前に添削案を確認する")
        if action == "pool":
            command.add_argument("--first-wave", action="store_true", help="前半の担当だけを確認する")
    args = parser.parse_args()
    try:
        if args.command == "start":
            start(args)
        elif args.command == "export-materials":
            export_materials()
        else:
            run = args.run.resolve()
            profile = settings(read_json(run / "settings.json"))
            if args.command == "round":
                review_round(run, profile, args.number, args.proposals_only)
            elif args.command == "round-ballot":
                round_ballot(run, profile, args.number)
            elif args.command == "pool":
                pool(run, profile, first_wave=args.first_wave)
            elif args.command == "reopen":
                reopen(run, profile, args.id, args.reason)
            elif args.command == "review-ballot":
                review_ballot(run, profile, args.label, args.ids, args.turn, args.baseline)
            elif args.command == "feedback-ballot":
                feedback_ballot(run, profile, args.other.resolve(), args.label, args.count)
            else:
                {"ballot": ballot, "finish": finish}[args.command](run, profile)
    except (OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
