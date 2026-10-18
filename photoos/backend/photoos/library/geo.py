"""GPS → 中国地级市 的**离线**反查缓存。

为什么自己实现而不联网查：
  * PRD 要求系统**可离线**运行，联网反查不满足这条。
  * 旧工具 `resolve-cities.ps1` 用 bigdatacloud + nominatim **在线**反查，
    把结果缓存成 `geo-cache.csv`（既有库 62 条 / 17 个城市）。那份缓存是可复用资产，
    本模块**完全兼容它的文件格式与网格取整规则**，因此既有库不需要重新联网反查。

缓存格式（UTF-8 BOM，与旧工具一致）::

    Cell,Lat,Lon,City,Province,Locality,Src
    "18.650,110.200","18.627962","110.211597","萬寧市","海南省","萬寧市","bigdatacloud"

`Cell` 的取整规则：``f"{round(lat / 0.05) * 0.05:.3f},{round(lon / 0.05) * 0.05:.3f}"``。
实测旧缓存 62 条与该公式**逐条一致**，所以两侧的网格不会漂移。

注意缓存里存的是**原始地名**（可能繁体、可能缺「市」后缀）。转成目录名必须再过
`classify.normalize_city_folder_name`，否则会与既有目录冲突（`萬寧市` vs `万宁市`）。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, replace
from pathlib import Path

#: 网格边长（度）。与旧工具 `-CellDeg` 默认值一致。
CELL_DEG = 0.05

#: 最近城市判定的最大球面距离（度）。与旧工具 `-NearestCityMaxDeg` 一致。
NEAREST_CITY_MAX_DEG = 0.6

CACHE_FILENAME = "geo-cache.csv"

#: `GeoCache.source` 上人工指认条目的标记（便于区分"人定的"与"机器查的"）
MANUAL_SRC = "manual"


def cell_key(lat: float, lon: float, *, cell_deg: float = CELL_DEG) -> str:
    """把经纬度落到网格单元键。**必须与旧工具逐位一致**，否则缓存命中率会掉。"""
    return f"{round(lat / cell_deg) * cell_deg:.3f},{round(lon / cell_deg) * cell_deg:.3f}"


def angular_distance_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """经度按纬度的余弦压缩后的平面距离（度）。与旧工具 `Get-CityForPoint` 同式。"""
    dlat = lat1 - lat2
    dlon = (lon1 - lon2) * math.cos(lat1 * math.pi / 180.0)
    return math.sqrt(dlat * dlat + dlon * dlon)


@dataclass(frozen=True)
class GeoEntry:
    """一个网格单元的判定结果。`city` 为**原始地名**，未规范化。"""

    cell: str
    lat: float
    lon: float
    city: str
    province: str | None = None
    locality: str | None = None
    src: str | None = None


@dataclass(frozen=True)
class GeoHit:
    """一次查得的城市。`how` 说明判定依据，会一路带到归类报告里。"""

    city: str
    how: str          # gps-cell | gps-near0.12 | manual
    entry: GeoEntry


class GeoCache:
    """网格 → 城市的本地缓存。**纯离线**，不发起任何网络请求。"""

    def __init__(self, entries: dict[str, GeoEntry] | None = None) -> None:
        self._entries: dict[str, GeoEntry] = dict(entries or {})

    # ------------------------------------------------------------------ 查询
    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> list[GeoEntry]:
        return sorted(self._entries.values(), key=lambda e: e.cell)

    def cities(self) -> dict[str, int]:
        """按原始城市名统计条目数（给 `geo` 命令做概览）。"""
        counts: dict[str, int] = {}
        for entry in self._entries.values():
            if entry.city:
                counts[entry.city] = counts.get(entry.city, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def exact(self, lat: float, lon: float) -> GeoHit | None:
        """网格直查。命中即返回，不再做距离计算。"""
        entry = self._entries.get(cell_key(lat, lon))
        if entry is None or not entry.city:
            return None
        how = "manual" if entry.src == MANUAL_SRC else "gps-cell"
        return GeoHit(city=entry.city, how=how, entry=entry)

    def nearest(
        self, lat: float, lon: float, *, max_deg: float = NEAREST_CITY_MAX_DEG
    ) -> GeoHit | None:
        """缓存内最近城市。超出 `max_deg` 视为无法判定（**宁可未知也不猜**）。"""
        best: GeoEntry | None = None
        best_distance = float("inf")
        for entry in self._entries.values():
            if not entry.city:
                continue
            distance = angular_distance_deg(lat, lon, entry.lat, entry.lon)
            if distance < best_distance:
                best_distance, best = distance, entry
        if best is None or best_distance > max_deg:
            return None
        return GeoHit(city=best.city, how=f"gps-near{best_distance:.2f}", entry=best)

    def lookup(
        self, lat: float | None, lon: float | None, *, max_deg: float = NEAREST_CITY_MAX_DEG
    ) -> GeoHit | None:
        """两级判定：先网格直查，再就近匹配。无 GPS 或均未命中时返回 None。"""
        if lat is None or lon is None:
            return None
        if lat == 0.0 and lon == 0.0:
            # (0,0) 是"没有 GPS"的常见占位写法，不是几内亚湾
            return None
        return self.exact(lat, lon) or self.nearest(lat, lon, max_deg=max_deg)

    # ------------------------------------------------------------------ 写入
    def assign(self, lat: float, lon: float, city: str, *, src: str = MANUAL_SRC) -> GeoEntry:
        """人工指认：把该网格单元钉到指定城市（用于 `photoos geo assign`）。"""
        if not city or not city.strip():
            raise ValueError("城市名不能为空")
        key = cell_key(lat, lon)
        entry = GeoEntry(
            cell=key, lat=float(lat), lon=float(lon), city=city.strip(),
            province=None, locality=None, src=src,
        )
        self._entries[key] = entry
        return entry

    def assign_radius(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        city: str,
        *,
        src: str = MANUAL_SRC,
        overwrite_machine: bool = False,
    ) -> tuple[int, int]:
        """把一个半径内的**所有网格单元**都指认给该城市，返回 `(已写入, 跳过)`。

        为什么需要它：网格边长 0.05°（约 5.5 km），一趟旅行会覆盖几十上百个格子，
        逐个指认不现实。而"以某点为中心、半径 N 公里内都算这座城市"才是人的思维方式。

        **默认不覆盖机器反查得到的条目**（`overwrite_machine=False`）：
        那些是联网反查的结论，不该被一次粗粒度的区域指认悄悄抹掉。
        要纠正单个格子，用 `assign()` 明确指认它。空条目与既有人工条目会被写入。
        """
        if not city or not city.strip():
            raise ValueError("城市名不能为空")
        if radius_km <= 0:
            raise ValueError("半径必须大于 0")
        radius_deg = radius_km / 111.0
        span = int(radius_deg / CELL_DEG) + 1
        written = skipped = 0
        for i in range(-span, span + 1):
            for j in range(-span, span + 1):
                cell_lat = round((lat + i * CELL_DEG) / CELL_DEG) * CELL_DEG
                cell_lon = round((lon + j * CELL_DEG) / CELL_DEG) * CELL_DEG
                if angular_distance_deg(lat, lon, cell_lat, cell_lon) > radius_deg:
                    continue
                key = cell_key(cell_lat, cell_lon)
                existing = self._entries.get(key)
                if (
                    existing is not None
                    and existing.city
                    and existing.src not in (None, "", MANUAL_SRC)
                    and not overwrite_machine
                ):
                    skipped += 1
                    continue
                self.assign(cell_lat, cell_lon, city, src=src)
                written += 1
        return written, skipped

    def with_entry(self, entry: GeoEntry) -> GeoCache:
        """返回含该条目的新缓存（不可变风格，便于测试）。"""
        merged = dict(self._entries)
        merged[entry.cell] = entry
        return GeoCache(merged)

    # ------------------------------------------------------------------ 载入/保存
    @classmethod
    def load(cls, path: Path | str | None) -> GeoCache:
        """读取缓存文件。文件不存在时返回空缓存（**不是错误**：新库本来就没有）。"""
        if path is None:
            return cls()
        path = Path(path)
        if not path.is_file():
            return cls()

        entries: dict[str, GeoEntry] = {}
        # `utf-8-sig` 兼容旧工具导出的 BOM
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                cell = (row.get("Cell") or "").strip()
                city = (row.get("City") or "").strip()
                if not cell:
                    continue
                try:
                    lat = float(row.get("Lat") or 0.0)
                    lon = float(row.get("Lon") or 0.0)
                except (TypeError, ValueError):
                    continue
                entries[cell] = GeoEntry(
                    cell=cell,
                    lat=lat,
                    lon=lon,
                    city=city,
                    province=(row.get("Province") or "").strip() or None,
                    locality=(row.get("Locality") or "").strip() or None,
                    src=(row.get("Src") or "").strip() or None,
                )
        return cls(entries)

    def save(self, path: Path | str) -> Path:
        """原子写回（先写临时文件再改名），避免半个文件覆盖掉好的缓存。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        with temp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Cell", "Lat", "Lon", "City", "Province", "Locality", "Src"])
            for entry in self.entries:
                writer.writerow([
                    entry.cell,
                    f"{entry.lat:.6f}",
                    f"{entry.lon:.6f}",
                    entry.city,
                    entry.province or "",
                    entry.locality or "",
                    entry.src or "",
                ])
        temp.replace(path)
        return path

    @classmethod
    def default_path(cls, data_root: Path | str) -> Path:
        """缓存默认放在**数据根**下：随数据根迁移，不写进数据库（PRD §5.1）。"""
        return Path(data_root) / CACHE_FILENAME

    def merged_with(self, other: GeoCache) -> GeoCache:
        """用 `other` 补齐本缓存缺失的网格（本缓存优先，人工指认不会被覆盖）。"""
        merged = dict(other._entries)
        merged.update(self._entries)
        return GeoCache(merged)


def entry_from_row(row: dict[str, str]) -> GeoEntry | None:
    """把一行 CSV（dict）转成条目；无效行返回 None。"""
    cell = (row.get("Cell") or "").strip()
    if not cell:
        return None
    try:
        lat, lon = float(row.get("Lat") or 0.0), float(row.get("Lon") or 0.0)
    except (TypeError, ValueError):
        return None
    return GeoEntry(
        cell=cell,
        lat=lat,
        lon=lon,
        city=(row.get("City") or "").strip(),
        province=(row.get("Province") or "").strip() or None,
        locality=(row.get("Locality") or "").strip() or None,
        src=(row.get("Src") or "").strip() or None,
    )


def with_updated_city(entry: GeoEntry, city: str) -> GeoEntry:
    return replace(entry, city=city)


__all__ = [
    "CACHE_FILENAME",
    "CELL_DEG",
    "MANUAL_SRC",
    "NEAREST_CITY_MAX_DEG",
    "GeoCache",
    "GeoEntry",
    "GeoHit",
    "angular_distance_deg",
    "cell_key",
    "entry_from_row",
    "with_updated_city",
]
