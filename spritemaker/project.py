"""项目状态：阶段产物落盘、断点续跑、帧选择、任务日志。

每个角色一个目录：
  projects/<name>/project.json
    00_source/  01_reference/  02_keyframe/  03_video/  04_frames/
    05_sprites_raw/ 06_sprites/ export/  logs/
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from . import config
from . import __version__

STAGE_DIRS = {
    "source": "00_source",           # 用户上传的角色底图（可选图生图输入）
    "reference": "01_reference",     # 一张正面全身立绘
    "keyframe": "02_keyframe",       # 每个方向一张单帧立绘（视频首/尾帧都从这里选）
    "video": "03_video",
    "frames": "04_frames",
    "sprites_raw": "05_sprites_raw",
    "sprites": "06_sprites",
    "export": "export",
}
# 可执行的生成阶段（顺序即流水线顺序）；"select" 只是界面上的挑帧步骤
STAGES = ("reference", "keyframe", "video", "frames", "sprites", "export")
# 删除某一步产物时要一并作废的下游阶段：上游没了，下游产物必然是过期的
CLEAR_CASCADE = {
    "reference": ("keyframe", "video", "frames", "sprites", "export"),
    "keyframe": ("video", "frames", "sprites", "export"),
    "video": ("frames", "sprites", "export"),
    "frames": ("sprites", "export"),
    "sprites": ("export",),
    "export": (),
}
# 每个阶段实际要清的目录（⑥ 的去背景原图与成品分两个目录）
STAGE_CLEAR_DIRS = {
    "reference": ("reference",), "keyframe": ("keyframe",), "video": ("video",),
    "frames": ("frames",), "sprites": ("sprites_raw", "sprites"), "export": ("export",),
}
# 按组删除：组名 = 视频段 id。只清这一组自己在这一步的产物 + 它自己的下游
GROUP_CLEAR_DIRS = {
    "frames": (("frames", "frames"), ("sprites_raw", "sprites"),
               ("sprites", "sprites"), ("export", "export")),
    "sprites": (("sprites_raw", "sprites"), ("sprites", "sprites"), ("export", "export")),
    "export": (("export", "export"),),
}
MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mkv", ".mov", ".zip"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov"}
# 导出目录除了媒体文件还有 spritesheet.json（引擎用的帧矩形元数据）
EXPORT_EXTS = MEDIA_EXTS | {".json"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ProjectError(RuntimeError):
    pass


def root_dir() -> Path:
    p = config.path("projects_dir")
    p.mkdir(parents=True, exist_ok=True)
    return p


def valid_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fa5]+", "_", (name or "").strip())
    if not s:
        raise ProjectError("项目名不能为空")
    return s[:60]


class Project:
    def __init__(self, name: str, *, create: bool = False, params: dict | None = None):
        self.name = valid_name(name)
        self.dir = root_dir() / self.name
        self.file = self.dir / "project.json"
        if create:
            if self.file.exists():
                raise ProjectError(f"项目「{self.name}」已存在")
            self.dir.mkdir(parents=True, exist_ok=True)
            defaults = config.default_params()
            defaults.update(params or {})
            self.data = {
                "name": self.name,
                "version": __version__,
                "created": _now(),
                "updated": _now(),
                "params": defaults,
                "stages": {k: {"status": "idle"} for k in STAGES},
                "selection": [],
                "jobs": [],
            }
            self.save()
        else:
            if not self.file.exists():
                raise ProjectError(f"找不到项目「{self.name}」")
            self.data = json.loads(self.file.read_text(encoding="utf-8"))
        for sub in STAGE_DIRS.values():
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        (self.dir / "logs").mkdir(exist_ok=True)
        self._migrate_keyframes()
        self._ensure_stages()
        self._migrate_flat_groups()

    # ------------------------------------------------------------ 基本
    @property
    def params(self) -> dict:
        return self.data["params"]

    def save(self) -> None:
        self.data["updated"] = _now()
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.file)

    def dir_of(self, stage: str) -> Path:
        if stage not in STAGE_DIRS:
            raise ProjectError(f"未知阶段：{stage}")
        p = self.dir / STAGE_DIRS[stage]
        p.mkdir(parents=True, exist_ok=True)
        return p

    def update_params(self, patch: dict) -> None:
        for k, v in (patch or {}).items():
            self.params[k] = v
        self.save()

    def resolve(self, rel: str) -> Path:
        """把项目内相对路径解析成绝对路径，禁止越界访问。"""
        p = (self.dir / str(rel)).resolve()
        if self.dir.resolve() not in p.parents and p != self.dir.resolve():
            raise ProjectError("路径越界")
        return p

    # ------------------------------------------------------------ 阶段状态
    def _migrate_flat_groups(self) -> None:
        """旧版本把多组去背景结果平铺为「组名__帧名.png」，与根目录旧版帧混在一起。

        升级后统一移进 06_sprites/<组名>/，让「一段视频 = 一个目录」成立；
        只搬前缀能对上已知视频段的文件，其余一律不动。
        """
        groups = {g["id"] for g in self.frame_groups() if g["id"]}
        if not groups:
            return
        for stage in ("sprites_raw", "sprites"):
            root = self.dir_of(stage)
            moved = 0
            for p in list(root.glob("*__frame_*.png")):
                grp, _, name = p.name.partition("__")
                if grp not in groups:
                    continue
                dest = root / grp
                dest.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(p, dest / name)
                    moved += 1
                except OSError:
                    continue
            # 根目录的质检报告统计的是「混排后」的帧，搬走一部分后已经对不上，直接作废
            stale = root / "alpha_report.json"
            if moved and stale.is_file():
                stale.unlink()
            if moved and stage == "sprites":
                self.refresh_sprite_reports()

    def refresh_sprite_reports(self) -> int:
        """按组重算 Alpha 质检报告：只读现有 PNG，不重新生成，代价很小。"""
        from . import imaging

        root = self.dir_of("sprites")
        dirs = [root] + [d for d in root.iterdir() if d.is_dir()]
        total = 0
        for d in dirs:
            files = sorted(d.glob("*.png"))
            if not files:
                continue
            report = {f.name: imaging.alpha_info(f) for f in files}
            (d / "alpha_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            total += len(files)
        return total

    def _ensure_stages(self) -> None:
        """旧项目的 stages 里没有新阶段（keyframe），补齐并清掉已废弃的阶段。"""
        stages = self.data.setdefault("stages", {})
        changed = False
        for dead in ("tune", "firstlast"):
            if stages.pop(dead, None) is not None:
                changed = True
        for key in STAGES:
            if key not in stages:
                stages[key] = {"status": "idle"}
                changed = True
        if changed:
            self.save()

    # ------------------------------------------------------------ 单帧（关键帧）库
    def _migrate_keyframes(self) -> None:
        """旧版 02_firstlast 里是「每组首尾两张」，新版只要单帧。

        把 `<组>_first.png` 改名为 `<组>.png`，丢掉与首帧重复的 `_last.png`，
        再把整个目录搬到 02_keyframe；pairs.json 已无意义，一并清掉。
        """
        old = self.dir / "02_firstlast"
        if not old.is_dir():
            return
        for f in list(old.glob("*_first.png")):
            try:
                os.replace(f, old / (f.name[: -len("_first.png")] + ".png"))
            except OSError:
                pass
        for f in list(old.glob("*_last.png")):
            try:
                f.unlink()
            except OSError:
                pass
        stale = old / "pairs.json"
        if stale.is_file():
            try:
                stale.unlink()
            except OSError:
                pass
        new = self.dir / STAGE_DIRS["keyframe"]
        new.mkdir(parents=True, exist_ok=True)
        for item in list(old.iterdir()):
            dest = new / item.name
            if dest.exists():
                continue
            try:
                os.replace(item, dest)
            except OSError:
                pass
        shutil.rmtree(old, ignore_errors=True)

    def keys_meta(self) -> dict:
        """读取 02_keyframe/keys.json：正面立绘基准 + 每张单帧的元信息。"""
        f = self.dir_of("keyframe") / "keys.json"
        if not f.is_file():
            return {}
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        raw.setdefault("keys", [])
        return raw

    def keys(self) -> list[dict]:
        return [k for k in (self.keys_meta().get("keys") or [])
                if isinstance(k, dict) and k.get("file")]

    def save_keys(self, meta: dict) -> None:
        d = self.dir_of("keyframe")
        (d / "keys.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self.save()

    def delete_key(self, key_id: str) -> dict:
        """删掉一张单帧（连同它的图），其它帧不受影响。"""
        meta = self.keys_meta()
        hit = next((k for k in meta.get("keys") or [] if k.get("id") == key_id), None)
        if hit is None:
            raise ProjectError("找不到这张单帧")
        d = self.dir_of("keyframe")
        f = d / str(hit.get("file") or "")
        if f.is_file() and f.parent == d:
            try:
                f.unlink()
            except OSError:
                pass                      # 图被占用时留个孤儿文件无妨，清单里已经不引用它了
        meta["keys"] = [k for k in meta["keys"] if k.get("id") != key_id]
        self.save_keys(meta)
        patch = {}
        if str(self.params.get("first_frame") or "") == key_id:
            patch["first_frame"] = ""
        if str(self.params.get("last_frame") or "") == key_id:
            patch["last_frame"] = ""
        if patch:
            self.update_params(patch)
        return meta

    def stage_begin(self, stage: str) -> dict:
        st = self.data["stages"].setdefault(stage, {})
        st.update(status="running", started=_now(), error=None)
        self.save()
        return st

    def stage_done(self, stage: str, **extra) -> None:
        st = self.data["stages"].setdefault(stage, {})
        st.update(status="done", finished=_now(), error=None, **extra)
        self.save()

    def stage_fail(self, stage: str, error: str) -> None:
        st = self.data["stages"].setdefault(stage, {})
        st.update(status="failed", finished=_now(), error=str(error)[:1000])
        self.save()

    def add_job(self, job: dict) -> None:
        self.data["jobs"].append({**job, "at": _now()})
        self.data["jobs"] = self.data["jobs"][-60:]
        self.save()

    def set_selection(self, files: list[str]) -> list[str]:
        """selection 存「相对 04_frames/ 的路径」（如 run_front_2026.../frame_0003.png）。

        旧项目里存的是纯文件名 frame_0003.png，这里按根目录帧自动补成合法相对路径，
        保证升级后已有选择不会丢。
        """
        root = self.dir_of("frames")
        lookup: dict[str, str] = {}
        for p in root.rglob("*.png"):
            rel = p.relative_to(root).as_posix()
            lookup[rel] = rel
            lookup.setdefault(p.name, rel)
        picked = []
        for f in dict.fromkeys(str(x).replace("\\", "/") for x in files):
            rel = lookup.get(f)
            if rel and rel not in picked:
                picked.append(rel)
        self.data["selection"] = picked
        self.save()
        return picked

    def frame_path(self, rel: str) -> Path:
        """把 selection 里的相对路径解析到 04_frames/ 下的实际文件。"""
        p = (self.dir_of("frames") / str(rel).replace("\\", "/")).resolve()
        d = self.dir_of("frames").resolve()
        if d not in p.parents:
            raise ProjectError("帧路径越界")
        return p

    @staticmethod
    def sprite_name(rel: str) -> str:
        """分组帧路径 → 扁平且唯一的输出名（旧版命名，仅用于兼容已有产物）。"""
        s = str(rel).replace("\\", "/").lstrip("/")
        return s.replace("/", "__") if "/" in s else s

    @staticmethod
    def group_of(rel: str) -> str:
        """帧 / 产物的相对路径 → 所属组名（= 视频段 id）；根目录产物属于 ""（旧版）。"""
        s = str(rel).replace("\\", "/").lstrip("/")
        return s.split("/")[0] if "/" in s else ""

    def group_label(self, gid: str) -> str:
        """组名 → 中文显示名：优先取视频段的 label，其次帧组记录，最后退回 id。"""
        if not gid:
            return "旧版帧序列"
        for r in self.video_records():
            rid = str(r.get("id") or Path(str(r.get("file") or "")).stem)
            if rid == gid:
                return str(r.get("label") or gid)
        for g in self.frame_groups():
            if g["id"] == gid and g.get("label"):
                return str(g["label"])
        return gid

    # ------------------------------------------------------------ 视频段
    def video_records(self) -> list[dict]:
        """读取 03_video/videos.json；旧项目没有清单时按目录里的视频自动生成。"""
        d = self.dir_of("video")
        idx = d / "videos.json"
        records: list[dict] = []
        if idx.is_file():
            try:
                raw = json.loads(idx.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    records = [r for r in raw if isinstance(r, dict) and r.get("file")]
            except (OSError, ValueError):
                records = []
        known = {str(r.get("file")) for r in records}
        # 兼容手工放进目录 / 旧版生成的视频：补一条最小记录
        for p in sorted(d.glob("*")):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and p.name not in known:
                records.append({
                    "id": p.stem, "file": p.name, "label": p.stem,
                    "action": "", "direction": "", "workflow": "",
                    "created": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "legacy": True,
                })
        records = [r for r in records if (d / str(r["file"])).is_file()]
        records.sort(key=lambda r: (str(r.get("created") or ""), str(r.get("file") or "")))
        for r in records:
            f = self._file_entry(d / str(r["file"]), self.dir)
            r["url"], r["size"] = f["url"], f["size"]
        return records

    def save_video_records(self, records: list[dict]) -> None:
        clean = [{k: v for k, v in r.items() if k not in ("url", "size")} for r in records]
        (self.dir_of("video") / "videos.json").write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_video_record(self, record: dict) -> list[dict]:
        records = [r for r in self.video_records() if r.get("file") != record.get("file")]
        records.append(record)
        self.save_video_records(records)
        return self.video_records()

    def rename_video(self, filename: str, label: str) -> list[dict]:
        records = self.video_records()
        hit = next((r for r in records if r.get("file") == filename), None)
        if hit is None:
            raise ProjectError("找不到该视频段")
        hit["label"] = str(label).strip()[:80] or hit.get("label", filename)
        self.save_video_records(records)
        return self.video_records()

    def update_video_seed(self, filename: str, seed: int) -> list[dict]:
        """改某段视频记录的种子（只改记录，不影响已出片的文件）。"""
        records = self.video_records()
        hit = next((r for r in records if r.get("file") == filename), None)
        if hit is None:
            raise ProjectError("找不到该视频段")
        hit["seed"] = int(seed)
        self.save_video_records(records)
        return self.video_records()

    def delete_video(self, filename: str) -> None:
        """删除一段视频及其专属帧组、选择项（不动其它视频段）。"""
        d = self.dir_of("video")
        target = (d / str(filename)).resolve()
        if d.resolve() not in target.parents or target.suffix.lower() not in VIDEO_EXTS:
            raise ProjectError("视频文件名非法")
        stem = target.stem
        for p in [target, d / f"{stem}.prompt.txt", d / f"{stem}_first.png",
                  d / f"{stem}_last.png"]:
            if p.is_file():
                p.unlink()
        self.save_video_records([r for r in self.video_records() if r.get("file") != filename])
        frames = self.dir_of("frames")
        group = frames / stem
        if group.is_dir():
            shutil.rmtree(group)
        # 去背景 / 导出也按组隔离，删段时一并清掉它专属的那一套
        for stage in ("sprites_raw", "sprites", "export"):
            root = self.dir_of(stage)
            gdir = root / stem
            if gdir.is_dir():
                shutil.rmtree(gdir)
            for p in root.glob(f"{stem}__*.png"):     # 旧版扁平命名（组名__帧名）
                p.unlink()
            for p in root.glob(f"{self.name}_{stem}_sprites.zip"):
                p.unlink()
        # 旧版 loop.mp4 的帧平铺在根目录，删段时一并清掉，避免残留孤儿帧
        legacy_flat = stem == "loop" and not group.is_dir()
        if legacy_flat:
            for p in frames.glob("frame_*.png"):
                p.unlink()
            mf = frames / "manifest.json"
            if mf.is_file():
                mf.unlink()
        self.data["selection"] = [f for f in (self.data.get("selection") or [])
                                  if not str(f).startswith(stem + "/")
                                  and not (legacy_flat and "/" not in str(f))]
        if str(self.params.get("frame_source") or "") == filename:
            self.params["frame_source"] = ""
        if str(self.params.get("frame_group") or "") == stem:
            self.params["frame_group"] = ""
        self.save()

    def frame_groups(self) -> list[dict]:
        """04_frames/ 下每个子目录 = 一段视频的专属帧组。"""
        root = self.dir_of("frames")
        out = []
        for d in sorted([p for p in root.iterdir() if p.is_dir()], key=lambda x: x.name):
            frames = sorted(d.glob("frame_*.png"))
            info: dict = {}
            mf = d / "manifest.json"
            if mf.is_file():
                try:
                    info = json.loads(mf.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            out.append({"id": d.name, "count": len(frames),
                        "source": info.get("source", ""), "updated": d.stat().st_mtime,
                        "label": info.get("label", "")})
        # 旧版平铺在根目录的帧也算一个组
        loose = sorted(root.glob("frame_*.png"))
        if loose:
            out.append({"id": "", "count": len(loose), "source": "loop.mp4",
                        "updated": max(f.stat().st_mtime for f in loose), "label": "旧版帧序列"})
        out.sort(key=lambda g: g["updated"], reverse=True)   # 最近切过的一组排最前
        return out

    # ------------------------------------------------------------ 删除产物
    def clear_stage(self, stage: str) -> dict:
        """删掉某一步的产物，并级联清掉依赖它的下游产物。

        只清 STAGE_DIRS 下的目录：project.json、logs、00_source（上传底图）都保留，
        所以清完可以直接重跑，不用重新上传素材。返回每步删了多少文件 / 字节。
        """
        if stage not in STAGES:
            raise ProjectError(f"未知阶段：{stage}")
        order = (stage, *CLEAR_CASCADE[stage])
        cleared = []
        for s in order:
            files = size = 0
            for name in STAGE_CLEAR_DIRS[s]:
                root = self.dir_of(name)
                for p in root.rglob("*"):
                    if p.is_file():
                        files += 1
                        try:
                            size += p.stat().st_size
                        except OSError:
                            pass
                for child in list(root.iterdir()):
                    try:
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink()
                    except OSError:
                        pass          # 被占用的文件删不掉，目录里已经不引用了
            cleared.append({"stage": s, "dirs": list(STAGE_CLEAR_DIRS[s]),
                            "files": files, "bytes": size})
            self.data["stages"][s] = {"status": "idle"}

        # 下游被清掉后，指向它们的参数必须复位，否则下一步会拿旧 id 找东西
        hit = set(order)
        patch: dict = {}
        if "keyframe" in hit:
            patch.update(first_frame="", last_frame="")
        if "video" in hit:
            patch.update(frame_source="")
        if "frames" in hit:
            patch.update(frame_group="")
            self.data["selection"] = []
        if "sprites" in hit:
            patch.update(sprite_group="")
        if "export" in hit:
            patch.update(export_group="")
        if patch:
            self.params.update(patch)
        self.save()
        return {"stage": stage, "cleared": cleared,
                "files": sum(c["files"] for c in cleared),
                "bytes": sum(c["bytes"] for c in cleared)}

    @staticmethod
    def _path_stats(path: Path) -> tuple[int, int]:
        """(文件数, 字节数)；目录按递归算，不存在返回 (0, 0)。"""
        if path.is_file():
            try:
                return 1, path.stat().st_size
            except OSError:
                return 1, 0
        if not path.is_dir():
            return 0, 0
        files = size = 0
        for p in path.rglob("*"):
            if p.is_file():
                files += 1
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
        return files, size

    def _reset_empty_stages(self, stages) -> None:
        """清完产物后，整步一个文件都不剩的阶段回到「未完成」。"""
        for s in stages:
            if s not in STAGES:
                continue
            files = self._path_stats(self.dir_of(s))[0]
            if s == "sprites":
                files += self._path_stats(self.dir_of("sprites_raw"))[0]
            if not files:
                self.data["stages"][s] = {"status": "idle"}

    def clear_group(self, stage: str, group: str) -> dict:
        """只删某一段视频（一个帧组）在本步的产物，并清掉它自己的下游。

        组名就是视频段 id：删「攻击」这一组的精灵帧，走路那组一个文件都不动。
        group 为空时退回整步删除。
        """
        group = str(group or "").replace("\\", "/").strip("/")
        if not group:
            return self.clear_stage(stage)
        if "/" in group or group in (".", ".."):
            raise ProjectError("组名非法")

        targets: list[tuple[str, Path]] = []      # (统计归属的阶段, 路径)
        video_file = ""
        if stage == "video":
            rec = next((r for r in self.video_records()
                        if str(r.get("id") or Path(str(r.get("file") or "")).stem) == group), None)
            if rec is None:
                raise ProjectError("找不到这段视频")
            video_file = str(rec["file"])
            stem = Path(video_file).stem
            vdir = self.dir_of("video")
            targets.append(("video", vdir / video_file))
            targets.append(("video", vdir / f"{stem}.prompt.txt"))
            for name, label in (("frames", "frames"), ("sprites_raw", "sprites"),
                                ("sprites", "sprites"), ("export", "export")):
                targets.append((label, self.dir_of(name) / stem))
        elif stage in GROUP_CLEAR_DIRS:
            targets += [(label, self.dir_of(name) / group)
                        for name, label in GROUP_CLEAR_DIRS[stage]]
        else:
            raise ProjectError("这一步不支持按组删除")

        cleared = []
        for label, path in targets:
            files, size = self._path_stats(path)
            if not files:
                continue
            cleared.append({"stage": label, "path": str(path.relative_to(self.dir)).replace("\\", "/"),
                            "files": files, "bytes": size})

        if video_file:
            self.delete_video(video_file)
        else:
            for _, path in targets:
                try:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    elif path.is_file():
                        path.unlink()
                except OSError:
                    pass

        # 只复位「真的空了」的阶段：其它组还有产物时这一步仍算已完成
        self._reset_empty_stages(("video", "frames", "sprites", "export"))

        if stage in ("video", "frames"):
            self.data["selection"] = [f for f in (self.data.get("selection") or [])
                                      if not str(f).startswith(group + "/")]
        # 任何指向这一组的参数都复位，避免下次执行拿着已删的组名去找东西
        for key in ("frame_group", "sprite_group", "export_group"):
            if str(self.params.get(key) or "") == group:
                self.params[key] = ""
        src = str(self.params.get("frame_source") or "")
        if src and Path(src).stem == group:
            self.params["frame_source"] = ""

        self.save()
        return {"stage": stage, "group": group, "cleared": cleared,
                "files": sum(c["files"] for c in cleared),
                "bytes": sum(c["bytes"] for c in cleared)}

    # ------------------------------------------------------------ 视图数据
    @staticmethod
    def _file_entry(p: Path, project_dir: Path, stage_dir: Path | None = None) -> dict:
        rel = p.relative_to(project_dir).as_posix()
        st = p.stat()
        # 项目名可能是中文或含特殊字符，URL 必须编码，否则会解析到错误项目。
        # v=文件版本：重新导出 GIF / spritesheet 后 URL 会变，浏览器不会拿旧的解码缓存。
        version = f"{int(st.st_mtime * 1000)}-{st.st_size}"
        entry = {
            "name": p.name, "path": rel,
            "url": f"/file?project={urllib.parse.quote(project_dir.name, safe='')}"
                   f"&path={urllib.parse.quote(rel, safe='/')}&v={version}",
            "size": st.st_size,
            "mtime": st.st_mtime,
        }
        if stage_dir is not None:
            inner = p.relative_to(stage_dir).as_posix()
            entry["rel"] = inner                       # 相对阶段目录，挑帧用
            entry["group"] = inner.split("/")[0] if "/" in inner else ""
        return entry

    def save_reference_image(self, data: bytes) -> dict:
        """保存上传的角色底图，统一转成 PNG 并开启「底图图生图」。"""
        import io
        from PIL import Image

        if not data:
            raise ProjectError("底图内容为空")
        if len(data) > 20 * 1024 * 1024:
            raise ProjectError("底图不能超过 20 MB")
        try:
            with Image.open(io.BytesIO(data)) as src:
                src.load()
                im = src.convert("RGBA")
        except Exception as exc:  # noqa: BLE001
            raise ProjectError(f"不是有效的图片文件：{exc}") from exc
        if max(im.size) > 4096:
            im.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        dest = self.dir_of("source") / "base.png"
        im.save(dest, "PNG")
        self.update_params({"reference_image": "00_source/base.png",
                            "reference_use_image": True})
        return self.manifest()

    # 这些阶段的产物按视频段分子目录存放，必须递归扫描
    GROUPED_STAGES = {"frames", "sprites_raw", "sprites", "export"}

    def files_of(self, stage: str, *, exts: set[str] | None = None) -> list[dict]:
        d = self.dir_of(stage)
        exts = exts or MEDIA_EXTS
        paths = d.rglob("*") if stage in self.GROUPED_STAGES else d.iterdir()
        return [self._file_entry(p, self.dir, d) for p in sorted(paths)
                if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(".")]

    def asset_groups(self, stage: str, *, exts: set[str] | None = None) -> list[dict]:
        """某阶段按组统计产物（去背景 / 导出都按组隔离）。

        返回 [{id, label, count, updated}]，id="" 表示旧版平铺在阶段根目录的产物。
        """
        root = self.dir_of(stage)
        exts = exts or (EXPORT_EXTS if stage == "export" else MEDIA_EXTS)
        skip = {"prompt.txt", "alpha_report.json", "manifest.json", "project.json"}

        def media(d: Path) -> list[Path]:
            return sorted(p for p in d.glob("*")
                          if p.is_file() and p.suffix.lower() in exts and p.name not in skip)

        # 与第 ③ 步的视频列表同序：新动作在前，旧版平铺组排最后
        order = {}
        for i, r in enumerate(self.video_records()):
            order[str(r.get("id") or Path(str(r.get("file") or "")).stem)] = i

        out = []
        loose = media(root)
        if loose:
            out.append({"id": "", "label": "旧版帧序列", "count": len(loose),
                        "updated": max(p.stat().st_mtime for p in loose)})
        for d in sorted([p for p in root.iterdir() if p.is_dir()], key=lambda x: x.name):
            files = media(d)
            if files:
                out.append({"id": d.name, "label": self.group_label(d.name), "count": len(files),
                            "updated": d.stat().st_mtime})
        out.sort(key=lambda g: (order.get(g["id"], -1), g["id"]), reverse=True)
        return out

    def manifest(self) -> dict:
        data = json.loads(json.dumps(self.data, ensure_ascii=False))
        data["files"] = {stage: self.files_of(
            stage, exts=EXPORT_EXTS if stage == "export" else None) for stage in STAGE_DIRS}
        data["paths"] = {stage: STAGE_DIRS[stage] for stage in STAGE_DIRS}
        # 种子可能超过 JS 的安全整数范围（2^53），转成字符串前端才不会显示错值
        data["videos"] = [{**r, "seed": str(r["seed"])} if r.get("seed") not in (None, "") else r
                          for r in self.video_records()]
        data["frame_groups"] = self.frame_groups()
        data["sprite_groups"] = self.asset_groups("sprites", exts={".png"})
        data["export_groups"] = self.asset_groups("export")
        data["keys"] = self.keys_meta()
        return data


def list_projects() -> list[dict]:
    out = []
    for p in sorted(root_dir().iterdir()):
        f = p / "project.json"
        if not f.is_file():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({
                "name": d.get("name", p.name),
                "created": d.get("created", ""),
                "updated": d.get("updated", ""),
                "character": d.get("params", {}).get("character", ""),
                "stages": {k: v.get("status", "idle") for k, v in (d.get("stages") or {}).items()},
            })
        except Exception:
            continue
    return out


def create_project(name: str, params: dict | None = None) -> Project:
    return Project(name, create=True, params=params)


def get_project(name: str) -> Project:
    return Project(name)


def delete_project(name: str) -> None:
    """删除项目目录（仅允许 projects/ 下的目录）。"""
    name = valid_name(name)
    p = root_dir() / name
    if not p.exists() or p.parent != root_dir():
        raise ProjectError("项目不存在或路径非法")
    shutil.rmtree(p)
