import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from werkzeug.serving import make_server

from app import create_app, crop_geometry
from crt import CrtFrames

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright and shutil.which("ffmpeg"), "Install requirements-dev.txt and FFmpeg for browser tests")
class BrowserTests(unittest.TestCase):
    def test_editor_and_channel_output_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            source = content / "Source" / "Test channel" / "Art" / "Paintings" / "sample.mp4"
            source.parent.mkdir(parents=True)
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                 "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                capture_output=True, check=True,
            )
            root_source = content / "Source" / "Test channel" / "root.mp4"
            long_name = "A very long video filename with spaces that should be truncated.mp4"
            sibling_source = content / "Source" / "Test channel" / "Art" / "Photos" / long_name
            sibling_source.parent.mkdir(parents=True)
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root_source)],
                capture_output=True, check=True,
            )
            shutil.copyfile(source, sibling_source)
            application = create_app(content)
            server = make_server("127.0.0.1", 0, application, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
                    options = {"executable_path": str(edge)} if edge.exists() else {}
                    browser = playwright.chromium.launch(headless=True, **options)
                    try:
                        page = browser.new_page(viewport={"width": 1440, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("dialog", lambda dialog: dialog.accept())

                        def set_time(time):
                            page.locator("#seek").fill(str(time))

                        def assert_icon_centered(button):
                            offset = button.evaluate("""(button) => {
                                const svg = button.querySelector('svg');
                                const bounds = svg.getBBox();
                                const center = new DOMPoint(bounds.x + bounds.width / 2,
                                    bounds.y + bounds.height / 2).matrixTransform(svg.getScreenCTM());
                                const rect = button.getBoundingClientRect();
                                return { x: center.x - rect.x - rect.width / 2,
                                    y: center.y - rect.y - rect.height / 2 };
                            }""")
                            self.assertAlmostEqual(offset["x"], 0, delta=0.5)
                            self.assertAlmostEqual(offset["y"], 0, delta=0.5)

                        def create_clip(anchor, endpoint):
                            set_time(anchor)
                            assert_icon_centered(page.locator("#clip-action"))
                            page.get_by_role("button", name="Start clip", exact=True).click()
                            assert_icon_centered(page.locator("#clip-action"))
                            self.assertTrue(page.get_by_role("button", name="Complete clip", exact=True).is_disabled())
                            set_time(endpoint)
                            page.get_by_role("button", name="Complete clip", exact=True).click()

                        def current_clips():
                            return page.evaluate("() => sourceClips()")

                        def drag_trim(index, edge, time):
                            track = page.locator("#clip-track").bounding_box()
                            handle = page.locator(".clip-block").nth(index).locator(f".clip-trim.{edge}").bounding_box()
                            x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
                            page.mouse.move(x, y)
                            page.mouse.down()
                            page.mouse.move(track["x"] + track["width"] * time / 2, y, steps=5)
                            page.mouse.up()

                        page.goto(f"http://127.0.0.1:{server.server_port}")
                        page.wait_for_function("() => document.querySelectorAll('.source-button').length === 3")
                        page.wait_for_function("() => document.getElementById('video').readyState >= 2")
                        self.assertEqual(page.locator("#source-name, #source-meta, .editor .eyebrow").count(), 0)
                        self.assertEqual(page.locator("#playlist, .playlist, #export-channel, #export-status").count(), 0)
                        self.assertTrue(page.locator("#error").is_hidden())
                        self.assertTrue(page.locator("#output-play").is_disabled())
                        self.assertTrue(page.get_by_role("slider", name="Channel playback position", exact=True).is_disabled())
                        self.assertTrue(page.get_by_role("button", name="Export all", exact=True).is_disabled())
                        export_bounds = page.locator("#export-all").bounding_box()
                        self.assertLess(export_bounds["x"], 25)
                        self.assertAlmostEqual(export_bounds["y"] + export_bounds["height"], 1088, delta=2)
                        self.assertEqual(page.locator(".library").bounding_box()["x"], 0)
                        source_stage = page.locator("#stage").bounding_box()
                        output_stage = page.locator("#output-stage").bounding_box()
                        self.assertAlmostEqual(source_stage["y"], output_stage["y"], delta=1)
                        self.assertAlmostEqual(source_stage["height"], output_stage["height"], delta=1)
                        self.assertAlmostEqual(page.locator("#seek").bounding_box()["y"],
                                               page.locator("#output-seek").bounding_box()["y"], delta=1)
                        divider = page.get_by_role("separator", name="Resize source and output panels")
                        before = page.locator("#source-editor").bounding_box()["width"]
                        bounds = divider.bounding_box()
                        x, y = bounds["x"] + bounds["width"] / 2, bounds["y"] + 30
                        page.mouse.move(x, y)
                        page.mouse.down()
                        page.mouse.move(x + 80, y, steps=5)
                        page.mouse.up()
                        self.assertAlmostEqual(page.locator("#source-editor").bounding_box()["width"], before + 80, delta=2)
                        divider.focus()
                        before = float(divider.get_attribute("aria-valuenow"))
                        page.keyboard.press("ArrowLeft")
                        self.assertAlmostEqual(float(divider.get_attribute("aria-valuenow")), before - 5, delta=1)
                        self.assertEqual(page.locator("header, .crop-controls, #crop-x, #crop-y, #crop-zoom, #crop-preview, #reset-crop").count(), 0)
                        self.assertEqual(page.locator(".clip-controls, #clip-start, #clip-end, #mark-in, #mark-out, #add-clip").count(), 0)
                        self.assertEqual(page.locator(".crop-handle").count(), 4)
                        self.assertEqual(page.locator(".library h2, .library > .hint, #source-count").count(), 0)
                        channel_node = page.locator(".file-tree > .tree-branch")
                        self.assertEqual(channel_node.count(), 1)
                        self.assertEqual(channel_node.locator(":scope > .tree-children > .tree-file > .source-button").inner_text(), "root.mp4")
                        art_node = page.get_by_role("button", name="Art", exact=True).locator("..").locator("..")
                        self.assertEqual(art_node.locator(":scope > .tree-children > .tree-branch > .tree-row > .folder-button").all_text_contents(), ["Paintings", "Photos"])
                        selected_file = page.locator(".source-button[aria-current=true]")
                        self.assertEqual(selected_file.get_attribute("title"), "Test channel/Art/Paintings/sample.mp4")
                        long_file = page.get_by_role("button", name=long_name, exact=True)
                        layout = long_file.evaluate("""(button) => {
                            const style = getComputedStyle(button);
                            const range = document.createRange();
                            range.selectNodeContents(button);
                            return { overflow: button.scrollWidth > button.clientWidth,
                                ellipsis: style.textOverflow,
                                lines: new Set([...range.getClientRects()].map(rect => rect.top)).size };
                        }""")
                        self.assertEqual(layout, {"overflow": True, "ellipsis": "ellipsis", "lines": 1})
                        self.assertTrue(long_file.get_attribute("title").endswith(long_name))
                        decorations = page.locator(".channel-button, .folder-button, .source-button").evaluate_all(
                            "(buttons) => buttons.map(button => getComputedStyle(button, '::before').content)"
                        )
                        self.assertTrue(all(content in ("none", "normal") for content in decorations))
                        page.get_by_role("button", name="Art", exact=True).click()
                        self.assertFalse(selected_file.is_visible())
                        self.assertTrue(page.get_by_role("button", name="root.mp4", exact=True).is_visible())
                        page.get_by_role("button", name="Art", exact=True).click()
                        page.get_by_role("button", name="Collapse Paintings", exact=True).click()
                        page.get_by_role("button", name="root.mp4", exact=True).click()
                        self.assertTrue(page.get_by_role("button", name="Expand Paintings", exact=True).is_visible())
                        page.get_by_role("button", name="Expand Paintings", exact=True).click()
                        page.locator(".source-button[title='Test channel/Art/Paintings/sample.mp4']").click()
                        page.get_by_role("button", name="Collapse Test channel", exact=True).click()
                        self.assertFalse(page.locator(".source-button[aria-current=true]").is_visible())
                        page.get_by_role("button", name="Expand Test channel", exact=True).click()
                        self.assertTrue(page.locator(".source-button[aria-current=true]").is_visible())
                        page.wait_for_function("() => document.getElementById('video').readyState >= 2")
                        original = page.locator("#crop-frame").bounding_box()
                        handle = page.locator(".crop-handle.se").bounding_box()
                        x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
                        page.mouse.move(x, y)
                        page.mouse.down()
                        page.mouse.move(x + 60, y + 45, steps=6)
                        page.mouse.up()
                        enlarged = page.locator("#crop-frame").bounding_box()
                        self.assertAlmostEqual(enlarged["width"] / enlarged["height"], 4 / 3, places=2)
                        self.assertLess(page.evaluate("() => sourceCrop().zoom"), 1)
                        # The viewport refits the enlarged frame, keeping its handles accessible.
                        stage = page.locator("#stage").bounding_box()
                        self.assertGreaterEqual(enlarged["x"], stage["x"])
                        self.assertLessEqual(enlarged["x"] + enlarged["width"], stage["x"] + stage["width"])
                        self.assertGreater(original["width"], 0)
                        handle = page.locator(".crop-handle.nw").bounding_box()
                        x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
                        before = page.evaluate("() => sourceCrop().zoom")
                        page.mouse.move(x, y)
                        page.mouse.down()
                        page.mouse.move(x + 40, y + 30, steps=5)
                        page.mouse.up()
                        self.assertGreater(page.evaluate("() => sourceCrop().zoom"), before)
                        # Pan through the source boundary rather than clamping the frame to it.
                        before = page.evaluate("() => sourceCrop()")
                        frame = page.locator("#crop-frame").bounding_box()
                        x, y = frame["x"] + frame["width"] / 2, frame["y"] + frame["height"] / 2
                        page.mouse.move(x, y)
                        page.mouse.down()
                        page.mouse.move(x + 150, y - 100, steps=5)
                        page.mouse.up()
                        framed_crop = page.evaluate("() => sourceCrop()")
                        self.assertGreater(framed_crop["x"], before["x"])
                        self.assertLess(framed_crop["y"], before["y"])
                        frame_width, frame_height, frame_x, frame_y = crop_geometry(
                            {"width": 320, "height": 180}, framed_crop,
                        )
                        self.assertTrue(frame_x + frame_width > 320 or frame_y < 0)
                        self.assertEqual(page.locator("#stage").evaluate("(stage) => getComputedStyle(stage).backgroundColor"), "rgb(0, 0, 0)")
                        set_time(0.2)
                        slider = page.locator("#seek").bounding_box()
                        marker = page.locator("#clip-action").bounding_box()
                        self.assertAlmostEqual(marker["x"] + marker["width"] / 2,
                                               slider["x"] + 7 + (slider["width"] - 14) * 0.1, delta=1)
                        create_clip(0.2, 0.6)
                        create_clip(1.5, 1)
                        self.assertEqual(page.locator(".clip-times, .clip-block-label").count(), 0)
                        self.assertNotIn("00:", page.locator("#clip-track").inner_text())
                        self.assertNotIn("00:", page.locator("#output-sequence").inner_text())
                        self.assertEqual([(clip["start"], clip["end"]) for clip in current_clips()], [(0.2, 0.6), (1, 1.5)])
                        self.assertEqual(page.locator(".sequence-clip").count(), 2)
                        # The second click can go in either direction, but cannot cross neighbors.
                        set_time(0.8)
                        page.get_by_role("button", name="Start clip", exact=True).click()
                        set_time(0)
                        self.assertAlmostEqual(float(page.locator("#seek").input_value()), 0.6)
                        set_time(2)
                        self.assertAlmostEqual(float(page.locator("#seek").input_value()), 1)
                        page.get_by_role("button", name="Complete clip", exact=True).click()
                        middle = page.locator(".clip-block").nth(1)
                        middle.hover()
                        assert_icon_centered(middle.locator(".clip-delete"))
                        middle.locator(".clip-delete").click()
                        self.assertEqual(page.locator(".clip-block").count(), 2)
                        self.assertEqual(page.locator(".sequence-clip").count(), 2)
                        set_time(0.4)
                        self.assertTrue(page.get_by_role("button", name="Start clip", exact=True).is_disabled())
                        set_time(0.1)
                        page.get_by_role("button", name="Start clip", exact=True).click()
                        set_time(0)
                        self.assertAlmostEqual(float(page.locator("#seek").input_value()), 0)
                        set_time(2)
                        self.assertAlmostEqual(float(page.locator("#seek").input_value()), 0.2)
                        page.keyboard.press("Escape")
                        self.assertEqual(page.locator(".clip-block").count(), 2)
                        set_time(1.8)
                        page.get_by_role("button", name="Start clip", exact=True).click()
                        set_time(0)
                        self.assertAlmostEqual(float(page.locator("#seek").input_value()), 1.5)
                        set_time(2)
                        page.get_by_role("button", name="Complete clip", exact=True).click()
                        last = page.locator(".clip-block").last
                        last.hover()
                        last.locator(".clip-play").click()
                        last.hover()
                        last.locator(".clip-delete").click()
                        page.wait_for_function("() => document.getElementById('video').paused")
                        self.assertEqual(page.locator(".clip-block").count(), 2)
                        # A source switch cancels an unfinished selection and uses that source's bounds.
                        set_time(0.8)
                        page.get_by_role("button", name="Start clip", exact=True).click()
                        page.get_by_role("button", name="root.mp4", exact=True).click()
                        self.assertEqual(page.locator(".clip-block").count(), 0)
                        create_clip(0.3, 0.5)
                        page.locator(".clip-block").hover()
                        page.locator(".clip-delete").click()
                        page.locator(".source-button[title='Test channel/Art/Paintings/sample.mp4']").click()
                        page.wait_for_function("() => document.getElementById('video').readyState >= 2")
                        drag_trim(0, "end", 2)
                        self.assertAlmostEqual(current_clips()[0]["end"], 1)
                        drag_trim(0, "end", 0.6)
                        self.assertAlmostEqual(current_clips()[0]["end"], 0.6)
                        track = page.locator("#clip-track").bounding_box()
                        handle = page.locator(".clip-block").first.locator(".clip-trim.end").bounding_box()
                        y = handle["y"] + handle["height"] / 2
                        page.mouse.move(handle["x"] + handle["width"] / 2, y)
                        page.mouse.down()
                        page.mouse.move(track["x"] + track["width"] * 0.45, y, steps=4)
                        page.keyboard.press("Escape")
                        page.mouse.up()
                        self.assertAlmostEqual(current_clips()[0]["end"], 0.6)
                        drag_trim(0, "start", -0.2)
                        self.assertAlmostEqual(current_clips()[0]["start"], 0)
                        drag_trim(0, "start", 0.2)
                        drag_trim(0, "end", 0)
                        self.assertAlmostEqual(current_clips()[0]["end"], 0.25)
                        drag_trim(0, "end", 0.6)
                        drag_trim(1, "start", 0)
                        self.assertAlmostEqual(current_clips()[1]["start"], 0.6)
                        drag_trim(1, "start", 1)
                        drag_trim(1, "end", 2.2)
                        self.assertAlmostEqual(current_clips()[1]["end"], 2)
                        drag_trim(1, "end", 1.5)
                        # Clip blocks are proportional to source time, independent of playlist order.
                        track = page.locator("#clip-track").bounding_box()
                        block = page.locator(".clip-block").first.bounding_box()
                        self.assertAlmostEqual(block["x"], track["x"] + track["width"] * 0.1, delta=1)
                        self.assertAlmostEqual(block["width"], track["width"] * 0.2, delta=1)
                        page.locator(".clip-block").first.hover()
                        page.locator(".clip-block").first.locator(".clip-play").click()
                        page.wait_for_function("() => document.getElementById('video').paused && document.getElementById('video').currentTime >= 0.6")
                        self.assertEqual(page.locator(".sequence-clip").count(), 2)
                        tiles = page.locator(".sequence-clip")
                        tiles.nth(1).drag_to(tiles.first, target_position={"x": 5, "y": 10})
                        page.locator(".clip-block").nth(1).locator(".clip-trim.end").focus()
                        page.keyboard.press("ArrowRight")
                        page.wait_for_function("() => revision === savedRevision")
                        saved = json.loads((content / "project.json").read_text())
                        clips = saved["channels"]["Test channel"]
                        self.assertEqual([clip["start"] for clip in clips], [1, 0.2])
                        self.assertEqual(clips[0]["end"], 1.6)
                        self.assertEqual(saved["crops"]["Test channel/Art/Paintings/sample.mp4"], framed_crop)
                        page.reload()
                        page.wait_for_function("() => document.querySelectorAll('.sequence-clip').length === 2")
                        # New source clips append, and output playback follows that order across files.
                        page.get_by_role("button", name="root.mp4", exact=True).click()
                        page.wait_for_function("() => document.getElementById('video').readyState >= 2")
                        create_clip(1.8, 2)
                        page.wait_for_function("() => outputState.ready && outputState.index === 0")
                        output_clips = page.evaluate("() => outputState.clips")
                        self.assertEqual([clip["start"] for clip in output_clips], [1, 0.2, 1.8])
                        self.assertEqual(output_clips[-1]["source"], "Test channel/root.mp4")
                        self.assertEqual(page.locator(".sequence-clip").count(), 3)
                        output_seek = page.get_by_role("slider", name="Channel playback position", exact=True)
                        self.assertTrue(output_seek.is_enabled())
                        self.assertAlmostEqual(float(output_seek.get_attribute("max")), 1.2)
                        source_time = page.evaluate("() => video.currentTime")
                        for position, index, media_time in [(0.25, 0, 1.25), (0.6, 1, 0.2),
                                                            (0.85, 1, 0.45), (1.1, 2, 1.9)]:
                            with self.subTest(output_position=position):
                                output_seek.fill(str(position))
                                page.wait_for_function(
                                    "(index) => outputState.ready && outputState.index === index", arg=index)
                                self.assertAlmostEqual(page.evaluate("() => outputVideo.currentTime"), media_time, delta=0.01)
                                self.assertAlmostEqual(float(output_seek.input_value()), position, delta=0.01)
                                self.assertTrue(page.evaluate("() => outputVideo.paused && !outputState.playing"))
                        output_seek.fill("1.2")
                        page.wait_for_function("() => outputState.ready && outputState.finished")
                        self.assertTrue(page.evaluate("() => outputVideo.currentTime < 2 && outputVideo.currentTime > 1.8"))
                        self.assertEqual(page.locator("#output-time").text_content(), "00:01.20 / 00:01.20")
                        # Superseded seeks across sources must not overwrite the latest target.
                        page.evaluate("""() => {
                            const slider = document.getElementById('output-seek');
                            for (const time of [0.1, 1.1, 0.8, 1.1]) {
                                slider.value = String(time);
                                slider.dispatchEvent(new Event('input'));
                            }
                        }""")
                        page.wait_for_function("() => outputState.ready && outputState.index === 2")
                        self.assertAlmostEqual(page.evaluate("() => outputVideo.currentTime"), 1.9, delta=0.01)
                        self.assertFalse(page.evaluate("() => outputState.finished"))
                        self.assertAlmostEqual(page.evaluate("() => video.currentTime"), source_time, delta=0.01)
                        output_seek.fill("0")
                        page.wait_for_function("() => outputState.ready && outputState.index === 0")
                        self.assertEqual(page.evaluate("""() => [...document.getElementById('output-canvas')
                            .getContext('2d').getImageData(0, 0, 1, 1).data].slice(0, 3)"""), [0, 0, 0])
                        source_time = page.evaluate("() => document.getElementById('video').currentTime")
                        page.evaluate("""() => {
                            window.outputEvents = [];
                            document.getElementById('output-video').addEventListener('playing', () => {
                                window.outputEvents.push({ source: document.getElementById('output-video').dataset.source,
                                    clip: document.getElementById('output-video').dataset.clipId,
                                    time: document.getElementById('output-video').currentTime });
                            });
                        }""")
                        page.get_by_role("button", name="Play channel", exact=True).click()
                        page.wait_for_function("() => Number(document.getElementById('output-seek').value) > 0.1")
                        page.wait_for_function("() => outputState.finished", timeout=15000)
                        self.assertAlmostEqual(float(output_seek.input_value()), 1.2)
                        events = page.evaluate("() => window.outputEvents")
                        self.assertEqual([event["clip"] for event in events], [clip["id"] for clip in output_clips])
                        for event, clip in zip(events, output_clips):
                            self.assertAlmostEqual(event["time"], clip["start"], delta=0.1)
                        self.assertEqual(events[-1]["source"], "Test channel/root.mp4")
                        pixel = page.evaluate("""() => [...document.getElementById('output-canvas')
                            .getContext('2d').getImageData(0, 0, 1, 1).data].slice(0, 3)""")
                        self.assertGreater(pixel[2], 200, pixel)
                        self.assertLess(max(pixel[:2]), 20, pixel)
                        self.assertFalse(page.evaluate("() => document.getElementById('output-video').muted"))
                        self.assertAlmostEqual(page.evaluate("() => document.getElementById('video').currentTime"), source_time, delta=0.02)
                        # Play after finishing restarts at the first clip; pause leaves the frame visible.
                        page.get_by_role("button", name="Play channel", exact=True).click()
                        page.wait_for_function("() => outputState.ready && outputState.playing && outputState.index === 0")
                        output_seek.fill("0.7")
                        page.wait_for_function("() => outputState.ready && outputState.playing && outputState.index === 1")
                        self.assertFalse(page.evaluate("() => outputVideo.paused"))
                        self.assertGreaterEqual(page.evaluate("() => outputVideo.currentTime"), 0.3 - 1e-8)
                        page.get_by_role("button", name="Pause channel", exact=True).click()
                        self.assertTrue(page.evaluate("() => document.getElementById('output-video').paused"))
                        self.assertTrue(page.evaluate("() => outputState.ready"))
                        tiles = page.locator(".sequence-clip")
                        tiles.nth(1).click()
                        page.wait_for_function("() => outputState.ready && outputState.playing && outputState.index === 1")
                        page.get_by_role("button", name="Pause channel", exact=True).click()
                        self.assertTrue(page.locator("#error").is_hidden())
                        # Deleting a source clip closes its gap in the output strip.
                        page.locator(".clip-block").hover()
                        page.locator(".clip-delete").click()
                        self.assertEqual(page.locator(".sequence-clip").count(), 2)
                        tiles = page.locator(".sequence-clip")
                        tiles.first.drag_to(tiles.last, target_position={"x": 115, "y": 10})
                        page.wait_for_function("() => revision === savedRevision")
                        saved = json.loads((content / "project.json").read_text())
                        self.assertEqual([clip["start"] for clip in saved["channels"]["Test channel"]], [0.2, 1])
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.wait_for_function("() => document.documentElement.scrollWidth <= window.innerWidth")
                        self.assertTrue(divider.is_hidden())
                        # Missing media never gets silently dropped or edited into another channel.
                        other = content / "Source" / "Other channel" / "other.mp4"
                        other.parent.mkdir(parents=True)
                        shutil.copyfile(source, other)
                        source.unlink()
                        root_source.unlink()
                        sibling_source.unlink()
                        page.reload()
                        page.wait_for_function("() => document.querySelectorAll('.channel-button').length === 2")
                        self.assertTrue(page.locator("#warnings").is_visible())
                        page.get_by_role("button", name="Test channel", exact=True).click()
                        self.assertTrue(page.locator("#editor-controls").is_hidden())
                        page.get_by_role("button", name="Remove edits for missing footage", exact=True).click()
                        page.wait_for_function("() => revision === savedRevision")
                        saved = json.loads((content / "project.json").read_text())
                        self.assertNotIn("Test channel", saved["channels"])
                        self.assertFalse(saved["crops"])
                        self.assertTrue(page.locator("#warnings").is_hidden())
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_crt_shader_pixels_controls_and_global_persistence(self):
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            source = content / "Source" / "Channel A" / "sample.mp4"
            source.parent.mkdir(parents=True)
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
                 "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                capture_output=True, check=True,
            )
            other = content / "Source" / "Channel B" / "sample.mp4"
            other.parent.mkdir(parents=True)
            shutil.copyfile(source, other)
            project = {"version": 2, "crops": {}, "channels": {
                name: [{"id": name, "source": f"{name}/sample.mp4", "start": 0.1, "end": 1.5}]
                for name in ("Channel A", "Channel B")
            }}
            (content / "project.json").write_text(json.dumps(project), encoding="utf-8")
            application = create_app(content)
            server = make_server("127.0.0.1", 0, application, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
                    options = {"executable_path": str(edge)} if edge.exists() else {}
                    browser = playwright.chromium.launch(headless=True, **options)
                    try:
                        page = browser.new_page(viewport={"width": 1440, "height": 1100})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        url = f"http://127.0.0.1:{server.server_port}"
                        page.goto(url)
                        page.wait_for_function("() => outputState.ready")
                        self.assertTrue(page.get_by_role("switch", name="CRT effect").is_checked())
                        self.assertTrue(page.locator("#crt-canvas").is_visible())
                        self.assertTrue(page.locator("#crt-error").is_hidden(), page.locator("#crt-error").text_content())
                        settings = page.evaluate("() => project.crt")
                        keys = [key for key in settings if key != "enabled"]
                        self.assertEqual(len(keys), 5)
                        for key in keys:
                            page.locator(f"#crt-{key}").fill("0")

                        def shader_difference():
                            return page.evaluate("""() => {
                                drawCrt();
                                const gl = crtCanvas.getContext('webgl');
                                const w = crtCanvas.width, h = crtCanvas.height;
                                const filtered = new Uint8Array(w * h * 4);
                                gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, filtered);
                                const raw = outputContext.getImageData(0, 0, w, h).data;
                                let sum = 0, maximum = 0, count = 0;
                                for (let y = 0; y < h; y += 3) {
                                    for (let x = 0; x < w; x += 3) {
                                        for (let c = 0; c < 3; c++) {
                                            const difference = Math.abs(raw[(y * w + x) * 4 + c] -
                                                filtered[((h - 1 - y) * w + x) * 4 + c]);
                                            sum += difference;
                                            maximum = Math.max(maximum, difference);
                                            count++;
                                        }
                                    }
                                }
                                return { average: sum / count, maximum, error: gl.getError() };
                            }""")

                        neutral = shader_difference()
                        self.assertEqual(neutral["error"], 0)
                        self.assertLessEqual(neutral["maximum"], 2, neutral)
                        source_time = page.evaluate("() => video.currentTime")
                        for key in keys:
                            with self.subTest(parameter=key):
                                page.locator(f"#crt-{key}").fill("1")
                                difference = shader_difference()
                                self.assertEqual(difference["error"], 0)
                                self.assertGreater(difference["average"], 0.1, difference)
                                self.assertEqual(page.locator(f"#crt-{key}-value").text_content(), "100%")
                                page.locator(f"#crt-{key}").fill("0")
                        self.assertEqual(page.evaluate("() => video.currentTime"), source_time)
                        self.assertTrue(page.evaluate("() => outputVideo.paused && Math.abs(outputVideo.currentTime - 0.1) < 0.001"))
                        self.assertEqual(page.evaluate("() => project.crops"), {})

                        for key in keys:
                            page.locator(f"#crt-{key}").fill("0.63")
                        expected = page.evaluate("() => project.crt")
                        page.get_by_role("switch", name="CRT effect").uncheck()
                        self.assertTrue(page.locator("#crt-canvas").is_hidden())
                        self.assertTrue(all(page.locator(f"#crt-{key}").is_disabled() for key in keys))
                        page.wait_for_function("() => revision === savedRevision")
                        page.reload()
                        page.wait_for_function("() => outputState.ready")
                        self.assertFalse(page.get_by_role("switch", name="CRT effect").is_checked())
                        self.assertTrue(page.locator("#crt-canvas").is_hidden())
                        page.get_by_role("switch", name="CRT effect").check()
                        self.assertEqual(page.evaluate("() => project.crt"), expected)
                        page.get_by_role("button", name="Channel B", exact=True).click()
                        page.wait_for_function("() => outputState.ready && outputVideo.dataset.source === 'Channel B/sample.mp4'")
                        self.assertEqual(page.evaluate("() => project.crt"), expected)
                        self.assertEqual(page.locator("#crt-glow").input_value(), "0.63")
                        # Tuning and toggling a playing preview must not stop or restart its clip.
                        page.get_by_role("button", name="Play channel", exact=True).click()
                        page.wait_for_function("() => outputVideo.currentTime > 0.2")
                        page.locator("#crt-glow").fill("0.64")
                        page.get_by_role("switch", name="CRT effect").uncheck()
                        self.assertTrue(page.evaluate("() => outputState.playing && !outputVideo.paused"))
                        self.assertGreater(page.evaluate("() => outputVideo.currentTime"), 0.2)
                        page.get_by_role("switch", name="CRT effect").check()
                        page.get_by_role("button", name="Pause channel", exact=True).click()
                        expected["glow"] = 0.64
                        page.wait_for_function("() => revision === savedRevision")
                        self.assertEqual(json.loads((content / "project.json").read_text())["crt"], expected)
                        page.reload()
                        page.wait_for_function("() => outputState.ready")
                        self.assertEqual(page.evaluate("() => project.crt"), expected)
                        # Context loss is explicit, leaves playback available, and recovers.
                        page.evaluate("""() => {
                            window.contextExtension = crtCanvas.getContext('webgl').getExtension('WEBGL_lose_context');
                            window.contextExtension.loseContext();
                        }""")
                        page.wait_for_function("() => !document.getElementById('crt-error').hidden")
                        self.assertTrue(page.locator("#crt-canvas").is_hidden())
                        self.assertFalse(page.get_by_role("button", name="Play channel", exact=True).is_disabled())
                        page.evaluate("() => window.contextExtension.restoreContext()")
                        page.wait_for_function("() => !crtError && !crtCanvas.hidden")
                        self.assertGreater(shader_difference()["average"], 1)
                        # The offline renderer and browser execute the same shader on the same pixels.
                        pixels = page.evaluate("""() => {
                            outputCanvas.width = 80;
                            outputCanvas.height = 60;
                            drawOutput();
                            const raw = Array.from(outputContext.getImageData(0, 0, 80, 60).data);
                            const gl = crtCanvas.getContext('webgl');
                            const filtered = new Uint8Array(80 * 60 * 4);
                            gl.readPixels(0, 0, 80, 60, gl.RGBA, gl.UNSIGNED_BYTE, filtered);
                            resizeOutput();
                            return { raw, filtered: Array.from(filtered) };
                        }""")
                        raw = bytes(pixels["raw"])
                        flipped = b"".join(raw[y * 320:(y + 1) * 320] for y in reversed(range(60)))
                        renderer = CrtFrames((80, 60), expected)
                        try:
                            offline = renderer.draw(flipped)
                        finally:
                            renderer.close()
                        differences = [abs(a - b) for a, b in zip(offline, pixels["filtered"])]
                        self.assertLess(sum(differences) / len(differences), 0.7)
                        self.assertLess(max(differences), 10)
                        # Export the current, not-yet-autosaved edits for every populated channel.
                        stale = content / "Output" / "stale.mp4"
                        stale.write_bytes(b"old export")
                        requests = []
                        page.on("request", lambda request: requests.append(request.post_data_json)
                                if request.url.endswith("/api/exports/all") else None)
                        page.locator("#crt-glow").fill("0.65")
                        expected["glow"] = 0.65
                        page.get_by_role("button", name="Export all", exact=True).click()
                        page.wait_for_function(
                            "() => document.getElementById('export-message').textContent === 'Exported 2 channels.'",
                            timeout=60000,
                        )
                        self.assertFalse(stale.exists())
                        self.assertEqual({p.name for p in (content / "Output").glob("*.mp4")},
                                         {"Channel A.mp4", "Channel B.mp4"})
                        self.assertEqual(json.loads((content / "project.json").read_text())["crt"], expected)
                        self.assertEqual(requests[-1]["project"]["crt"], expected)
                        self.assertTrue(page.get_by_role("button", name="Export all", exact=True).is_enabled())
                        before = {p.name: p.read_bytes() for p in (content / "Output").glob("*.mp4")}
                        exporter = application.extensions["exporter"]
                        with patch.object(exporter, "command", side_effect=RuntimeError("test export failure")):
                            page.get_by_role("button", name="Export all", exact=True).click()
                            page.wait_for_function(
                                "() => document.getElementById('export-message').textContent.includes('test export failure')")
                        self.assertTrue(page.locator("#error").is_visible())
                        self.assertTrue(page.get_by_role("button", name="Export all", exact=True).is_enabled())
                        self.assertEqual({p.name: p.read_bytes() for p in (content / "Output").glob("*.mp4")}, before)
                        # No WebGL support is a visible degradation, not a broken player.
                        fallback = browser.new_page()
                        fallback.add_init_script("""(() => {
                            const original = HTMLCanvasElement.prototype.getContext;
                            HTMLCanvasElement.prototype.getContext = function(type, ...args) {
                                return type === 'webgl' ? null : original.call(this, type, ...args);
                            };
                        })()""")
                        fallback.goto(url)
                        fallback.wait_for_function("() => outputState.ready")
                        self.assertIn("Unfiltered preview is shown", fallback.locator("#crt-error").text_content())
                        self.assertTrue(fallback.locator("#crt-canvas").is_hidden())
                        fallback.get_by_role("button", name="Play channel", exact=True).click()
                        fallback.wait_for_function("() => outputVideo.currentTime > 0.2")
                        self.assertTrue(fallback.evaluate("() => outputState.playing"))
                        fallback.close()
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
