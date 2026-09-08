"""
Download a sample of ~5-6 documents per state for analysis purposes.
Run this before the full download to quickly get representative docs.
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.config import MANIFEST_PATH
from pipeline.step02_download import run

with open(MANIFEST_PATH) as f:
    manifest = json.load(f)

# Pick 6 per state (varied index to get diverse docs)
from collections import defaultdict
by_state = defaultdict(list)
for item in manifest:
    by_state[item['state_code']].append(item)

sample = []
for state, items in by_state.items():
    # Pick from different parts of the list for variety
    step = max(1, len(items) // 6)
    picked = items[::step][:6]
    sample.extend(picked)
    print(f"{state}: picking {len(picked)} from {len(items)}")

print(f"Total sample: {len(sample)} documents")
run(sample)
