"""用 CDP 检查/截图 Electron 窗口（复用产品 venv 里已有的 websockets，不装新依赖）。

    python tools/cdp.py list
    python tools/cdp.py eval "document.title"
    python tools/cdp.py shot  _shots/desktop.png

前提：Electron 以 `--remote-debugging-port=9333` 启动（`pnpm start:inspect`）。
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

PORT = 9333


def targets() -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as response:
        return json.load(response)


def page_target() -> dict:
    for target in targets():
        if target.get("type") == "page":
            return target
    raise SystemExit("没有 page 目标（Electron 起来了吗？带 --inspect-shell 了吗？）")


class Session:
    def __init__(self, url: str) -> None:
        self.ws = connect(url, max_size=None, legacy=True)
        self.seq = 0

    def call(self, method: str, **params):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.seq:
                if "error" in message:
                    raise SystemExit(f"{method} 失败：{message['error']}")
                return message.get("result", {})

    def close(self) -> None:
        self.ws.close()


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "list"

    if command == "list":
        for target in targets():
            print(f"{target.get('type'):8} {target.get('title','')[:40]:42} {target.get('url','')[:90]}")
        return 0

    target = page_target()
    session = Session(target["webSocketDebuggerUrl"])
    try:
        if command == "eval":
            result = session.call("Runtime.evaluate", expression=argv[2], returnByValue=True, awaitPromise=True)
            print(json.dumps(result.get("result", {}), ensure_ascii=False))
        elif command == "eval-file":
            expression = Path(argv[2]).read_text(encoding="utf-8")
            result = session.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
            print(json.dumps(result.get("result", {}), ensure_ascii=False))
        elif command == "shot":
            session.call("Page.enable")
            data = session.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"]
            out = Path(argv[2])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(data))
            print(f"wrote {out} ({out.stat().st_size} bytes)")
        elif command == "metrics":
            session.call("Page.enable")
            metrics = session.call("Page.getLayoutMetrics")
            print(json.dumps(metrics.get("cssVisualViewport", metrics), ensure_ascii=False))
        else:
            raise SystemExit(f"未知命令：{command}")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
