#!/usr/bin/env python3
"""Validate and import a reviewed CF Snap question pack."""

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfshot.db import get_db, init_db  # noqa: E402


def normalized(text):
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def validate_pack(items):
    errors = []
    seen_aliases = {}
    normalized_clues = []
    required = {"key", "title", "clue", "rating", "contest_time", "round_type", "source_url", "aliases"}
    for index, item in enumerate(items):
        label = item.get("key", f"item #{index + 1}")
        missing = sorted(required - set(item))
        if missing:
            errors.append(f"{label}: missing {', '.join(missing)}")
            continue
        clue = normalized(str(item["clue"]))
        title = normalized(str(item["title"]))
        clue_kind = str(item.get("clue_kind", "statement"))
        image_path = str(item.get("image_path", "")).strip()
        if clue_kind not in {"statement", "fragment", "image"}:
            errors.append(f"{label}: clue_kind must be statement, fragment, or image")
        if clue_kind == "statement" and (len(clue) < 180 or len(clue.split()) < 28):
            errors.append(f"{label}: statement clue is too short to be uniquely recognizable")
        if clue_kind == "fragment" and len(clue) < 12:
            errors.append(f"{label}: text fragment must contain at least 12 normalized characters")
        if clue_kind == "image":
            target = Path(__file__).resolve().parents[1] / image_path
            if not image_path or not target.is_file():
                errors.append(f"{label}: image_path must reference an existing repository image")
        if title and title in clue:
            errors.append(f"{label}: clue leaks the problem title")
        if "gym" in str(item["round_type"]).lower() or "/gym/" in str(item["source_url"]).lower():
            errors.append(f"{label}: Gym problems are not allowed")
        if not isinstance(item["rating"], int) or not 800 <= item["rating"] <= 4000:
            errors.append(f"{label}: rating must be an integer from 800 to 4000")
        if not isinstance(item["aliases"], list) or not item["aliases"]:
            errors.append(f"{label}: at least one alias is required")
        else:
            for alias in item["aliases"]:
                if not isinstance(alias, list) or len(alias) != 4:
                    errors.append(f"{label}: aliases must be [contest_id, index, round_number, division]")
                    continue
                alias_key = f"{alias[0]}{str(alias[1]).upper()}"
                previous = seen_aliases.get(alias_key)
                if previous and previous[1] != title:
                    errors.append(f"{label}: alias {alias_key} is already used by {previous[0]} with another title")
                seen_aliases[alias_key] = (label, title)
        for other_label, other_clue in normalized_clues:
            similarity = difflib.SequenceMatcher(None, clue, other_clue).ratio()
            if similarity >= 0.86:
                errors.append(f"{label}: clue is {similarity:.0%} similar to {other_label}")
        normalized_clues.append((label, clue))
    return errors


def import_pack(items):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        for item in items:
            db.execute(
                """
                INSERT INTO questions(canonical_key,title,clue,rating,contest_time,round_type,source_url,clue_kind,image_path,brain,active,unique_checked)
                VALUES(?,?,?,?,?,?,?,?,?,?,1,1)
                ON CONFLICT(canonical_key) DO UPDATE SET title=excluded.title,clue=excluded.clue,
                    rating=excluded.rating,contest_time=excluded.contest_time,round_type=excluded.round_type,
                    source_url=excluded.source_url,clue_kind=excluded.clue_kind,image_path=excluded.image_path,
                    brain=excluded.brain,active=1,unique_checked=1
                """,
                (
                    item["key"], item["title"], item["clue"], item["rating"], item["contest_time"],
                    item["round_type"], item["source_url"], item.get("clue_kind", "statement"),
                    item.get("image_path"), int(bool(item.get("brain", False))),
                ),
            )
            qid = db.execute("SELECT id FROM questions WHERE canonical_key=?", (item["key"],)).fetchone()["id"]
            db.execute("DELETE FROM aliases WHERE question_id=?", (qid,))
            for contest_id, problem_index, round_number, division in item["aliases"]:
                db.execute(
                    "INSERT INTO aliases(question_id,contest_id,problem_index,round_number,division) VALUES(?,?,?,?,?)",
                    (qid, int(contest_id), str(problem_index).upper(), int(round_number), str(division)),
                )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path, help="UTF-8 JSON file containing a list of questions")
    parser.add_argument("--dry-run", action="store_true", help="validate without changing the database")
    args = parser.parse_args()
    items = json.loads(args.pack.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise SystemExit("question pack must be a JSON list")
    errors = validate_pack(items)
    if errors:
        print("Question pack rejected:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    if args.dry_run:
        print(f"OK: {len(items)} questions passed validation")
        return
    init_db()
    import_pack(items)
    print(f"Imported {len(items)} questions")


if __name__ == "__main__":
    main()
