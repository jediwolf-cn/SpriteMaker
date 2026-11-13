"""精灵帧后处理：Alpha 清理、公共裁剪、脚底锚点对齐、纯色背景 / 色键、spritesheet / GIF 打包。

依赖 Pillow（requirements.txt）。视频切帧由 ffmpeg_tools.py 负责。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable

try:
    from PIL import Image, ImageChops, ImageStat
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("缺少 Pillow，请执行 pip install Pillow") from exc

PAD = 8
TRANS_INDEX = 255          # GIF 全局调色板里固定留给透明的索引


def _noop(_: str) -> None:
    pass


def rgb_of(value: str) -> tuple[int, int, int]:
    """#RRGGBB → (r,g,b)，无法解析时返回绿幕色。"""
    s = str(value or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except (ValueError, IndexError):
        return 0, 255, 0


def alpha_info(path: Path) -> dict:
    """统计 PNG 的透明通道情况，用于质检。"""
    with Image.open(path) as im:
        im = im.convert("RGBA")
        a = im.getchannel("A")
        hist = a.histogram()
        total = im.width * im.height
        opaque = sum(hist[200:]) / total
        transparent = hist[0] / total
        bbox = a.point(lambda v: 255 if v > 12 else 0).getbbox()
        return {
            "width": im.width, "height": im.height,
            "has_alpha": True,
            "transparent_ratio": round(transparent, 4),
            "opaque_ratio": round(opaque, 4),
            "bbox": list(bbox) if bbox else None,
        }


def _clean_alpha(im: Image.Image, threshold: int) -> Image.Image:
    """把低透明度噪点彻底抹掉，避免抠背景后的灰边。"""
    a = im.getchannel("A")
    a = a.point(lambda v: 0 if v < threshold else v)
    im.putalpha(a)
    return im


def _alpha_bbox(im: Image.Image, threshold: int) -> tuple[int, int, int, int] | None:
    return im.getchannel("A").point(lambda v: 255 if v > threshold else 0).getbbox()


def _common_bbox(frames: list[Image.Image], threshold: int) -> tuple[int, int, int, int]:
    """所有帧 alpha 包围盒的并集 = 全组统一的公共裁剪框。

    逐帧各自裁剪会让角色在帧间左右/上下跳（每帧留白不同）；
    用同一个矩形裁所有帧，帧间相对位置才完全一致。
    """
    boxes = [b for b in (_alpha_bbox(f, threshold) for f in frames) if b]
    if not boxes:
        f = frames[0]
        return 0, 0, f.width, f.height
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _fill_color(im: Image.Image, color: str) -> tuple[int, int, int]:
    """留白区该铺什么颜色。

    有真实 Alpha 的图按配置色铺；已经是纯色底的图按**实测**背景色铺——
    模型出的绿几乎不是 #00FF00（实测常是 (39,185,0)），硬铺配置色会在
    缩小的角色周围留一圈颜色不同的矩形边，视频模型会把它画成边框。
    """
    alpha = im.getchannel("A")
    if sum(alpha.histogram()[:13]) > im.width * im.height * 0.02:
        return rgb_of(color)
    got, spread = detect_background(im)
    return got if spread <= 30 else rgb_of(color)


def _mask_canvas(im: Image.Image, tol: int = 26) -> Image.Image:
    """前景遮罩：有真实 Alpha 就按 Alpha，纯色底就按实测背景色差值。"""
    alpha = im.getchannel("A")
    if sum(alpha.histogram()[:13]) > im.width * im.height * 0.02:
        return alpha.point(lambda v: 255 if v > 12 else 0)
    rgb = im.convert("RGB")
    bg = detect_background(im)[0]                    # 实测背景色，不认配置值
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg))
    r, g, b = diff.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b).point(
        lambda v: 255 if v > tol else 0)


def content_box(src: Path, *, tol: int = 26) -> tuple[int, int, int, int]:
    """角色在画面里实际占的矩形（左, 上, 右, 下），用于看离四边还有多少余量。"""
    with Image.open(src) as f:
        im = f.convert("RGBA")
        return _mask_canvas(im, tol).getbbox() or (0, 0, im.width, im.height)


def edge_gap(src: Path, *, tol: int = 26) -> tuple[int, int, int, int]:
    """角色离画面左 / 上 / 右 / 下四边还剩多少像素：0 表示已经贴边、被切掉了。"""
    with Image.open(src) as f:
        w, h = f.size
    x0, y0, x1, y1 = content_box(src, tol=tol)
    return (x0, y0, w - x1, h - y1)


def _clean_pad(pad) -> tuple[float, float, float, float]:
    """把四向留白规范成 0~0.45 的比例（左, 上, 右, 下）。"""
    vals = list(pad or (0, 0, 0, 0))
    vals += [0] * (4 - len(vals))
    return tuple(max(0.0, min(0.45, float(v or 0))) for v in vals[:4])


def background_drift(sources, *, tol: int = 60, step: int = 4
                     ) -> tuple[Path, tuple[int, int, int], tuple[int, int, int]] | None:
    """找出背景色偏离首帧最多的一帧（抽样检查，返回空表示没漂）。

    视频模型偶尔会在动作中把纯色底漂成另一种颜色（实测绿底整段变紫），
    这时色键去背景会把整片“紫底”当前景留下来，切出来的帧全废。
    所以切帧后先抽样量一下，漂了就直接提醒重新生成，别等到抠图才发现。
    """
    items = list(sources)
    if len(items) < 2:
        return None
    with Image.open(items[0]) as f:
        base = detect_background(f)[0]
    worst: tuple[Path, tuple[int, int, int], tuple[int, int, int]] | None = None
    worst_d = 0
    for src in items[::max(1, step)]:
        with Image.open(src) as f:
            got = detect_background(f)[0]
        d = max(abs(a - b) for a, b in zip(base, got))
        if d > worst_d:
            worst, worst_d = (src, base, got), d
    return worst if worst_d > tol else None


def canvas_size(src: Path, pad=(0.12, 0.12, 0.12, 0.12), *, step: int = 32
                ) -> tuple[int, int]:
    """按四向留白算出**扩展后**的画布尺寸（左, 上, 右, 下各扩 pad 比例）。

    对齐到 step 的倍数：视频工作流的 ImageScaleToTotalPixels 用
    resolution_steps=32 出图，先对齐，成片分辨率才和这里的画布对得上，
    宽高比也不会被取整带歪。
    """
    with Image.open(src) as f:
        w, h = f.size
    left, top, right, bottom = _clean_pad(pad)
    nw = max(1, round(w * (1 + left + right)))
    nh = max(1, round(h * (1 + top + bottom)))
    if step > 1:
        nw = max(step, round(nw / step) * step)
        nh = max(step, round(nh / step) * step)
    return nw, nh


def compose_frame(src: Path, dest: Path, color: str, *,
                  pad=(0.12, 0.12, 0.12, 0.12), step: int = 32) -> Path:
    """用背景色**往外扩画布**，单帧原样贴回去，四边各多出 pad 比例的底色。

    视频模型只做动作、不取景：首帧里角色顶满画面时，一跳或一挥砍，手脚和
    武器就直接出画，切出来的帧缺手缺脚也补不回来。做法是给画面加空间，而
    不是把角色缩小——缩小等于白丢分辨率，而扩画布之后视频分辨率会跟着
    画布一起变大，角色在成片里的清晰度一点没降。

    pad 全为 0 时等同于原样铺底（不改尺寸、不改构图）。
    """
    with Image.open(src) as f:
        im = f.convert("RGBA")
    w, h = im.size
    left, top, right, bottom = _clean_pad(pad)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if left + top + right + bottom <= 0:
        canvas = Image.new("RGBA", (w, h), (*_fill_color(im, color), 255))
        canvas.alpha_composite(im)
        canvas.convert("RGB").save(dest, "PNG")
        return dest

    nw, nh = canvas_size(src, pad, step=step)
    canvas = Image.new("RGBA", (nw, nh), (*_fill_color(im, color), 255))
    # 角色的像素一个都不动（不缩放、不重绘），但位置重新摆：
    # 按四向留白把角色居中到新画布上。原图里角色本来就偏一点（模型经常画歪），
    # 直接照搬会让某一侧白白少掉几十像素空间，扩画布就白扩了。
    x0, y0, x1, y1 = _mask_canvas(im).getbbox() or (0, 0, w, h)
    core = im.crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)))
    px = round(_share(nw, core.width, left, right))
    py = round(_share(nh, core.height, top, bottom))
    canvas.alpha_composite(core, (max(0, px), max(0, py)))
    canvas.convert("RGB").save(dest, "PNG")
    return dest


def _share(total: float, size: float, head: float, tail: float) -> float:
    """一维上的起始坐标：剩余空间按 head:tail 分配，两边都为 0 时居中。"""
    extra = max(0.0, total - size)
    s = head + tail
    return extra * (head / s if s > 0 else 0.5)


def same_aspect(src: Path, size: tuple[int, int], *, tol: float = 0.02) -> bool:
    """图片比例是否和目标画布一致（容差 2%）：一致就不用重画，直接复制。"""
    with Image.open(src) as f:
        w, h = f.size
    if not w or not h:
        return False
    return abs(w / h - size[0] / size[1]) <= tol * (size[0] / size[1])


def count_subjects(src: Path, color: str | None = None, *, tol: int = 26) -> int:
    """数一张图里有几个并排的角色，只用于质检。

    图生图偶尔会无视提示词，把并排的多个人物画进来，这时输出废了必须重试。
    判定方式是按列统计前景，数被空隙隔开的几段——同一角色身上的细窄缝隙
    （飘带、武器）忽略不计。

    背景色默认从画面四边实测：模型出片的绿几乎不会是 #00FF00
    （实测常是 (39,185,0)），拿配置色硬比会把整张图都算成前景。
    """
    with Image.open(src) as f:
        im = f.convert("RGBA")
    alpha = im.getchannel("A")
    # 只有「确实有一片透明区」才按 Alpha 数；全不透明的 PNG 里偶尔混进
    # 几个 254 的像素，只看最小值会把整张图当成一个角色
    if sum(alpha.histogram()[:13]) > im.width * im.height * 0.02:
        mask = alpha.point(lambda v: 255 if v > 12 else 0)
    else:
        rgb = im.convert("RGB")
        bg = detect_background(im)[0]                    # 实测背景色，不认配置值
        diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg))
        r, g, b = diff.split()
        mask = ImageChops.lighter(ImageChops.lighter(r, g), b).point(
            lambda v: 255 if v > tol else 0)
    w, h = mask.size
    sw = min(600, w)
    sh = max(1, round(h * sw / w))
    raw = mask.resize((sw, sh), Image.Resampling.BILINEAR).tobytes()
    occupied = [max(raw[x::sw]) > 0 for x in range(sw)]

    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x, filled in enumerate(occupied):
        if filled and start is None:
            start = x
        elif not filled and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, sw))

    min_gap, min_width = max(2, round(sw * 0.02)), sw * 0.04
    merged: list[tuple[int, int]] = []
    for a, b in runs:
        if merged and a - merged[-1][1] < min_gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return len([r for r in merged if r[1] - r[0] >= min_width])


def detect_background(im: Image.Image) -> tuple[tuple[int, int, int], float]:
    """从画面四边取小块估算实际背景色，并返回样本离散度。

    视频模型常把 #00FF00 渲染成 (0,185,0) 之类的近邻色，
    拿配置色直接做色键会差几十个色阶，必须按出片实测取色。
    """
    rgb = im.convert("RGB")
    w, h = rgb.size
    k = max(4, min(w, h) // 16)
    boxes = [(0, 0, k, k), (w - k, 0, w, k), (0, h - k, k, h), (w - k, h - k, w, h),
             (w // 2 - k // 2, 0, w // 2 + k // 2, k),
             (w // 2 - k // 2, h - k, w // 2 + k // 2, h),
             (0, h // 2 - k // 2, k, h // 2 + k // 2),
             (w - k, h // 2 - k // 2, w, h // 2 + k // 2)]
    samples = [tuple(round(v) for v in ImageStat.Stat(rgb.crop(box)).median) for box in boxes]
    bg = tuple(sorted(s[i] for s in samples)[len(samples) // 2] for i in range(3))
    spread = max(max(abs(s[i] - bg[i]) for i in range(3)) for s in samples)
    return bg, spread



def chroma_key(src: Path, dest: Path, color: str | None = None,
               tolerance: int = 60, *, despill: bool = True,
               log: Callable[[str], None] = _noop) -> Path:
    """色键去背景：把接近背景色的像素变透明，边缘柔和过渡并反算真实色。

    color=None 时自动从画面边缘取样背景色（推荐）；传 #RRGGBB 则强制指定。
    仅在背景确实保持纯净时可靠，默认流水线仍走图生图重绘。
    """
    with Image.open(src) as f:
        im = f.convert("RGBA")
    if color:
        bg_rgb, spread = rgb_of(color), 0.0
    else:
        bg_rgb, spread = detect_background(im)
    if spread > tolerance / 2:
        log(f"  ! {src.name} 背景色取样离散度 {spread:.0f} 偏大（可能有渐变/场景），色键结果可能不干净")

    bg = Image.new("RGB", im.size, bg_rgb)
    diff = ImageChops.difference(im.convert("RGB"), bg)
    r, g, b = diff.split()
    dist = ImageChops.lighter(ImageChops.lighter(r, g), b)   # 最大通道差，近似色距上界
    lo = max(0, int(tolerance * 0.55))
    hi = max(lo + 1, int(tolerance))
    alpha = dist.point(
        lambda v: 0 if v <= lo else (255 if v >= hi else int((v - lo) * 255 / (hi - lo))))

    if despill:
        # 半透明边缘是「前景与背景的混合色」，直接保留会有绿边；按 alpha 反算前景色
        px, apx = im.load(), alpha.load()
        cr, cg, cb = bg_rgb
        for y in range(im.height):
            for x in range(im.width):
                a = apx[x, y]
                if 0 < a < 255:
                    t = a / 255.0
                    r0, g0, b0, _ = px[x, y]
                    px[x, y] = (max(0, min(255, round((r0 - (1 - t) * cr) / t))),
                                max(0, min(255, round((g0 - (1 - t) * cg) / t))),
                                max(0, min(255, round((b0 - (1 - t) * cb) / t))), a)
    im.putalpha(alpha)
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "PNG")
    log(f"  色键基准色 RGB{bg_rgb}，边缘离散度 {spread:.0f}")
    return dest


def _fit_size(target: tuple[int, int] | None, ref_w: int, ref_h: int) -> tuple[int, int, float]:
    """按参考包围矩形算出画布尺寸与统一缩放系数。"""
    if target and target[0] > 0 and target[1] > 0:
        cw, ch = int(target[0]), int(target[1])
        scale = min(cw / max(ref_w + PAD * 2, 1), ch / max(ref_h + PAD * 2, 1))
    else:
        cw, ch = ref_w + PAD * 2, ref_h + PAD * 2
        scale = 1.0
    return cw, ch, scale


def raw_ref(paths: list[Path], *, threshold: int = 12) -> tuple[int, int] | None:
    """一组去背景原图裁掉透明边后的参考尺寸（宽, 高）。

    逐张读包围盒取**并集**——和 normalize_groups 里的公共裁剪框口径完全一致，
    但不常驻内存：只重绘某一段视频时，用它给「统一基准」补上其它组。
    """
    x0 = y0 = None
    x1 = y1 = 0
    for p in paths:
        try:
            with Image.open(p) as src:
                im = _clean_alpha(src.convert("RGBA"), threshold)
                box = _alpha_bbox(im, threshold)
        except (OSError, ValueError):
            continue
        if not box:
            continue
        x0 = box[0] if x0 is None else min(x0, box[0])
        y0 = box[1] if y0 is None else min(y0, box[1])
        x1, y1 = max(x1, box[2]), max(y1, box[3])
    if x0 is None or x1 <= x0 or y1 <= y0:
        return None
    return x1 - x0, y1 - y0


def normalize_groups(groups: dict[str, list[Path]], out_root: Path, *,
                     target: tuple[int, int] | None = None,
                     align: str = "bottom-center",
                     trim: bool = True, threshold: int = 12,
                     per_group_scale: bool = False,
                     extra_refs: dict[str, tuple[int, int]] | None = None,
                     log: Callable[[str], None] = _noop) -> dict[str, list[Path]]:
    """按帧组分别规范化，写到 out_root/<组名>/（组名为 "" 时直接写 out_root）。

    组内：所有帧共用同一个公共裁剪框。逐帧独立裁剪会让角色在帧间左右上下跳。
    组间：默认共用同一个缩放基准（取全部组里最大的包围矩形）；否则「攻击」这种大动作
          会把画布占满，「走路」却缩得很小，游戏里切换动作时角色会忽然变大变小。
          需要每组各自适配画布时传 per_group_scale=True。
    extra_refs：不参与本次规范化、但必须计入统一基准的其它组参考尺寸
          （只处理某一段视频时，用它保证新产物和其它组的大小仍然一致）。
    """
    loaded: dict[str, list[Image.Image]] = {}
    for gid, paths in groups.items():
        frames: list[Image.Image] = []
        for p in paths:
            with Image.open(p) as src:
                frames.append(_clean_alpha(src.convert("RGBA"), threshold))
        if frames:
            loaded[gid] = frames
    if not loaded:
        return {}

    cropped: dict[str, list[Image.Image]] = {}
    notes: dict[str, str] = {}
    for gid, frames in loaded.items():
        if trim:
            box = _common_bbox(frames, threshold)
            cropped[gid] = [f.crop(box) for f in frames]
            notes[gid] = f"，公共裁剪框 {box}"
        else:
            cropped[gid] = frames
            notes[gid] = "，未裁剪"

    if per_group_scale:
        refs = {gid: (max(f.width for f in fs), max(f.height for f in fs))
                for gid, fs in cropped.items()}
    else:
        extra = list((extra_refs or {}).values())
        ref_w = max([f.width for fs in cropped.values() for f in fs] + [w for w, _ in extra])
        ref_h = max([f.height for fs in cropped.values() for f in fs] + [h for _, h in extra])
        refs = {gid: (ref_w, ref_h) for gid in cropped}
        if extra:
            log(f"统一基准：纳入另外 {len(extra)} 组的参考尺寸，基准 {ref_w}×{ref_h}")

    result: dict[str, list[Path]] = {}
    for gid, frames in cropped.items():
        out_dir = (out_root / gid) if gid else out_root
        out_dir.mkdir(parents=True, exist_ok=True)
        cw, ch, scale = _fit_size(target, *refs[gid])
        written: list[Path] = []
        for src_path, frame in zip(groups[gid], frames):
            if abs(scale - 1.0) > 1e-6:
                nw, nh = max(1, round(frame.width * scale)), max(1, round(frame.height * scale))
                frame = frame.resize((nw, nh), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            x = (cw - frame.width) // 2
            y = (ch - frame.height - PAD) if align == "bottom-center" else (ch - frame.height) // 2
            canvas.alpha_composite(frame, (max(0, x), max(0, min(y, ch - frame.height))))
            dest = out_dir / src_path.name
            canvas.save(dest, "PNG")
            written.append(dest)
        log(f"[{gid or '旧版帧序列'}] 已对齐 {len(written)} 帧 → 画布 {cw}×{ch}，"
            f"缩放系数 {scale:.3f}，锚点 {align}{notes[gid]}")
        result[gid] = written
    return result


def normalize_frames(paths: list[Path], out_dir: Path, *,
                     target: tuple[int, int] | None = None,
                     align: str = "bottom-center",
                     trim: bool = True, threshold: int = 12,
                     log: Callable[[str], None] = _noop) -> list[Path]:
    """单组版本（保留旧接口）：一组帧直接写到 out_dir。"""
    got = normalize_groups({"": list(paths)}, out_dir, target=target, align=align,
                           trim=trim, threshold=threshold, log=log)
    return got.get("", [])


def make_spritesheet(paths: list[Path], out: Path, *, columns: int = 8,
                     fps: int = 12, log: Callable[[str], None] = _noop) -> dict:
    """横向排布的 spritesheet + 同名 .json 元数据（帧矩形、播放时长）。"""
    if not paths:
        raise RuntimeError("没有可打包的精灵帧")
    cols = max(1, int(columns))
    with Image.open(paths[0]) as first:
        fw, fh = first.size
    rows = math.ceil(len(paths) / cols)
    sheet = Image.new("RGBA", (fw * cols, fh * rows), (0, 0, 0, 0))
    meta_frames = []
    for i, p in enumerate(paths):
        with Image.open(p) as src:
            im = src.convert("RGBA")
            if im.size != (fw, fh):
                im = im.resize((fw, fh), Image.Resampling.LANCZOS)
        x, y = (i % cols) * fw, (i // cols) * fh
        sheet.alpha_composite(im, (x, y))
        meta_frames.append({"index": i, "file": p.name, "x": x, "y": y, "w": fw, "h": fh})
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "PNG")
    meta = {
        "image": out.name,
        "frame_width": fw, "frame_height": fh,
        "columns": cols, "rows": rows, "count": len(paths),
        "duration_ms": round(1000 / max(1, int(fps))),
        "frames": meta_frames,
    }
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"spritesheet → {out.name}（{cols} 列 × {rows} 行，{len(paths)} 帧）")
    return meta


def make_gif(paths: list[Path], out: Path, *, fps: int = 12,
             log: Callable[[str], None] = _noop) -> Path:
    """循环 GIF 预览（透明背景）。

    旧实现每帧各自做自适应量化，帧间调色板不同、transparency 索引漂移，
    播放时背景会在透明 / 黑色之间闪。这里改成：
      1) 全组帧合成出一张样本，量化出唯一的 255 色全局调色板；
      2) 索引 255 固定保留给透明（调色板里放一个几乎不会出现的品红）；
      3) 每帧都映射到同一张调色板，透明区强制写 255；
      4) optimize=False，避免 Pillow 重排调色板，disposal=2 每帧清背景。
    """
    if not paths:
        raise RuntimeError("没有可生成 GIF 的精灵帧")
    src_frames: list[Image.Image] = []
    for p in paths:
        with Image.open(p) as f:
            src_frames.append(f.convert("RGBA"))
    w, h = src_frames[0].size

    # 1) 全组统一调色板：网格拼样，帧数多时每帧仍保留足够像素
    cols = math.ceil(math.sqrt(len(src_frames)))
    rows = math.ceil(len(src_frames) / cols)
    sample = Image.new("RGB", (w * cols, h * rows), (0, 0, 0))
    for i, f in enumerate(src_frames):
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 255))
        layer.alpha_composite(f)
        sample.paste(layer.convert("RGB"), ((i % cols) * w, (i // cols) * h))
    sample.thumbnail((512, 512), Image.Resampling.LANCZOS)
    quantized = sample.quantize(colors=255, method=Image.Quantize.MEDIANCUT,
                                dither=Image.Dither.NONE)
    palette255 = (quantized.getpalette() or [])[:255 * 3]
    palette255 += [0] * (255 * 3 - len(palette255))
    full_palette = palette255 + [255, 0, 255]      # 索引 255 = 透明哨兵色
    pal_img = Image.new("P", (1, 1))
    pal_img.putpalette(full_palette)

    # 2) 每帧映射到同一张调色板 + 固定透明索引
    gif_frames: list[Image.Image] = []
    for f in src_frames:
        layer = Image.new("RGBA", f.size, (0, 0, 0, 255))
        layer.alpha_composite(f)
        q = layer.convert("RGB").quantize(palette=pal_img, dither=Image.Dither.NONE)
        raw = bytearray(q.tobytes())
        mask = f.getchannel("A").point(lambda v: 255 if v < 128 else 0).tobytes()
        for i, m in enumerate(mask):
            if m:
                raw[i] = TRANS_INDEX
        frame = Image.frombytes("P", f.size, bytes(raw))
        frame.putpalette(full_palette)
        frame.info["transparency"] = TRANS_INDEX
        gif_frames.append(frame)

    out.parent.mkdir(parents=True, exist_ok=True)
    gif_frames[0].save(
        out, format="GIF", save_all=True, append_images=gif_frames[1:],
        duration=round(1000 / max(1, int(fps))), loop=0,
        disposal=2, transparency=TRANS_INDEX, optimize=False,
    )
    log(f"循环预览 GIF → {out.name}（{len(gif_frames)} 帧 @ {fps}fps，统一调色板）")
    return out
