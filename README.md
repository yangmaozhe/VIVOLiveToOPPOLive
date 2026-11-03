# vivo 实况照片转 OPPO 相册可识别格式

把 vivo（OriginOS）拍摄的实况照片，批量转换成 OPPO / 一加（ColorOS 15+）相册能识别并播放的实况照片。

- 只做重新封装，**不重新编码视频**，画质无损
- 支持单目录批量，也支持递归整个相册并保持原有目录结构
- 支持断点续跑，中断后重跑不会重做已完成的文件

## 背景：两家格式不一样

| | vivo（OriginOS） | OPPO / 一加（ColorOS 15+） |
|---|---|---|
| 文件形态 | `IMG_xxx.jpg` + `IMG_xxx.mp4` 两个独立文件 | 单个 `IMG_xxx.jpg`，mp4 追加在 JPEG 的 EOI 之后 |
| 关联方式 | 相册按同名文件配对 | XMP 元数据（`GCamera:MotionPhoto`、`OpCamera:OLivePhotoVersion=2`、`Container:Directory`）+ mp4 moov 里的 `lpex` 盒子 |

直接把 vivo 的 jpg+mp4 拷到 OPPO 上，相册只会当成“一张静态图 + 一个视频”。本工具把两者合并成 OPPO 需要的单文件格式。

## 环境要求

- Windows（脚本本身跨平台，路径示例按 Windows 写）
- Python 3.9+
- 依赖：
  ```powershell
  python -m pip install pillow
  ```
- 可选（只有需要转换 HEIC 时才用）：
  ```powershell
  python -m pip install pillow-heif
  ```

## 快速开始

### 1. 先跑演示，确认环境正常

`demo/` 下是一组可直接运行的示例（图片和视频为合成素材，非真实照片）：

```
demo
├─ vivo_live
│  ├─ IMG_20240101_120000.jpg      原图
│  └─ IMG_20240101_120000.mp4      同名视频
└─ output_oppo_live_result
   └─ IMG_20240101_120000.jpg      转换后的实况图（演示结果）
```

```powershell
python vivo2oppo_live.py "demo/vivo_live" -o "demo/output_oppo_live_result"
```

转换后 `IMG_20240101_120000.jpg` 从 0.33MB 变成约 1.97MB（原图 + 1.56MB 视频合成在一个文件里），可以直接拷到 OPPO 上看效果。

### 2. 转换自己的某个文件夹

```powershell
python vivo2oppo_live.py "D:\vivo导出\Camera" -o "D:\oppo\Camera"
```

默认只处理该目录**当前一层**的 jpg/jpeg，不递归。

### 3. 递归整个相册，保持目录结构（推荐）

```powershell
python vivo2oppo_live.py "D:\vivo相册" -o "D:\oppo相册" --mirror
```

先看计划、不写文件：

```powershell
python vivo2oppo_live.py "D:\vivo相册" -o "D:\oppo相册" --mirror --dry-run
```

### 4. 顺带把 HEIC 转成 JPEG

```powershell
# 只转“有配对 mp4”的 HEIC，并做成实况（推荐）
python vivo2oppo_live.py "D:\vivo相册" -o "D:\oppo相册" --mirror --heic-to-jpeg

# 所有 HEIC 都转成 JPEG
python vivo2oppo_live.py "D:\vivo相册" -o "D:\oppo相册" --mirror --heic-to-jpeg all
```

### 5. 中断后续跑

```powershell
python vivo2oppo_live.py "D:\vivo相册" -o "D:\oppo相册" --mirror --heic-to-jpeg --resume
```

参数可自由组合，例如：

```powershell
python vivo2oppo_live.py "D:\vivo相册" -o "D:\oppo相册" --mirror --heic-to-jpeg all --resume
```

## 两种模式的区别

| | 单目录（默认） | `--mirror` |
|---|---|---|
| 扫描范围 | 只处理输入目录第一层 | 递归所有子文件夹 |
| 目录结构 | 扁平输出 | 与源目录一一对应 |
| 普通照片 / PNG / HEIC | 不处理 | 原样复制 |
| 孤立 mp4 | 不处理 | 原样复制，避免丢视频 |
| 适合 | 只转某一批实况图 | 整库迁移到 OPPO |

## 参数说明

| 参数 | 说明 |
|---|---|
| `src` | 输入目录，默认 `vivo_live` |
| `-o, --out` | 输出目录，默认 `oppo_ready`；不能位于输入目录内部 |
| `--mirror` | 递归处理所有子文件夹并保持目录结构，非实况/无法转换的文件原样复制 |
| `--heic-to-jpeg [live\|all]` | 把 HEIC/HEIF 转成 JPEG（重新编码一次）。`live`（默认）= 只转带配对 mp4 的并做成实况；`all` = 全部 HEIC 都转。仅 `--mirror` 下有效 |
| `--resume` | 断点续跑：输出目录里已完整生成的文件跳过。仅 `--mirror` 下有效 |
| `--cover` | 封面帧位置，取视频时长的比例，0~1，默认 `0.4` |
| `--dry-run` | 只打印处理计划，不写任何文件 |
| `--no-lpex` | 不写入 `lpex` 盒子，只写 XMP。仅供排障，正常不要用 |

## 处理规则（`--mirror` 模式）

| 源文件情况 | 输出结果 |
|---|---|
| jpg + 同名 mp4 | 合并为单个 OPPO 实况 jpg；原 jpg 和 mp4 **都不输出** |
| jpg 没有同名 mp4 | 原样复制 |
| png / heic / 其他文件 | 原样复制（字节一致，保留修改时间） |
| 孤立 mp4（没有同名 jpg） | 原样复制，避免丢失普通视频 |
| jpg 本身已是实况照片 | 原样复制，不再重复叠加视频；同名 mp4 不复制 |
| `.nomedia` | 跳过（复制过去会让 OPPO 相册整目录隐藏） |
| 转换失败 | 原 jpg 原样复制，并在结尾列出失败明细 |
| HEIC + 同名 mp4（未开 `--heic-to-jpeg`） | 两者都原样复制，并在结尾提示 |

HEIC 说明：

- OPPO 的实况容器只能承载 JPEG，所以 HEIC 需要先转 JPEG 一次（`--heic-to-jpeg`）。
- 转换会保留 EXIF（拍摄时间、机型等）、ICC 色彩配置和原文件修改时间，并按照 EXIF 方向把像素转正。
- 同目录已存在同名 jpg（如 `IMG_1.jpg` 与 `IMG_1.heic` 并存）时，HEIC 不转换、原样复制并提示，避免相互覆盖。
- HEIC→JPEG 是**重新编码**（quality 95），10bit HDR 会变成 8bit SDR，这部分画质不可逆。

## 断点续跑与数据安全

- 所有输出都是**先写 `xxx.part`，校验通过后再改名**为正式文件，程序中途被杀不会留下半截图片/半截实况图。
- `--resume` 跳过前会校验完整性：
  - 实况图：XMP 中的视频长度、文件尾部 EOI 位置、视频 `ftyp` 头是否对得上；
  - 原样复制的文件：大小必须与源文件一致。
  校验不通过视为未完成，会重新生成。
- `--resume` 只按“输出是否已完整”判断。**改了参数想全部重做时不要加 `--resume`**，或换一个输出目录。
- 中断后输出目录可能残留 `*.part`，可直接删除，不影响续跑。
- 源文件全程只读，不会修改或删除。

## 输出自检

每个实况文件写完都会自检，任一项不通过则该文件按失败处理（原图原样复制，结尾列出）：

- JPEG 能被解码（Pillow）
- mp4 盒子结构完整、总长度与视频长度一致
- 视频轨道存在
- `stco/co64` 的 chunk 偏移不越界
- XMP 中的视频长度与实际追加的数据长度一致

## 在 OPPO 手机上查看

1. 只拷贝输出目录里的 **jpg**（转换过的实况照片不需要再带 mp4）。
2. 建议放到 `内部存储/DCIM/Camera/`，数据线或文件管理器均可。
3. 打开相册下拉刷新；如仍不显示，重启一次让媒体库重建。

## 常见问题

**Q：转出来的文件在 OPPO 相册里还是静态图？**
确认文件是转换得到的单文件 jpg（体积应明显大于原图，包含视频），且拷贝时没有被其他工具重新压缩或剥离元数据。另外部分 ColorOS 版本会校验 EXIF 里的 OPPO 私有标记，若遇到可反馈，需要再加一个兼容变体。

**Q：为什么单目录模式没有生成结果？**
默认模式只处理输入目录第一层，且必须 jpg 与 mp4 同名同目录。子文件夹或散落的文件请用 `--mirror`。

**Q：为什么不去动 PNG / HEIC 本身？**
ColorOS 的实况容器是 JPEG + 追加 mp4，PNG/HEIC 无法直接承载。不做无谓的重新编码，避免画质损失。

**Q：会不会改动原文件？**
不会。所有写入都发生在输出目录，源文件只读。

**Q：封面帧不准怎么办？**
用 `--cover` 调整比例（默认 0.4，即视频 40% 处），例如 `--cover 0.5`。

## 文件说明

- `vivo2oppo_live.py`：主脚本
- `README.md`：本文档
- `demo/vivo_live/`：演示素材（一张 jpg + 同名 mp4）
- `demo/output_oppo_live_result/`：演示转换结果，可随时删除后用命令重新生成
