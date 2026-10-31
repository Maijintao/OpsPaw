"""对 /api/extract 跑标注样例,核对 service_id 与 request_type(expected_action 人工目检)。"""
import json
import pathlib
import sys

import httpx

SAMPLES = pathlib.Path(__file__).parent.parent / "tests/data/extract_samples.json"
BASE = "http://localhost:8000"


def main() -> int:
    samples = json.loads(SAMPLES.read_text())
    failures = 0
    for i, s in enumerate(samples, 1):
        r = httpx.post(f"{BASE}/api/extract", json={"text": s["text"]}, timeout=60)
        r.raise_for_status()
        got = r.json()
        exp = s["expected"]
        ok = got["service_id"] == exp["service_id"] and got["request_type"] == exp["request_type"]
        status = "PASS" if ok else "FAIL"
        failures += not ok
        print(f"[{status}] #{i} {s['text'][:24]}...")
        print(f"       期望 service_id={exp['service_id']} type={exp['request_type']}")
        print(f"       实际 service_id={got['service_id']} type={got['request_type']} 期望动作={got['expected_action']}")
    print(f"\n{len(samples) - failures}/{len(samples)} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
