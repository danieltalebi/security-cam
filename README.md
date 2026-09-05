# yolo-rtsp-security-cam
Python RTSP security camera app with motion detection features that are based on image processing instead of a dedicated sensor. Also includes YOLO object detection so you can set it to record only specific things such as people, dogs, other animals or particular objects. All that's required is an RTSP camera and a PC with a GPU capable of running YOLO.

## Getting Started

### Linux (Debian/Ubuntu)

First make sure your system has the required software:

```bash
sudo apt update
sudo apt install gcc python3-dev python3-pip git ffmpeg
```

YOLO object detection also requires CUDA for Nvidia GPUs or ROCm for AMD GPUs. I've created step by step guides on how to install both of these.

CUDA Guide: https://phazertech.com/tutorials/cuda.html

ROCm Guide: https://phazertech.com/tutorials/rocm.html

It might be possible to run YOLO on the CPU, but it will be slow and I didn't test it, so you're highly encouraged to use GPU acceleration.

Clone this repository and install dependencies:

```bash
git clone https://github.com/PhazerTech/yolo-rtsp-security-cam
cd yolo-rtsp-security-cam
pip3 install -r requirements.txt
```

### Windows

**Requirements:**

- **Python 3.11** — download from https://www.python.org/downloads/release/python-3119/ and check "Add Python to PATH" during installation. Python 3.11 is required because newer versions may not be supported by PyTorch.
- **ffmpeg** — download the latest build from https://www.gyan.dev/ffmpeg/builds/, extract the zip, and add the `bin\` folder to your system PATH.
- **GPU acceleration (recommended)** — install PyTorch with CUDA support before running setup: https://pytorch.org/get-started/locally/. Select Windows, Pip, Python, and your CUDA version, then run the generated install command. AMD GPUs are not supported on Windows (ROCm is Linux-only).

Once the requirements are installed, clone the repository and run the setup script:

```bat
git clone https://github.com/PhazerTech/yolo-rtsp-security-cam
cd yolo-rtsp-security-cam
setup_windows.bat
```

For the basic RTSP recorder, open `run_windows.bat` in a text editor, set `STREAM_URL` and other options, then double-click it to start the app.

#### Windows smart DVRIP + Telegram monitor

The dual-lens property classifier uses a separate launcher so it can wait for the camera's own DVRIP person event, take an immediate JPEG snapshot, classify the person as `private`, `public`, or `uncertain`, and only use RTSP as a fallback for uncertain results.

1. Double-click `setup_camera_windows.bat` and give the camera a name, such as `garage` or `back patio`. Choose `property` for the dual-lens public/private classifier, or `presence` when any detected person should alert.
2. The setup creates one non-secret JSON file per camera and adds it to the ignored `multi-camera.json`. It stores its credentials as Windows user variables derived from the name: `CAMERA_GARAGE_USER`, `CAMERA_GARAGE_PASSWORD`, and, for a property camera, `CAMERA_GARAGE_RTSP_URL`.
3. Open the property camera JSON only to calibrate its layout, zones, and classifier model path. Do not put passwords in it.
4. Close the setup window, open a new Command Prompt, then double-click `run_multi_windows.bat`.

The assistant uses Windows user environment variables because they are simple, work with the monitor directly, and avoid storing secrets in the repository. They are not encrypted. For a shared Windows computer or stricter secret protection, use a dedicated Windows account and restrict access; a future enhancement can use Windows Credential Manager.

The same `CAMERA_<NAME>_USER` and `CAMERA_<NAME>_PASSWORD` are used for both DVRIP and ONVIF. There are no separate DVRIP credential variables to keep in sync.

It sends Telegram notifications only for confirmed `private` detections. Every annotated result is saved locally under `events\\review\\private`, `events\\review\\public`, or `events\\review\\uncertain` for later review. Press `Ctrl+C` in its terminal window to stop it.

To add another camera later, run `setup_camera_windows.bat` again. Run `diagnose_cameras_windows.bat` first to check every DVRIP connection and JPEG snapshot; add `--telegram` after it in a Command Prompt if you also want Telegram test messages.

## Running the App

The only arguments required to run the app are --stream followed by the RTSP address of your video stream, and --yolo followed by a comma separated list of objects you'd like the app to detect. The list of valid objects can be found in the coco.names file.

To run it with default settings, enter the following and replace 'ip:port/stream-name' with your stream's address.  Feel free to modify 'person,dog,cat' to which ever objects you'd like the app to detect.

```bash
python3 yolo-rtsp-security-cam.py --stream rtsp://ip:port/stream-name --yolo person,dog,cat
```

To open a window where you can view the stream while the program is running, include the --monitor argument. The YOLO bounding boxes and class IDs will be drawn on the stream in this window, however the recordings will not include these bounding boxes, only the raw stream will be recorded.

```bash
python3 yolo-rtsp-security-cam.py --stream rtsp://ip:port/stream-name --yolo person,dog,cat --monitor
```

The program will print a message whenever it starts a recording and ends a recording, and also provide a timestamp.
For example:

```bash
$ python3 yolo-rtsp-security-cam.py --stream rtsp://192.168.0.156:8554/frontdoor --yolo person,dog,cat
13-05-09 recording started
13-05-30 recording stopped
14-01-01 recording started
14-01-09 recording stopped
```

It will create a folder with the current date for storing that day's recordings. A new folder will be created each day with the current date so that it can be left to run indefinitely. Press the 'Q' key to quit the program. If your IP camera disconnects for some reason, the program will indicate this and attempt to reconnect every 5 seconds.

By default, YOLO will run the nano sized model, but you can change this by using the --model argument and specify which sized model you want to run. For example, enter '--model yolov8s' to run the small model, or '--model yolov8m' to run the medium model, and so on. See the official YOLOv8 repo for more info on these models: https://github.com/ultralytics/ultralytics

AMD GPUs might stall for a minute the first time they run a new model, in which case you might see errors about dropped frames, but this should only happen during the first run.

Larger models will provide better detection results, but will also require more memory and processing power. I recommend sticking with the default nano model or the small model, because anything larger will have major diminishing returns.  If you'd like to run the program without YOLO so that it only records based on motion detection, simply omit the --yolo argument. Doing this will make the app extremely lightweight and able to run on low power devices such as a Raspberry Pi 4, exactly like the previous version of the app I made here: https://github.com/PhazerTech/rtsp-security-cam

## Advanced Settings

The program doesn't constantly run YOLO object detection, instead it constantly detects motion and only starts the YOLO object detection if it detects motion first. It works this way in order to be more power efficient.  If the default motion detection settings are providing poor results, additional arguments can be provided to tweak the sensitivity of the motion detection algorithm and to enable testing mode which helps to find the optimal threshold value, but in most cases this shouldn't be necessary.

--threshold - Threshold value determines the amount of motion required to trigger a recording. Higher values decrease sensitivity to help reduce false positives. If using YOLO detection, false positives are less of a concern so the default value should be fine in most cases. Default is 350. Max is 10000.

--start_frames - The number of consecutive frames with motion activity required to start a recording. Raising this value might help if there's too many false positive recordings, especially when using a high frame rate stream greater than 30 FPS. But again, if using YOLO then the default value should be fine. Default is 3. Max is 30.

--tail_length - The number of seconds without motion activity required to stop a recording. Raising this value might help if the recordings are stopping too early. Default is 8. Max is 30.

--auto_delete - Entering this argument enables the auto-delete feature. Recordings that have a total length equal to the tail_length value are assumed to be false positives and are auto-deleted.

--testing - Testing mode disables recordings and prints out the motion value for each frame if greater than threshold. Helpful when fine tuning the threshold value.

--frame_click - Allows the user to advance frames one by one by pressing any key. For use with testing mode on video files, not live streams, so make sure to provide a video file instead of an RTSP address for the --stream argument if using this feature.

--roi - Restricts YOLO detection to a rectangular region of the frame, specified as x1,y1,x2,y2 in pixels. Useful for dual-lens cameras where the combined feed includes areas outside your property. Run with --monitor to see a green rectangle showing the monitored region, and adjust the coordinates until it covers only the desired area. Motion detection still uses the full frame, but YOLO only fires within the ROI. Example: --roi 0,0,640,720

### Smart monitoring for dual-lens cameras

`--smart_config` enables separate rules for a fixed lens and a PTZ lens. Person detections are associated between frames so a zone can alert immediately (`immediate`) or only after the configured `dwell_seconds` (`dwell`). Events are printed as JSON and can optionally be appended to a JSONL file for a future Home Assistant, MQTT, or webhook integration.

Copy `smart-monitor.example.json`, adjust the lens rectangles and zone polygons to the real stream, then run:

```bash
python3 yolo-rtsp-security-cam.py \
  --stream "$CAMERA_GARAGE_RTSP_URL" \
  --yolo person \
  --smart_config smart-monitor.json \
  --monitor
```

All coordinates use the original stream resolution. A person's bottom-center point (approximately their feet) determines the active region and zone. The monitor window draws lens regions in blue and rule zones in yellow to help calibration. `dwell_seconds`, matching distance, track timeout, alert cooldown, and detection interval are configurable.

The example contains ONVIF connection placeholders but ONVIF position polling is not enabled yet. Credentials should be provided through environment variables when that stage is added; do not put camera passwords in the configuration file or repository.

When the `onvif.enabled` setting is `true`, the monitor overlay shows the camera's current pan, tilt, and zoom. Install the updated dependencies, set the two environment variables named in the configuration, and keep the ONVIF endpoint and profile name in the JSON. Moving the mouse over the monitor shows the raw stream `X`/`Y` coordinate and a crosshair, which makes polygon calibration practical.

If the overlay updates but its PTZ values do not change while the camera moves, run `onvif_diagnose.py` to query every ONVIF media profile and identify whether the camera exposes an absolute PTZ position at all.

### Building a property classifier dataset

The PTZ lens does not need to show the complete garage and sidewalk in every frame. The future classifier will segment only the visible part of each PTZ frame into `private_property`, `public_area`, and `unknown`. A person will be classified using the pixel beneath their feet; `unknown` means “do not alert yet” rather than making a risky guess.

First collect varied, clean PTZ views. Start the monitor with a dataset directory:

```bash
python3 yolo-rtsp-security-cam.py \
  --stream "$CAMERA_GARAGE_RTSP_URL" \
  --yolo person \
  --smart_config smart-monitor.json \
  --monitor \
  --dataset_dir dataset
```

When the camera has reached a useful view, focus the video window and press `C`. This writes the unannotated bottom/PTZ image to `dataset/raw/YYYY-MM-DD/`, together with a small JSON metadata file. Capture distinct positions—not consecutive copies—including the driveway, gate, sidewalk, far ends of the property, and ambiguous views, in daylight and at night. Begin with roughly 200–500 samples; include examples after the PTZ has tracked people, because those are the views the app must handle.

The next step is to label the visible pixels in those images. Do not try to label an area that is off-screen: leave it as `unknown`.

#### Local video labeler

To label a local recording without uploading it anywhere, run:

```bash
python3 label_property_video.py \
  --video /path/to/recording.mp4 \
  --output_dir dataset/labels \
  --step_seconds 2 \
  --crop 0,1296,2304,2592
```

`--crop` is optional, but recommended for a combined stream: the example crops the lower/PTZ lens using this camera's 2304×2592 layout. The video window is the editor. Mark only the **public area** (sidewalk/street) with polygons: click to place points, press `Enter` to finish a polygon, and press `S` to save the current frame. Right-click removes the last unfinished point. The committed polygons remain while you move between frames, which is useful while the PTZ view is unchanged. `A`/`D` moves one frame; `J`/`L` jumps by the configured interval. `U` clears the working polygons when the view changes and `Q` exits.

For every saved frame, the labeler creates a clean image, a single-channel PNG mask, and an editable JSON file under `dataset/labels/`. Pixel value `1` is `public_area`; every other pixel is `not_public`. The future classifier uses this as a public-area suppressor: a person whose feet are on a high-confidence public pixel does not trigger an alert. Only mark public ground that is visible in that frame.

#### Train the public-area segmenter

Once you have examples from multiple PTZ positions and lighting conditions, train locally:

```bash
python3 train_public_area.py \
  --dataset_dir dataset/labels \
  --output_dir models/public-area \
  --epochs 40
```

The trainer splits whole source videos between training and validation, so its validation score is based on views the model did not train on. It prints pixel-level intersection-over-union (IoU), precision, and recall after each epoch and writes the best checkpoint to `models/public-area/model.pt`. Start with the default 40 epochs on CPU; the model is intentionally small. You can validate the dataset and see the chosen split without training by adding `--dry_run`.

Preview the trained model over a video or the live combined stream:

```bash
python3 preview_public_area.py \
  --model models/public-area/model.pt \
  --stream "$CAMERA_GARAGE_RTSP_URL" \
  --crop 0,1296,2304,2592
```

The preview paints pixels predicted as public area in orange. It is read-only: press `Q` to close it. Use `--threshold 0.7` to require greater confidence before marking an area as public, or `--every_n_frames 3` if CPU inference is too slow.

#### DVRIP camera-event listener

`dvrip_listen.py` is a read-only diagnostic listener for the camera's own push alarms. It subscribes to DVRIP `AlarmInfo` events; it does not change detection settings, PTZ position, or any camera configuration.

```bash
python3 -u dvrip_listen.py --config smart-monitor.json --duration 120
```

Trigger the camera's human detection during that time and retain the printed `ALARM` JSON. Different OEM firmware versions use different event payload fields, so this one-time check confirms how this specific camera identifies a human alarm before it is used to activate RTSP analysis.

When `dvrip.listen_events` and `public_area_classifier.enabled` are enabled in `smart-monitor.json`, the main monitor uses the confirmed DVRIP human `Start` event to open a short RTSP analysis window. By default it keeps a single RTSP decoder warm (`dvrip.keep_rtsp_warm: true`), so the event is analysed using current frames immediately rather than waiting for an RTSP reconnection. This does not run YOLO while there is no event. A person in the fixed top lens alerts immediately. For the PTZ lens, a high-confidence public-area prediction suppresses the alert, a low probability alerts immediately, and intermediate values require the configured dwell time. The monitor shows public pixels in orange.

### Multiple cameras and diagnostics

Use one configuration file per physical camera, then list them in `multi-camera.json` (start from `multi-camera.example.json`). Every camera has a name and a matching environment prefix: `garage` uses `CAMERA_GARAGE_USER` and `CAMERA_GARAGE_PASSWORD`; `back patio` uses `CAMERA_BACK_PATIO_USER` and `CAMERA_BACK_PATIO_PASSWORD`. Both DVRIP and ONVIF share those credentials. A `property` camera additionally uses `CAMERA_<NAME>_RTSP_URL`.

```bash
python3 multi_camera_monitor.py --config multi-camera.json
```

The `property` mode runs the public/private classifier; `presence` is for areas such as a back yard where any human detection is positive. It captures a JPEG, saves it, and sends it to Telegram without opening RTSP. On Windows, `setup_camera_windows.bat` creates and registers either mode interactively.

To test every configured camera without waiting for an event:

```bash
python3 diagnose_cameras.py --config multi-camera.json
```

Add `--telegram` to send a Telegram test message for each configured camera. The diagnostic checks DVRIP login and a JPEG snapshot; it never changes camera configuration.

Check out my video about this app on my YouTube channel for more details: https://youtu.be/m8dIJN6ePKA

## Contact & Support

If you found value in this software then consider supporting me: https://phazertech.com/funding.html

If you have any questions feel free to contact me: https://phazertech.com/contact.html

## Copyright

Copyright (c) 2023, Phazer Tech

This source code is licensed under the Affero GPL. See the LICENSE file for details.
