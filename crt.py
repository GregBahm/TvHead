from array import array
from pathlib import Path
import json
import subprocess
import sys
import tempfile

import moderngl


FRAGMENT_SHADER = (Path(__file__).resolve().parent / "static" / "crt.frag").read_text(encoding="utf-8")


class CrtFrames:
    def __init__(self, size, settings):
        self.context = moderngl.create_standalone_context(require=330)
        try:
            self.program = self.context.program(
                vertex_shader="""#version 330
                    in vec2 a_position;
                    out vec2 v_uv;
                    void main() {
                        v_uv = a_position * 0.5 + 0.5;
                        gl_Position = vec4(a_position, 0.0, 1.0);
                    }
                """,
                fragment_shader=(
                    "#version 330\nin vec2 v_uv;\nout vec4 fragmentColor;\n"
                    "#define gl_FragColor fragmentColor\n#define texture2D texture\n" + FRAGMENT_SHADER
                ),
            )
            vertices = self.context.buffer(array("f", [-1, -1, 3, -1, -1, 3]))
            self.triangle = self.context.simple_vertex_array(self.program, vertices, "a_position")
            self.texture = self.context.texture(size, 4)
            self.texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self.texture.repeat_x = self.texture.repeat_y = False
            self.framebuffer = self.context.simple_framebuffer(size, components=4)
            self.framebuffer.use()
            self.context.viewport = (0, 0, *size)
            self.program["u_image"] = 0
            self.program["u_resolution"] = size
            for key, value in settings.items():
                if key != "enabled":
                    self.program[f"u_{key}"] = value
        except (moderngl.Error, KeyError, ValueError):
            self.context.release()
            raise

    def draw(self, frame):
        self.texture.write(frame)
        self.texture.use(0)
        self.triangle.render(mode=moderngl.TRIANGLES)
        return self.framebuffer.read(components=4, alignment=1)

    def close(self):
        self.context.release()


def render_crt(ffmpeg, source, destination, size, settings, progress):
    # Isolate graphics-driver initialization failures and crashes from the server.
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), ffmpeg, str(source), str(destination),
             str(size[0]), str(size[1]), json.dumps(settings)],
            stdout=subprocess.PIPE, stderr=errors, text=True,
        ) as worker:
            try:
                for line in worker.stdout:
                    progress(int(line))
                if worker.wait():
                    errors.seek(0)
                    detail = errors.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"CRT rendering failed (OpenGL 3.3 is required): {detail[-6000:]}")
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait()


def _render_crt(ffmpeg, source, destination, size, settings, progress):
    renderer = None
    try:
        renderer = CrtFrames(size, settings)
        with tempfile.TemporaryFile() as decode_errors, tempfile.TemporaryFile() as encode_errors:
            decoder = encoder = None
            try:
                decoder = subprocess.Popen(
                    [ffmpeg, "-v", "error", "-nostdin", "-i", str(source),
                     "-map", "0:v:0", "-vf", "vflip", "-pix_fmt", "rgba", "-f", "rawvideo", "-"],
                    stdout=subprocess.PIPE, stderr=decode_errors,
                )
                encoder = subprocess.Popen(
                    [ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
                     "-s", f"{size[0]}x{size[1]}", "-r", "30", "-i", "-", "-i", str(source),
                     "-map", "0:v:0", "-map", "1:a:0", "-vf", "vflip,format=yuv420p",
                     "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "copy",
                     "-video_track_timescale", "15360", "-movflags", "+faststart", str(destination)],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=encode_errors, bufsize=0,
                )
                frame_size = size[0] * size[1] * 4
                count = 0
                while frame := decoder.stdout.read(frame_size):
                    if len(frame) != frame_size:
                        raise RuntimeError("CRT decoder returned an incomplete video frame.")
                    try:
                        remaining = memoryview(renderer.draw(frame))
                        while remaining:
                            written = encoder.stdin.write(remaining)
                            if not written:
                                raise RuntimeError("CRT encoder closed its input.")
                            remaining = remaining[written:]
                    except BrokenPipeError as exc:
                        encode_errors.seek(0)
                        detail = encode_errors.read().decode("utf-8", errors="replace")
                        raise RuntimeError(f"CRT encoder failed: {detail[-6000:]}") from exc
                    count += 1
                    if count % 30 == 0:
                        progress(count)
                encoder.stdin.close()
                if decoder.wait():
                    decode_errors.seek(0)
                    raise RuntimeError(f"CRT decoder failed: {decode_errors.read().decode('utf-8', errors='replace')[-6000:]}")
                if encoder.wait():
                    encode_errors.seek(0)
                    raise RuntimeError(f"CRT encoder failed: {encode_errors.read().decode('utf-8', errors='replace')[-6000:]}")
                if not count:
                    raise RuntimeError("CRT decoder produced no video frames.")
            finally:
                for process in (decoder, encoder):
                    if process is not None:
                        if process.poll() is None:
                            process.kill()
                        process.wait()
                if decoder is not None:
                    decoder.stdout.close()
                if encoder is not None and not encoder.stdin.closed:
                    encoder.stdin.close()
    except moderngl.Error as exc:
        raise RuntimeError(f"CRT export failed (OpenGL 3.3 is required): {exc}") from exc
    finally:
        if renderer is not None:
            renderer.close()


if __name__ == "__main__":
    _render_crt(
        sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), (int(sys.argv[4]), int(sys.argv[5])),
        json.loads(sys.argv[6]), lambda count: print(count, flush=True),
    )
