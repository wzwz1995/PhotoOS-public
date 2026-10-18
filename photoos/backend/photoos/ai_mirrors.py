"""AI 下载源（镜像）的读写与连通性测试。

为什么单独一个模块：镜像属于"用户环境偏好"，不是产品逻辑；
它要能被设置页改、被安装器读，但不该污染 `Settings` 里那堆业务参数。
落盘位置仍是 `data/config.json`（与其它配置同一份文件，方便用户手改）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .ai_install import DEFAULT_MODEL_BASE, DEFAULT_PIP_INDEX
from .config import CONFIG_PATH, Settings, _read_existing_config, save_settings_overrides

PIP_KEY = "pip_index_url"
MODEL_KEY = "model_base_url"


def current(settings: Settings | None = None) -> dict[str, str]:
    """返回当前生效的镜像（未配置时给默认值）。

    直接从 `config.json` 读：镜像不属于 `Settings` 的业务字段，
    免得为了两个字符串去动 pydantic 模型（多一处能写错的地方）。
    """
    stored = _read_existing_config(CONFIG_PATH)
    return {
        PIP_KEY: str(stored.get(PIP_KEY) or DEFAULT_PIP_INDEX),
        MODEL_KEY: str(stored.get(MODEL_KEY) or DEFAULT_MODEL_BASE),
    }


def save(settings: Settings, values: dict[str, Any]) -> dict[str, str]:
    """校验并保存镜像配置。

    只接受 http(s) 地址：`file://` 或本地路径会让"下载"变成"读本地"，
    既不是用户预期，也会让后面的 sha256 校验失去意义。
    """
    cleaned: dict[str, str] = {}
    for key in (PIP_KEY, MODEL_KEY):
        if key not in values:
            continue
        raw = str(values[key] or "").strip()
        if not raw:
            continue
        if not (raw.startswith("http://") or raw.startswith("https://")):
            raise ValueError(f"{key} 必须是 http(s) 地址，收到：{raw[:60]}")
        cleaned[key] = raw.rstrip("/")
    if not cleaned:
        raise ValueError("没有可保存的镜像配置")
    save_settings_overrides(settings, cleaned)
    return current(settings)


def probe(url: str, *, timeout: float = 6.0) -> dict[str, Any]:
    """测一个镜像地址是否可用（只发 HEAD/GET 头，不下载内容）。"""
    # ⚠️ 不能用 httpx：它只是开发/测试依赖，绿色包里没有
    #（实测报错：测试镜像时 ModuleNotFoundError: No module named 'httpx'）。
    # 用标准库 urllib 发一个 Range 请求即可，零依赖。
    import urllib.error
    import urllib.request

    target = str(url or "").strip()
    if not target:
        return {"ok": False, "detail": "地址为空"}
    if not (target.startswith("http://") or target.startswith("https://")):
        return {"ok": False, "detail": "必须是 http(s) 地址"}
    try:
        request = urllib.request.Request(
            target, headers={"Range": "bytes=0-0", "User-Agent": "PhotoOS"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = int(getattr(response, "status", 200))
        return {"ok": code < 400, "status": code, "detail": f"HTTP {code}"}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "detail": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 —— 网络错误一律转成"不可用"
        return {"ok": False, "detail": str(exc)[:200]}


def model_url(base: str, relative: str) -> str:
    """把相对路径拼到模型镜像上（统一处理斜杠）。"""
    return f"{str(base).rstrip('/')}/{str(relative).lstrip('/')}"


def config_path() -> Path:
    """给"打开配置文件所在目录"之类操作用。"""
    from .config import CONFIG_PATH

    return Path(CONFIG_PATH)
