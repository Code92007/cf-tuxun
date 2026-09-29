#!/usr/bin/env python3
"""Build a non-Gym Codeforces catalog skeleton from the official public API.

The output intentionally has empty clue fields.  An editor must add a unique,
title-free statement excerpt before import_pack.py will accept it.
"""

import argparse
import json
import re
import time
import urllib.request
from collections import defaultdict
from pathlib import Path


API = "https://codeforces.com/api"
ROUND_RE = re.compile(r"(?:Codeforces Round|Educational Codeforces Round)\s+(\d+)")


def fetch(method, query=""):
    request = urllib.request.Request(
        f"{API}/{method}{query}",
        headers={"User-Agent": "CF-Snap-Catalog/1.0 (public API; one-shot sync)"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if payload.get("status") != "OK":
        raise RuntimeError(payload.get("comment", "Codeforces API failed"))
    return payload["result"]


def division_for(name):
    if name.startswith("Educational Codeforces Round"):
        return "Edu"
    match = re.search(r"Div\.\s*([123])", name)
    return f"Div. {match.group(1)}" if match else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-contest", type=int, default=1)
    parser.add_argument("--max-contest", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    contests = fetch("contest.list", "?gym=false")
    time.sleep(2.1)
    problem_result = fetch("problemset.problems", "?lang=en")
    contest_map = {}
    for contest in contests:
        contest_id = contest["id"]
        name = contest["name"]
        round_match = ROUND_RE.search(name)
        division = division_for(name)
        if (
            contest.get("type") != "CF" or contest.get("phase") != "FINISHED"
            or not round_match or not division or contest_id < args.min_contest
            or (args.max_contest is not None and contest_id > args.max_contest)
        ):
            continue
        contest_map[contest_id] = {
            "round": int(round_match.group(1)),
            "division": division,
            "name": name,
            "time": contest.get("startTimeSeconds", 0),
        }

    groups = defaultdict(list)
    for problem in problem_result["problems"]:
        contest_id = problem.get("contestId")
        contest = contest_map.get(contest_id)
        if not contest or problem.get("type") != "PROGRAMMING":
            continue
        group_key = (problem["name"].strip().lower(), contest["time"])
        groups[group_key].append((problem, contest))

    output = []
    for members in groups.values():
        problem, contest = members[0]
        aliases = [
            [p["contestId"], p["index"], c["round"], c["division"]]
            for p, c in sorted(members, key=lambda item: item[0]["contestId"])
        ]
        canonical = f'{problem["contestId"]}{problem["index"]}'
        output.append({
            "key": canonical,
            "title": problem["name"],
            "clue": "",
            "rating": problem.get("rating", 0),
            "contest_time": contest["time"],
            "round_type": " / ".join(sorted({c["division"] for _, c in members})),
            "source_url": f'https://codeforces.com/contest/{problem["contestId"]}/problem/{problem["index"]}',
            "aliases": aliases,
            "tags": problem.get("tags", []),
        })
    output.sort(key=lambda item: (item["contest_time"], item["key"]), reverse=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(output)} non-Gym problems to {args.output}")


if __name__ == "__main__":
    main()
