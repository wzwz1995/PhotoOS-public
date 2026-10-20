"""人脸检测 / 识别 / 聚类 / 原型学习（Phase 4 · 分人，迁移 008 的算法侧）。

## 这个模块解决的唯一问题

把 21,400 张照片里的脸找出来，**分成"可能是同一个人"的小簇**，交给用户命名与确认。
它**不回答"这是谁"** —— 名字永远来自用户（`person.name` 由界面写入）。

## 三个已实测的结论决定了实现方式（见 PHASE3_PLAN 附录 F）

| 结论 | 后果 |
| --- | --- |
| **单链聚类必须禁用**：阈值 0.363 时 686/905 张脸并成一簇 | 只用 kNN 图 + Chinese Whispers |
| **EG2 的人脸嵌入不可用**（平均两两余弦 0.8245，全塌成一簇） | 必须用 SFace（128 维，专用人脸识别模型） |
| **侧脸会让聚类按"拍摄角度"分组而不是按人** | 先按"正面清晰"判据筛，侧脸**不参与聚类**，只作候选 |

## 坐标口径（不写清楚就无法复算，所以钉在文档里）

一张 preview 上的人脸有三个坐标系，本模块**同时**保存其中两个，各司其职：

| 坐标系 | 存在哪 | 用途 |
| --- | --- | --- |
| **检测坐标系**（长边缩到 `DETECT_MAX`） | `face.landmarks_json`（YuNet 原始 15 浮点）、`face.face_px` | 复现 `alignCrop` —— SFace 的对齐要求关键点与图像在**同一个**坐标系；`face_px` 是 `min(w,h)` 在**该坐标系**下的值 |
| **preview 原始像素** | `face.box_x` / `box_y` / `box_w` / `box_h` | 界面按任意分辨率裁剪人脸缩略图 |

缩放系数可由 `min(1, DETECT_MAX / max(preview_w, preview_h))` 精确重算，
所以两者可以互相还原。

> ⚠️ **这一节曾经写错过，订正依据是活库数据**（2026-10-17，见 20-reference Q-9/Q-14）：
> 旧写法把 `face_px` 也说成"preview 原始像素"，但代码里
> `face_px = min(values[2], values[3])`（**未乘 `1/scale`**），只有 `box_*` 还原了。
> 实测三个比值同时等于 `0.5625`（= 900/1600 = `DETECT_MAX` / preview 长边）：
> `median(landmarks_json[2] / box_w)` = `median(face_px / min(box_w, box_h))` = `900/1600`。
> **所以：`box_*` 是 preview 像素，`landmarks_json` 与 `face_px` 是检测坐标系。**
> 迁移 `008_phase4_people_review.sql:34-38` 的注释同样写错了，但**已发布的迁移不许改**
> （改文件会让 checksum 对不上、服务拒绝启动），只能在这里记明。

## 为什么嵌入一定要带 `prep_version`

SFace 的对齐裁剪口径（112×112、5 点相似变换）一旦变化，**同一个人的向量就会变**，
旧向量与新向量不能再比余弦。这与 `embedding.prep_version` 是同一套纪律。

## 依赖

`cv2`（opencv-python-headless）。**产品 venv 不装它** —— 与 CLIP/EG2 一样，
模型侧只在命令行环境（`venv`）里跑，`serve` 只读结果。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .model_store import face_model_dir
from .vectorstore import VectorStoreError, l2_normalize

# --------------------------------------------------------------------------- 身份常量
#: 检测器身份。换检测模型必须同时改这两个值（旧行保留，多版本并存）。
DETECTOR = "yunet-2023mar"
#: 识别模型身份。
MODEL_NAME = "sface-2021dec"
#: 对齐裁剪口径版本。改裁剪尺寸/关键点顺序/插值方式都必须改它。
PREP_VERSION = "sf-112-align-p1.0"
MODEL_DIM = 128
MODEL_NORMALIZATION = "l2"

#: 检测时长边上限。900 是实测取舍：再大对 24px 以上的脸没有增益，只是变慢。
DETECT_MAX = 900
#: YuNet 置信度门槛（与实验脚本一致）。
SCORE_THRESHOLD = 0.7
#: 小于这个像素数的脸直接丢掉 —— 24px 以下的脸识别模型给不出有意义的向量。
MIN_FACE_PX = 24

#: 正面清晰判据（**事先写死，不按结果调** —— 见 PHASE3_PLAN 附录 F）
FRONTAL_SYMMETRY_MAX = 0.16      # |鼻-眼中点| / 眼距，侧脸会变大
FRONTAL_EYE_RATIO_MIN = 0.30     # 眼距 / 脸宽，太小的不可靠
#: 太小的人脸像素不足。
#: ⚠️ 单位是**检测坐标系**的像素（长边已被缩到 `DETECT_MAX = 900`），不是 preview 像素。
#: 折算：1600 px 长边的预览上，这个门槛 ≈ **71 px**（40 / 0.5625）。
#: 前两条判据是**比值**，与缩放无关；只有这一条与缩放绑定 —— 见模块头「坐标口径」。
FRONTAL_FACE_PX_MIN = 40

#: SFace 官方的同一人判定余弦阈值（`FaceRecognizerSF` 文档值）。
#: **只在"还没有任何否决样本"时作为保守起点**；一旦有负例，阈值由样本学出来。
SFACE_DEFAULT_THRESHOLD = 0.363

#: kNN 建图的**连边下限**：余弦低于这个值的两个节点不算"邻居"。
#:
#: ## 它防的是什么
#:
#: 两个毫无关系的人脸向量做完 L2 归一化之后，余弦并不恰好是 0。若建图只要求 `> 0`，
#: 一个跟谁都无关的孤立人脸（海报上的脸、雕像、边缘畸变）会拿到若干条极小的边，
#: 被硬拽进某一簇 —— 于是"这个人"名下莫名其妙多出一堆陌生脸。
#:
#: ## 实测（2026-10-14，全库 6,368 张脸 / 2,313 张正面清晰）
#:
#: | 量 | 实测值 |
#: | --- | --- |
#: | 最近邻相似度的第 1 百分位 | **0.336** |
#: | 最近邻相似度低于 0.20 的比例 | **0.0%** |
#: | 随机人脸对的余弦 | 均值 0.105 · 标准差 0.120 |
#:
#: **结论要说实话：在这个库上这条下限目前不生效**（每张脸的最近邻都在 0.33 以上）。
#: 保留它的理由不是"实测有效"，而是它**代价为零**、并且把上面那个退化情形挡在门外 ——
#: 该情形由 `test_unrelated_face_is_not_glued_into_a_cluster` 用合成数据守着。
#: 换句话说：这是一道**保险**，不是一项**调优**，文档里不许把它说成后者。
LINK_MIN_SIMILARITY = 0.20


def detector_version() -> str:
    """`face.detector_version` 的取值：`cv2-<版本>`。

    为什么把 cv2 版本写进身份：YuNet 的前后处理在 OpenCV 版本之间变动过，
    同一条 ONNX 在不同 cv2 上可能给出不同的框。写进去之后，
    "换了 cv2 之后结果变了"就变成可解释的，而不是玄学。
    """
    try:
        import cv2
    except ImportError:  # pragma: no cover - 取决于环境
        return "cv2-unknown"
    return f"cv2-{cv2.__version__}"


def model_version() -> str:
    return f"{detector_version()}-fp32"


# --------------------------------------------------------------------------- 正面判据

def frontal_metrics(det15: Sequence[float]) -> dict[str, float]:
    """从 YuNet 的 15 个浮点算"正面程度"。**纯函数，可单独测试**。

    关键点顺序（YuNet 官方）：`[x, y, w, h, 右眼x, 右眼y, 左眼x, 左眼y,
    鼻尖x, 鼻尖y, 右嘴角x, 右嘴角y, 左嘴角x, 左嘴角y, 分数]`。

    三个判据全部用**比值**，所以与检测缩放无关（换 `DETECT_MAX` 不会让旧行失效）：

      * `symmetry` —— 鼻尖相对两眼连线中点的水平偏移 ÷ 眼距。正脸接近 0，侧脸变大。
      * `eye_ratio` —— 眼距 ÷ 脸宽。闭眼/遮挡/过小的脸这个值偏低。
      * `mouth_ratio` —— 嘴中点到眼中点的距离 ÷ 脸宽。仰头/低头会异常（仅记录，不设阈值）。
    """
    values = [float(v) for v in det15]
    if len(values) < 15:
        raise ValueError(f"YuNet 输出必须至少 15 个浮点，实际 {len(values)}")
    _, _, w, h = values[0], values[1], values[2], values[3]
    re_x, re_y = values[4], values[5]
    le_x, le_y = values[6], values[7]
    n_x, _n_y = values[8], values[9]
    rm_x, rm_y = values[10], values[11]
    lm_x, lm_y = values[12], values[13]
    eye_d = math.hypot(le_x - re_x, le_y - re_y)
    mid_x = (re_x + le_x) / 2.0
    mouth_d = math.hypot((rm_x + lm_x) / 2 - mid_x, (rm_y + lm_y) / 2 - (re_y + le_y) / 2)
    width = max(float(w), 1e-6)
    return {
        "symmetry": abs(n_x - mid_x) / max(eye_d, 1e-6),
        "eye_ratio": eye_d / width,
        "mouth_ratio": mouth_d / width,
        "score": values[14],
        "face_px": min(float(w), float(h)),
    }


def is_frontal(metrics: dict[str, float], *, symmetry_max: float = FRONTAL_SYMMETRY_MAX,
               eye_ratio_min: float = FRONTAL_EYE_RATIO_MIN,
               face_px_min: float = FRONTAL_FACE_PX_MIN) -> bool:
    """是否"可信到能参与聚类"。

    ⚠️ 这条判据的作用**只是缩小参与聚类的集合**，不是一个"这张脸好不好"的评价 ——
    没通过的脸**不会被丢掉**，它们以"候选"的形式留给用户确认（PRD §9.2：
    系统降低选择成本，不替用户做选择）。
    """
    return (
        metrics.get("symmetry") is not None
        and metrics["symmetry"] <= symmetry_max
        and metrics.get("eye_ratio") is not None
        and metrics["eye_ratio"] >= eye_ratio_min
        and metrics.get("face_px", 0.0) >= face_px_min
    )


# --------------------------------------------------------------------------- 模型加载

class FaceNotAvailable(RuntimeError):
    """模型或 cv2 缺失。**不静默降级** —— 调用方必须给出可照做的提示。"""


@dataclass
class DetectedFace:
    """一张检出的人脸（尚未落库）。字段与 `face` 表一一对应。"""

    det15: list[float]           # 检测坐标系的 15 浮点（原样保留，供 alignCrop）
    box_x: float                 # preview 像素
    box_y: float
    box_w: float
    box_h: float
    det_score: float
    face_px: float
    symmetry: float
    eye_ratio: float
    mouth_ratio: float
    frontal: bool

    def as_row(self) -> dict[str, Any]:
        import json as _json
        return {
            "box_x": round(self.box_x, 3),
            "box_y": round(self.box_y, 3),
            "box_w": round(self.box_w, 3),
            "box_h": round(self.box_h, 3),
            "landmarks_json": _json.dumps([round(v, 4) for v in self.det15]),
            "det_score": round(self.det_score, 6),
            "face_px": round(self.face_px, 3),
            "symmetry": round(self.symmetry, 6),
            "eye_ratio": round(self.eye_ratio, 6),
            "is_frontal": self.frontal,
        }


@dataclass
class FaceModels:
    """YuNet + SFace 的封装。**构造即加载**（一次加载，循环里反复用）。"""

    detector: Any
    recognizer: Any
    detector_name: str = DETECTOR
    detector_version: str = field(default_factory=detector_version)
    model_name: str = MODEL_NAME
    model_version: str = field(default_factory=model_version)

    @classmethod
    def create(cls, data_root: Path) -> "FaceModels":
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise FaceNotAvailable(
                "未安装 opencv（cv2）。人脸检测走 YuNet/SFace 的 OpenCV 实现，"
                '安装：pip install "opencv-python-headless>=4.8"'
            ) from exc

        directory = face_model_dir(data_root)
        det_path = directory / "face_detection_yunet_2023mar.onnx"
        rec_path = directory / "face_recognition_sface_2021dec.onnx"
        for path in (det_path, rec_path):
            if not path.exists():
                raise FaceNotAvailable(
                    f"人脸模型缺失：{path}。请先运行 `photoos ai fetch-model --engine face`"
                )
        detector = cv2.FaceDetectorYN.create(
            str(det_path), "", (320, 320), SCORE_THRESHOLD, 0.3, 5000)
        recognizer = cv2.FaceRecognizerSF.create(str(rec_path), "")
        return cls(detector=detector, recognizer=recognizer)

    # ------------------------------------------------------------------ 检测
    def detect(self, image_bgr, *, preview_w: int, preview_h: int,
               detect_max: int = DETECT_MAX) -> list[DetectedFace]:
        """在**已解码的 BGR 图像**上检测，返回坐标已还原到 preview 像素的结果。

        为什么入参是已解码图像而不是路径：一张照片常常在"检测 + 对齐裁剪"两个阶段
        都要用（`alignCrop` 需要同一个检测坐标系的图），重复 `imread` 会白白多读一遍盘。
        """
        import cv2

        height, width = image_bgr.shape[:2]
        scale = min(1.0, detect_max / max(height, width))
        if scale < 1.0:
            scaled = cv2.resize(image_bgr, (int(width * scale), int(height * scale)),
                                interpolation=cv2.INTER_AREA)
        else:
            scaled = image_bgr
        scaled_h, scaled_w = scaled.shape[:2]
        self.detector.setInputSize((scaled_w, scaled_h))
        _retval, detections = self.detector.detect(scaled)
        if detections is None:
            return []

        inverse = 1.0 / scale if scale > 0 else 1.0
        out: list[DetectedFace] = []
        for row in detections:
            values = [float(v) for v in row]
            metrics = frontal_metrics(values)
            if metrics["score"] < SCORE_THRESHOLD or metrics["face_px"] < MIN_FACE_PX:
                continue
            out.append(DetectedFace(
                det15=values,
                box_x=values[0] * inverse,
                box_y=values[1] * inverse,
                box_w=values[2] * inverse,
                box_h=values[3] * inverse,
                det_score=metrics["score"],
                face_px=min(values[2], values[3]),
                symmetry=metrics["symmetry"],
                eye_ratio=metrics["eye_ratio"],
                mouth_ratio=metrics["mouth_ratio"],
                frontal=is_frontal(metrics),
            ))
        return out

    # ------------------------------------------------------------------ 嵌入
    def embed(self, image_bgr, faces: Sequence[DetectedFace], *,
              detect_max: int = DETECT_MAX) -> np.ndarray:
        """对齐裁剪 + 128 维特征（**已 L2 归一化**）。

        ## 一个必须复现的细节：alignCrop 的坐标系必须与检测时一致

        实验阶段踩过：`alignCrop` 只给框、关键点补零 → **不报错、也不对齐**，
        等于拿歪框喂识别模型。更隐蔽的是坐标系：如果把 preview 缩放到检测尺寸后检测，
        却拿**原图**去 `alignCrop`，关键点位置就全错了。

        所以这里**重新做一次与 `detect` 完全相同的缩放**，用同一组 15 浮点对齐。
        这也是 `face.landmarks_json` 存检测坐标系而不是 preview 坐标系的原因。
        """
        import cv2

        if not faces:
            return np.zeros((0, MODEL_DIM), dtype="<f4")
        height, width = image_bgr.shape[:2]
        scale = min(1.0, detect_max / max(height, width))
        if scale < 1.0:
            scaled = cv2.resize(image_bgr, (int(width * scale), int(height * scale)),
                                interpolation=cv2.INTER_AREA)
        else:
            scaled = image_bgr

        vectors: list[np.ndarray] = []
        for face in faces:
            row = np.asarray([face.det15], dtype=np.float32)
            try:
                aligned = self.recognizer.alignCrop(scaled, row)
            except Exception:  # noqa: BLE001 - OpenCV 对退化输入会抛各种异常
                aligned = None
            if aligned is None or getattr(aligned, "size", 0) == 0:
                vectors.append(np.zeros(MODEL_DIM, dtype=np.float32))
                continue
            vectors.append(np.asarray(self.recognizer.feature(aligned), dtype=np.float32).ravel())
        matrix = np.vstack(vectors).astype(np.float32)
        return l2_normalize(matrix).astype("<f4")

    def embed_rows(self, image_bgr, rows: Sequence[dict[str, Any]], *,
                   detect_max: int = DETECT_MAX) -> np.ndarray:
        """`embed` 的落库版入口：直接从 `face` 表行还原 15 浮点再对齐。

        这样"重新算一遍特征"（比如换了 SFace 版本）只需要库里的数据，不需要重新检测。
        """
        import json as _json

        faces = []
        for row in rows:
            raw = row.get("landmarks_json")
            if not raw:
                faces.append(None)
                continue
            faces.append(DetectedFace(
                det15=_json.loads(raw), box_x=row["box_x"], box_y=row["box_y"],
                box_w=row["box_w"], box_h=row["box_h"], det_score=row["det_score"],
                face_px=row["face_px"], symmetry=row.get("symmetry") or 0.0,
                eye_ratio=row.get("eye_ratio") or 0.0, mouth_ratio=0.0, frontal=False,
            ))
        usable = [f for f in faces if f is not None]
        matrix = self.embed(image_bgr, usable, detect_max=detect_max)
        # 把"无法对齐"的行补成零向量，保持与 rows 一一对应（调用方按位置落库）
        if len(usable) != len(faces):
            full = np.zeros((len(faces), MODEL_DIM), dtype="<f4")
            cursor = 0
            for index, face in enumerate(faces):
                if face is None:
                    continue
                full[index] = matrix[cursor]
                cursor += 1
            return full
        return matrix


def matrix_from_rows(rows: Sequence[Any]) -> np.ndarray:
    """把 `face_embedding.vector` 的库行解成 (N, dim) float32 并 L2 归一化。

    **长度不符就报错**，不静默补零：一个维度对不上的向量会让整簇的余弦
    变成无意义的数，而那种错误在界面上表现为"分人突然不准了"，极难追查。

    ## 为什么先把所有 BLOB 拼成一块再解析（一次实测的性能修复）

    早先是"逐行 `bytes()` + `np.frombuffer` + 收进 list + 一次 `vstack`"。
    在 7,371 行 × 768 维上实测要 **42 秒** —— 而点积本身只要 0.7 毫秒。
    瓶颈全在 Python 层的那几千次调用与 `vstack` 的反复拷贝。
    改成 `b"".join(...)` 之后只调一次 `np.frombuffer`，降到毫秒级。
    这个差异决定了"点一下就能看到重排"到底做不做得到。
    """
    if not rows:
        return np.zeros((0, 0), dtype="f4")
    dim = int(rows[0]["dim"])
    width = dim * 4
    chunks: list[bytes] = []
    for row in rows:
        raw = bytes(row["vector"])
        if len(raw) != width:
            raise VectorStoreError(
                f"face_id={row.get('face_id')} 的向量长度 {len(raw)} 与 dim={dim} 不符")
        chunks.append(raw)
    flat = np.frombuffer(b"".join(chunks), dtype="<f4")
    return l2_normalize(flat.reshape(len(chunks), dim).astype("f4"))


# --------------------------------------------------------------------------- 聚类

def knn_graph(matrix: np.ndarray, *, k: int = 8,
              min_similarity: float = LINK_MIN_SIMILARITY) -> list[set[int]]:
    """每个节点连最近 k 个邻居（互为邻居，得到无向图）。

    **为什么不用"余弦 ≥ 阈值"的单链聚类**：那做的是传递闭包 —— A 像 B、B 像 C
    就把 A 和 C 并成一人。实测在 905 张脸上，阈值 0.363 时最大簇 686 张（76% 全并一起）。

    `min_similarity` 断掉噪声边（见该常量的说明）。**k 的默认值 8 也是实测取舍**：
    在同一份两簇合成数据上，k=4 会碎成 12/8/4 三簇、k=12 会并成 13/11 两簇但不均，
    只有 k=8 稳定给出 12/12。k 太小图不连通（同一人被打散），
    k 太大图过连通（不同人被拉近）—— 这是一条有最优点、且最优点在 8 附近的曲线。
    """
    n = len(matrix)
    if n == 0:
        return []
    sims = matrix @ matrix.T
    np.fill_diagonal(sims, -np.inf)
    width = max(1, min(int(k), n - 1))
    adjacency: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        neighbours = np.argpartition(-sims[i], width - 1)[:width]
        for j in neighbours:
            j = int(j)
            if sims[i, j] >= float(min_similarity):
                adjacency[i].add(j)
                adjacency[j].add(i)
    return adjacency


def chinese_whispers(adjacency: Sequence[set[int]], *, iterations: int = 40,
                     seed: int = 0) -> np.ndarray:
    """kNN 图上的迭代投票（人脸聚类常用）。

    抗链式的原因：每次只让一个节点跟随它**直接邻居**的多数派，改变是局部的；
    A—B—C 这条链不会因此把 A 与 C 直接并起来。

    `seed` 固定 → 同样的输入得到同样的分簇（可复算，铁律 #4）。
    """
    import random

    n = len(adjacency)
    if n == 0:
        return np.zeros(0, dtype=int)
    rng = random.Random(seed)
    labels = list(range(n))
    order = list(range(n))
    for _ in range(iterations):
        changed = 0
        rng.shuffle(order)
        for node in order:
            neighbours = adjacency[node]
            if not neighbours:
                continue
            counts: dict[int, int] = {}
            for other in neighbours:
                counts[labels[other]] = counts.get(labels[other], 0) + 1
            best = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
            if labels[node] != best:
                labels[node] = best
                changed += 1
        if changed == 0:
            break
    _unique, inverse = np.unique(np.array(labels), return_inverse=True)
    return inverse


def mean_pairwise_cosine(matrix: np.ndarray) -> float:
    """全样本的两两平均余弦（对比基线）。随机人脸对的基线约 0.64（实测于 905 张脸）。"""
    n = len(matrix)
    if n < 2:
        return float("nan")
    sims = matrix @ matrix.T
    return float((sims.sum() - n) / (n * (n - 1)))


def separation_ratio(matrix: np.ndarray, labels: np.ndarray, *,
                     baseline: float | None = None) -> float:
    """同簇平均余弦 ÷ **全样本**平均余弦。

    ⚠️ `baseline` 必须来自**整个矩阵**，不能来自这一簇自己 ——
    这是一个真实踩过的错：早先的实现把 `matrix` 直接传成"这一簇的块"，
    于是分子分母都在这块里算，比值恒等于 1.00。界面上那一列全是 1.00，
    看起来像"分得很好"，实际上什么信息都没有（典型的**度量本身出错**）。
    """
    n = len(matrix)
    if n < 2:
        return float("nan")
    base = mean_pairwise_cosine(matrix) if baseline is None else float(baseline)
    if base <= 1e-9:
        return float("nan")
    sims = matrix @ matrix.T
    within: list[float] = []
    for label in np.unique(labels):
        index = np.where(labels == label)[0]
        size = len(index)
        if size < 2:
            continue
        block = sims[np.ix_(index, index)]
        within.append((float(block.sum()) - size) / (size * (size - 1)))
    if not within:
        return float("nan")
    return float(np.mean(within) / base)


@dataclass
class Cluster:
    """一个候选人物：成员索引 + 可分性指标。"""

    members: list[int]
    ratio: float

    @property
    def size(self) -> int:
        return len(self.members)


def discover_clusters(matrix: np.ndarray, *, k: int = 8, min_size: int = 3,
                      iterations: int = 40, seed: int = 0,
                      min_similarity: float = LINK_MIN_SIMILARITY) -> list[Cluster]:
    """把特征矩阵分成候选簇（只保留 `min_size` 以上的）。

    返回按大小降序排列 —— 大簇更可能是真实的主要人物，先让用户看到它们。
    """
    if len(matrix) < min_size:
        return []
    adjacency = knn_graph(matrix, k=k, min_similarity=min_similarity)
    labels = chinese_whispers(adjacency, iterations=iterations, seed=seed)
    # ★ 基线必须取自**整个矩阵**（见 separation_ratio 的说明）
    baseline = mean_pairwise_cosine(matrix)
    clusters: list[Cluster] = []
    for label in np.unique(labels):
        index = np.where(labels == label)[0].tolist()
        if len(index) < min_size:
            continue
        block = matrix[index]
        clusters.append(Cluster(
            members=index,
            ratio=separation_ratio(block, np.zeros(len(index), dtype=int),
                                   baseline=baseline)))
    clusters.sort(key=lambda c: (-c.size, c.members[0]))
    return clusters


# --------------------------------------------------------------------------- 原型学习

@dataclass
class LearnedProfile:
    """一次学习的结果（要写进 `person_profile` 的东西）。"""

    vector: np.ndarray                    # 原型（正例加权均值，L2 归一化）
    negative_vector: np.ndarray | None
    pos_count: int
    neg_count: int
    threshold: float
    margin: float | None
    pos_sims: list[float]
    neg_sims: list[float]
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        margin = "—" if self.margin is None else f"{self.margin:+.3f}"
        return (f"原型 {self.pos_count} 正 / {self.neg_count} 负 · "
                f"阈值 {self.threshold:.3f} · 间隔 {margin}")


def decide_threshold(pos_sims: Sequence[float], neg_sims: Sequence[float]) -> tuple[float, float | None, list[str]]:
    """从**用户标注**的两个相似度分布里学出接受阈值。

    规则只有一条：**在所有候选切点里选平衡准确率最高的那个，并列时取更低的那个**。

      * 完全可分（正例最低 > 负例最高）→ 最低的合法切点就是"正好把负例挡在外面"，
        于是阈值落在 `min(正例)` 上 —— 这是**召回最高而又不误报**的选择。
      * 分布交叠 → 仍然是同一条规则，选错得最少的切点，并**明确记一条 notes**。

    ## 为什么不是取正负例的中点（这是一个真实缺陷）

    早先的实现对"完全可分"的情形直接取中点。但正例样本的相似度是**自我打分**
    （它们就在原型里），而真正要判的是**没参加建模的同一个人**，其相似度必然更低。
    取中点会把阈值推到两者之间偏高的位置，于是同一个人的其余照片几乎全被挡在门外 ——
    实测就是这样：某个簇用 5 张种子学出阈值 0.845，召回从 100% 掉到 56%，
    另一个簇从 49% 掉到 0%。**"学出来的阈值反而更差"比不学还糟。**

    ⚠️ 调用方必须传**留一法**得到的正例相似度（见 `fit_profile`），
    否则上面这个问题只是被换了个地方发生。
    """
    notes: list[str] = []
    positives = sorted(float(v) for v in pos_sims)
    negatives = sorted(float(v) for v in neg_sims)
    if positives and negatives:
        candidates = sorted(set([round(v, 4) for v in positives]
                                + [round(v, 4) for v in negatives]))
        best_threshold, best_score = candidates[0], -1.0
        for cut in candidates:
            sensitivity = sum(1 for v in positives if v >= cut) / len(positives)
            specificity = sum(1 for v in negatives if v < cut) / len(negatives)
            score = sensitivity + specificity
            # 并列时**保留更低的切点**（candidates 升序遍历，用严格大于才替换）：
            # 同样准确的前提下，宁可选召回更高的那一刀。
            if score > best_score:
                best_threshold, best_score = cut, score
        separable = min(positives) > max(negatives)
        if not separable:
            notes.append("正负例分布交叠：阈值取平衡准确率最高的切点，仍会有误判。")
        return best_threshold, min(positives) - max(negatives), notes
    if positives:
        index = max(0, int(round(0.1 * (len(positives) - 1))))
        threshold = max(SFACE_DEFAULT_THRESHOLD, positives[index] - 0.05)
        notes.append(
            "尚无否决样本：阈值是按正例的 10% 分位保守估计的，"
            "标几张「不是这个人」之后它会变成学出来的值。")
        return threshold, None, notes
    notes.append("没有任何标注：沿用 SFace 官方默认阈值。")
    return SFACE_DEFAULT_THRESHOLD, None, notes


def fit_profile(matrix: np.ndarray, *, positive: Sequence[int], auto: Sequence[int],
                negative: Sequence[int], user_weight: float = 2.0,
                ) -> LearnedProfile:
    """从带标注的样本估计原型与阈值（**只做线性代数，不训练权重**）。

    ## 为什么不是微调（这一点必须对用户说清楚）

    1. 全库人脸是千级，微调 SFace 这种量级只会过拟合；
    2. 微调**破坏可复算性** —— 同一个向量在不同时刻得到不同分数，
       "当初为什么判成他"就再也算不出来了，违反铁律 #4；
    3. 原型学习的每一步都能用库里的向量重算，而且**立刻生效**、用户看得见。

    ## 权重规则（简单到能解释）

    `用户确认过的 × user_weight(2) + 模型提议的 × 1`。
    理由：用户点过的才是真标签，模型提议只是初筛；用户每点一次，
    原型就往他的判断挪一步 —— 这就是"持续学习"在这个项目里的具体含义。

    ## ★ 阈值用的是**留一法**相似度（一个真实缺陷的修正）

    正例样本本身就在原型里，所以"它对原型的相似度"是**自我打分**，必然虚高。
    拿这种虚高的数去定阈值，等于按"参加过考试的人能考多少分"来划及格线 ——
    结果是**没参加过建模的同一个人反而被挡在外面**。实测过：某簇用 5 张种子
    学出阈值 0.845，同一个人的召回从 100% 掉到 56%，另一簇从 49% 掉到 0%。
    所以这里对每个正例都**把它自己排除后**重算原型再打分（留一法），
    得到的分布才与实际要判的那些脸可比。
    """
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        raise VectorStoreError("fit_profile 需要非空的 (N, dim) 特征矩阵")

    total = matrix.shape[0]
    for name, index in (("positive", positive), ("auto", auto), ("negative", negative)):
        for i in index:
            if not (0 <= int(i) < total):
                raise VectorStoreError(f"{name} 索引 {i} 超出范围 0..{total - 1}")

    positives = [int(i) for i in positive]
    autos = [int(i) for i in auto]
    negatives = [int(i) for i in negative]

    weights: list[tuple[int, float]] = [(i, float(user_weight)) for i in positives]
    weights.extend((i, 1.0) for i in autos if i not in set(positives))
    if not weights:
        raise VectorStoreError("这个人一个正例都没有，无法估计原型")

    def centroid_of(pairs: Sequence[tuple[int, float]]) -> np.ndarray:
        stacked = np.vstack([matrix[i] for i, _w in pairs])
        weight_vector = np.asarray([w for _i, w in pairs], dtype=np.float32)[:, None]
        raw = (stacked * weight_vector).sum(axis=0) / float(weight_vector.sum())
        norm = float(np.linalg.norm(raw))
        if norm <= 1e-9:
            raise VectorStoreError("正例均值退化为零向量（样本可能互相抵消），拒绝写入")
        return (raw / norm).astype(np.float32)

    centroid = centroid_of(weights)

    negative_vector = None
    if negatives:
        neg = l2_normalize(np.vstack([matrix[i] for i in negatives]))
        mean = neg.mean(axis=0)
        mean_norm = float(np.linalg.norm(mean))
        if mean_norm > 1e-9:
            negative_vector = (mean / mean_norm).astype(np.float32)

    # 留一法正例相似度：每个正例对"不含它自己"的原型打分。
    # 只有一个正例时无法留一（剩下零个样本），退回自我打分并**如实标注**。
    pos_sims: list[float] = []
    if len(weights) == 1:
        pos_sims = [float(centroid @ matrix[weights[0][0]])]
    else:
        for index, _w in weights:
            rest = [(i, w) for i, w in weights if i != index]
            try:
                pos_sims.append(float(centroid_of(rest) @ matrix[index]))
            except VectorStoreError:
                pos_sims.append(float(centroid @ matrix[index]))
    neg_sims = [float(centroid @ matrix[i]) for i in negatives]
    threshold, margin, notes = decide_threshold(pos_sims, neg_sims)
    if len(weights) == 1:
        notes.append("只有一个正例：阈值是自比对的估计值，再确认几张会更可靠。")

    # 对外报告的"正例相似度"仍然用最终原型（这才是用户核对时该看的数）；
    # 只有**阈值**用留一法的分布。两者混用会让界面上的数字与判定线对不上。
    reported_pos = [float(centroid @ matrix[i]) for i, _w in weights]
    if not negatives:
        notes.append("没有任何「不是这个人」的标注，误判不会被纠正。")
    return LearnedProfile(
        vector=centroid, negative_vector=negative_vector,
        pos_count=len(positives), neg_count=len(negatives),
        threshold=float(threshold), margin=margin,
        pos_sims=reported_pos, neg_sims=neg_sims, notes=notes,
    )


@dataclass
class Assignment:
    """一条"模型认为这张脸属于某人"的候选（**等用户确认**）。"""

    person_id: int
    face_index: int
    similarity: float
    accepted: bool


def assign_faces(
    profile_vectors: dict[int, np.ndarray],
    *,
    thresholds: dict[int, float],
    matrix: np.ndarray,
    candidate_indices: Iterable[int],
    blocked: dict[int, set[int]] | None = None,
) -> dict[int, list[Assignment]]:
    """用**当前原型**给未归类的脸打分（增量那一步的核心）。

    `blocked[person_id]` 是该人被用户否决过的样本索引集合 —— 这些脸**永远不再**
    被提议给他。少了这条，用户每次点"不是他"都只是当下消失，下次增量又冒出来。

    返回 `{person_id: [Assignment...]}`，**每个候选脸只归给最像的那个人**：
    同一张脸出现在多个人的候选清单里会让用户重复劳动，而且容易两边都点"是"。
    """
    assignments: dict[int, list[Assignment]] = {pid: [] for pid in profile_vectors}
    if matrix.shape[0] == 0 or not profile_vectors:
        return assignments
    candidates = [int(i) for i in candidate_indices]
    if not candidates:
        return assignments

    blocked = blocked or {}
    person_ids = sorted(profile_vectors)
    prototypes = np.vstack([np.asarray(profile_vectors[pid], dtype=np.float32)
                            for pid in person_ids])
    block = matrix[candidates]
    sims = block @ prototypes.T                       # (候选数, 人物数)

    for row, face_index in enumerate(candidates):
        scores = sims[row]
        order = np.argsort(-scores)
        for rank in order:
            person_id = person_ids[int(rank)]
            if face_index in blocked.get(person_id, set()):
                continue
            similarity = float(scores[int(rank)])
            threshold = float(thresholds.get(person_id, SFACE_DEFAULT_THRESHOLD))
            assignments[person_id].append(Assignment(
                person_id=person_id, face_index=face_index, similarity=similarity,
                accepted=similarity >= threshold,
            ))
            break
    return assignments


__all__ = [
    "Assignment",
    "Cluster",
    "DETECT_MAX",
    "DETECTOR",
    "DetectedFace",
    "FaceModels",
    "FaceNotAvailable",
    "FRONTAL_EYE_RATIO_MIN",
    "FRONTAL_FACE_PX_MIN",
    "FRONTAL_SYMMETRY_MAX",
    "LINK_MIN_SIMILARITY",
    "LearnedProfile",
    "MIN_FACE_PX",
    "MODEL_DIM",
    "MODEL_NAME",
    "MODEL_NORMALIZATION",
    "PREP_VERSION",
    "SCORE_THRESHOLD",
    "SFACE_DEFAULT_THRESHOLD",
    "assign_faces",
    "chinese_whispers",
    "decide_threshold",
    "detector_version",
    "discover_clusters",
    "fit_profile",
    "frontal_metrics",
    "is_frontal",
    "knn_graph",
    "matrix_from_rows",
    "mean_pairwise_cosine",
    "model_version",
    "separation_ratio",
]
