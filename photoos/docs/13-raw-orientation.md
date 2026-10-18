# 13 · RAW 预览方向反了，以及它对"判断"的实际影响

> 状态：**已定位，未修复**（修复需要 `E:` 盘在线 —— 该盘当前离线，356 GB 原片读不到）。
>
> 相关：[`05-ai.md`](05-ai.md) 向量与人脸 · [`08-decisions.md`](08-decisions.md) 决策与坑 ·
> [`07-runbook.md`](07-runbook.md) 排障

---

## 一、怎么发现的

用户在看「人物 → 我」这一页时问：*"判定线和判定余量这俩参数代表啥"*。
那个人的牌面是 `已确认 308 张 / 已否决 134 张 / 判定线 0.536 / 判定余量 -0.671`。

查 `-0.671` 是怎么来的（`library/faces.py:598` 就是一句减法）：

```python
return best_threshold, min(positives) - max(negatives), notes
```

按库里的向量重算这个人的分布：

| | 最低 | 中位 | 最高 |
|---|---|---|---|
| 确认的 308 张 | **0.062** | 0.774 | 0.896 |
| 否决的 134 张 | 0.224 | 0.378 | **0.728** |

`0.062 - 0.728 = -0.666`（库里存 -0.671，差 0.005 是因为阈值走留一法）。

**整个负余量几乎全由一对照片造成。** 去看那个 0.062：

- `希腊市\2026-10-06_1146_至_10-07_1908\DSC_0264.NEF`，`orientation = 8`
- 把它的 preview 下载下来看：**人是倒的，背景里那块希腊文招牌是镜像的**

一张 `orientation=8`（"需逆时针转 90°"）的照片，预览却是 **180° 颠倒** —— 自相矛盾。

---

## 二、根因：把 LibRaw 的 `sizes.flip` 用在了内嵌预览上

`media/adapter.py`：

```python
def _apply_raw_flip(image, flip):
    if flip == 3: return image.rotate(180, expand=True)
    if flip == 5: return image.transpose(FLIP_LEFT_RIGHT).rotate(90, expand=True)
    if flip == 6: return image.rotate(-90, expand=True)
    return image
```

LibRaw 作者 Alex Tutubalin 的原话（[libraw.org](https://www.libraw.org/comment/5341#comment-5341)）：

> `imgdata.sizes.flip` **is related to raw data only.**
> Assuming modern camera with embedded JPEG preview:
> `unpack_thumb()` **does nothing with rotation, JPEG data are extracted AS is** —
> (for most cameras) extracted JPEG is either already rotated or contains correct
> EXIF.Orientation tag.

**`flip` 描述的是传感器原始数据，不是相机内嵌的 JPEG 预览。** 对已经摆正的内嵌
预览再转一次 = **二次旋转**。作者在同一帖里描述的正是这个症状：
"按 `flip` 转了，预览反而转错"（他那个例子是 CR2，`flip=5` 而预览已正）。

`media/metadata.py:318` 还把 LibRaw flip 映射成 EXIF 值给界面用：

```python
info.orientation = {0: 1, 3: 6, 5: 8, 6: 5}.get(flip, 1)
```

这个映射本身很可能不对（LibRaw 文档：3=180°、5=90°CCW、6=90°CW，
而 EXIF 6=转90°CW、8=转90°CCW），**但由于原片读不到，这一条尚未核实**，
不要在修的时候把它当既定事实。

### 影响范围

| 项 | 数量 |
|---|---|
| `orientation ∈ {6,8}` 的照片（预览方向可疑） | **3,861** |
| 其中 `file_type = 'raw'` | **1,345** |
| 全库照片 | 21,340 |

---

## 三、关键问题：这会不会影响"判断"？—— 实测：**看是哪种判断**

两套模型对"倒过来"的敏感度**完全不同**。都实测过。

### 内容检索（CLIP）：几乎不受影响

同一张图转 90°/180°/270°，与正立的余弦（`_probe/clip_rotation.py`，14 张 JPEG）：

| 旋转 | 中位 | 最低 | 最高 |
|---|---|---|---|
| 90° | 0.983 | 0.860 | 0.992 |
| 180° | 0.980 | 0.803 | 0.997 |
| 270° | 0.988 | 0.872 | 0.993 |

本仓库自己的口径（`library/embedding.py` 顶部注释）：**同图 0.832–0.996、
不同图 0.463–0.652**。42 个旋转样本里只有 1 个低于 0.832，**没有一个掉进"不同图"区间**。

**结论：找照片 / 主题 / 精选的场景相似度基本不受影响。**

### 人脸（SFace）：几乎完全失效

同一张脸，正立 vs 倒转的余弦（`_probe/face_rotation.py`，
person 17 的 36 张已确认人脸，在裁块上重新检测 + 对齐）：

| | 值 |
|---|---|
| 中位 | **0.105** |
| 最低 | -0.004 |
| 最高 | 0.181 |
| < 0.3 的比例 | **36 / 36** |

对照：同一个人不同照片的正常余弦约 **0.5–0.9**；这个人的判定线是 **0.536**。

**倒过来之后，模型不是"分数低一点"，而是完全不认识这个人。**

> 为什么人脸这么脆：SFace 靠 `alignCrop` 用 5 个关键点把脸**摆正**再比对。
> 倒着的脸喂进去，对齐到的是倒的模板，特征自然崩。
> 而 CLIP 是在整图上做语义，对朝向本来就宽容。

### 这解释了 `-0.671`

person 17 的确认照片里，方向正常那组与需旋转那组**中位几乎一样**
（0.775 vs 0.771）—— 说明**大多数"需旋转"照片的预览其实没被转错**。
但最低值差了一个数量级（0.383 vs **0.062**）：

```text
确认·方向正常(o=1)      n=253  中位 0.775  最低 0.383
确认·方向需旋转(o=6/8)  n=55   中位 0.771  最低 0.062
```

**即：只有一部分照片的预览真的转错了，转错的那批人脸分数直接崩到底。**
`0.062 / 0.101 / 0.151 / 0.179 / 0.185` 这几张全在 `orientation=8` 的文件里。

---

## 四、修复怎么做（等 `E:` 盘上线）

**顺序不能颠倒：**

1. **核实**：读一张 `orientation=8` 的 NEF，比对"内嵌 JPEG 自带的
   EXIF Orientation"与 `sizes.flip`，确认哪个可信。**不要跳过这一步** ——
   现在是"预览确实错了"（已证实）+"映射公式错了"（推测），两者要分开确认。
2. **修 `_apply_raw_flip` 的调用条件**：内嵌预览路径不该按 `sizes.flip` 转
   （LibRaw 明确说预览是 as-is，方向由它自己的 EXIF 决定）。
3. **只重生成受影响的那批** preview/thumb（`orientation ∈ {6,8}`，3,861 张），
   不要全库重跑 —— 全库 42,680 张派生图，没必要。
4. **重跑人脸**：

   ```text
   photoos people detect   # 预览换了，坐标系变了，必须重新检测
   photoos people embed    # 重算 128 维特征
   photoos people learn    # 从你的确认/否决重新估计原型与阈值
   ```

   ⚠️ **不能只跑 `embed`**：`face.landmarks_json` 存的是**检测坐标系**下的
   15 浮点，预览一转坐标系就全变了。`embed_rows()` 的"不用重新检测"
   只适用于**换了 SFace 版本但预览没变**的场景。

5. **重算场景分组**（`scenes rebuild`）—— 如果第 3 步确实换了像素。
   但按 CLIP 的实测，这一步对结果几乎没影响，属于"顺手做掉"。

预期效果：person 17 的负余量应当收窄（那 5 张崩到底的确认样本回到正常区间），
阈值与召回随之改善。

---

## 五、可复用的两条经验

1. **两个模型对同一种输入扰动的敏感度可以差一个数量级。**
   "图片方向不对"对 CLIP 是无所谓（0.98），对 SFace 是致命的（0.105）。
   所以"这个缺陷影响哪些功能"**必须逐模型实测**，不能一概而论。
   （实测脚本留在 `_probe/clip_rotation.py` 与 `_probe/face_rotation.py`。）

2. **"预览类"派生数据的方向是单点故障。**
   preview / thumb 一旦方向错，下游**全部**模型输入都错，
   而且**错得没有报错**——检测器照样给出 0.87 的高置信度、框也框得正正好好，
   只是特征崩了。这类"沉默的输入错误"只能靠**看**（把图下下来看）发现，
   靠日志和断言都发现不了。

---

返回 [`README.md`](README.md)
