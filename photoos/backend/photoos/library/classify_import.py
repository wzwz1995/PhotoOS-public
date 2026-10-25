"""基础物理分类导入：把一批外部素材分类后 **append** 进既有照片库。

对应用户要求的三步：
    ① 选择原始素材路径 —— `scan_source` / `build_plan`
    ② 执行基础物理分类 —— `library.classify` 判定城市、切分时段、算出目标相对路径
    ③ 建立增量索引 —— 复制后登记 asset / metadata / session，再只对新增文件补预览

**只复制、不移动**（已锁定决策 #1，不触碰铁律 #1）：
  * 源永远只读 —— 不 move / rename / delete，这条代码路径在本模块里**不存在**；
  * 目标写入沿用既有安全契约：临时文件 → `fsync` → size + BLAKE3 双端校验 → `os.replace` 原子落位；
  * 同名绝不覆盖（同名同内容 → 跳过；同名异内容 → `__N`）。
  * **源的清理不在本模块范围内**：事后打印"已逐文件校验、可安全删除"的清单，由用户手动执行
    （与 `cleanup-staging.ps1 -Execute` 故意不复刻保持一致）。

**照片与视频联合切段**（见 PHASE1_EXT_TECH_PLAN §1.3.6）：
  一次导入若同时含照片与视频，先生成**一个**联合计划，再按 `kind` 分流到各自的 Library 根；
  锚点也同时取自两个库。否则同一段拍摄会在两个盘上得到不同目录名，
  下次导入还会再切出平行时段（实测：只看照片只能复现既有目录名的 68/71）。
"""

from __future__ import annotations

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from ..config import Settings
from ..db import Database
from ..errors import PhotoOSError, SafetyError
from ..hashing import HASH_ALGO, hash_file
from ..media.detect import detect_by_extension_only
from ..media.metadata import MediaInfo, extract_metadata
from ..repos import AssetRepo, MetadataRepo, SessionRepo, asset_absolute_path
from . import classify as classify_mod
from . import importer as importer_mod
from . import paths as paths_mod
from . import walker
from .geo import GeoCache
from .store import LibraryStore

ProgressFn = Callable[[int, int, str], None]


def _noop_progress(done: int, total: int, note: str) -> None:  # pragma: no cover - 默认值
    return None


# --------------------------------------------------------------------------- 源扫描
@dataclass
class SourceEntry:
    """源侧的一个待导入文件及其判定输入。"""

    item: classify_mod.ClassifyItem
    path: Path
    relative: str
    file_type: str
    mime: str | None
    size: int
    info: MediaInfo | None = None


@dataclass
class ClassifyImportPlan:
    """一次分类导入的完整计划（**只读源与库，未写任何文件**）。"""

    entries: list[SourceEntry] = field(default_factory=list)
    plan: classify_mod.ClassifyPlan = field(default_factory=classify_mod.ClassifyPlan)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: key → 目标 Library 的根路径（照片与视频分流到各自的库）
    target_root: dict[str, Path] = field(default_factory=dict)
    #: key → 目标 Library id
    library_of: dict[str, int] = field(default_factory=dict)
    #: 目标位置已有**完全相同内容**因而被跳过的文件：`(源 key, 既有相对路径)`。
    #: 这才是"重复导入同一张存储卡"的正确结果 —— 跳过，而不是复制出 `__2` 副本。
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    #: 源中受支持的媒体文件**总数**（不一定等于 `total`：预演可以只抽样一部分）。
    #: 界面上必须把它显示出来，否则"确认执行"就是一个没有刻度的按钮。
    source_total: int = 0
    #: 源中媒体文件的总字节数（用于给出复制耗时预估）
    source_bytes: int = 0
    #: 本次计划是否只覆盖了源的一部分（即 `total < source_total`）
    preview_sampled: bool = False
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def total(self) -> int:
        return len(self.plan.files)

    def by_library(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for library_id in self.library_of.values():
            counts[library_id] = counts.get(library_id, 0) + 1
        return counts

    def conflicts(self) -> list[classify_mod.ClassifiedFile]:
        """因同名而改名的文件（预演时值得显眼列出）。"""
        return [item for item in self.plan.files if item.collision_with]


def scan_source(
    source_root: Path,
    *,
    limit: int | None = None,
    workers: int = 8,
    progress: ProgressFn = _noop_progress,
) -> tuple[list[SourceEntry], list[tuple[str, str]], int, int]:
    """遍历源，为前 `limit` 个文件读取拍摄时间 / GPS，并**数清源里一共有多少、多大**。

    返回 `(entries, skipped, total_media, total_bytes)`。

    为什么整个目录都要走到：只预演一部分时，用户仍然必须知道"这批到底有多少个文件"，
    否则「确认执行」就是一个没有刻度的按钮。实测踩到过：预演上限 30，
    用户以为点确认执行会处理整张卡（实际只导入 30 个）。

    读 EXIF 是整个导入里最慢的一步（RAW 必须遍历整个 TIFF，存储卡上约 0.6 s/文件），
    而遍历目录本身很便宜 —— 所以**超出 limit 之后只计数、不再读 EXIF**。
    """
    source_root = Path(source_root)
    selected: list[walker.FileEntry] = []
    skipped: list[tuple[str, str]] = []
    total_media = 0
    total_bytes = 0

    for walked in walker.walk_media(source_root, include_videos=True):
        if isinstance(walked, walker.WalkError):
            skipped.append((walked.relative or str(walked.path), walked.message))
            continue
        if detect_by_extension_only(walked.path) is None:
            skipped.append((walked.relative, "不是受支持的照片/视频类型"))
            continue
        total_media += 1
        total_bytes += int(walked.size or 0)
        if limit is None or len(selected) < limit:
            selected.append(walked)

    entries = _read_metadata_parallel(selected, skipped, workers=workers, progress=progress)
    return entries, skipped, total_media, total_bytes


def _read_metadata_parallel(
    selected: list[walker.FileEntry],
    skipped: list[tuple[str, str]],
    *,
    workers: int,
    progress: ProgressFn,
) -> list[SourceEntry]:
    """并行读取 EXIF（`extract_metadata` 会释放 GIL，因此线程确实能并行）。

    实测（存储卡，每轮 60~90 个文件）：1 worker 5.0 文件/秒、2 → 9.5、4 → 15.8、
    8 → 25.5、12 → 30.8 —— 扩展性很好，因为这一步只读每个文件的头部
    （NEF 需遍历 TIFF，但仍是小读）。默认由 `Settings.effective_import_scan_workers` 给出（8）。
    """
    if not selected:
        return []
    total = len(selected)
    infos: list[MediaInfo | None] = [None] * total
    failures: dict[int, str] = {}

    def work(pair: tuple[int, walker.FileEntry]) -> tuple[int, MediaInfo | None, str | None]:
        index, walked = pair
        try:
            return index, extract_metadata(walked.path), None
        except Exception as exc:  # noqa: BLE001 - 读不到 EXIF 不该拦住导入
            return index, None, f"{type(exc).__name__}: {exc}"

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for index, info, error in pool.map(work, list(enumerate(selected))):
            infos[index] = info
            if error is not None:
                failures[index] = error
            done += 1
            if done % 25 == 0 or done == total:
                progress(done, total, f"已读取 {done}/{total} 个文件的拍摄信息")

    entries: list[SourceEntry] = []
    for index, walked in enumerate(selected):
        detected = detect_by_extension_only(walked.path)
        if detected is None:  # pragma: no cover - 进入 selected 前已判过
            continue
        info = infos[index]
        if index in failures:
            skipped.append(
                (walked.relative, f"EXIF 读取失败（{failures[index]}），将按未知时间/未知地点处理")
            )
        mtime = datetime.fromtimestamp(walked.mtime_ns / 1e9) if walked.mtime_ns else None
        is_video = detected.file_type == "video"
        item = classify_mod.ClassifyItem(
            key=walked.relative,
            name=walked.path.name,
            kind="video" if is_video else "photo",
            size=walked.size,
            # 视频：容器只给 UTC，没有本地时间 —— 交给 classify 从邻近照片借偏移
            local_time=None if is_video else classify_mod.parse_wall_clock(
                info.capture_at if info else None
            ),
            utc_time=classify_mod.parse_wall_clock(info.capture_at_utc if info else None),
            offset_minutes=None if is_video else classify_mod.offset_minutes_from_text(
                info.tz_offset if info else None
            ),
            time_how=_time_how_for(info, is_video),
            lat=info.gps_lat if info else None,
            lon=info.gps_lon if info else None,
            file_mtime=mtime,
        )
        entries.append(
            SourceEntry(
                item=item,
                path=walked.path,
                relative=walked.relative,
                file_type=detected.file_type,
                mime=detected.mime,
                size=walked.size,
                info=info,
            )
        )
    return entries


def _time_how_for(info: MediaInfo | None, is_video: bool) -> str:
    if is_video:
        return classify_mod.TIME_CONTAINER_UTC
    if info is None or not info.capture_at:
        return classify_mod.TIME_NONE
    return classify_mod.TIME_EXIF if info.tz_offset else classify_mod.TIME_EXIF_WALLCLOCK


# --------------------------------------------------------------------------- 锚点
def library_anchors(db: Database, library_ids: Iterable[int]) -> list[classify_mod.SessionAnchor]:
    """从**指定的若干个库**派生时段锚点（照片库 + 视频库必须一起给，见模块说明）。"""
    rows: list[tuple[str, str | None]] = []
    #: 照片的 `(本地时间, 偏移分钟)` —— 供视频**借时区**用（证据，不是猜测）
    photo_times: list[tuple[datetime, int]] = []
    for library_id in library_ids:
        for row in db.query(
            "SELECT a.relative_path AS relative_path, m.capture_at AS capture_at,"
            " m.tz_offset AS tz_offset"
            " FROM asset AS a LEFT JOIN metadata AS m ON m.asset_id = a.id"
            " WHERE a.library_id = ? AND a.status NOT IN ('MISSING','QUARANTINED','SKIPPED')",
            (int(library_id),),
        ):
            local_text = row["capture_at"]
            rows.append((str(row["relative_path"]), local_text))
            local = classify_mod.parse_wall_clock(local_text) if local_text else None
            offset = classify_mod.offset_minutes_from_text(row["tz_offset"])
            if local is not None and offset is not None:
                photo_times.append((local, offset))
        # 视频没有本地时间（capture_at 为空），用「UTC + 借来的偏移」补一个近似本地时间，
        # 否则视频锚点会全部丢失，新视频块就会为同一时段另建目录。
        for row in db.query(
            "SELECT a.relative_path AS relative_path, m.capture_at_utc AS utc"
            " FROM asset AS a LEFT JOIN metadata AS m ON m.asset_id = a.id"
            " WHERE a.library_id = ? AND a.file_type = 'video'"
            "   AND a.status NOT IN ('MISSING','QUARANTINED','SKIPPED')"
            "   AND m.capture_at_utc IS NOT NULL",
            (int(library_id),),
        ):
            rows.append((str(row["relative_path"]), _video_local_from_utc(str(row["utc"]), photo_times)))
    return classify_mod.anchors_from_rows(rows)


def _video_local_from_utc(utc_text: str, photo_times: list[tuple[datetime, int]]) -> str | None:
    """给视频锚点估一个本地时间：向**时间最邻近的照片**借时区偏移。

    与 `classify.infer_video_local_times` 同一策略 —— "那是**证据**，不是猜测"：

    1. 先用库内**最常见**的偏移做一次粗估，得到视频的近似本地时间；
    2. 再取"本地时间与这个粗估最接近"的那张照片的偏移来换算。

    为什么要两步：照片行里只有本地时间（没有 UTC），所以先用常见偏移把 UTC 拉到
    本地时间的量级上，才能和照片比"谁更近"。

    ⚠️ 找不到任何**带偏移**的照片时返回 `None`（**不猜**）。旧实现直接把 UTC 当本地
    时间，境外拍摄会整体错几小时，而且不报错 —— 见 20-reference Q-19。
    这个值只用于**锚点时间范围**的估计，不写回任何 `metadata`。
    """
    utc = classify_mod.parse_wall_clock(utc_text)
    if utc is None or not photo_times:
        return None
    dominant = Counter(offset for _local, offset in photo_times).most_common(1)[0][0]
    guess = utc + timedelta(minutes=dominant)
    nearest = min(photo_times, key=lambda item: abs((item[0] - guess).total_seconds()))
    return (utc + timedelta(minutes=nearest[1])).strftime("%Y-%m-%d %H:%M:%S")


def reserved_targets(db: Database, library_id: int) -> dict[str, tuple[int | None, str | None]]:
    """目标库中已存在的相对路径 → `(size, content_hash)`。

    预演阶段据此同时避开"重名覆盖"与"重复导入"：体积与内容哈希都拿来判定，
    因此不需要读取库里的原片。
    """
    return {
        str(row["relative_path"]).lower(): (
            int(row["size"]) if row["size"] is not None else None,
            row["content_hash"],
        )
        for row in db.query(
            "SELECT relative_path, size, content_hash FROM asset WHERE library_id = ?",
            (int(library_id),),
        )
    }


def drop_content_duplicates(
    plan: classify_mod.ClassifyPlan,
    by_key: dict[str, SourceEntry],
    reserved_index: dict[str, tuple[int | None, str | None]],
    *,
    target_root: dict[str, Path],
    progress: ProgressFn = _noop_progress,
) -> tuple[list[classify_mod.ClassifiedFile], list[tuple[str, str]]]:
    """把"库内**已经有完全相同内容**"的文件从计划里剔除，两条判据都查。

    1. **同名冲突**：目标位置那一条记录的 `(size, content_hash)` 与源相同 → 跳过。
    2. **库内其它路径已有同内容** → 也跳过（与 `importer.plan_import` 的
       `known_hashes` 口径对齐，见 20-reference Q-38）。

    为什么必须做：分类路径若只按"同名就改名"，重复导入同一张存储卡会把整个库
    再复制一遍（每个文件多出一个 `__2` 副本）。而实际语义应该是**跳过** ——
    这正是既有 `plan_import`（非分类路径）早就有的行为。

    成本设计（关键）：判据 2 先用**体积**筛一遍 —— 库内没有任何资产与源文件同体积，
    就**不必读源文件**（正常导入新照片时绝大多数文件会被这一步挡掉，零开销）。
    只有体积撞上时才真正算一次 BLAKE3。重复导入同一张卡时退化为"每个文件算一次哈希"，
    仍比真的再复制一遍 20 GB 便宜得多。
    """
    #: 体积 → {content_hash: 库内某个相对路径}。只用于判据 2，且**只**在体积撞上时查。
    by_size: dict[int, dict[str, str]] = {}
    for rel, (size, digest) in reserved_index.items():
        if size is None or not digest:
            continue
        by_size.setdefault(int(size), {})[str(digest)] = rel

    kept: list[classify_mod.ClassifiedFile] = []
    duplicates: list[tuple[str, str]] = []
    checked = 0
    for item in plan.files:
        entry = by_key.get(item.key)
        if entry is None:
            kept.append(item)
            continue

        existing_rel = item.collision_with
        reserved = reserved_index.get(existing_rel.lower()) if existing_rel else None

        # ③ 库里没有这条记录、但**目标位置真的有文件**（典型：库记录被清空过，
        #    照片文件还在）。此时不去比内容，复制阶段就会"拒绝覆盖"，
        #    整批看起来全失败（实测 50 张全记失败）。这里补一次磁盘比对。
        if reserved is None and existing_rel:
            root = target_root.get(item.key)
            if root is not None:
                disk_target = Path(root) / Path(str(existing_rel).replace("\\", "/"))
                try:
                    if disk_target.is_file() and disk_target.stat().st_size == entry.size \
                            and hash_file(disk_target) == hash_file(entry.path):
                        duplicates.append((item.key, existing_rel))
                        continue
                except OSError:
                    pass

        if reserved is not None:
            # ① 同名冲突：只跟**目标位置那一条**比，比完就结束（命中即跳过，否则改名落位）
            reserved_size, reserved_hash = reserved
            if (reserved_size is not None and entry.size != reserved_size) or not reserved_hash:
                kept.append(item)
                continue
            checked += 1
            progress(checked, 0, f"校验是否与库内重复：{entry.relative}")
            try:
                if hash_file(entry.path) == reserved_hash:
                    duplicates.append((item.key, existing_rel))
                    continue
            except OSError:
                pass  # 读不了就当不重复，交给后续复制阶段报错
            kept.append(item)
            continue

        # ② 没有同名冲突，但库内**别的路径**可能已经有同一张：体积先筛，命中才算哈希
        candidates = by_size.get(int(entry.size))
        if candidates:
            checked += 1
            progress(checked, 0, f"校验是否与库内重复：{entry.relative}")
            try:
                digest = hash_file(entry.path)
            except OSError:
                kept.append(item)
                continue
            hit = candidates.get(digest)
            if hit is not None:
                duplicates.append((item.key, hit))
                continue
        kept.append(item)
    return kept, duplicates


# --------------------------------------------------------------------------- 计划
def build_plan(
    db: Database,
    source_root: Path,
    *,
    photo_library_id: int,
    video_library_id: int | None = None,
    geo: GeoCache | None = None,
    gap_hours: float = classify_mod.DEFAULT_GAP_HOURS,
    limit: int | None = None,
    city_override: str | None = None,
    scan_workers: int = 8,
    progress: ProgressFn = _noop_progress,
) -> ClassifyImportPlan:
    """生成分类导入计划。**只读源与库，不写任何文件。**

    `city_override` 是整批强制城市：相机常常整段没有 GPS（未连手机定位），
    境外拍摄即使有 GPS 也可能不在离线缓存里 —— 这两种情况都只能由人来指定。
    """
    started = time.perf_counter()
    store = LibraryStore(db)
    photo_library = store.get(photo_library_id)
    if not photo_library.root_path_hint:
        raise PhotoOSError(f"Library #{photo_library.id} 没有可用的路径提示，无法确定导入目标")
    photo_root = Path(photo_library.root_path_hint)

    video_root: Path | None = None
    if video_library_id is not None:
        video_library = store.get(video_library_id)
        if not video_library.root_path_hint:
            raise PhotoOSError(f"Library #{video_library.id} 没有可用的路径提示")
        video_root = Path(video_library.root_path_hint)

    source_root = Path(source_root)
    if not source_root.is_dir():
        # 不说"不存在"就完事：MTP 便携设备路径 / 相对路径需要**可操作的**指引
        raise PhotoOSError(paths_mod.explain_unusable_source(str(source_root), source_root))
    # 源不能位于**任一**目标库内部，否则会自我复制
    for root in (photo_root, video_root):
        if root is not None and paths_mod.is_same_root(root, source_root):
            raise SafetyError(
                f"源目录位于目标库内部，拒绝导入（会自我复制）：{source_root}",
                detail={"kind": "source_inside_library"},
            )

    scan_started = time.perf_counter()
    entries, skipped, source_total, source_bytes = scan_source(
        source_root, limit=limit, workers=scan_workers, progress=progress
    )
    scan_seconds = time.perf_counter() - scan_started

    # 同时取两个库的锚点：时段名是跨库共享的
    library_ids = [photo_library_id] + ([video_library_id] if video_library_id is not None else [])
    anchors = library_anchors(db, library_ids)
    reserved: dict[str, tuple[int | None, str | None]] = dict(
        reserved_targets(db, photo_library_id)
    )
    if video_library_id is not None:
        reserved.update(reserved_targets(db, video_library_id))

    plan = classify_mod.plan_classification(
        [entry.item for entry in entries],
        geo=geo,
        anchors=anchors,
        gap_hours=gap_hours,
        reserved_relative=set(reserved),
        city_override=city_override,
    )

    # 同名冲突还要做**内容级**判定：同内容 → 跳过（重复导入同一张卡的正确行为），
    # 异内容 → 保留 __N 改名。不做这一步，重复导入会把整个库再复制一遍。
    #
    # ⚠️ 这里传的是 classify_mod.ClassifyPlan（还没有 target_root 字段）；
    # 磁盘比对需要的目标根必须单独算，不能让函数去 plan.target_root 上取 ——
    # 那条路径只在 ClassifyImportPlan 上存在，真实触发过 AttributeError。
    planned_roots: dict[str, Path] = {}
    for classified in plan.files:
        if classified.kind == "video":
            if video_root is not None:
                planned_roots[classified.key] = video_root
        else:
            planned_roots[classified.key] = photo_root

    by_key = {entry.item.key: entry for entry in entries}
    kept, duplicates = drop_content_duplicates(
        plan, by_key, reserved, target_root=planned_roots, progress=progress
    )
    if duplicates:
        plan = classify_mod.ClassifyPlan(
            files=kept,
            sessions=[s for s in plan.sessions
                      if any(f.session == s.folder and f.city == s.city for f in kept)],
            unresolved_cells=plan.unresolved_cells,
            unknown_city_count=sum(1 for f in kept if f.city == classify_mod.UNKNOWN_CITY),
            unknown_time_count=sum(1 for f in kept if f.time_unknown),
            notes=plan.notes,
        )

    notes = list(plan.notes)
    if duplicates:
        notes.append(
            f"{len(duplicates)} 个文件在目标位置已有完全相同的内容，已跳过（不重复复制）"
        )
    target_root: dict[str, Path] = {}
    library_of: dict[str, int] = {}
    missing_video_library = 0
    for classified in plan.files:
        if classified.kind == "video":
            if video_root is None:
                missing_video_library += 1
                continue
            target_root[classified.key] = video_root
            library_of[classified.key] = int(video_library_id)  # type: ignore[arg-type]
        else:
            target_root[classified.key] = photo_root
            library_of[classified.key] = int(photo_library_id)

    # 计划阶段因同内容被剔除的源（`duplicates`）不在 `plan.files` 里；它们仍然
    # 需要一个可核对的 target_root/library_of，否则收件箱台账无法记录这些"已安全
    # 在库里"的源文件，扫描会永远把它们显示成可导入。
    for key, _existing_relative in duplicates:
        entry = by_key.get(key)
        if entry is None:
            continue
        if entry.item.kind == "video":
            if video_root is None:
                continue
            target_root[key] = video_root
            library_of[key] = int(video_library_id)  # type: ignore[arg-type]
        else:
            target_root[key] = photo_root
            library_of[key] = int(photo_library_id)

    if missing_video_library:
        raise PhotoOSError(
            f"源中含 {missing_video_library} 个视频，但没有指定视频库。"
            "请用 `--video-library <id|名称>` 指定（照片与视频要 append 到各自的 Library，"
            "且两者必须一起切段，否则同一段拍摄会在两个盘上得到不同目录名）。"
        )
    if not anchors:
        notes.append("目标库中还没有任何时段锚点，本次会新建全部时段目录")
    else:
        notes.append(f"已载入 {len(anchors)} 个既有时段锚点（照片库 + 视频库），用于避免新建平行目录")

    sampled = source_total > len(entries)
    if sampled:
        # 给一个**诚实**的预估：读取与复制是两个独立的大头，各自说清楚。
        # 复制速率取实测值（该外置盘 ~74 MB/s，见 M7 试点），并注明取决于盘速。
        read_multiple = source_total / max(1, len(entries))
        copy_minutes = source_bytes / 1e6 / 74 / 60
        notes.append(
            f"源中共有 {source_total} 个媒体文件（约 {source_bytes / 1e9:.1f} GB），"
            f"本次预演只抽样了前 {len(entries)} 个（其余按同一套规则处理）"
        )
        notes.append(
            f"点「确认执行」会处理全部 {source_total} 个文件：读取拍摄信息约需 "
            f"{scan_seconds * read_multiple / 60:.0f} 分钟，复制约需 "
            f"{copy_minutes:.0f} 分钟（按 74 MB/s，实际取决于盘速）。"
            "任务可随时暂停或取消。"
        )

    return ClassifyImportPlan(
        entries=entries,
        plan=plan,
        skipped=skipped,
        target_root=target_root,
        library_of=library_of,
        duplicates=duplicates,
        source_total=source_total,
        source_bytes=source_bytes,
        preview_sampled=sampled,
        notes=notes,
        seconds=time.perf_counter() - started,
    )


# --------------------------------------------------------------------------- 执行
@dataclass
class ClassifyImportReport:
    copied: int = 0
    failed: int = 0
    skipped_duplicate: int = 0
    bytes_copied: int = 0
    sessions_created: int = 0
    sessions_updated: int = 0
    assets_registered: int = 0
    metadata_written: int = 0
    errors: list[str] = field(default_factory=list)
    sources_copied: list[str] = field(default_factory=list)
    #: 目标位置已有**相同内容**、因而不必再复制的源文件。
    #: 与 `sources_copied` 一起构成"这批源文件已经安全在库里了"的完整集合 ——
    #: 收件箱台账据此记录，避免下一次扫描把它们再排一次队。
    sources_skipped: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"复制成功 {self.copied}，跳过（内容相同） {self.skipped_duplicate}，失败 {self.failed}",
            f"已复制 {self.bytes_copied / 1e9:.2f} GB；登记 Asset {self.assets_registered}，"
            f"写入元数据 {self.metadata_written}",
            f"时段（session）：新建 {self.sessions_created}，更新 {self.sessions_updated}",
        ]
        for message in self.errors[:20]:
            out.append(f"  错误：{message}")
        if len(self.errors) > 20:
            out.append(f"  ……另有 {len(self.errors) - 20} 条错误未显示")
        return out


def execute_plan(
    db: Database,
    plan: ClassifyImportPlan,
    *,
    geo: GeoCache | None = None,
    geo_cache_path: Path | None = None,
    progress: ProgressFn = _noop_progress,
) -> ClassifyImportReport:
    """按计划复制并登记。**源文件只读，绝不移动或删除。**"""
    report = ClassifyImportReport()
    report.skipped_duplicate = len(plan.duplicates)
    by_key = {entry.item.key: entry for entry in plan.entries}
    for key, _existing_relative in plan.duplicates:
        entry = by_key.get(key)
        if entry is not None:
            report.sources_skipped.append(str(entry.path))
    #: library_id → [(relative, digest, SourceEntry)]
    per_library: dict[int, list[tuple[str, str, SourceEntry]]] = {}

    files = plan.plan.files
    for index, classified in enumerate(files, start=1):
        entry = by_key[classified.key]
        library_id = plan.library_of[classified.key]
        root = plan.target_root[classified.key]
        target = root / Path(classified.target_relative.replace("\\", "/"))

        if not paths_mod.is_same_root(root, target):
            report.failed += 1
            report.errors.append(f"{classified.key}: 目标越出 Library 根，已拒绝")
            continue
        if target.exists():
            # 目标已存在：**先比内容**。库记录被清空过、或用户重复导入同一批时，
            # 目标文件就在那儿 —— 一律"拒绝覆盖"会让整批显示失败（实测 50/50）。
            # 内容一致 → 计入"跳过（内容相同）"；不一致 → 仍然拒绝覆盖。
            src_digest = ""
            try:
                src_digest = importer_mod.hash_file(entry.path)
                same = (target.stat().st_size == entry.size
                        and importer_mod.hash_file(target) == src_digest)
            except Exception:  # noqa: BLE001
                same = False
            if same:
                report.skipped_duplicate += 1
                report.sources_skipped.append(str(entry.path))
                # ★ 跳过**也要登记**：文件已经在库位置，但数据库里可能没有这条记录
                #   （库记录被清空、或换了新数据根）。不登记的话导入报告说"跳过 50"，
                #   照片库却是空的 —— 实测就是这样。
                per_library.setdefault(library_id, []).append(
                    (classified.target_relative, src_digest, entry))
                continue
            report.failed += 1
            report.errors.append(
                f"{classified.key}: 目标已存在（{classified.target_relative}）且内容不同，拒绝覆盖")
            continue
        try:
            digest = importer_mod._copy_verified(entry.path, target)  # noqa: SLF001 - 同包复用同一安全契约
        except (OSError, PhotoOSError) as exc:
            report.failed += 1
            report.errors.append(f"{classified.key}: {exc}")
            importer_mod._cleanup_temp(target)  # noqa: SLF001
            continue

        report.copied += 1
        report.bytes_copied += entry.size
        report.sources_copied.append(str(entry.path))
        per_library.setdefault(library_id, []).append((classified.target_relative, digest, entry))
        if progress is not None and (index % 10 == 0 or index == len(files)):
            progress(index, len(files), f"已复制 {index}/{len(files)}")

    assets = AssetRepo(db)
    for library_id, items in per_library.items():
        rows = []
        for relative, _digest, entry in items:
            root = plan.target_root[entry.item.key]
            stat = (root / Path(relative.replace("\\", "/"))).stat()
            rows.append((relative, stat.st_size, stat.st_mtime_ns, entry.file_type, entry.mime))
        outcome = assets.upsert_discovered(library_id, rows)
        report.assets_registered += outcome.added

        with db.write() as conn:
            for relative, digest, _entry in items:
                conn.execute(
                    "UPDATE asset SET content_hash = ?, hash_algo = ?"
                    " WHERE library_id = ? AND relative_path = ?",
                    (digest, HASH_ALGO, library_id, relative),
                )

        # 复用**规划阶段**读到的 EXIF：省掉索引阶段再读一遍原片（RAW 尤其明显）
        id_by_relative = {
            str(row["relative_path"]): int(row["id"])
            for row in db.query(
                "SELECT id, relative_path FROM asset WHERE library_id = ?", (library_id,)
            )
        }
        metas = MetadataRepo(db)
        for relative, _digest, entry in items:
            if entry.info is None:
                continue
            asset_id = id_by_relative.get(relative)
            if asset_id is None:
                continue
            metas.upsert(entry.info.to_row(asset_id))
            report.metadata_written += 1

    # ---- session 行（Phase 2 的 Scene 将挂在这些「时段」上）
    used_sessions = {
        (classified.city, classified.session) for classified in plan.plan.files
    }
    rows = []
    for session in plan.plan.sessions:
        if (session.city, session.folder) not in used_sessions:
            continue
        rows.append(
            {
                "name": f"{session.city}\\{session.folder}",
                "city_hint": session.city,
                "period_hint": session.folder,
                "captured_from": session.captured_from.strftime("%Y-%m-%d %H:%M:%S"),
                "captured_to": session.captured_to.strftime("%Y-%m-%d %H:%M:%S"),
                "confidence": session.confidence,
                "source": SessionRepo.SOURCE_AUTO,
            }
        )
    created, updated = SessionRepo(db).upsert_many(rows)
    report.sessions_created = created
    report.sessions_updated = updated

    LibraryStore(db).log_event(
        "classify_import",
        library_id=next(iter(per_library), None),
        payload={
            "source": str(plan.entries[0].path.parent) if plan.entries else None,
            "copied": report.copied,
            "failed": report.failed,
            "sessions_created": created,
            "sessions_updated": updated,
        },
    )

    # 人工指认过的网格要回写缓存，下次同类地点就能自动命中
    if geo is not None and geo_cache_path is not None:
        geo.save(geo_cache_path)
    return report


def safe_to_delete_sources(report: ClassifyImportReport) -> list[str]:
    """返回"已逐文件校验、可安全删除"的源文件清单。

    **PhotoOS 不替用户删除**（铁律 #1 与 PRD §11.1：破坏性操作必须由用户显式执行）。
    这份清单只是把"哪些源已经安全落库"讲清楚，删除动作留给用户或既有脚本。
    """
    return sorted(set(report.sources_copied))


__all__ = [
    "ClassifyImportPlan",
    "ClassifyImportReport",
    "SourceEntry",
    "build_plan",
    "execute_plan",
    "library_anchors",
    "reserved_targets",
    "safe_to_delete_sources",
    "scan_source",
]
