# Copyright (c) 2023, Phazer Tech
# All rights reserved.

# View the GNU AFFERO license found in the
# LICENSE file in the root directory.
import os
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
import time
import sys
import cv2
import queue
import threading
import numpy as np
import json
import subprocess
from datetime import datetime
from urllib.parse import urlparse
from ffmpeg import FFmpeg
from skimage.metrics import mean_squared_error as ssim
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter, BooleanOptionalAction
from sshkeyboard import listen_keyboard, stop_listening

# Parse command line arguments
parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
parser.add_argument("--stream", type=str, default=os.environ.get("CAMERA_GARAGE_RTSP_URL"), help="RTSP address of video stream. Defaults to CAMERA_GARAGE_RTSP_URL.")
parser.add_argument('--monitor', default=False, action=BooleanOptionalAction, help="View the live stream. If no monitor is connected then leave this disabled (no Raspberry Pi SSH sessions).")
parser.add_argument("--yolo", type=str, help="Enables YOLO object detection. Enter a comma separated list of objects you'd like the program to record. The list can be found in the coco.names file")
parser.add_argument("--model", default='yolov8n', type=str, help="Specify which model size you want to run. Default is the nano model.")
parser.add_argument("--threshold", default=350, type=int, choices=range(1,10000), help="Determines the amount of motion required to start recording. Higher values decrease sensitivity to help reduce false positives. Default 350, max 10000.")
parser.add_argument("--start_frames", default=3, type=int, choices=range(1,30), help="Number of consecutive frames with motion required to start recording. Raising this might help if there's too many false positive recordings, especially with a high frame rate stream of 60 FPS. Default 3, max 30.")
parser.add_argument("--tail_length", default=8, type=int, choices=range(1,30), help="Number of seconds without motion required to stop recording. Raise this value if recordings are stopping too early. Default 8, max 30.")
parser.add_argument("--auto_delete", default=False, action=BooleanOptionalAction, help="Enables auto-delete feature. Recordings that have total length equal to the tail_length value (seconds) are assumed to be false positives and are auto-deleted.")
parser.add_argument('--testing', default=False, action=BooleanOptionalAction, help="Testing mode disables recordings and prints out the motion value for each frame if greater than threshold. Helps fine tune the threshold value.")
parser.add_argument('--frame_click', default=False, action=BooleanOptionalAction, help="Allows user to advance frames one by one by pressing any key. For use with testing mode on video files, not live streams, so set a video file instead of an RTSP address for the --stream argument.")
parser.add_argument("--roi", type=str, default=None, help="Restrict YOLO detection to a region of the frame: x1,y1,x2,y2 in pixels. Useful for dual-lens cameras where only part of the frame covers your property.")
parser.add_argument("--smart_config", type=str, default=None, help="JSON configuration for dual-lens zones, person tracking, dwell time, and alert events. Requires --yolo person.")
parser.add_argument("--dataset_dir", type=str, default=None, help="Directory for manually saved clean PTZ samples. With --monitor, press C to save the current PTZ view for classifier training.")
parser.add_argument("--dvrip_events", default=None, action=BooleanOptionalAction, help="Use camera DVRIP human-detection events to activate short RTSP analysis windows.")
args = vars(parser.parse_args())

if not args["stream"]:
    parser.error("provide --stream or set CAMERA_GARAGE_RTSP_URL")

rtsp_stream = args["stream"]
monitor = args["monitor"]
thresh = args["threshold"]
start_frames = args["start_frames"]
tail_length = args["tail_length"]
auto_delete = args["auto_delete"]
testing = args["testing"]
frame_click = args["frame_click"]
if frame_click:
    testing = True
    monitor = True
    print("frame_click enabled. Press any key to advance the frame by one, or hold down the key to advance faster. Make sure the video window is selected, not the terminal, when advancing frames.")
if args["yolo"]:
    yolo_list = [s.strip() for s in args["yolo"].split(",")]
    yolo_on = True
else:
    yolo_on = False

if args["roi"]:
    roi = tuple(int(v) for v in args["roi"].split(","))
else:
    roi = None

smart_monitor = None
smart_detection_interval = 0.25
ptz_poller = None
dataset_dir = args["dataset_dir"]
property_alert_engine = None
public_area_classifier = None
public_area_probability = None
dvrip_event_mode = False
dvrip_config = {}
if args["smart_config"]:
    if not yolo_on or "person" not in yolo_list:
        parser.error("--smart_config requires --yolo to include person")
    from smart_monitor import SmartMonitor
    with open(args["smart_config"], encoding="utf-8") as smart_config_file:
        smart_config = json.load(smart_config_file)
    smart_monitor = SmartMonitor(smart_config)
    smart_detection_interval = float(smart_config.get("detection", {}).get("interval_seconds", 0.25))
    if smart_config.get("onvif", {}).get("enabled", False):
        from ptz_status import PtzStatusPoller
        ptz_poller = PtzStatusPoller(smart_config["onvif"])
    classifier_config = smart_config.get("public_area_classifier", {})
    if classifier_config.get("enabled", False):
        from public_area import PublicAreaClassifier
        from property_alerts import PropertyAlertEngine
        model_path = classifier_config.get("model_path", "models/public-area/model.pt")
        if not os.path.exists(model_path):
            parser.error(f"Public-area model was not found: {model_path}")
        public_area_classifier = PublicAreaClassifier(model_path, tuple(smart_config["layout"]["ptz"]), classifier_config.get("device", "auto"))
        property_alert_engine = PropertyAlertEngine(smart_config, smart_monitor.event_sink.emit)
    dvrip_config = smart_config.get("dvrip", {})
    dvrip_event_mode = dvrip_config.get("listen_events", False) if args["dvrip_events"] is None else args["dvrip_events"]

if dataset_dir and not monitor:
    parser.error("--dataset_dir requires --monitor so you can save samples with the C key")
if dataset_dir and not smart_monitor:
    parser.error("--dataset_dir requires --smart_config so the PTZ portion of the combined stream is known")
if dvrip_event_mode and not smart_monitor:
    parser.error("--dvrip_events requires --smart_config with DVRIP settings")

# Set up variables for YOLO detection
if yolo_on:
    from ultralytics import YOLO
    stop_error = False

    CONFIDENCE = 0.5
    font_scale = 1
    thickness = 1
    labels = open("coco.names").read().strip().split("\n")
    colors = np.random.randint(0, 255, size=(len(labels), 3), dtype="uint8")
    model = YOLO(args["model"]+".pt")

    # Check if the user provided list has valid objects
    for coconame in yolo_list:
        if coconame not in labels:
            print("Error! '"+coconame+"' not found in coco.names")
            stop_error = True
    if stop_error:
        exit("Exiting")

# Set up other internal variables
loop = True
ffmpeg_pipe_proc = None
analysis_stream_enabled = threading.Event()
analysis_lock = threading.Lock()
analysis_deadline = 0.0
analysis_seconds = float(dvrip_config.get("analysis_seconds", 10))
# Keeping one low-latency RTSP decoder open avoids losing the subject while
# ffmpeg negotiates a brand-new RTSP session after the camera's alarm.  DVRIP
# still controls *when* YOLO/classification runs; it does not keep inference
# running continuously.
keep_rtsp_warm = bool(dvrip_config.get("keep_rtsp_warm", dvrip_event_mode))
dvrip_listener = None

if not dvrip_event_mode:
    analysis_stream_enabled.set()


def activate_event_analysis(event):
    """Open or extend the RTSP analysis window only for confirmed human starts."""
    global analysis_deadline
    if event.get("type") != "person" or event.get("status") != "Start":
        return
    with analysis_lock:
        analysis_deadline = max(analysis_deadline, time.monotonic() + analysis_seconds)
        analysis_stream_enabled.set()
    print("DVRIP human event: RTSP analysis window opened")


def analysis_window_active() -> bool:
    if not dvrip_event_mode:
        return True
    with analysis_lock:
        return time.monotonic() < analysis_deadline

def probe_stream_info(url):
    cmd = [
        'ffprobe', '-v', 'error', '-print_format', 'json',
        '-show_streams', '-rtsp_transport', 'tcp', url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError as error:
        raise RuntimeError("ffprobe was not found. Install ffmpeg and ensure ffprobe is on PATH.") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Timed out while connecting to the video stream.") from error

    try:
        info = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError("ffprobe returned invalid output while probing the video stream.") from error

    for stream in info.get('streams', []):
        if stream['codec_type'] == 'video':
            w = stream['width']
            h = stream['height']
            fps_str = stream.get('r_frame_rate') or stream.get('avg_frame_rate', '25/1')
            num, den = fps_str.split('/')
            return w, h, float(num) / float(den)
    details = (result.stderr or "No video stream was returned").strip()
    # Do not include the URL here because RTSP URLs commonly contain credentials.
    raise RuntimeError(f"Could not open a video stream with ffprobe: {details}")

print("Probing stream...")
stream_width, stream_height, fps = probe_stream_info(rtsp_stream)
print(f"Stream info: {stream_width}x{stream_height} @ {fps:.2f} fps")

period = 1/fps
tail_length = tail_length*fps
recording = False
ffmpeg_copy = 0
activity_count = 0
yolo_count = 0
last_smart_detection = 0.0
last_smart_result = {"object_found": False, "person_found": False, "alert": False}

if stream_width / stream_height > 1.55:
    res = (256, 144)
else:
    res = (216, 162)
blank = np.zeros((res[1], res[0]), np.uint8)
img = np.zeros((stream_height, stream_width, 3), np.uint8)
# Keep a clean copy for the training dataset. The monitor image receives YOLO
# boxes and calibration overlays, neither of which belongs in training data.
raw_img = img.copy()
resized_frame = cv2.resize(img, res)
gray_frame = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2GRAY)
old_frame = cv2.GaussianBlur(gray_frame, (5, 5), 0)
if monitor:
    # Keep the composite stream's original aspect ratio. Without this flag a
    # manually resized HighGUI window can make calibration overlays look wrong.
    cv2.namedWindow(rtsp_stream, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)

mouse_position = None

def monitor_mouse(event, x, y, _flags, _userdata):
    """Convert monitor-window coordinates back to raw stream coordinates."""
    global mouse_position
    if event != cv2.EVENT_MOUSEMOVE:
        return
    try:
        _, _, displayed_width, displayed_height = cv2.getWindowImageRect(rtsp_stream)
    except cv2.error:
        displayed_width, displayed_height = stream_width, stream_height
    displayed_width = displayed_width or stream_width
    displayed_height = displayed_height or stream_height
    mouse_position = (
        max(0, min(stream_width - 1, round(x * stream_width / displayed_width))),
        max(0, min(stream_height - 1, round(y * stream_height / displayed_height))),
    )

if monitor:
    cv2.setMouseCallback(rtsp_stream, monitor_mouse)


def save_ptz_sample():
    """Save the current unannotated PTZ crop and enough metadata to label it later."""
    if not dataset_dir or not smart_monitor:
        return

    ptz_region = smart_monitor.regions.get("ptz")
    if not ptz_region:
        print("Dataset sample was not saved: smart config has no 'ptz' layout region.")
        return

    x1, y1, x2, y2 = ptz_region.bounds
    sample = raw_img[y1:y2, x1:x2]
    if sample.size == 0:
        print("Dataset sample was not saved: PTZ bounds are outside the current stream.")
        return

    captured_at = datetime.now()
    folder = os.path.join(dataset_dir, "raw", captured_at.strftime("%Y-%m-%d"))
    os.makedirs(folder, exist_ok=True)
    stem = captured_at.strftime("ptz_%Y%m%d_%H%M%S_%f")
    image_path = os.path.join(folder, stem + ".jpg")
    metadata_path = os.path.join(folder, stem + ".json")

    if not cv2.imwrite(image_path, sample, [cv2.IMWRITE_JPEG_QUALITY, 95]):
        print("Dataset sample was not saved: OpenCV could not write the JPEG.")
        return

    metadata = {
        "captured_at": captured_at.astimezone().isoformat(),
        "source_resolution": [stream_width, stream_height],
        "ptz_bounds_in_source": [x1, y1, x2, y2],
        "sample_resolution": [int(sample.shape[1]), int(sample.shape[0])],
        "label_status": "unlabeled",
    }
    if ptz_poller:
        status = ptz_poller.snapshot()
        metadata["onvif_status"] = {
            key: status.get(key) for key in ("state", "profile", "pan", "tilt", "zoom", "updated_at")
        }
    with open(metadata_path, "w", encoding="utf-8") as metadata_file:
        json.dump(metadata, metadata_file, ensure_ascii=False, indent=2)
    print(f"Saved PTZ classifier sample: {image_path}")

if ptz_poller:
    ptz_poller.start()

if dvrip_event_mode:
    from dvrip_events import DVRIPAlarmListener
    endpoint = urlparse(smart_config["onvif"]["device_service"])
    dvrip_host = dvrip_config.get("host", endpoint.hostname)
    dvrip_port = int(dvrip_config.get("port", 34567))
    dvrip_username = os.environ.get(dvrip_config.get("username_env", "CAMERA_DVRIP_USER")) or os.environ.get(smart_config["onvif"].get("username_env", "CAMERA_ONVIF_USER"))
    dvrip_password = os.environ.get(dvrip_config.get("password_env", "CAMERA_DVRIP_PASSWORD")) or os.environ.get(smart_config["onvif"].get("password_env", "CAMERA_ONVIF_PASSWORD"))
    if not dvrip_host or not dvrip_username or not dvrip_password:
        parser.error("DVRIP events require the credential variables named in smart-monitor.json")
    dvrip_listener = DVRIPAlarmListener(
        dvrip_host, dvrip_port, dvrip_username, dvrip_password,
        on_event=activate_event_analysis,
        on_status=lambda message: print("DVRIP " + message),
    )

# used to suppress C errors from ffmpeg library when trying to reconnect camera
class suppress_stdout_stderr(object):
    def __enter__(self):
        self.outnull_file = open(os.devnull, 'w')
        self.errnull_file = open(os.devnull, 'w')
        self.old_stdout_fileno_undup    = sys.stdout.fileno()
        self.old_stderr_fileno_undup    = sys.stderr.fileno()
        self.old_stdout_fileno = os.dup ( sys.stdout.fileno() )
        self.old_stderr_fileno = os.dup ( sys.stderr.fileno() )
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr
        os.dup2 ( self.outnull_file.fileno(), self.old_stdout_fileno_undup )
        os.dup2 ( self.errnull_file.fileno(), self.old_stderr_fileno_undup )
        sys.stdout = self.outnull_file
        sys.stderr = self.errnull_file
        return self
    def __exit__(self, *_):
        sys.stdout = self.old_stdout
        sys.stderr = self.old_stderr
        os.dup2 ( self.old_stdout_fileno, self.old_stdout_fileno_undup )
        os.dup2 ( self.old_stderr_fileno, self.old_stderr_fileno_undup )
        os.close ( self.old_stdout_fileno )
        os.close ( self.old_stderr_fileno )
        self.outnull_file.close()
        self.errnull_file.close()

def open_ffmpeg_pipe():
    cmd = [
        'ffmpeg', '-loglevel', 'quiet',
        '-rtsp_transport', 'tcp',
        # Bound stalled network reads. This is especially important because
        # this process writes raw frames to a pipe and the reader otherwise
        # has no frame-level timeout of its own.
        '-rw_timeout', '5000000',
        '-fflags', 'nobuffer', '-flags', 'low_delay',
        '-i', rtsp_stream,
        '-f', 'rawvideo', '-pix_fmt', 'bgr24',
        'pipe:1',
    ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=10**8,
    )

q = queue.Queue(maxsize=2)


def queue_latest_frame(frame):
    """Keep RTSP latency bounded while inference is slower than the camera."""
    while True:
        try:
            q.put_nowait(frame)
            return
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                return


# Thread for receiving stream frames. In DVRIP mode the default is to keep the
# decoder warm, so a DVRIP Start can analyse an already-current frame instead
# of waiting several seconds for a new RTSP handshake.
def receive_frames():
    global ffmpeg_pipe_proc
    frame_size = stream_width * stream_height * 3
    proc = None
    while loop:
        should_receive = keep_rtsp_warm or analysis_stream_enabled.is_set()
        if not should_receive:
            if proc and proc.poll() is None:
                proc.kill()
            proc = None
            ffmpeg_pipe_proc = None
            # Do not wait indefinitely here: a later DVRIP event wakes the
            # receiver on the next short polling interval.
            time.sleep(0.10)
            continue
        if proc is None:
            try:
                proc = open_ffmpeg_pipe()
                ffmpeg_pipe_proc = proc
                print(datetime.now().strftime('%H-%M-%S') + " RTSP analysis connected")
            except Exception:
                time.sleep(2)
                continue
        raw = proc.stdout.read(frame_size)
        if len(raw) != frame_size:
            if proc.poll() is None:
                proc.kill()
            proc = None
            ffmpeg_pipe_proc = None
            if analysis_stream_enabled.is_set():
                print(datetime.now().strftime('%H-%M-%S') + " RTSP unavailable; retrying")
                time.sleep(2)
            continue
        queue_latest_frame(np.frombuffer(raw, np.uint8).reshape((stream_height, stream_width, 3)).copy())
    if proc and proc.poll() is None:
        proc.kill()

# Record the stream when object is detected
def start_ffmpeg():
    try:
        ffmpeg_copy.execute()
    except:
        print("Issue recording the stream. Trying again.")
        time.sleep(1)
        ffmpeg_copy.execute()

def stop_ffmpeg():
    global ffmpeg_copy, recording
    ffmpeg_copy.terminate()
    ffmpeg_copy = 0
    recording = False

# Functions for detecting key presses
def press(key):
    global loop
    if key == 'q':
        loop = False

def input_keyboard():
    listen_keyboard(
        on_press=press,
    )

def timer():
    delay = False
    period = 2
    now = datetime.now()
    now_time = now.time()
    start1 = now_time.replace(hour=0, minute=0, second=0, microsecond=0)
    start2 = now_time.replace(hour=0, minute=0, second=2, microsecond=10000)
    start_t=time.time()
    while loop:
        now = datetime.now()
        now_time = now.time()
        if(now_time>=start1 and now_time<=start2):
            day_num = now.weekday()
            if day_num == 0: print("Monday "+now.strftime('%m-%d-%Y'))
            elif day_num == 1: print("Tuesday "+now.strftime('%m-%d-%Y'))
            elif day_num == 2: print("Wednesday "+now.strftime('%m-%d-%Y'))
            elif day_num == 3: print("Thursday "+now.strftime('%m-%d-%Y'))
            elif day_num == 4: print("Friday "+now.strftime('%m-%d-%Y'))
            elif day_num == 5: print("Saturday "+now.strftime('%m-%d-%Y'))
            elif day_num == 6: print("Sunday "+now.strftime('%m-%d-%Y'))
            delay = True
        time.sleep(period - ((time.time() - start_t) % period))
        if delay:
            delay = False
            time.sleep(period - ((time.time() - start_t) % period))

# Process YOLO object detection
def process_yolo():
    global img, public_area_probability

    if roi:
        x1, y1, x2, y2 = roi
        detect_img = img[y1:y2, x1:x2]
    else:
        detect_img = img
        x1, y1 = 0, 0

    results = model.predict(detect_img, conf=CONFIDENCE, verbose=False)[0]
    object_found = False
    person_boxes = []

    # Loop over the detections
    for data in results.boxes.data.tolist():
        # Get the bounding box coordinates, confidence, and class id
        xmin, ymin, xmax, ymax, confidence, class_id = data

        # Converting the coordinates and the class id to integers, offset by ROI origin
        xmin = int(xmin) + x1
        ymin = int(ymin) + y1
        xmax = int(xmax) + x1
        ymax = int(ymax) + y1
        class_id = int(class_id)

        if labels[class_id] in yolo_list:
            object_found = True
        if labels[class_id] == "person":
            person_boxes.append((xmin, ymin, xmax, ymax))

        # Draw a bounding box rectangle and label on the image
        color = [int(c) for c in colors[class_id]]
        cv2.rectangle(img, (xmin, ymin), (xmax, ymax), color=color, thickness=thickness)
        text = f"{labels[class_id]}: {confidence:.2f}"
        # Calculate text width & height to draw the transparent boxes as background of the text
        (text_width, text_height) = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, fontScale=font_scale, thickness=thickness)[0]
        text_offset_x = xmin
        text_offset_y = ymin - 5
        box_coords = ((text_offset_x, text_offset_y), (text_offset_x + text_width + 2, text_offset_y - text_height))
        overlay = img.copy()
        cv2.rectangle(overlay, box_coords[0], box_coords[1], color=color, thickness=cv2.FILLED)
        # Add opacity (transparency to the box)
        img = cv2.addWeighted(overlay, 0.6, img, 0.4, 0)
        # Now put the text (label: confidence %)
        cv2.putText(img, text, (xmin, ymin - 5), cv2.FONT_HERSHEY_SIMPLEX,
            fontScale=font_scale, color=(0, 0, 0), thickness=thickness)

    now = time.time()
    if property_alert_engine:
        # One segmentation inference per YOLO pass, then use the feet point of
        # each PTZ person against that probability map.
        public_area_probability = public_area_classifier.predict(raw_img)
        events = property_alert_engine.process(person_boxes, public_area_classifier.probability_at, now)
    else:
        events = smart_monitor.process(person_boxes, now) if smart_monitor else []
    return {
        "object_found": object_found,
        "person_found": bool(person_boxes),
        "alert": bool(events),
    }


def start_recording():
    """Start copying the raw stream after a legacy detection or smart event."""
    global ffmpeg_copy, recording, ffmpeg_thread, filename
    filedate = datetime.now().strftime('%H-%M-%S')
    if not testing:
        folderdate = datetime.now().strftime('%Y-%m-%d')
        if not os.path.isdir(folderdate):
            os.mkdir(folderdate)
        filename = '%s/%s.mkv' % (folderdate, filedate)
        ffmpeg_copy = (
            FFmpeg()
            .option("y")
            .input(rtsp_stream, rtsp_transport="tcp", rtsp_flags="prefer_tcp")
            .output(filename, vcodec="copy", acodec="copy")
        )
        ffmpeg_thread = threading.Thread(target=start_ffmpeg)
        ffmpeg_thread.start()
        print(filedate + " recording started")
    else:
        print(filedate + " recording started - Testing mode")
    recording = True


# Start the background threads
receive_thread = threading.Thread(target=receive_frames)
receive_thread.start()
if dvrip_listener:
    dvrip_listener.start()
keyboard_thread = threading.Thread(target=input_keyboard)
keyboard_thread.start()
timer_thread = threading.Thread(target=timer)
timer_thread.start()

# Main loop
while loop:
    if dvrip_event_mode and not recording and not analysis_window_active():
        analysis_stream_enabled.clear()
    if q.empty() != True:
        raw_img = q.get()
        img = raw_img.copy()

        # Resize image, make it grayscale, then blur it
        resized_frame = cv2.resize(img, res)
        gray_frame = cv2.cvtColor(resized_frame,cv2.COLOR_BGR2GRAY)
        final_frame = cv2.GaussianBlur(gray_frame, (5,5), 0)

        # Calculate difference between current and previous frame, then get ssim value
        diff = cv2.absdiff(final_frame, old_frame)
        result = cv2.threshold(diff, 5, 255, cv2.THRESH_BINARY)[1]
        ssim_val = int(ssim(result,blank))
        old_frame = final_frame

        # Print value for testing mode
        if testing and ssim_val > thresh:
            print("motion: "+ str(ssim_val))

        # Count the number of frames where the ssim value exceeds the threshold value.
        # If the number of these frames exceeds start_frames value, run YOLO detection.
        # Start recording if an object from the user provided list is detected
        if not recording:
            now = time.time()
            if dvrip_event_mode:
                # A DVRIP human alarm already opened this window. Run YOLO even
                # if the person has stopped moving before RTSP connected.
                if yolo_on and now - last_smart_detection >= smart_detection_interval:
                    last_smart_result = process_yolo()
                    last_smart_detection = now
                if last_smart_result["alert"]:
                    start_recording()
                    last_smart_result["alert"] = False
            else:
                smart_track_active = smart_monitor and smart_monitor.has_active_tracks(now)
                if ssim_val > thresh or smart_track_active:
                    activity_count += 1
                    if activity_count >= start_frames:
                        if yolo_on:
                            if smart_monitor:
                                if now - last_smart_detection >= smart_detection_interval:
                                    last_smart_result = process_yolo()
                                    last_smart_detection = now
                            else:
                                result = process_yolo()
                                if result["object_found"]:
                                    yolo_count += 1
                                else:
                                    yolo_count = 0
                        smart_trigger = smart_monitor and last_smart_result["alert"]
                        legacy_trigger = not smart_monitor and (not yolo_on or yolo_count > 1)
                        if smart_trigger or legacy_trigger:
                            start_recording()
                            activity_count = 0
                            yolo_count = 0
                            last_smart_result["alert"] = False
                else:
                    activity_count = 0
                    yolo_count = 0

        # If already recording, count the number of frames where there's no motion activity
        # or no object detected and stop recording if it exceeds the tail_length value
        else:
            detection_result = process_yolo() if yolo_on else None
            no_relevant_object = yolo_on and not detection_result["object_found"]
            if no_relevant_object or not yolo_on and ssim_val < thresh:
                activity_count += 1
                if activity_count >= tail_length:
                    filedate = datetime.now().strftime('%H-%M-%S')
                    if not testing:
                        stop_ffmpeg()
                        ffmpeg_thread.join()
                        print(filedate + " recording stopped")
                        # If auto_delete argument was provided, delete recording if total
                        # length is equal to the tail_length value, indicating a false positive
                        if auto_delete:
                            recorded_file = cv2.VideoCapture(filename)
                            recorded_frames = recorded_file.get(cv2.CAP_PROP_FRAME_COUNT)
                            if recorded_frames < tail_length + (fps/2) and os.path.isfile(filename):
                                os.remove(filename)
                                print(filename + " was auto-deleted")
                    else:
                        print(filedate + " recording stopped - Testing mode")
                    recording = False
                    activity_count = 0
            else:
                activity_count = 0

        # Monitor the stream
        if monitor:
            if roi:
                cv2.rectangle(img, (roi[0], roi[1]), (roi[2], roi[3]), (0, 255, 0), 2)
            if public_area_probability is not None and public_area_classifier:
                x1, y1, x2, y2 = public_area_classifier.ptz_bounds
                public_mask = public_area_probability >= property_alert_engine.public_threshold
                ptz_overlay = img[y1:y2, x1:x2]
                public_color = np.array((0, 165, 255), dtype=np.float32)
                ptz_overlay[public_mask] = (0.58 * ptz_overlay[public_mask] + 0.42 * public_color).astype(np.uint8)
            if smart_monitor:
                for region in smart_monitor.regions.values():
                    x1, y1, x2, y2 = region.bounds
                    cv2.rectangle(img, (x1, y1), (x2, y2), (255, 180, 0), 2)
                    cv2.putText(img, region.name, (x1 + 8, y1 + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 180, 0), 2)
                for zone in smart_monitor.zones:
                    points = np.array(zone.polygon, dtype=np.int32)
                    cv2.polylines(img, [points], True, (0, 255, 255), 2)
                    zx, zy = points[0]
                    cv2.putText(img, zone.name, (int(zx) + 8, int(zy) + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            if mouse_position:
                mx, my = mouse_position
                cv2.drawMarker(img, (mx, my), (255, 255, 0), cv2.MARKER_CROSS, 28, 2)
                cv2.putText(img, f"X: {mx}  Y: {my}", (mx + 14, my - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
            if ptz_poller:
                ptz_status = ptz_poller.snapshot()
                if ptz_status["state"] == "connected":
                    age = time.monotonic() - ptz_status["updated_at"] if ptz_status["updated_at"] else -1
                    ptz_text = "PTZ {} #{} age={:.1f}s  pan={:.4f}  tilt={:.4f}  zoom={:.4f}".format(
                        ptz_status["profile"] or "unknown",
                        ptz_status["poll_count"],
                        age,
                        ptz_status["pan"] or 0, ptz_status["tilt"] or 0, ptz_status["zoom"] or 0
                    )
                else:
                    ptz_text = "PTZ " + ptz_status["state"]
                cv2.putText(img, ptz_text, (20, stream_height - 24), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 0), 2)
            if dataset_dir:
                cv2.putText(img, "C: save clean PTZ classifier sample", (20, stream_height - 58), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            if dvrip_event_mode:
                event_text = "DVRIP: analyzing" if analysis_window_active() or recording else "DVRIP: waiting for human event"
                cv2.putText(img, event_text, (20, 86), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 255), 2)
            cv2.imshow(rtsp_stream, img)
            if frame_click:
                cv_key = cv2.waitKey(0) & 0xFF
                if cv_key == ord("q"):
                    loop = False
                elif cv_key == ord("c"):
                    save_ptz_sample()
                if cv_key == ord("n"):
                    continue
            else:
                cv_key = cv2.waitKey(1) & 0xFF
                if cv_key == ord('q'):
                    loop = False
                elif cv_key == ord('c'):
                    save_ptz_sample()
    else:
        time.sleep(period/2)

# Gracefully end threads and exit
stop_listening()
if ffmpeg_copy:
    ffmpeg_copy.terminate()
    ffmpeg_thread.join()
if ffmpeg_pipe_proc and ffmpeg_pipe_proc.poll() is None:
    ffmpeg_pipe_proc.kill()
if ptz_poller:
    ptz_poller.stop()
if dvrip_listener:
    dvrip_listener.stop()
receive_thread.join()
keyboard_thread.join()
timer_thread.join()
cv2.destroyAllWindows()
print("Exiting")
