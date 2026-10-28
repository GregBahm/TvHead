import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from app import CRT_PARAMETERS, DEFAULT_CROP, InvalidProject, VideoLibrary, create_app, crop_geometry, upgrade_project, validate_project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "id": "Channel/Art/one.mp4", "channel": "Channel", "name": "one.mp4",
            "width": 1920, "height": 1080, "duration": 12, "hasAudio": False,
        }
        self.sources = {self.source["id"]: self.source}
        self.project = {
            "version": 2, "crops": {self.source["id"]: DEFAULT_CROP.copy()},
            "channels": {"Channel": [
                {"id": "first", "source": self.source["id"], "start": 2, "end": 5},
                {"id": "second", "source": self.source["id"], "start": 0, "end": 1},
            ]},
        }

    def test_crop_preserves_ratio_with_zoom_and_out_of_bounds_centers(self):
        self.assertEqual(crop_geometry(self.source, DEFAULT_CROP), (1440, 1080, 240, 0))
        self.assertEqual(crop_geometry(self.source, {"zoom": 2, "x": 1, "y": 1}),
                         (720, 540, 1560, 810))
        self.assertEqual(crop_geometry(self.source, {**DEFAULT_CROP, "zoom": 0.5}),
                         (2880, 2160, -480, -540))
        odd = dict(self.source, width=641, height=501)
        width, height, x, y = crop_geometry(odd, {"zoom": 1.37, "x": 0.5, "y": 0.5})
        self.assertEqual(width * 3, height * 4)
        self.assertTrue(all(value % 2 == 0 for value in (width, height, x, y)))
        self.assertLessEqual(x + width, odd["width"])
        self.assertLessEqual(y + height, odd["height"])

    def test_legacy_migration_preserves_exact_pixel_framing_without_rewriting_file(self):
        project = copy.deepcopy(self.project)
        project["version"] = 1
        project["crops"][self.source["id"]] = {"x": 0.312, "y": 1, "zoom": 1.37}
        unit = int(min(1920 / 4, 1080 / 3) / 1.37 / 2) * 2
        width, height = unit * 4, unit * 3
        expected = (width, height, int((1920 - width) * 0.312 / 2) * 2, 1080 - height)
        with tempfile.TemporaryDirectory() as temporary:
            library = VideoLibrary(Path(temporary), "ffprobe")
            library.sources = self.sources
            original = json.dumps(project)
            library.project_path.write_text(original, encoding="utf-8")
            upgraded = library.load_project()
            self.assertEqual(upgraded["version"], 2)
            self.assertEqual(crop_geometry(self.source, upgraded["crops"][self.source["id"]]), expected)
            self.assertEqual(library.project_path.read_text(), original)
            self.assertEqual(upgrade_project(upgraded, self.sources), upgraded)
            self.assertEqual(validate_project(project, self.sources), upgraded)

    def test_zoom_out_and_off_source_positions_can_be_saved(self):
        project = copy.deepcopy(self.project)
        project["crops"][self.source["id"]] = {"x": -0.5, "y": 1.5, "zoom": 0.25}
        self.assertEqual(validate_project(project, self.sources), project)

    def test_clips_may_touch_but_not_overlap_and_minimum_duration_is_stable(self):
        project = copy.deepcopy(self.project)
        clips = project["channels"]["Channel"]
        clips[0].update(start=0.35, end=0.4)
        clips[1].update(start=0.4, end=1)
        self.assertEqual(validate_project(project, self.sources), project)
        clips[1]["start"] = 0.39
        with self.assertRaisesRegex(InvalidProject, "cannot overlap"):
            validate_project(project, self.sources)

    def test_validation_preserves_playlist_order_and_repeated_source_ranges(self):
        result = validate_project(self.project, self.sources)
        self.assertEqual([c["id"] for c in result["channels"]["Channel"]], ["first", "second"])
        self.assertEqual(result["channels"]["Channel"][0]["start"], 2)

    def test_global_crt_settings_round_trip_without_changing_channels_or_crops(self):
        project = copy.deepcopy(self.project)
        project["crt"] = {"enabled": False, **{key: 0.63 for key in CRT_PARAMETERS}}
        with tempfile.TemporaryDirectory() as temporary:
            library = VideoLibrary(Path(temporary), "ffprobe")
            library.sources = self.sources
            self.assertEqual(library.save_project(project), project)
            self.assertEqual(library.load_project(), project)
        self.assertNotIn("crt", validate_project(self.project, self.sources))

    def test_invalid_crt_values_are_rejected_on_save_and_load(self):
        settings = {"enabled": True, **{key: 0.5 for key in CRT_PARAMETERS}}
        invalid = [None, [], {}, {**settings, "enabled": 1}]
        for key in CRT_PARAMETERS:
            for value in [-0.01, 1.01, True, None, "0.5", float("nan"), float("inf")]:
                invalid.append({**settings, key: value})
        with tempfile.TemporaryDirectory() as temporary:
            library = VideoLibrary(Path(temporary), "ffprobe")
            library.sources = self.sources
            for crt in invalid:
                project = {**self.project, "crt": crt}
                with self.subTest(crt=crt):
                    with self.assertRaises(InvalidProject):
                        library.save_project(project)
                    library.project_path.write_text(json.dumps(project), encoding="utf-8")
                    with self.assertRaises(InvalidProject):
                        library.load_project()

    def test_invalid_ranges_and_crops_are_rejected(self):
        for start, end in [(-1, 2), (2, 2), (3, 2), (0, 13), (True, 2), (0, float("nan"))]:
            with self.subTest(start=start, end=end):
                project = copy.deepcopy(self.project)
                project["channels"]["Channel"][0].update(start=start, end=end)
                with self.assertRaises(InvalidProject):
                    validate_project(project, self.sources)
        for key, value in [("x", -2.1), ("y", 3.1), ("zoom", 0.24), ("zoom", 4.1), ("zoom", float("inf"))]:
            with self.subTest(key=key, value=value):
                project = copy.deepcopy(self.project)
                project["crops"][self.source["id"]][key] = value
                with self.assertRaises(InvalidProject):
                    validate_project(project, self.sources)

    def test_missing_source_wrong_channel_and_duplicate_clip_are_rejected(self):
        for change in ("missing", "wrong-channel", "duplicate"):
            project = copy.deepcopy(self.project)
            if change == "missing":
                project["channels"]["Channel"][0]["source"] = "../outside.mp4"
            elif change == "wrong-channel":
                project["channels"]["Other"] = project["channels"].pop("Channel")
            else:
                project["channels"]["Channel"][1]["id"] = "first"
            with self.subTest(change=change), self.assertRaises(InvalidProject):
                validate_project(project, self.sources)

    def test_atomic_save_and_failed_validation_preserve_previous_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = VideoLibrary(Path(temporary), "ffprobe")
            library.sources = self.sources
            library.save_project(self.project)
            self.assertEqual(library.load_project(), self.project)
            project = copy.deepcopy(self.project)
            project["channels"]["Channel"][0]["end"] = 100
            with self.assertRaises(InvalidProject):
                library.save_project(project)
            self.assertEqual(library.load_project(), self.project)
            self.assertEqual(list(Path(temporary).glob("project-*.json")), [])
            library.sources = {}
            self.assertEqual(library.load_project(), self.project)

    def test_corrupt_project_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = VideoLibrary(Path(temporary), "ffprobe")
            library.project_path.write_text("{bad json", encoding="utf-8")
            with self.assertRaisesRegex(InvalidProject, "Cannot read saved project"):
                library.load_project()
            self.assertEqual(library.project_path.read_text(), "{bad json")

    def test_media_rejects_escape_from_source_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "outside.mp4").write_bytes(b"not video")
            library = VideoLibrary(root, "ffprobe")
            with self.assertRaises(InvalidProject):
                library.source_path("../outside.mp4")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class VideoIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.content = Path(cls.temporary.name)
        art = cls.content / "Source" / "Channel A" / "Art"
        art.mkdir(parents=True)
        cls.red_id = "Channel A/Art/red.mp4"
        cls.blue_id = "Channel A/blue.mp4"
        cls.make_video(art / "red.mp4", "red", "160x90", audio=False)
        cls.make_video(art.parent / "blue.mp4", "blue", "320x240", audio=True)
        cls.make_video(art.parent.parent / "Channel B" / "other.mp4", "green", "80x60", audio=False)
        (art / "broken.mp4").write_bytes(b"broken video")
        (art / "notes.txt").write_text("not a video")
        cls.application = create_app(cls.content)
        cls.application.testing = True
        cls.client = cls.application.test_client()
        cls.client.get("/api/library")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def make_video(cls, path, color, size, audio):
        path.parent.mkdir(parents=True, exist_ok=True)
        command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30"]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        command += ["-t", "1.2", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio:
            command += ["-c:a", "aac"]
        subprocess.run([*command, str(path)], capture_output=True, check=True)

    def project(self):
        return {
            "version": 2, "crops": {self.blue_id: DEFAULT_CROP.copy()},
            "channels": {
                "Channel A": [
                    {"id": "blue", "source": self.blue_id, "start": 0.1, "end": 0.6},
                    {"id": "red", "source": self.red_id, "start": 0.2, "end": 0.7},
                ],
                "Channel B": [{"id": "other", "source": "Channel B/other.mp4", "start": 0, "end": 0.5}],
            },
        }

    def wait_for_job(self, job_id):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/exports/{job_id}").get_json()
            if job["status"] in {"complete", "failed"}:
                return job
            time.sleep(0.05)
        self.fail("Export did not finish in 60 seconds")

    def test_scan_groups_nested_sources_and_reports_unreadable_files(self):
        response = self.client.get("/api/library")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        sources = {s["id"]: s for s in data["sources"]}
        self.assertEqual(len(sources), 3)
        self.assertEqual(sources[self.red_id]["folder"], "Art")
        self.assertFalse(sources[self.red_id]["hasAudio"])
        self.assertTrue(sources[self.blue_id]["hasAudio"])
        self.assertTrue(any("broken.mp4" in error for error in data["errors"]))

    def test_project_round_trip_and_media_range_requests(self):
        response = self.client.put("/api/project", json=self.project())
        self.assertEqual(response.status_code, 200, response.get_json())
        reloaded = self.client.get("/api/library").get_json()["project"]
        self.assertEqual(reloaded, self.project())
        response = self.client.get(f"/api/media/{self.red_id}", headers={"Range": "bytes=0-99"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.data), 100)
        response.close()
        self.assertEqual(self.client.get("/api/media/../project.json").status_code, 400)

    def test_export_resolution_order_audio_and_multiple_channels(self):
        project = self.project()
        response = self.client.post("/api/exports", json={
            "project": project, "channels": ["Channel A", "Channel B"],
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        job = self.wait_for_job(response.get_json()["id"])
        self.assertEqual(job["status"], "complete", job)
        self.assertEqual(job["progress"], 100)
        self.assertEqual(len(job["outputs"]), 2)
        self.assertEqual((job["outputs"][0]["width"], job["outputs"][0]["height"]), (320, 240))
        output = self.content / "Output" / "Channel A.mp4"
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
            capture_output=True, text=True, check=True,
        )
        metadata = json.loads(result.stdout)
        video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
        audio = next(s for s in metadata["streams"] if s["codec_type"] == "audio")
        self.assertEqual((video["width"], video["height"], video["codec_name"]), (320, 240, "h264"))
        self.assertEqual(video["avg_frame_rate"], "30/1")
        self.assertEqual(video["pix_fmt"], "yuv420p")
        self.assertEqual((audio["codec_name"], audio["channels"], audio["sample_rate"]), ("aac", 2, "48000"))
        self.assertAlmostEqual(float(metadata["format"]["duration"]), 1, delta=0.1)
        for timestamp, dominant in [("0.2", 2), ("0.8", 0)]:
            frame = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", timestamp, "-i", str(output),
                 "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                capture_output=True, check=True,
            ).stdout
            self.assertGreater(frame[dominant], 200, frame)
        audio_data = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(output), "-f", "f32le", "-ac", "1", "-"],
            capture_output=True, check=True,
        ).stdout
        import array
        samples = array.array("f", audio_data)
        audible = samples[int(0.1 * 48000):int(0.4 * 48000)]
        silent = samples[int(0.7 * 48000):int(0.9 * 48000)]
        self.assertGreater(max(abs(s) for s in audible), 0.01)
        self.assertLess(max(abs(s) for s in silent), 0.001)
        download = self.client.get("/api/output/Channel A.mp4")
        self.assertEqual(download.status_code, 200)
        download.close()

    def test_export_all_replaces_old_exports_and_skips_empty_channels(self):
        output = self.content / "Output"
        obsolete = output / "Old channel.MP4"
        obsolete.write_bytes(b"obsolete export")
        notes = output / "notes.txt"
        notes.write_text("not an export", encoding="utf-8")
        project = self.project()
        project["channels"]["Channel B"] = []
        project["crt"] = {"enabled": False, **{key: 1 for key in CRT_PARAMETERS}}
        response = self.client.post("/api/exports/all", json={"project": project})
        self.assertEqual(response.status_code, 202, response.get_json())
        job = self.wait_for_job(response.get_json()["id"])
        self.assertEqual(job["status"], "complete", job)
        self.assertEqual([item["channel"] for item in job["outputs"]], ["Channel A"])
        self.assertEqual({p.name for p in output.iterdir() if p.suffix.lower() == ".mp4"}, {"Channel A.mp4"})
        self.assertEqual(notes.read_text(), "not an export")
        before = (output / "Channel A.mp4").read_bytes()
        project["channels"] = {"Channel A": [], "Channel B": []}
        response = self.client.post("/api/exports/all", json={"project": project})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((output / "Channel A.mp4").read_bytes(), before)

    def test_export_all_render_failure_preserves_the_whole_previous_set(self):
        output = self.content / "Output"
        (output / "Channel A.mp4").write_bytes(b"old A")
        (output / "Channel B.mp4").write_bytes(b"old B")
        before = {p.name: p.read_bytes() for p in output.glob("*.mp4")}
        exporter = self.application.extensions["exporter"]
        original = exporter.render_clip

        def fail_second_channel(clip, source, *args):
            if source["channel"] == "Channel B":
                raise RuntimeError("second channel render failed")
            return original(clip, source, *args)

        with patch.object(exporter, "render_clip", side_effect=fail_second_channel):
            response = self.client.post("/api/exports/all", json={"project": self.project()})
            job = self.wait_for_job(response.get_json()["id"])
        self.assertEqual(job["status"], "failed", job)
        self.assertIn("second channel", job["error"])
        self.assertEqual({p.name: p.read_bytes() for p in output.glob("*.mp4")}, before)
        self.assertEqual(list(output.glob(".tvhead-*")), [])

    def test_export_all_bakes_crt_into_video_and_keeps_audio_and_duration(self):
        project = self.project()
        project["channels"] = {"Channel A": [project["channels"]["Channel A"][0]]}
        project["crt"] = {"enabled": True, **{key: 0.7 for key in CRT_PARAMETERS}}
        response = self.client.post("/api/exports/all", json={"project": project})
        self.assertEqual(response.status_code, 202, response.get_json())
        job = self.wait_for_job(response.get_json()["id"])
        self.assertEqual(job["status"], "complete", job)
        output = self.content / "Output" / "Channel A.mp4"
        frame = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1",
             "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
            capture_output=True, check=True,
        ).stdout
        self.assertEqual(len(frame), 320 * 240 * 3)
        self.assertLess(max(frame[:3]), 10)
        blue = [frame[(y * 320 + 160) * 3 + 2] for y in range(80, 160)]
        self.assertGreater(sum(abs(a - b) for a, b in zip(blue, blue[1:])) / (len(blue) - 1), 2, blue)
        metadata = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
            capture_output=True, text=True, check=True,
        ).stdout)
        self.assertAlmostEqual(float(metadata["format"]["duration"]), 0.5, delta=0.1)
        self.assertTrue(any(stream["codec_type"] == "audio" for stream in metadata["streams"]))
        self.assertEqual(metadata["streams"][0]["avg_frame_rate"], "30/1")

    def test_failed_export_keeps_existing_output_and_reports_error(self):
        output = self.content / "Output" / "Channel B.mp4"
        output.write_bytes(b"previous output")
        exporter = self.application.extensions["exporter"]
        with patch.object(exporter, "command", side_effect=RuntimeError("test encoder failure")):
            response = self.client.post("/api/exports", json={
                "project": self.project(), "channels": ["Channel B"],
            })
            job = self.wait_for_job(response.get_json()["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("test encoder failure", job["error"])
        self.assertEqual(output.read_bytes(), b"previous output")
        self.assertEqual(list(output.parent.glob(".tvhead-*")), [])

    def test_export_pixels_include_black_padding_for_enlarged_shifted_and_empty_frames(self):
        library = self.application.extensions["library"]
        exporter = self.application.extensions["exporter"]
        source = library.sources[self.red_id]
        clip = {"source": self.red_id, "start": 0, "end": 0.2}
        cases = [
            ({"x": 0.5, "y": 0.5, "zoom": 0.5}, (240, 180),
             [(120, 90)], [(4, 90), (235, 90), (120, 4), (120, 175)]),
            ({"x": 1, "y": 0.5, "zoom": 1}, (120, 90), [(20, 45)], [(100, 45)]),
            ({"x": -0.5, "y": 0.5, "zoom": 1}, (120, 90), [], [(20, 45), (100, 45)]),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            for index, (crop, expected_size, red_pixels, black_pixels) in enumerate(cases):
                with self.subTest(crop=crop):
                    size = crop_geometry(source, crop)[:2]
                    self.assertEqual(size, expected_size)
                    output = Path(temporary) / f"padded-{index}.mp4"
                    exporter.render_clip(clip, source, crop, size, output)
                    result = subprocess.run(
                        ["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1",
                         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                        capture_output=True, check=True,
                    )
                    self.assertEqual(len(result.stdout), size[0] * size[1] * 3)
                    for x, y in red_pixels:
                        offset = (y * size[0] + x) * 3
                        r, g, b = result.stdout[offset:offset + 3]
                        self.assertGreater(r, 200)
                        self.assertLess(max(g, b), 20)
                    for x, y in black_pixels:
                        offset = (y * size[0] + x) * 3
                        self.assertLess(max(result.stdout[offset:offset + 3]), 10)

    def test_rejects_invalid_export_and_cross_origin_writes(self):
        for channels in [[], ["Unknown"], ["Channel A", "Channel A"], "Channel A"]:
            with self.subTest(channels=channels):
                response = self.client.post("/api/exports", json={"project": self.project(), "channels": channels})
                self.assertEqual(response.status_code, 400)
        response = self.client.put("/api/project", json=self.project(), headers={"Origin": "https://example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/library", headers={"Host": "example.com"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
