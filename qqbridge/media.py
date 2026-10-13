"""媒体：**QQ 那一头拿到的字节 → 变成大脑里能显示、能看懂的引用**。

参考项目（Derpyu520/qq-bridge）的做法是自己在桥里落盘、自己起静态服务、自己抽帧。
我们不这么干，理由有两条：

1. 前端聊天记录只认大脑那套附件引用（`/api/files/<session>/…` + 缩略图 + 画廊 + 真删级联）。
   桥自己另存一份，等于把图片管理劈成两半，删消息时文件就漏了。
2. 抽帧 / 转码这类活大脑已经写好了（`/api/media/video/analyze`，ffmpeg 抽 ≤8 帧）。
   重复实现一遍只会两处不一致。

所以这里的每个函数都只做一件事：**下载 → 交给大脑 → 拿回引用**。

下载安全：只允许 http / https / file 三种协议，且限制体积；URL 由**网关**给出
（不是模型、不是用户文本），但仍然做 https 之外的体积与超时兜底。
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

from . import config as config_mod
from .brain import BrainClient, BrainError

# 附件管道的上限（图片 20MB / 视频 100MB）。语音更小：QQ 语音条通常几十秒。
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_VOICE_BYTES = 25 * 1024 * 1024

VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def tmp_dir() -> Path:
    path = config_mod.STATE_DIR / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def download(url: str, *, max_bytes: int, ext: str = "") -> Path:
    """把网关给的媒体拿到本地。返回一个**临时文件**，由调用方负责删。

    认四种形态（各家网关给的不一样，见 README）：
    - `http(s)://…` 下载地址（最常见）
    - `file:///C:/…` 本地绝对路径
    - `C:/…` / `/home/…` 裸本地路径（Windows 上 `urlparse` 会把 `C:` 当成协议名，得单独认）
    - 纯文件名 → 不要（那要调 `get_image` 换 URL，见 OneBotClient.get_media_url）

    安全：本地路径这种形态等于「按路径读文件」。**网关是同一台机器上我们自己起的进程**
    （还有 access_token 挡着），它本来就有本地读写权；这里只是不额外扩大权限，不做路径白名单
    —— 网关给的目录在哪只有网关自己知道。
    """
    parsed = urlparse(url or "")
    dest = tmp_dir() / f"{uuid.uuid4().hex[:12]}{ext}"
    raw = (url or "").strip().strip('"')
    # 裸 Windows 路径 / POSIX 绝对路径：先看它是不是真文件，免得和 http 分支搅在一起。
    if parsed.scheme in ("", "file") or (len(parsed.scheme) == 1 and raw[1:3] in (":\\", ":/")):
        source = Path(urllib.request.url2pathname(parsed.path)) if parsed.scheme == "file" else Path(raw)
        if source.is_file():
            if source.stat().st_size > max_bytes:
                raise ValueError(f"文件太大（{source.stat().st_size // 1024 // 1024}MB）")
            shutil.copy2(source, dest)
            return dest
        if parsed.scheme == "file":
            raise ValueError(f"网关给的本地文件不存在：{source.name}")
    if parsed.scheme in ("http", "https"):
        request = urllib.request.Request(url, headers={"User-Agent": "xiaotangren-qq-bridge/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - 见模块开头
                size = int(response.headers.get("Content-Length") or 0)
                if size and size > max_bytes:
                    raise ValueError(f"文件太大（{size // 1024 // 1024}MB）")
                with dest.open("wb") as fp:
                    total = 0
                    while chunk := response.read(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("文件太大")
                        fp.write(chunk)
        except OSError as exc:
            _cleanup(dest)
            raise ValueError(f"下载失败：{exc}") from exc
        return dest
    raise ValueError(f"不认识的媒体地址：{url or '(空)'}")


def data_url(path: Path) -> str:
    """本地图片 → data URL（**必须带 `data:image/…;base64,` 前缀**，裸 base64 会被中转打 400）。"""
    import base64

    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


def upload_data_url(brain: BrainClient, session_id: str, url: str, *, source: str = "qq") -> str:
    """data URL → 附件管道 → 引用 URL。抽帧封面走这里（库里只存引用，不存一大坨 base64）。"""
    import base64

    head, _, payload = url.partition(",")
    mime = head.split(":")[1].split(";")[0] if ":" in head else "image/jpeg"
    raw = base64.b64decode(payload)
    suffix = mimetypes.guess_extension(mime) or ".jpg"
    path = tmp_dir() / f"frame-{uuid.uuid4().hex[:12]}{suffix}"
    path.write_bytes(raw)
    try:
        return str(brain.upload_attachment(session_id, path, source=source).get("orig") or "")
    finally:
        _cleanup(path)


# ---- 进出两个方向 ----

def ingest(brain: BrainClient, session_id: str, url: str, *, kind: str) -> tuple[dict, Path]:
    """QQ 来的媒体 → 大脑附件管道。返回 `(落盘结果, 本地临时文件)`。

    kind: `image`（普通图）/ `sticker`（表情包）/ `video`。**只落盘，不解析**。
    本地临时文件交给调用方 —— 送模型要 data URL，同一个文件没必要再从网上下一遍。
    """
    if kind == "video":
        limit, source = MAX_VIDEO_BYTES, "qq"
    elif kind == "sticker":
        limit, source = 5 * 1024 * 1024, "sticker"
    else:
        limit, source = MAX_IMAGE_BYTES, "qq"
    mime = mimetypes.guess_type(urlparse(url).path)[0] or ("video/mp4" if kind == "video" else "image/png")
    path = download(url, max_bytes=limit, ext=".mp4" if kind == "video" else (mimetypes.guess_extension(mime) or ".png"))
    try:
        return brain.upload_attachment(session_id, path, source=source), path
    except BrainError:
        _cleanup(path)
        raise


def video_frames(brain: BrainClient, url: str) -> tuple[list[str], str]:
    """QQ 来的视频 → 本地抽帧 → data URL 列表（喂视觉路）。返回 (frames, task_id)。

    大脑的抽帧有硬边界：**≤15 秒 / ≤50MB**，且要装 ffmpeg。QQ 里转发的视频经常超，
    所以这里失败是**正常路径** —— 调用方拿到空列表就退化成「[视频]」占位，不报错。
    """
    import httpx

    path = download(url, max_bytes=50 * 1024 * 1024, ext=".mp4")
    try:
        with path.open("rb") as fp:
            response = httpx.post(
                brain.base_url + "/api/media/video/analyze",
                files={"file": (path.name, fp, "video/mp4")},
                timeout=90.0,
            )
        if response.status_code != 200:
            return [], ""
        info = response.json()
        out: list[str] = []
        for frame in (info.get("frames") or [])[:8]:
            # 帧就落在大脑的 data/media_tasks/<task>/frames 下，直接从本地读，
            # 不必绕 HTTP 再下一次（那条路还是自己写的解析，少一层错）。
            resolved = _frame_to_path(frame)
            if resolved:
                out.append(data_url(resolved))
        return out, str(info.get("task_id") or "")
    except (httpx.HTTPError, ValueError, OSError):
        return [], ""
    finally:
        _cleanup(path)


def _frame_to_path(frame_url: str) -> Path | None:
    """`/api/media-files/<task>/frames/<name>` → 本地文件（抽帧就落在后端 data 下）。"""
    parts = [p for p in str(frame_url).split("/") if p]
    if len(parts) < 5 or parts[0] != "api" or parts[1] != "media-files":
        return None
    backend_root = Path(__file__).resolve().parent.parent.parent.parent / "backend"
    path = backend_root / "data" / "media_tasks" / parts[2] / "frames" / parts[-1]
    return path if path.is_file() else None


# ---- 语音 ----

def prepare_audio(path: Path) -> tuple[Path, bool]:
    """音频 → 适合送 ASR 的格式。返回 `(文件, 是不是新产生的)`。

    Edge 的 mp3 / piper 的 wav 本来就能直接转写；QQ 网关给的可能是 amr / silk，
    那才需要 ffmpeg 转 16k 单声道 wav。**没有 ffmpeg 也照送原始文件** ——
    上游多半自己会处理，硬拦下来反而连试都没法试。
    """
    if path.suffix.lower() == ".wav":
        return path, False
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return path, False
    dest = path.with_suffix(".16k.wav")
    try:
        subprocess.run(  # noqa: S603
            [ffmpeg, "-y", "-i", str(path), "-ar", "16000", "-ac", "1", str(dest)],
            capture_output=True, timeout=60, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        _cleanup(dest)
        return path, False
    return (dest, True) if dest.is_file() else (path, False)


def voice_seconds(path: Path) -> float:
    """语音时长（秒）。ffprobe 不在就返回 0（调用方不拿它做硬判断）。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        out = subprocess.run(  # noqa: S603
            [ffprobe, "-v", "error", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20, check=True,
        )
        return float((json.loads(out.stdout).get("format") or {}).get("duration") or 0)
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError, OSError):
        return 0.0


def speak(brain: BrainClient, text: str, *, engine: str, voice: str) -> Path:
    """让小糖人出声 → 落一个本地音频文件（QQ 网关按 `file://` 读它发语音条）。"""
    audio, ext = brain.tts_bytes(text, engine=engine, voice=voice)
    path = tmp_dir() / f"voice-{uuid.uuid4().hex[:12]}.{ext}"
    path.write_bytes(audio)
    return path


def transcribe(brain: BrainClient, path: Path, cfg: dict) -> str:
    """一段语音文件 → 文字。

    **本地没有 ASR**（本体的语音模块只有 TTS）：所以这里必须有一个能听的引擎，
    见 config.json 的 `asr`。engine=none 时直接返回空串 —— 调用方按「[语音]」处理，
    不会假装听懂。engine=openai 时走 OpenAI 兼容的 `/audio/transcriptions`
    （base_url/api_key 留空就用大脑给本功能配的那一份中转）。

    拿到的可能是 amr / silk（QQ 语音条），先用 ffmpeg 转 16k 单声道 wav 再送 ——
    ffmpeg 不在就直接送原始文件，让上游自己判断。
    """
    asr = cfg.get("asr") or {}
    if not asr.get("enabled", True) or str(asr.get("engine") or "none") == "none":
        return ""
    seconds = voice_seconds(path)
    if seconds and seconds > float(asr.get("max_seconds") or 120):
        return ""
    audio, produced = prepare_audio(path)
    try:
        base_url = str(asr.get("base_url") or "")
        api_key = str(asr.get("api_key") or "")
        if not base_url or not api_key:
            provider = brain.api_config()
            base_url = base_url or str(provider.get("base_url") or "")
            api_key = api_key or str(provider.get("api_key") or "")
        return brain.transcribe(audio, base_url=base_url, api_key=api_key,
                                model=str(asr.get("model") or "whisper-1"),
                                language=str(asr.get("language") or "zh"))
    except (BrainError, ValueError):
        return ""
    finally:
        if produced:
            _cleanup(audio)