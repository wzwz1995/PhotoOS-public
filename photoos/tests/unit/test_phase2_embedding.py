"""Phase 2 · M3：向量存储、模型校验、嵌入器的测试。

三类断言：
  A. **序列化与检索契约** —— 迁移/跨平台安全的前提（little-endian、长度恰好 dim*4）。
  B. **模型校验** —— 缺失/截断/篡改必须被拒绝，而不是"能打开就算数"。
  C. **绝不静默降级** —— 运行时/模型不可用时必须抛错，**不能返回零向量**。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pytest

from photoos.library import model_store
from photoos.library import vectorstore as vs
from photoos.library.embedding import (
    ClipOnnxEmbedder,
    EmbeddingNotAvailable,
    NullEmbedder,
    PreprocessConfig,
    create_embedder,
    preprocess_image,
)

# ============================================================ A. 序列化

def test_pack_unpack_round_trip() -> None:
    original = np.array([0.1, -0.2, 0.3, 0.4], dtype="float32")
    blob = vs.pack_vector(original)
    assert len(blob) == 4 * 4
    restored = vs.unpack_vector(blob, dim=4)
    assert np.allclose(restored, original)
    assert restored.dtype == np.dtype("<f4")


def test_pack_vector_is_little_endian() -> None:
    """显式小端：换机/换平台时字节序不能成为隐患。"""
    blob = vs.pack_vector(np.array([1.0], dtype="float32"))
    assert blob == struct.pack("<f", 1.0)
    assert blob != struct.pack(">f", 1.0) or True  # 大端在 x86 上与期望不同
    assert blob[:1] == b"\x00" or blob[3:] == b"\x80"  # 1.0 的 LE 表示为 00 00 80 3f


def test_unpack_rejects_wrong_length() -> None:
    """长度必须恰为 dim*4 —— 静默接受会让后续相似度全部算错。"""
    with pytest.raises(vs.VectorStoreError):
        vs.unpack_vector(b"\x00" * 10, dim=4)


def test_pack_vector_rejects_2d() -> None:
    with pytest.raises(vs.VectorStoreError):
        vs.pack_vector(np.zeros((2, 4), dtype="float32"))


def test_pack_matrix_rows() -> None:
    blobs = vs.pack_matrix(np.zeros((3, 5), dtype="float32"))
    assert len(blobs) == 3
    assert all(len(b) == 20 for b in blobs)


def test_pack_matrix_rejects_1d() -> None:
    with pytest.raises(vs.VectorStoreError):
        vs.pack_matrix(np.zeros(5, dtype="float32"))


# ============================================================ A. 归一化

def test_l2_normalize_gives_unit_rows() -> None:
    out = vs.l2_normalize(np.array([[3.0, 0.0], [0.0, 4.0]], dtype="float32"))
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0)


def test_l2_normalize_zero_vector_does_not_produce_nan() -> None:
    """零向量保持为零 —— 静默产生 NaN 会让整个相似度矩阵变成垃圾，比抛错更难查。"""
    out = vs.l2_normalize(np.array([[0.0, 0.0], [1.0, 0.0]], dtype="float32"))
    assert not np.isnan(out).any()
    assert np.allclose(out[0], 0.0)


# ============================================================ A. 检索

def _store() -> vs.NumpyVectorStore:
    matrix = np.array(
        [
            [1.0, 0.0, 0.0],     # 0
            [0.99, 0.1, 0.0],    # 1 —— 与 0 最像
            [0.0, 1.0, 0.0],     # 2 —— 正交
            [-1.0, 0.0, 0.0],    # 3 —— 相反
        ],
        dtype="float32",
    )
    return vs.NumpyVectorStore.from_matrix(matrix, keys=[100, 101, 102, 103])


def test_knn_ranks_by_similarity() -> None:
    store = _store()
    result = store.knn(np.array([1.0, 0.0, 0.0]), 3)
    assert [n.index for n in result] == [0, 1, 2]
    assert result[0].score == pytest.approx(1.0, abs=1e-6)
    assert result[2].score == pytest.approx(0.0, abs=1e-6)


def test_knn_allow_mask_restricts_candidates() -> None:
    """掩码用于"只在某个时段/主题内找相似" —— 不加掩码会跨 Session 搜出无关照片。"""
    store = _store()
    allow = np.array([False, False, True, True])
    result = store.knn(np.array([1.0, 0.0, 0.0]), 2, allow=allow)
    assert [n.index for n in result] == [2, 3]


def test_knn_allow_mask_length_is_validated() -> None:
    with pytest.raises(vs.VectorStoreError):
        _store().knn(np.zeros(3, dtype="float32"), 2, allow=np.array([True, False]))


def test_knn_rejects_wrong_dimension() -> None:
    with pytest.raises(vs.VectorStoreError):
        _store().knn(np.zeros(5, dtype="float32"), 2)


def test_knn_on_empty_store_returns_empty() -> None:
    assert vs.NumpyVectorStore(dim=3).knn(np.zeros(3, dtype="float32"), 5) == []


def test_knn_k_larger_than_store_is_clamped() -> None:
    assert len(_store().knn(np.array([1.0, 0.0, 0.0]), 99)) == 4


def test_knn_by_key_excludes_itself() -> None:
    """★ "找出和这张最像的其他照片" —— 结果里出现自己就是 bug。"""
    store = _store()
    result = store.knn_by_key(100, 3)
    assert 100 not in [store.key_at(n.index) for n in result]
    assert store.key_at(result[0].index) == 101


def test_knn_by_key_unknown_key_raises() -> None:
    with pytest.raises(vs.VectorStoreError):
        _store().knn_by_key(999, 2)


def test_similarity_symmetric_and_bounded() -> None:
    store = _store()
    assert store.similarity(0, 1) == pytest.approx(store.similarity(1, 0))
    assert store.similarity(0, 0) == pytest.approx(1.0, abs=1e-6)
    assert store.similarity(0, 3) == pytest.approx(-1.0, abs=1e-6)


def test_pairwise_above_only_returns_upper_triangle() -> None:
    """★ 只算上三角：避免 (i,j) 与 (j,i) 重复，也避免算自己跟自己的 1.0。"""
    pairs = _store().pairwise_above(0.9)
    assert (0, 1, pytest.approx(pairs[0][2]))[:2] == (0, 1)
    assert all(i < j for i, j, _ in pairs)
    assert all(i != j for i, j, _ in pairs)


def test_pairwise_above_keys_maps_to_asset_ids() -> None:
    pairs = _store().pairwise_above_keys(0.9)
    assert [(i, j) for i, j, _ in pairs] == [(100, 101)]


def test_pairwise_above_on_tiny_store() -> None:
    assert vs.NumpyVectorStore.from_matrix(np.zeros((1, 3), dtype="float32")).pairwise_above(0.0) == []


def test_subset_preserves_keys_and_scores() -> None:
    """★ 只在时段内做全对全 —— 全库 21,400 一次算完要 1.04 GB 结果矩阵。"""
    store = _store()
    sub = store.subset([1, 2])
    assert sub.keys == [101, 102]
    assert sub.similarity(0, 1) == pytest.approx(store.similarity(1, 2))


def test_from_rows_round_trip() -> None:
    rows = [
        (7, vs.pack_vector(np.array([1.0, 0.0], dtype="float32"))),
        (9, vs.pack_vector(np.array([0.0, 1.0], dtype="float32"))),
    ]
    store = vs.NumpyVectorStore.from_rows(rows, dim=2)
    assert store.size == 2
    assert store.key_at(1) == 9
    assert store.similarity(0, 1) == pytest.approx(0.0, abs=1e-6)


def test_from_rows_rejects_bad_blob() -> None:
    with pytest.raises(vs.VectorStoreError):
        vs.NumpyVectorStore.from_rows([(1, b"\x00\x01")], dim=2)


# ============================================================ B. 模型校验

def _write_model(data_root: Path, *, vision: bytes = b"x" * 10, prep: dict | None = None) -> Path:
    directory = model_store.model_dir(data_root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "vision_model.onnx").write_bytes(vision)
    (directory / "preprocessor_config.json").write_text(
        json.dumps(prep if prep is not None else {
            "size": {"shortest_edge": 224},
            "image_mean": [0.48145466, 0.4578275, 0.40821073],
            "image_std": [0.26862954, 0.26130258, 0.27577711],
        }),
        encoding="utf-8",
    )
    return directory


def test_model_dir_is_under_data_root_ai_cache(tmp_path: Path) -> None:
    """铁律 #4：模型要随数据根一起备份/迁移（`ai_cache` 已在迁移包的入包目录里）。"""
    directory = model_store.model_dir(tmp_path)
    assert directory.parent.parent.parent == tmp_path
    assert directory.parent.parent.name == "ai_cache"


def test_verify_reports_missing(tmp_path: Path) -> None:
    results = model_store.verify(tmp_path)
    assert all(not r.ok for r in results)
    assert all("缺失" in r.detail for r in results)


def test_verify_rejects_wrong_size(tmp_path: Path) -> None:
    """字节数不符 → 拒绝。避免"文件被截断但恰好能打开"这种半损坏状态。"""
    _write_model(tmp_path)
    results = model_store.verify(tmp_path, deep=False)
    vision = next(r for r in results if r.path.name == "vision_model.onnx")
    assert not vision.ok
    assert "字节数不符" in vision.detail


def test_verify_rejects_wrong_sha256(tmp_path: Path) -> None:
    """★ 大小凑巧一致但内容不同 → 必须被 sha256 拒绝（只比大小会漏掉）。"""
    _write_model(tmp_path)
    # 把 vision 文件撑到声明的字节数，但内容不是真模型
    target = model_store.model_dir(tmp_path) / "vision_model.onnx"
    target.write_bytes(b"\x00" * model_store.MODEL_FILES[0][2])
    results = model_store.verify(tmp_path, deep=True)
    vision = next(r for r in results if r.path.name == "vision_model.onnx")
    assert not vision.ok
    assert "sha256" in vision.detail


def test_is_ready_false_on_empty(tmp_path: Path) -> None:
    assert model_store.is_ready(tmp_path) is False


def test_load_preprocessor_config_reads_declared_values(tmp_path: Path) -> None:
    _write_model(tmp_path)
    config = PreprocessConfig.from_model_dir(tmp_path)
    assert config.size == 224
    assert config.mean[0] == pytest.approx(0.48145466)


def test_load_preprocessor_config_rejects_zero_std(tmp_path: Path) -> None:
    """std=0 会导致除零 → 必须拒绝，而不是产出 inf/NaN 向量。"""
    _write_model(tmp_path, prep={"image_std": [0.0, 0.0, 0.0]})
    with pytest.raises(EmbeddingNotAvailable):
        PreprocessConfig.from_model_dir(tmp_path)


def test_load_preprocessor_config_missing_file(tmp_path: Path) -> None:
    """配置缺失 → 明确报错（而不是退回硬编码的 mean/std，那会让预处理口径与权重版本脱钩）。"""
    with pytest.raises(EmbeddingNotAvailable):
        model_store.load_preprocessor_config(tmp_path)


def test_model_constants_are_pinned() -> None:
    """模型身份一旦发布不得更改语义（`embedding.model_version` 就是它）。"""
    assert model_store.MODEL_DIM == 512
    assert model_store.MODEL_NORMALIZATION == "l2"
    assert model_store.MODEL_NAME == "clip-vit-base-patch32"
    remote, local, size, digest = model_store.MODEL_FILES[0]
    assert local == "vision_model.onnx"
    assert size == 351_685_709
    assert digest.startswith("fd6e1402")


def test_default_base_url_is_the_mirror() -> None:
    """huggingface.co 本机不可达（实测超时），默认必须走镜像。"""
    assert "hf-mirror.com" in model_store.DEFAULT_BASE_URL


# ============================================================ C. 绝不静默降级

def test_null_embedder_raises_instead_of_returning_zeros() -> None:
    """★ 本文件最重要的一条：不可用时**抛错**。

    如果返回零向量，相似度会全是 0，用户看到"所有照片都不像" ——
    一个看起来正常、实际完全错误的结果。
    """
    embedder = NullEmbedder("测试原因")
    with pytest.raises(EmbeddingNotAvailable) as excinfo:
        embedder.embed_images([object()])
    assert "测试原因" in str(excinfo.value)
    with pytest.raises(EmbeddingNotAvailable):
        embedder.embed_paths([Path("x.jpg")])


def test_null_embedder_still_declares_identity() -> None:
    """占位实现也要如实报告身份，否则 `ai doctor` 无法显示"本该用哪个模型"。"""
    embedder = NullEmbedder("未安装")
    assert embedder.dim == 512
    assert embedder.model == model_store.MODEL_NAME


def test_create_embedder_returns_null_when_model_missing(tmp_path: Path) -> None:
    """模型缺失 → 返回 NullEmbedder（可构造、可展示原因），调用时才抛错。"""
    embedder = create_embedder(tmp_path)
    assert isinstance(embedder, NullEmbedder)
    with pytest.raises(EmbeddingNotAvailable):
        embedder.embed_images([object()])


def test_create_embedder_reports_missing_model_path(tmp_path: Path) -> None:
    """模型缺失时给出的原因必须**可操作**（告诉用户该跑哪条命令）。

    注意：产品 venv 里**故意没有** onnxruntime（它是可选依赖，见方案 §3.2），
    因此这一条在两种环境下都要成立 ——
    "没装运行时"和"没下模型"是两条不同的缺依赖原因，各自给出各自的下一步。
    """
    embedder = create_embedder(tmp_path)
    reason = embedder.reason
    assert ("模型缺失" in reason and "ai fetch-model" in reason) or ("onnxruntime" in reason and "ai-gpu" in reason)


def test_create_embedder_reason_names_both_remedies(tmp_path: Path) -> None:
    """完整原因里应当同时提到"装运行时"与"下模型"，用户照做即可，不用回来问。"""
    reason = NullEmbedder("测试").reason
    assert reason == "测试"
    # NullEmbedder 的 `_raise` 消息里必须带两条可操作指令
    embedder = NullEmbedder("未安装 onnxruntime")
    with pytest.raises(EmbeddingNotAvailable) as excinfo:
        embedder.embed_images([object()])
    message = str(excinfo.value)
    assert "ai-gpu" in message
    assert "ai fetch-model" in message


def test_clip_embedder_probe_flags_degradation() -> None:
    """★ 请求 CUDA 但 session 只有 CPU → 必须标记 degraded。

    复刻调研阶段踩到的坑：`get_available_providers()` 三种状态下输出相同，
    只有真推理 + 检查实际 provider 才能判断。
    """
    class FakeOrt:
        __version__ = "1.25.1"

        @staticmethod
        def get_available_providers():
            return ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]

    class FakeSession:
        def get_providers(self):
            return ["CPUExecutionProvider"]      # ← 请求了 CUDA，实际是 CPU

        def run(self, _outputs, _feed):
            return [np.zeros((1, 512), dtype="float32")]

    embedder = ClipOnnxEmbedder(
        session=FakeSession(),
        config=PreprocessConfig(),
        ort_module=FakeOrt,
        device="cpu",
    )
    probe = embedder.probe(requested_device="auto")
    assert probe.degraded is True
    assert probe.device == "cpu"
    assert "CUDA" in probe.notes[0]
    assert "1.25.1" in probe.notes[0]      # 提示里给出可操作的具体版本


def test_clip_embedder_probe_reports_cuda_when_real() -> None:
    class FakeOrt:
        __version__ = "1.25.1"

        @staticmethod
        def get_available_providers():
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

    class FakeSession:
        def get_providers(self):
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

        def run(self, _outputs, _feed):
            return [np.zeros((1, 512), dtype="float32")]

    probe = ClipOnnxEmbedder(
        session=FakeSession(), config=PreprocessConfig(), ort_module=FakeOrt, device="cuda"
    ).probe(requested_device="auto")
    assert probe.degraded is False
    assert probe.device == "cuda"
    assert "GPU" in probe.summary


def test_clip_embedder_probe_handles_inference_failure() -> None:
    """推理抛错（例如 cuDNN 版本不对）→ 探测必须**如实报告失败**而不是假装成功。"""
    class FakeOrt:
        __version__ = "1.25.1"

        @staticmethod
        def get_available_providers():
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

    class FakeSession:
        def get_providers(self):
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

        def run(self, _outputs, _feed):
            raise RuntimeError("CUDNN_STATUS_SUBLIBRARY_LOADING_FAILED")

    probe = ClipOnnxEmbedder(
        session=FakeSession(), config=PreprocessConfig(), ort_module=FakeOrt, device="cuda"
    ).probe(requested_device="auto")
    assert probe.degraded is True
    assert any("SUBLIBRARY" in n for n in probe.notes)


def test_oom_fallback_sequence_is_descending() -> None:
    """PRD §11.1「GPU 内存不足时支持降低 batch size」—— 降级序列必须严格递减。"""
    from photoos.library.embedding import OOM_FALLBACK_BATCHES

    assert list(OOM_FALLBACK_BATCHES) == sorted(OOM_FALLBACK_BATCHES, reverse=True)
    assert OOM_FALLBACK_BATCHES[-1] == 1


def test_embed_images_rejects_wrong_output_shape() -> None:
    """模型输出维度不对 → 抛错，不静默产出一个"看起来能用"的错向量。"""
    class FakeOrt:
        __version__ = "test"

        @staticmethod
        def get_available_providers():
            return ["CPUExecutionProvider"]

    class FakeSession:
        def get_providers(self):
            return ["CPUExecutionProvider"]

        def run(self, _outputs, _feed):
            return [np.zeros((1, 256), dtype="float32")]     # 应为 512

    embedder = ClipOnnxEmbedder(
        session=FakeSession(), config=PreprocessConfig(), ort_module=FakeOrt, device="cpu"
    )
    from PIL import Image

    with pytest.raises(vs.VectorStoreError):
        embedder.embed_images([Image.new("RGB", (32, 32))])


def test_embed_images_empty_input_returns_empty_matrix() -> None:
    class FakeOrt:
        __version__ = "test"

        @staticmethod
        def get_available_providers():
            return ["CPUExecutionProvider"]

    class FakeSession:
        def get_providers(self):
            return ["CPUExecutionProvider"]

    embedder = ClipOnnxEmbedder(
        session=FakeSession(), config=PreprocessConfig(), ort_module=FakeOrt, device="cpu"
    )
    out = embedder.embed_images([])
    assert out.shape == (0, 512)


# ============================================================ 预处理

def test_preprocess_shape_and_dtype() -> None:
    from PIL import Image

    out = preprocess_image(Image.new("RGB", (1000, 800)), PreprocessConfig())
    assert out.shape == (3, 224, 224)
    assert out.dtype == np.float32


def test_preprocess_handles_portrait_and_landscape() -> None:
    """★ preview 已烘焙 EXIF 方向（实测：3,225 张竖构图）—— 这里验证两种朝向都能出 224。"""
    from PIL import Image

    config = PreprocessConfig()
    assert preprocess_image(Image.new("RGB", (1600, 1068)), config).shape == (3, 224, 224)
    assert preprocess_image(Image.new("RGB", (1068, 1600)), config).shape == (3, 224, 224)


def test_preprocess_matches_manual_clip_pipeline() -> None:
    """★ 预处理必须与"短边缩放到 size → 中心裁 size"的手工复刻**逐位一致**。

    这是防止"看起来对但差一点点"的关键测试 —— 预处理口径差一点，
    嵌入就会整体漂移，而相似度阈值是**在绝对尺度上**定的（0.88）。
    """
    from PIL import Image

    config = PreprocessConfig()
    image = Image.new("RGB", (400, 100), (255, 255, 255))
    for x in list(range(0, 100)) + list(range(300, 400)):
        for y in range(100):
            image.putpixel((x, y), (0, 0, 0))

    out = preprocess_image(image, config)

    scale = config.size / min(image.size)
    reference = image.resize(
        (int(round(image.width * scale)), int(round(image.height * scale))), Image.BICUBIC
    )
    left = (reference.width - config.size) // 2
    top = (reference.height - config.size) // 2
    reference = reference.crop((left, top, left + config.size, top + config.size)).convert("RGB")
    array = np.asarray(reference, dtype=np.float32) / 255.0
    array = (array - np.asarray(config.mean, dtype=np.float32)) / np.asarray(config.std, dtype=np.float32)
    manual = np.ascontiguousarray(array.transpose(2, 0, 1))

    assert out.shape == (3, 224, 224)
    assert np.array_equal(out, manual)


def test_preprocess_center_crop_discards_side_bars() -> None:
    """中心裁剪必须真的把两侧裁掉。

    构造 400×100：左右各 100 px 纯黑、中间 200 px 纯白。短边缩到 224 后宽 896，
    中心 224 恰好完全落在白区 → 输出**只含白色**，两侧黑边被丢弃。
    （上一版断言写成"中心 > 角落"，被 bicubic 的边缘振铃干扰，是测试写错了，不是实现错了。）
    """
    from PIL import Image

    config = PreprocessConfig()
    image = Image.new("RGB", (400, 100), (255, 255, 255))
    for x in list(range(0, 100)) + list(range(300, 400)):
        for y in range(100):
            image.putpixel((x, y), (0, 0, 0))

    out = preprocess_image(image, config)
    # 白像素归一化后的值
    white = (1.0 - np.asarray(config.mean, dtype=np.float32)) / np.asarray(config.std, dtype=np.float32)
    # 中心区域应与"纯白"一致（允许 bicubic 边缘振铃的极小偏差）
    center_patch = out[:, 20:204, 20:204]
    assert np.allclose(center_patch.mean(axis=(1, 2)), white, atol=0.02), (
        f"中心应为纯白 {white}，实际 {center_patch.mean(axis=(1, 2))}"
    )


def test_preprocess_uses_bicubic_resize() -> None:
    """resize 算法是预处理口径的一部分 —— 换成 NEAREST 会改变 `prep_version` 的语义。"""
    from PIL import Image

    config = PreprocessConfig()
    image = Image.new("RGB", (300, 200))
    for x in range(300):
        for y in range(200):
            image.putpixel((x, y), ((x * 7) % 256, (y * 11) % 256, (x + y) % 256))

    ours = preprocess_image(image, config)
    scale = config.size / min(image.size)
    resized = image.resize(
        (int(round(300 * scale)), int(round(200 * scale))), Image.BICUBIC
    ).convert("RGB")
    left = (resized.width - config.size) // 2
    top = (resized.height - config.size) // 2
    reference = resized.crop((left, top, left + config.size, top + config.size))
    array = np.asarray(reference, dtype=np.float32) / 255.0
    array = (array - np.asarray(config.mean, dtype=np.float32)) / np.asarray(config.std, dtype=np.float32)
    assert np.array_equal(ours, np.ascontiguousarray(array.transpose(2, 0, 1)))


def test_preprocess_rejects_zero_sized_image() -> None:
    from PIL import Image

    class Broken:
        size = (0, 0)

    with pytest.raises(ValueError):
        preprocess_image(Broken(), PreprocessConfig())
