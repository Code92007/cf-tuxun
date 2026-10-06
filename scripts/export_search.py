#!/usr/bin/env python3
"""Export only public Codeforces search data for versioning and recovery."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cfshot.search_snapshot import DEFAULT_SNAPSHOT, export_snapshot

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_SNAPSHOT)
    args = parser.parse_args()
    count = export_snapshot(args.output)
    print(f'Exported {count} public problems to {args.output}')
