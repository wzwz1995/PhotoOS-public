"""按 URL 片段截图某个 CDP 目标（例如桌面壳的输入框窗口）。

    python tools/shot_target.py prompt.html _shots/prompt-dialog.png
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.request
from pathlib import Path

from websockets.sync.client import connect


def main(argv: list[str]) -> int:
    needle, out = argv[1], Path(argv[2])
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list", timeout=5))
    for target in targets:
        print(f"  {target.get('type'):8} {target.get('title','')[:30]:32} {target.get('url','')[:80]}")
    matches = [t for t in targets if needle in (t.get("url") or "")]
    if not matches:
        print(f"没找到 URL 含 {needle!r} 的目标")
        return 1
    ws = connect(matches[0]["webSocketDebuggerUrl"], max_size=None, legacy=True)
    ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
    ws.recv()
    ws.send(json.dumps({"id": 2, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") == 2:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(message["result"]["data"]))
            print(f"wrote {out}")
            break
    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
