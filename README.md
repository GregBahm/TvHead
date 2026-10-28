# TvHead

A local browser editor for assembling iPad-ready video channels from source footage.
Original videos are never modified.

## Run on Windows

Install Python 3.11+ and [FFmpeg](https://ffmpeg.org/download.html) (both `ffmpeg`
and `ffprobe` must be on `PATH`). For example, `winget install Gyan.FFmpeg`.
Exporting with CRT enabled also requires graphics drivers supporting OpenGL 3.3.
Then, from this repository in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Open **http://127.0.0.1:5050** in Edge or Chrome. The server is local-only; keep
the terminal running while editing or exporting. To stop it, press Ctrl+C.
For another media location or port:

```powershell
.\.venv\Scripts\python.exe app.py --content D:\Art\VideoContent --port 5051
```

## Organize and edit

```text
VideoContent\
  Source\
    Channel A\
      Art\
        painting.mp4
      interview.mov
    Channel B\
      installation.mp4
  project.json
  Output\
    Channel A.mp4
    Channel B.mp4
```

Each top-level source folder is a channel. Nested folders group footage in the
sidebar file tree: channel, folders, then source videos. Use the arrows or folder
names to expand/collapse branches; select a filename to edit it. Videos directly
inside a channel appear directly under that channel. Supported file extensions:
MP4, MOV, M4V, MKV, AVI, WebM, MTS, and M2TS.
Reload the page after adding files; unreadable files are reported.
The entire `VideoContent` folder is ignored by git, including edits and exports.
Back it up separately.

1. Select a source. Drag the **4:3 landscape** frame to move it; drag any
   corner to resize it while keeping 4:3 (arrow keys also resize a focused corner).
   All framing controls are directly on the video overlay; there is no separate
   crop panel. Enlarge the frame beyond the footage to add black padding. It can
   extend outside any edge; the area inside the frame shows the resulting
   composition. A source's frame applies to all of its clips.
   Zoom ranges from 0.25x to 4x (limited further for extremely small sources);
   the frame center can travel from -200% to 300% of the source dimensions.
2. Seek to unused footage and press the **+** above the playhead to start a clip.
   The button becomes a checkmark. Move the slider forward or backward and press
   the **checkmark** to finish. Selection stops at neighboring clips or the video's
   beginning/end, so clips from one source never overlap. Escape cancels a draft.
3. Clips appear as time-aligned blocks in a row beneath the slider. Each block
   has play/delete buttons; on narrow blocks, hover or focus to reveal them.
   Drag either edge to trim without crossing another clip (minimum length 0.05s).
   Focus an edge and use Left/Right for 0.1s steps, or Shift for 1s steps.
   Escape cancels an active trim. Changes autosave to `project.json`.
4. The right-hand output preview plays all of the current channel's clips, in
   order, with their actual 4:3 framing, black padding, and audio. Press **Play**
   to run the sequence, **Pause** to pause, or click a clip in the strip below
   to play from there. Scrub the output time slider to seek anywhere across the
   complete channel; it follows playback and preserves the playing/paused state.
   Playing again after the end restarts the sequence.
5. Drag clips left/right in the output strip to reorder them (Alt+Left/Right also
   works when a clip has keyboard focus). New clips always append at the end;
   deleting a source clip removes it from the strip and closes the gap.
6. Use **CRT effect** below the output strip to toggle a real-time CRT shader.
   Tune scanlines/phosphor stripes, screen curvature, RGB separation, glow, and
   vignette with the sliders. Changes appear immediately, even while paused,
   without restarting playback. One set of settings is autosaved for all channels.
   Switching the effect off preserves the tuning and shows the unfiltered output.
   The same shader is baked into exports when enabled; source footage is unchanged.
   WebGL is required; if unavailable or interrupted, a visible warning appears and
   unfiltered playback remains available.
7. Press **Export all** at the bottom left to replace the MP4 exports in
   `VideoContent\Output` with one video per channel that currently has clips.
   Clip order, trimming, framing, black padding, audio, and the CRT toggle/tunings
   are captured when you click. Empty channels are skipped. Progress appears
   above the button, and reconnects if you refresh during an export.
   The new set renders before any existing export is replaced; a rendering
   failure leaves the old set intact. Obsolete MP4 exports are removed only
   after every new video is ready. Other files and subfolders are not deleted.

The file tree sits at the left edge. Drag the divider between the source editor
and output preview to resize the panels; arrow keys adjust a focused divider.
On narrow screens the panels stack vertically. Output playback is independent
of the source playhead, and source/output audio do not play over each other.
Crop changes appear live; changing the clip order or ranges resets the output
preview to the beginning. This is a browser preview, not a pre-rendered file:
source changes can briefly buffer, and footage must use browser-supported codecs.

Saved edits are development data; compatibility across updates is not guaranteed yet.

Export all uses `/api/exports/all`; `/api/exports` still supports a channel subset.
Exports use H.264 video, AAC stereo audio, 30 fps, square pixels, and MP4 fast-start.
The largest frame dimensions in each playlist determine its output resolution,
including zoom and any added black padding. Enlarging the frame can therefore
increase the exported pixel dimensions. Dimensions are rounded down to multiples of
8 x 6 for exact 4:3 and H.264 compatibility. Smaller clips are upscaled; larger
ones are not needlessly upscaled. Original audio is preserved and converted to
AAC; silent footage and short audio tracks are padded with silence. Clips are
joined with straight cuts, not transitions. Encoding can take time for large
sources; only one export job runs at a time.

Browsers cannot preview every codec FFmpeg can read. A preview error does not
prevent timeline range selection or export. Use browser-compatible H.264 MP4
sources when visual preview is needed. Extremely high-resolution exports may
exceed older iPads' hardware decoding limits; the editor deliberately retains
the requested highest source resolution rather than applying a device cap.
Do not rename/move sources after editing: edits refer to relative file paths.
Missing footage is flagged; restore it or explicitly remove its edits.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover project validation/persistence, library/media routes, and real
FFmpeg exports using small generated fixtures. Video integration tests skip
when FFmpeg is unavailable. No JavaScript build step or external web assets
are required.

For the optional end-to-end browser test, install `requirements-dev.txt` instead.
It uses installed Microsoft Edge on Windows; otherwise install the test browser
with `python -m playwright install chromium` inside the virtual environment.
The same unittest command then also exercises overlay-only framing, clip editing,
reordering, autosave/reload, cross-source output playback, crop/padding pixels,
the adjustable layout, CRT shader pixels, shared settings, graphics-context
recovery, and export-all replacement through the UI. CRT tests also verify that
the browser and offline GPU renderers produce matching pixels.