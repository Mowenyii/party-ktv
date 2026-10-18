"""局域网 KTV：电脑负责播放，手机扫码点歌。

视频不落盘：yt-dlp 只解析真实地址，ffmpeg 带上防盗链请求头实时拉流、
不转码地封装成 HLS 给浏览器播放，歌曲结束后临时分片立即删除。
"""
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import imageio_ffmpeg
import qrcode
import qrcode.image.svg
import uvicorn
import yt_dlp
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PORT = int(os.environ.get("KTV_PORT", "8000"))
MAX_HEIGHT = int(os.environ.get("KTV_MAX_HEIGHT", "720"))
PRELOAD = 1  # 除当前歌曲外，提前缓冲接下来几首，切歌时无需等待
# 手机上的互动按钮：鼓掌、口哨、烟花、点赞、比心、哈哈、送花、干杯
REACTIONS = {"clap", "whistle", "firework", "like", "heart", "haha", "flower", "cheers"}
REACT_LIMIT, REACT_WINDOW = 5, 3.0  # 每个连接每 3 秒最多 5 次，防止狂点刷屏
RESOLVE_TTL = 20 * 60  # 解析出的地址会过期，超过这个秒数就重新解析
TRANSIENT_ERRORS = ("timed out", "IncompleteRead", "TransportError", "Connection reset", "HTTP Error 5")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
HLS_ROOT = Path(tempfile.mkdtemp(prefix="ktv-hls-"))
URL_RE = re.compile(r"https?://[^\s<>\"'，。！？【】（）]+")
# HLS 用 MPEG-TS 分片，只能装 H.264 + AAC，所以优先选这两种编码
FORMAT = (
    f"bv*[vcodec^=avc][height<={MAX_HEIGHT}]+ba[acodec^=mp4a]"
    f"/b[vcodec^=avc][acodec^=mp4a][height<={MAX_HEIGHT}]/b"
)


def lan_ip():
    if os.environ.get("KTV_HOST"):
        return os.environ["KTV_HOST"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # UDP connect 不发包，只用来选出口网卡
        address = s.getsockname()[0]
        if not address.startswith(("127.", "169.254.")) and address != "0.0.0.0":
            return address
    except OSError:
        pass
    finally:
        s.close()
    # 默认路由不可用时，从主机名解析结果里找一个非回环 IPv4 地址；Windows、macOS、Linux 都支持。
    try:
        addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in addresses:
            address = info[4][0]
            if not address.startswith(("127.", "169.254.")) and address != "0.0.0.0":
                return address
    except OSError:
        pass
    return "127.0.0.1"


JOIN_URL = f"http://{lan_ip()}:{PORT}/"
# 代码版本：页面发现版本变了就自动刷新。断线自动重连会让旧页面一直连着新服务器跑旧代码
VERSION = hashlib.md5(b"".join(
    p.read_bytes() for p in [Path(__file__)] + sorted(STATIC.glob("*.html")) + [STATIC / "fx.js"])).hexdigest()[:8]
NO_CACHE = {"Cache-Control": "no-cache"}

# ---------------------------------------------------------------- 共享状态
# 歌曲 status: resolving 解析中 → queued 排队 → buffering 缓冲中 → ready 可播放；或 error
HISTORY_FILE = Path(os.environ.get("KTV_HISTORY", ROOT / "history.json"))
HISTORY_MAX = 100

lock = threading.RLock()
dirty = threading.Event()  # 状态变了，等广播协程推给所有客户端
state = {
    "current": None, "queue": [], "paused": False, "restart": 0,
    # volume_mode: system 调系统音量；player 调播放器音量（Windows、HDMI 等系统音量不可调时）
    "volume": None, "volume_mode": "system", "player_volume": 100,
    "history": [],
    "player_id": None,  # 只让一个播放窗口工作：最后打开或最后点“开始”的那个
}
relays = {}  # qid -> Relay
resolved = {}  # qid -> (解析时间, [(地址, 请求头, 视频编码), ...])
volume_lock = threading.Lock()  # 连点音量键时，保证“读当前值 → 写新值”不交错


def same_video(a, b):
    """同一个视频可能来自不同链接（B 站每次分享的 b23.tv 短链都不同），优先比平台+视频 ID；
    旧记录没存 ID，就退回比链接或“标题+UP 主”。"""
    if a.get("vid") and b.get("vid"):
        return a["vid"] == b["vid"]
    return a.get("url") == b.get("url") or (
        bool(a.get("title")) and (a.get("title"), a.get("uploader")) == (b.get("title"), b.get("uploader")))


def dedupe(history):
    """去重，每个视频只留最新的一条（列表本身按时间从新到旧排列）。"""
    kept = []
    for h in history:
        if not any(same_video(h, k) for k in kept):
            kept.append(h)
    return kept[:HISTORY_MAX]


def save_history():
    try:
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state["history"], ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, HISTORY_FILE)  # 原子替换：写到一半崩溃也不会留下损坏的文件
    except OSError:
        pass


def load_history():
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(history, list):
        history = []
    valid = [h for h in history if isinstance(h, dict) and isinstance(h.get("url"), str)]  # 丢掉损坏的条目
    state["history"] = dedupe(valid)
    if len(state["history"]) != len(history):
        save_history()


def record_history(song):
    entry = {k: song.get(k) for k in ("url", "vid", "title", "uploader", "duration", "thumbnail", "by")}
    entry["played_at"] = time.time()
    state["history"] = dedupe([entry] + state["history"])
    save_history()


def new_song(url, by):
    return {
        "qid": uuid.uuid4().hex[:8], "url": url, "vid": None, "title": url, "uploader": "",
        "duration": None, "thumbnail": "", "by": by or "匿名",
        "status": "resolving", "error": "",
    }


def set_song(song, **fields):
    with lock:
        song.update(fields)
    dirty.set()


def all_songs():
    return ([state["current"]] if state["current"] else []) + state["queue"]


def find(qid):
    return next((s for s in all_songs() if s["qid"] == qid), None)


def advance():
    """当前歌曲结束，换成队列里第一首没出错的。"""
    with lock:
        if state["current"] and state["current"]["status"] == "ready":  # 只记真正播过的，没解析完就切掉的不算
            record_history(state["current"])
        for i, s in enumerate(state["queue"]):
            if s["status"] != "error":
                state["current"] = state["queue"].pop(i)
                break
        else:
            state["current"] = None
        state["paused"] = False
        schedule()


def fail(song, message):
    with lock:
        song.update(status="error", error=message)
        if state["current"] is song:  # 正在唱的歌失败了：放回队首留着重试，先切下一首
            state["queue"].insert(0, song)
            advance()
        schedule()


def schedule():
    """停掉已离开歌单的中转，给当前和接下来 PRELOAD 首启动中转。"""
    with lock:
        alive = {s["qid"] for s in all_songs()}
        for qid in [q for q in relays if q not in alive]:
            relays.pop(qid).stop()
        for qid in [q for q in resolved if q not in alive]:
            del resolved[qid]
        upcoming = [s for s in all_songs() if s["status"] != "error"][: 1 + PRELOAD]
        for s in upcoming:
            if s["status"] == "queued" and s["qid"] not in relays:
                relays[s["qid"]] = Relay(s)
        dirty.set()


def system_volume():
    """读取 macOS 输出音量；其他系统或不支持调节的输出设备返回 None。"""
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["osascript", "-e", "output volume of (get volume settings)"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return int(r.stdout) if r.stdout.strip().isdigit() else None


def set_system_volume(value):
    """尝试设置 macOS 系统音量；不支持时让调用方改用播放器音量。"""
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(["osascript", "-e", f"set volume output volume {value}"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def change_volume(delta):
    with volume_lock:
        current = system_volume()
        if current is not None and delta:
            new_volume = max(0, min(100, current + delta))
            current = new_volume if set_system_volume(new_volume) else None
        with lock:
            if current is None:  # 系统音量调不了，改调播放器音量
                state["volume_mode"] = "player"
                state["player_volume"] = max(0, min(100, state["player_volume"] + delta))
                state["volume"] = state["player_volume"]
            else:
                state["volume_mode"] = "system"
                state["volume"] = current
        dirty.set()


def snapshot():
    with lock:
        return json.dumps({"type": "state", **state, "join_url": JOIN_URL, "version": VERSION}, ensure_ascii=False)


# ---------------------------------------------------------------- 解析与中转
def clean_error(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()
    if "412" in msg:
        return "B 站暂时限制了请求，稍后点重试"
    if "deleted or geo-restricted" in msg or "Unsupported URL" in msg:
        return "视频不存在、已删除，或不是支持的视频链接"
    if "Sign in" in msg or "not a bot" in msg:
        return "YouTube 要求登录，见 README 里的 KTV_COOKIES_FROM_BROWSER"
    return msg[:150]


def resolve(song):
    """解析标题等信息和真实播放地址，只拿地址不下载。"""
    opts = {"format": FORMAT, "noplaylist": True, "quiet": True, "no_warnings": True, "socket_timeout": 10}
    if os.environ.get("SSL_CERT_FILE"):
        opts["compat_opts"] = {"no-certifi"}  # 用 SSL_CERT_FILE 指定的证书（公司代理需要）
    if os.environ.get("KTV_COOKIES_FROM_BROWSER"):
        opts["cookiesfrombrowser"] = (os.environ["KTV_COOKIES_FROM_BROWSER"],)
    for attempt in range(3):  # B 站接口偶尔超时或只返回一半，多是一时的网络抖动，稍等重试
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(song["url"], download=False)
            break
        except yt_dlp.utils.DownloadError as e:
            if attempt == 2 or not any(k in str(e) for k in TRANSIENT_ERRORS):
                raise
            time.sleep(1 + 2 * attempt)
    if "entries" in info:
        raise ValueError("这是合集/播放列表，请贴单个视频的链接")
    inputs = []
    for f in info.get("requested_formats") or [info]:
        headers = dict(f.get("http_headers") or {})
        if "bilibili" in info.get("extractor", "").lower():
            headers.setdefault("Referer", "https://www.bilibili.com/")
        inputs.append((f["url"], headers, f.get("vcodec") or ""))
    # B 站 App 分享的 b23 短链会带 p=1，yt-dlp 给 ID 加上 _p1；单 P 视频要和网页链接算同一个
    vid = re.sub(r"_p1$", "", f"{info.get('extractor_key')}:{info.get('id')}")
    set_song(
        song, vid=vid,
        title=info.get("title") or song["url"], uploader=info.get("uploader") or "",
        duration=info.get("duration"), thumbnail=info.get("thumbnail") or "",
    )
    with lock:
        resolved[song["qid"]] = (time.time(), inputs)
    return inputs


def fetch_meta(song):
    """点歌后立刻解析一次：尽早显示标题，坏链接马上报错。"""
    try:
        resolve(song)
    except Exception as e:
        fail(song, clean_error(e))
        return
    with lock:
        if song["status"] == "resolving":
            song["status"] = "queued"
        schedule()


def read_playlist(path):
    """返回 [(时长, 分片文件名), ...]。"""
    try:
        text = path.read_text()
    except OSError:
        return []
    return [(float(d), name) for d, name in re.findall(r"#EXTINF:([\d.]+),\s*\n(\S+)", text)]


def write_playlist(path, entries, ended):
    """原子地重写播放列表，避免播放器读到写了一半的文件。"""
    lines = [
        "#EXTM3U", "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{math.ceil(max((d for d, _ in entries), default=4))}",
        "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:EVENT",
    ]
    for d, name in entries:
        lines += [f"#EXTINF:{d:.6f},", name]
    if ended:
        lines.append("#EXT-X-ENDLIST")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)


def playlist_info(path):
    """返回 (分片数, 已产出秒数)。"""
    entries = read_playlist(path)
    return len(entries), sum(d for d, _ in entries)


class Relay:
    """把一首歌实时中转成 HLS 分片，断流时重拉补齐，最多重试 3 次。

    断流后不用 -ss 从断点续拉：不转码时视频只能从断点前的关键帧开始、音频却精确从断点开始，
    衔接处会出现没声音的分片和时间戳重置，浏览器播到那里就卡死。改为从头重拉到单独目录——
    同样的输入从头切片，切点和时间戳与第一次完全一致——再把超出现有进度的分片接到主播放列表后面。
    """

    def __init__(self, song):
        self.song = song
        self.dir = HLS_ROOT / song["qid"]
        self.playlist = self.dir / "index.m3u8"
        self.proc = None
        self.stopped = False
        self.guard = threading.Lock()  # 保证 stop() 之后不会再拉起新的 ffmpeg
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        proc = None
        with self.guard:
            self.stopped = True
            if self.proc and self.proc.poll() is None:
                proc = self.proc
                try:
                    proc.kill()
                except OSError:
                    pass
        # Windows 在子进程完全退出前会锁住它正在写的分片，先等句柄释放再清理目录。
        if proc:
            try:
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def _inputs(self, fresh):
        with lock:
            cached = resolved.get(self.song["qid"])
        if fresh or not cached or time.time() - cached[0] > RESOLVE_TTL:
            return resolve(self.song)
        return cached[1]

    def _command(self, inputs, out_dir):
        cmd = [FFMPEG, "-v", "error", "-y"]
        for url, headers, _ in inputs:
            extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k.lower() != "user-agent")
            if extra:
                cmd += ["-headers", extra]
            if headers.get("User-Agent"):
                cmd += ["-user_agent", headers["User-Agent"]]
            # B 站 CDN 偶尔会连接不断却不再发数据：rw_timeout 让 ffmpeg 8 秒没数据就断开，从断点续传，
            # 否则会一直干等、画面卡死。服务器返回 5xx 或网络出错时也自动重连
            cmd += ["-rw_timeout", "8000000", "-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_on_network_error", "1", "-reconnect_on_http_error", "5xx",
                    "-reconnect_delay_max", "10"]
            cmd += ["-i", url]
        if len(inputs) > 1:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        if any(vcodec.startswith("avc") for _, _, vcodec in inputs):
            # YouTube 有些分片以非 IDR 关键帧开头、不带 SPS/PPS，单独解码不了，拖进度条跳到那里会卡住。
            # 在每个关键帧前补上参数头，让每个分片都能独立起播。
            cmd += ["-bsf:v", "h264_mp4toannexb,dump_extra=freq=keyframe"]
        return cmd + [
            "-c", "copy", "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
            "-hls_playlist_type", "event",
            # 结束标记由我们在确认拉完后再写：ffmpeg 出错退出时也会写，播放器会把断点当成歌曲结尾
            "-hls_flags", "omit_endlist",
            "-hls_segment_filename", str(out_dir / "seg%05d.ts"),
            str(out_dir / "index.m3u8"),
        ]

    def _merge(self, retry_dir):
        """把重拉目录里超出主播放列表进度的分片接到主播放列表后面。"""
        main, fresh = read_playlist(self.playlist), read_playlist(retry_dir / "index.m3u8")
        n = len(main)
        if len(fresh) <= n:
            return
        if n and abs(fresh[n - 1][0] - main[n - 1][0]) > 0.05:
            raise RuntimeError("重拉到的视频和之前的对不上，无法衔接")
        for _, name in fresh[n:]:  # 先复制成临时文件再改名，播放器不会读到复制了一半的分片
            shutil.copyfile(retry_dir / name, self.dir / f"{name}.tmp")
            os.replace(self.dir / f"{name}.tmp", self.dir / name)
        write_playlist(self.playlist, main + fresh[n:], ended=False)

    def _run(self):
        song = self.song
        try:
            set_song(song, status="buffering")
            inputs = self._inputs(fresh=False)
            self.dir.mkdir(parents=True, exist_ok=True)
            log = self.dir / "ffmpeg.log"
            for attempt in range(3):
                out_dir = self.dir if attempt == 0 else self.dir / f"retry{attempt}"
                out_dir.mkdir(exist_ok=True)
                with self.guard:
                    if self.stopped:
                        return
                    with open(log, "ab") as err:
                        self.proc = subprocess.Popen(
                            self._command(inputs, out_dir),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                        )
                while True:
                    finished = self.proc.poll() is not None
                    if out_dir != self.dir:
                        self._merge(out_dir)
                    if song["status"] != "ready" and playlist_info(self.playlist)[0] > 0:
                        set_song(song, status="ready")
                    if finished:
                        break
                    time.sleep(0.2)
                if out_dir != self.dir:
                    shutil.rmtree(out_dir, ignore_errors=True)
                if self.stopped:
                    return
                entries = read_playlist(self.playlist)
                if self.proc.returncode == 0:
                    write_playlist(self.playlist, entries, ended=True)
                    return
                if entries:  # 出错退出时最后一个分片可能被截断，先撤掉，重拉时换成完整的
                    write_playlist(self.playlist, entries[:-1], ended=False)
                time.sleep(2)
                try:
                    inputs = self._inputs(fresh=True)  # 断流可能是地址失效，重新解析
                except Exception:
                    pass  # 重新解析也失败多半是网络抖动，旧地址通常还有效，先接着用，不要让唱了一半的歌直接失败
            reason = log.read_text(errors="ignore").strip().splitlines()[-1:] or ["未知错误"]
            if playlist_info(self.playlist)[0] > 0:  # 已经能播一部分：收尾，让播放器正常结束
                write_playlist(self.playlist, read_playlist(self.playlist), ended=True)
            else:
                fail(song, "拉流失败：" + reason[0][:120])
        except Exception as e:
            if not self.stopped:
                fail(song, clean_error(e))


# ---------------------------------------------------------------- 客户端指令
def handle(msg):
    """处理手机/播放器发来的指令，出错时返回提示文字。"""
    kind, qid = msg.get("type"), msg.get("qid")
    with lock:
        song = find(qid) if qid else None
        current = state["current"]
        if kind == "add":
            m = URL_RE.search(msg.get("text", ""))
            if not m:
                return "没找到链接，请粘贴 B 站或 YouTube 的视频链接/分享文字"
            song = new_song(m.group(0), str(msg.get("by", "")).strip()[:20])
            state["queue"].append(song)
            if current is None:
                advance()
            threading.Thread(target=fetch_meta, args=(song,), daemon=True).start()
        elif kind == "remove" and song and song in state["queue"]:
            state["queue"].remove(song)
        elif kind == "top" and song and song in state["queue"]:
            state["queue"].remove(song)
            state["queue"].insert(0, song)
        elif kind == "move" and song and song in state["queue"]:
            # 用“放到哪首歌前面”定位，而不是下标：拖动时别人增删了歌，下标就对不上了
            to = int(msg.get("to", 0))  # 先解析参数，非法输入在改动队列之前就报错，不会把歌弄丢
            anchor = find(msg["before"]) if msg.get("before") else None
            state["queue"].remove(song)
            if anchor in state["queue"]:
                state["queue"].insert(state["queue"].index(anchor), song)
            elif "before" in msg and msg["before"] is None:  # 拖到了最后
                state["queue"].append(song)
            else:  # 没带锚点，或锚点那首刚被删了：退回用下标
                state["queue"].insert(max(0, min(to, len(state["queue"]))), song)
        elif kind == "react":
            if msg.get("kind") not in REACTIONS:
                return "无效指令"
            push({"type": "react", "kind": msg["kind"], "by": str(msg.get("by", "")).strip()[:20]})
            return None
        elif kind == "hello" and msg.get("role") == "player":
            state["player_id"] = str(msg.get("id", ""))[:32]
        elif kind == "clear_history":
            state["history"] = []
            save_history()
        elif kind == "volume":
            delta = max(-100, min(100, int(msg.get("delta", 0))))
            threading.Thread(target=change_volume, args=(delta,), daemon=True).start()
        elif kind == "retry" and song and song["status"] == "error":
            if qid in relays:
                relays.pop(qid).stop()
            song.update(status="resolving", error="")
            threading.Thread(target=fetch_meta, args=(song,), daemon=True).start()
            if current is None:
                advance()
        elif kind in ("skip", "ended") and current and current["qid"] == qid:
            advance()  # 带 qid 校验：两个人同时点切歌也只切一首
        elif kind == "player_error" and current and current["qid"] == qid:
            fail(current, "播放器无法播放这个视频")
        elif kind == "pause":
            # 带上想要的状态而不是“切换”：两个人同时按暂停，结果仍是暂停
            state["paused"] = bool(msg["paused"]) if "paused" in msg else not state["paused"]
        elif kind == "progress" and current and current["qid"] == qid:
            # 播放进度每秒一次，单独转发给手机，不触发整份状态广播
            push({"type": "progress", "qid": qid, "time": float(msg.get("time") or 0),
                  "duration": float(msg.get("duration") or current["duration"] or 0)})
            return None
        elif kind == "seek" and current and current["qid"] == qid:
            push({"type": "seek", "qid": qid, "time": max(0.0, float(msg.get("time") or 0))})
            return None
        elif kind == "restart" and current:
            state["restart"] += 1
            state["paused"] = False
        schedule()
    return None


# ---------------------------------------------------------------- Web 服务
class Client:
    """每个连接一个发送队列和发送协程：某个手机网络卡住时只会堵它自己，不会拖住别人。"""

    BACKLOG = 40  # 积压超过约 10 秒的消息就断开它，手机页会自动重连拿最新状态

    def __init__(self, ws):
        self.ws = ws
        self.reacts = []  # 最近几次互动的时间，用来限流
        self.player_id = None  # 这个连接是播放窗口时的 id
        self.outbox = asyncio.Queue(self.BACKLOG)
        self.task = asyncio.create_task(self._pump())

    def allow_react(self):
        now = time.monotonic()
        self.reacts = [t for t in self.reacts if now - t < REACT_WINDOW]
        if len(self.reacts) >= REACT_LIMIT:
            return False
        self.reacts.append(now)
        return True

    def offer(self, text):
        try:
            self.outbox.put_nowait(text)
        except asyncio.QueueFull:
            self.drop()

    def drop(self):
        clients.discard(self)
        self.task.cancel()
        asyncio.create_task(self._close())

    async def _close(self):
        try:
            await asyncio.wait_for(self.ws.close(code=1013), 5)
        except Exception:
            pass

    async def _pump(self):
        try:
            while True:
                await self.ws.send_text(await self.outbox.get())
        except asyncio.CancelledError:
            raise
        except Exception:
            clients.discard(self)


clients = set()


def push(msg):
    """给所有客户端发一条消息（只能在事件循环线程里调用）。"""
    text = json.dumps(msg, ensure_ascii=False)
    for c in list(clients):
        c.offer(text)


async def broadcaster():
    while True:
        await asyncio.sleep(0.25)
        if dirty.is_set():
            dirty.clear()
            text = snapshot()
            for c in list(clients):
                c.offer(text)


@asynccontextmanager
async def lifespan(_app):
    load_history()
    threading.Thread(target=change_volume, args=(0,), daemon=True).start()  # 读一次当前音量
    task = asyncio.create_task(broadcaster())
    yield
    task.cancel()
    with lock:
        for r in relays.values():
            r.stop()
    shutil.rmtree(HLS_ROOT, ignore_errors=True)


app = FastAPI(lifespan=lifespan)


@app.get("/")
def phone_page():
    return FileResponse(STATIC / "index.html", headers=NO_CACHE)


@app.get("/player")
def player_page():
    return FileResponse(STATIC / "player.html", headers=NO_CACHE)


@app.get("/hls.min.js")
def hls_js():
    return FileResponse(STATIC / "hls.min.js")


@app.get("/fx.js")
def fx_js():
    return FileResponse(STATIC / "fx.js", headers=NO_CACHE)


@app.get("/qr.svg")
def qr_code():
    img = qrcode.make(JOIN_URL, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return Response(img.to_string(), media_type="image/svg+xml")


@app.get("/hls/{qid}/{name}")
def hls_file(qid: str, name: str):
    if not re.fullmatch(r"[0-9a-f]{8}", qid) or not re.fullmatch(r"index\.m3u8|seg\d{5}\.ts", name):
        raise HTTPException(404)
    path = HLS_ROOT / qid / name
    if name.endswith(".m3u8"):
        # 播放列表会被整体替换：FileResponse 先 stat 取长度、再另行打开读取，两步之间文件被换掉
        # 长度就对不上，播放器会收到残缺响应。一次性读进内存再发，内容和长度一定一致。
        try:
            body = path.read_bytes()
        except OSError:
            raise HTTPException(404)
        return Response(body, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "no-store"})


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    client = Client(ws)
    clients.add(client)
    client.offer(snapshot())
    try:
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("type") == "hello" and msg.get("role") == "player":
                client.player_id = str(msg.get("id", ""))[:32]
            if isinstance(msg, dict) and msg.get("type") == "react" and not client.allow_react():
                client.offer(json.dumps({"type": "error", "message": "太快啦，歇一下再点"}, ensure_ascii=False))
                continue
            try:
                error = handle(msg) if isinstance(msg, dict) else "无效指令"
            except (ValueError, TypeError, KeyError):
                error = "无效指令"
            if error:
                client.offer(json.dumps({"type": "error", "message": error}, ensure_ascii=False))
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        clients.discard(client)
        client.task.cancel()
        with lock:
            if client.player_id and state["player_id"] == client.player_id:  # 正在播放的窗口关了，让别的窗口接手
                state["player_id"] = None
                dirty.set()


if __name__ == "__main__":
    print(f"\n  🎤 KTV 已启动\n  播放页（电脑）: http://localhost:{PORT}/player\n  点歌页（手机）: {JOIN_URL}\n")
    def open_player():  # 重启时旧的播放页会自动重连（并因版本变化自动刷新），有了就不再多开一个
        if not state["player_id"]:
            webbrowser.open(f"http://localhost:{PORT}/player")
    if not os.environ.get("KTV_NO_BROWSER"):
        timer = threading.Timer(4, open_player)
        timer.daemon = True  # 端口占用等启动失败时不要为了等浏览器计时器而卡住进程
        timer.start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
