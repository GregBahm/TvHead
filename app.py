from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import uuid

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from crt import FRAGMENT_SHADER, render_crt

ROOT = Path(__file__).resolve().parent
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts"}
DEFAULT_CROP = {"x": 0.5, "y": 0.5, "zoom": 1.0}
MIN_ZOOM, MAX_ZOOM = 0.25, 4
MIN_CENTER, MAX_CENTER = -2, 3
CRT_PARAMETERS = {
    "scanlines": {"label": "Scanlines", "default": 0.35},
    "curvature": {"label": "Screen curvature", "default": 0.25},
    "rgbSeparation": {"label": "RGB separation", "default": 0.2},
    "glow": {"label": "Glow", "default": 0.25},
    "vignette": {"label": "Vignette", "default": 0.35},
}


class InvalidProject(ValueError):
    pass


def number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidProject(f"{name} must be a number.")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise InvalidProject(f"{name} must be between {minimum:g} and {maximum:g}.")
    return float(value)


def validate_crt(settings):
    if not isinstance(settings, dict) or not isinstance(settings.get("enabled"), bool):
        raise InvalidProject("CRT settings must contain an enabled boolean.")
    return {
        "enabled": settings["enabled"],
        **{key: number(settings.get(key), f"CRT {parameter['label']}", 0, 1)
           for key, parameter in CRT_PARAMETERS.items()},
    }


def crop_geometry(source, crop):
    # Multiples of 8 x 6 preserve exact 4:3 and even dimensions for H.264.
    unit = int(min(source["width"] / 4, source["height"] / 3) / crop["zoom"] / 2) * 2
    if unit < 2:
        raise InvalidProject("This crop is too small to export.")
    width, height = unit * 4, unit * 3
    x = math.floor((source["width"] * crop["x"] - width / 2) / 2 + 0.5) * 2
    y = math.floor((source["height"] * crop["y"] - height / 2) / 2 + 0.5) * 2
    return width, height, x, y


def upgrade_project(data, sources):
    if not isinstance(data, dict) or data.get("version") not in (1, 2):
        raise InvalidProject("Unsupported project format; expected version 1 or 2.")
    crops, channels = data.get("crops"), data.get("channels")
    if not isinstance(crops, dict) or not isinstance(channels, dict):
        raise InvalidProject("Project must contain crops and channels.")
    if data["version"] == 2:
        return data
    upgraded = copy.deepcopy(data)
    for source_id, crop in upgraded["crops"].items():
        if source_id not in sources:
            continue
        if not isinstance(crop, dict):
            raise InvalidProject("Invalid saved crop.")
        x = number(crop.get("x"), "Legacy crop position X", 0, 1)
        y = number(crop.get("y"), "Legacy crop position Y", 0, 1)
        zoom = number(crop.get("zoom"), "Legacy crop zoom", 1, MAX_ZOOM)
        source = sources[source_id]
        width, height, _, _ = crop_geometry(source, {**DEFAULT_CROP, "zoom": zoom})
        # Version 1 positioned within the available travel; version 2 stores
        # the frame center in source coordinates, including outside the source.
        old_x = int((source["width"] - width) * x / 2) * 2
        old_y = int((source["height"] - height) * y / 2) * 2
        upgraded["crops"][source_id] = {
            "x": (old_x + width / 2) / source["width"],
            "y": (old_y + height / 2) / source["height"], "zoom": zoom,
        }
    upgraded["version"] = 2
    return upgraded


def validate_project(data, sources):
    data = upgrade_project(data, sources)
    crops, channels = data["crops"], data["channels"]
    clean = {"version": 2, "crops": {}, "channels": {}}
    if "crt" in data:
        clean["crt"] = validate_crt(data["crt"])
    for source_id, crop in crops.items():
        if source_id not in sources:
            raise InvalidProject(f"Source is missing or unreadable: {source_id}")
        if not isinstance(crop, dict):
            raise InvalidProject("Invalid crop.")
        clean_crop = {
            "x": number(crop.get("x"), "Crop center X", MIN_CENTER, MAX_CENTER),
            "y": number(crop.get("y"), "Crop center Y", MIN_CENTER, MAX_CENTER),
            "zoom": number(crop.get("zoom"), "Crop zoom", MIN_ZOOM, MAX_ZOOM),
        }
        crop_geometry(sources[source_id], clean_crop)
        clean["crops"][source_id] = clean_crop
    known_channels = {source["channel"] for source in sources.values()}
    clip_ids = set()
    for channel, clips in channels.items():
        if channel not in known_channels or not isinstance(clips, list):
            raise InvalidProject(f"Unknown channel or invalid playlist: {channel}")
        clean_clips = []
        for clip in clips:
            if not isinstance(clip, dict):
                raise InvalidProject("Invalid clip.")
            source_id = clip.get("source")
            source = sources.get(source_id) if isinstance(source_id, str) else None
            if source is None or source["channel"] != channel:
                raise InvalidProject(f"Clip source is missing or belongs to another channel: {source_id}")
            clip_id = clip.get("id")
            if not isinstance(clip_id, str) or not clip_id or len(clip_id) > 100 or clip_id in clip_ids:
                raise InvalidProject("Clip IDs must be unique nonempty strings.")
            clip_ids.add(clip_id)
            start = number(clip.get("start"), "Clip start", 0, source["duration"])
            end = number(clip.get("end"), "Clip end", 0, source["duration"])
            if end - start < 0.05 - 1e-8:
                raise InvalidProject("Clips must be at least 0.05 seconds long.")
            clean_clips.append({"id": clip_id, "source": source_id, "start": start, "end": end})
        previous = {}
        for clip in sorted(clean_clips, key=lambda c: (c["source"], c["start"])):
            if clip["start"] < previous.get(clip["source"], 0) - 1e-8:
                raise InvalidProject("Clips from the same source cannot overlap.")
            previous[clip["source"]] = clip["end"]
        clean["channels"][channel] = clean_clips
    return clean


class VideoLibrary:
    def __init__(self, content, ffprobe):
        self.content = content.resolve()
        self.source_dir = self.content / "Source"
        self.output_dir = self.content / "Output"
        self.project_path = self.content / "project.json"
        self.ffprobe = ffprobe
        self.lock = threading.RLock()
        self.sources = {}
        self.cache = {}
        self.errors = []

    def source_path(self, source_id):
        if not isinstance(source_id, str):
            raise InvalidProject("Invalid source ID.")
        path = (self.source_dir / source_id).resolve()
        if not path.is_relative_to(self.source_dir.resolve()) or not path.is_file():
            raise InvalidProject(f"Source is missing or outside the library: {source_id}")
        return path

    def probe(self, path, source_id):
        result = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=True,
        )
        metadata = json.loads(result.stdout)
        videos = [s for s in metadata["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")]
        if not videos:
            raise ValueError("No video stream found.")
        video = videos[0]
        width, height = int(video["width"]), int(video["height"])
        sar = video.get("sample_aspect_ratio", "1:1").split(":")
        if len(sar) == 2 and int(sar[0]) > 0 and int(sar[1]) > 0:
            width = round(width * int(sar[0]) / int(sar[1]))
        rotation = float(video.get("tags", {}).get("rotate", 0))
        for side in video.get("side_data_list", []):
            rotation = float(side.get("rotation", rotation))
        if round(rotation) % 180:
            width, height = height, width
        duration = float(video.get("duration") or metadata.get("format", {}).get("duration") or 0)
        if not math.isfinite(duration) or duration <= 0 or min(width, height) < 8:
            raise ValueError("Video has invalid duration or dimensions.")
        relative = Path(source_id)
        return {
            "id": source_id, "name": path.name, "channel": relative.parts[0],
            "folder": str(Path(*relative.parts[1:-1])) if len(relative.parts) > 2 else "",
            "width": width, "height": height, "duration": duration,
            "hasAudio": any(s["codec_type"] == "audio" for s in metadata["streams"]),
            "videoStream": video["index"],
        }

    def scan(self):
        with self.lock:
            self.source_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            sources, errors, cache = {}, [], {}
            for path in sorted(self.source_dir.rglob("*"), key=lambda p: str(p).casefold()):
                if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                relative = path.relative_to(self.source_dir)
                source_id = relative.as_posix()
                if len(relative.parts) < 2:
                    errors.append(f"{path.name}: place videos inside a channel folder.")
                    continue
                try:
                    resolved = self.source_path(source_id)
                    stat = resolved.stat()
                    stamp = (stat.st_size, stat.st_mtime_ns)
                    old = self.cache.get(source_id)
                    source = old[1] if old and old[0] == stamp else self.probe(resolved, source_id)
                    sources[source_id] = source
                    cache[source_id] = (stamp, source)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
                    errors.append(f"{source_id}: {detail.strip()}")
            self.sources, self.errors, self.cache = sources, errors, cache
            return {"sources": list(sources.values()), "errors": errors}

    def load_project(self):
        with self.lock:
            if not self.project_path.exists():
                return {"version": 2, "crops": {}, "channels": {}}
            try:
                data = json.loads(self.project_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise InvalidProject(f"Cannot read saved project: {exc}") from exc
            # Keep missing-source edits intact; the UI flags them instead of discarding work.
            data = upgrade_project(data, self.sources)
            if "crt" in data:
                data["crt"] = validate_crt(data["crt"])
            return data

    def save_project(self, data):
        with self.lock:
            clean = validate_project(data, self.sources)
            self.content.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix="project-", suffix=".json", dir=self.content)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(clean, file, indent=2, allow_nan=False)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.project_path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return clean


class Exporter:
    def __init__(self, library, ffmpeg):
        self.library = library
        self.ffmpeg = ffmpeg
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = None

    def start(self, project, channels=None, *, replace_all=False):
        with self.lock, self.library.lock:
            if self.active:
                raise InvalidProject("An export is already running.")
            clean = validate_project(project, self.library.sources)
            if replace_all:
                channels = [name for name, clips in clean["channels"].items() if clips]
            if not isinstance(channels, list) or not channels or any(not isinstance(c, str) for c in channels):
                raise InvalidProject("Choose at least one channel to export.")
            if len(set(channels)) != len(channels):
                raise InvalidProject("Export channels must be unique.")
            for channel in channels:
                if not clean["channels"].get(channel):
                    raise InvalidProject(f"No clips selected for {channel}.")
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {
                "id": job_id, "status": "queued", "progress": 0, "message": "Preparing export",
                "outputs": [], "error": None,
            }
            self.active = job_id
            sources = copy.deepcopy(self.library.sources)
            thread = threading.Thread(target=self.run, args=(job_id, clean, channels, sources, replace_all), daemon=True)
            thread.start()
            return copy.deepcopy(self.jobs[job_id])

    def update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values)

    def get(self, job_id):
        with self.lock:
            return copy.deepcopy(self.jobs.get(job_id))

    def command(self, args):
        result = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode:
            raise RuntimeError(f"FFmpeg failed: {result.stderr.strip()[-6000:]}")

    def render_clip(self, clip, source, crop, size, destination):
        width, height, x, y = crop_geometry(source, crop)
        duration = clip["end"] - clip["start"]
        if x >= source["width"] or y >= source["height"] or x + width <= 0 or y + height <= 0:
            framing = f"scale={width}:{height},drawbox=color=black:t=fill"
        else:
            left, top = max(0, -x), max(0, -y)
            pad_width = math.ceil(max(source["width"] + left, x + width + left) / 2) * 2
            pad_height = math.ceil(max(source["height"] + top, y + height + top) / 2) * 2
            framing = (
                f"pad={pad_width}:{pad_height}:{left}:{top}:color=black,"
                f"crop={width}:{height}:{x + left}:{y + top}"
            )
        filters = (
            f"scale={source['width']}:{source['height']},setsar=1,"
            f"{framing},scale={size[0]}:{size[1]}:flags=lanczos,"
            "fps=30,format=yuv420p"
        )
        args = ["-ss", str(clip["start"]), "-i", str(self.library.source_path(clip["source"]))]
        if not source["hasAudio"]:
            args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        args += [
            "-map", f"0:{source['videoStream']}", "-map", "0:a:0" if source["hasAudio"] else "1:a:0",
            "-t", str(duration), "-vf", filters, "-af", "apad",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-video_track_timescale", "15360", "-map_metadata", "-1", "-metadata:s:v:0", "rotate=0",
            "-movflags", "+faststart", str(destination),
        ]
        self.command(args)

    def run(self, job_id, project, channels, sources, replace_all):
        try:
            crt = project.get("crt")
            use_crt = bool(crt and crt["enabled"])
            total = sum(len(project["channels"][c]) + 1 + use_crt for c in channels)
            completed = 0
            self.update(job_id, status="running")
            self.library.output_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".tvhead-", dir=self.library.output_dir) as temporary:
                work = Path(temporary)
                pending = []
                for channel_index, channel in enumerate(channels):
                    clips = project["channels"][channel]
                    sizes = [crop_geometry(sources[c["source"]], project["crops"].get(c["source"], DEFAULT_CROP))[:2]
                             for c in clips]
                    size = max(sizes, key=lambda s: s[0])
                    for index, clip in enumerate(clips):
                        self.update(job_id, message=f"{channel}: rendering clip {index + 1} of {len(clips)}",
                                    progress=round(100 * completed / total))
                        self.render_clip(clip, sources[clip["source"]],
                                         project["crops"].get(clip["source"], DEFAULT_CROP),
                                         size, work / f"clip-{index:05d}.mp4")
                        completed += 1
                    manifest = work / "concat.txt"
                    manifest.write_text(
                        "".join(f"file 'clip-{i:05d}.mp4'\n" for i in range(len(clips))), encoding="utf-8"
                    )
                    self.update(job_id, message=f"{channel}: assembling MP4",
                                progress=round(100 * completed / total))
                    assembled = work / "assembled.mp4"
                    self.command(["-f", "concat", "-safe", "1", "-i", str(manifest),
                                  "-c", "copy", "-movflags", "+faststart", str(assembled)])
                    completed += 1
                    staged = work / f"channel-{channel_index:05d}.mp4"
                    if use_crt:
                        self.update(job_id, message=f"{channel}: applying CRT effect",
                                    progress=round(100 * completed / total))
                        frames = sum(c["end"] - c["start"] for c in clips) * 30
                        render_crt(
                            self.ffmpeg, assembled, staged, size, crt,
                            lambda count: self.update(
                                job_id, progress=round(100 * (completed + min(0.99, count / frames)) / total)),
                        )
                        completed += 1
                    else:
                        os.replace(assembled, staged)
                    output = self.library.output_dir / f"{channel}.mp4"
                    if output.parent != self.library.output_dir.resolve():
                        raise InvalidProject("Invalid output channel path.")
                    pending.append((staged, output, {
                        "channel": channel, "filename": output.name,
                        "width": size[0], "height": size[1],
                    }))
                self.update(job_id, message="Replacing exports")
                previous = [path for path in self.library.output_dir.iterdir()
                            if path.is_file() and path.suffix.lower() == ".mp4"] if replace_all else []
                for staged, output, metadata in pending:
                    os.replace(staged, output)
                current = {output for _, output, _ in pending}
                for obsolete in previous:
                    if obsolete not in current:
                        obsolete.unlink()
                self.update(job_id, outputs=[metadata for _, _, metadata in pending])
            self.update(job_id, status="complete", progress=100, message="Export complete")
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
            self.update(job_id, status="failed", message="Export failed", error=str(exc))
        finally:
            with self.lock:
                self.active = None


def create_app(content=None, ffmpeg=None, ffprobe=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
    library = VideoLibrary(Path(content or ROOT / "VideoContent"), ffprobe or shutil.which("ffprobe") or "ffprobe")
    exporter = Exporter(library, ffmpeg or shutil.which("ffmpeg") or "ffmpeg")
    app.extensions["library"] = library
    app.extensions["exporter"] = exporter

    @app.before_request
    def local_requests_only():
        # Local app: reject DNS rebinding and cross-origin writes to local files.
        if request.host.split(":")[0].lower() not in {"127.0.0.1", "localhost"}:
            return jsonify(error="Use localhost or 127.0.0.1 to access TvHead."), 403
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="Cross-origin requests are not allowed."), 403
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return jsonify(error="Cross-site requests are not allowed."), 403

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(InvalidProject)
    def invalid_project(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(OSError)
    def io_error(error):
        app.logger.exception("File operation failed")
        return jsonify(error=f"File operation failed: {error}"), 500

    @app.get("/")
    def index():
        return render_template("index.html", crt_parameters=CRT_PARAMETERS, crt_shader=FRAGMENT_SHADER)

    @app.get("/api/library")
    def get_library():
        result = library.scan()
        result["project"] = library.load_project()
        with exporter.lock:
            result["activeJob"] = exporter.active
        return jsonify(result)

    @app.put("/api/project")
    def save_project():
        return jsonify(library.save_project(request.get_json()))

    @app.get("/api/media/<path:source_id>")
    def media(source_id):
        if source_id not in library.sources:
            raise InvalidProject("Unknown source.")
        return send_file(library.source_path(source_id), conditional=True)

    @app.post("/api/exports")
    def start_export():
        data = request.get_json()
        if not isinstance(data, dict):
            raise InvalidProject("Expected an export request.")
        return jsonify(exporter.start(data.get("project"), data.get("channels"))), 202

    @app.post("/api/exports/all")
    def export_all():
        data = request.get_json()
        if not isinstance(data, dict):
            raise InvalidProject("Expected an export request.")
        return jsonify(exporter.start(data.get("project"), replace_all=True)), 202

    @app.get("/api/exports/<job_id>")
    def export_status(job_id):
        job = exporter.get(job_id)
        if not job:
            return jsonify(error="Export job not found. The server may have restarted."), 404
        return jsonify(job)

    @app.get("/api/output/<filename>")
    def output(filename):
        path = (library.output_dir / filename).resolve()
        if path.parent != library.output_dir.resolve() or path.suffix != ".mp4" or not path.is_file():
            return jsonify(error="Output not found."), 404
        return send_file(path, conditional=True, as_attachment=True)

    return app


def main():
    parser = argparse.ArgumentParser(description="TvHead local channel editor")
    parser.add_argument("--content", type=Path, default=ROOT / "VideoContent", help="VideoContent folder")
    parser.add_argument("--port", type=int, default=5050)
    args = parser.parse_args()
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        parser.error(f"Install FFmpeg and add these tools to PATH: {', '.join(missing)}")
    app = create_app(args.content)
    print(f"TvHead: http://127.0.0.1:{args.port}", flush=True)
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
