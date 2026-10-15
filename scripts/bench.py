"""Median reply latency of several /ws settings, via probe.py against a running server.

Usage: python scripts/bench.py PORT RUNS 'label=query' ['label=query' ...]
Each run is one real call (--until-reply); needs ELEVENLABS_* in .env for the caller voice.
"""
import os
import statistics
import subprocess
import sys

SAY = os.environ.get('BENCH_SAY') or 'Xin chào, cho mình gửi lời nhắn cho anh Tiến, mình tên là Lan, nhờ anh gọi lại cho mình khi rảnh nhé.'
port, runs, *variants = sys.argv[1:]
for variant in variants:
    label, _, query = variant.partition('=')
    times = []
    for _ in range(int(runs)):
        out = subprocess.run(
            [sys.executable, 'scripts/probe.py', port, '30', '--say', SAY, '--until-reply', '--query', query],
            capture_output=True, text=True, encoding='utf-8',
        ).stdout
        for line in out.splitlines():
            if 'reply latency' in line:
                times.append(int(line.split('latency ')[1].split('ms')[0]))
    print(f'{label:32} median {statistics.median(times) if times else "-":>6} ms  runs {times}', flush=True)
