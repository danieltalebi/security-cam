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
from ffmpeg import FFmpeg
from skimage.metrics import mean_squared_error as ssim
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter, BooleanOptionalAction
from sshkeyboard import listen_keyboard, stop_listening

# Parse command line arguments
parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
parser.add_argument("--stream", type=str, help="RTSP address of video stream.")
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
args = vars(parser.parse_args())

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

def probe_stream_info(url):
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_streams', '-rtsp_transport', 'tcp', url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    info = json.loads(result.stdout)
    for stream in info['streams']:
        if stream['codec_type'] == 'video':
            w = stream['width']
            h = stream['height']
            fps_str = stream.get('r_frame_rate') or stream.get('avg_frame_rate', '25/1')
            num, den = fps_str.split('/')
            return w, h, float(num) / float(den)
    raise RuntimeError("No video stream found in ffprobe output")

print("Probing stream...")
stream_width, stream_height, fps = probe_stream_info(rtsp_stream)
print(f"Stream info: {stream_width}x{stream_height} @ {fps:.2f} fps")

period = 1/fps
tail_length = tail_length*fps
recording = False
ffmpeg_copy = 0
activity_count = 0
yolo_count = 0

if stream_width / stream_height > 1.55:
    res = (256, 144)
else:
    res = (216, 162)
blank = np.zeros((res[1], res[0]), np.uint8)
img = np.zeros((stream_height, stream_width, 3), np.uint8)
resized_frame = cv2.resize(img, res)
gray_frame = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2GRAY)
old_frame = cv2.GaussianBlur(gray_frame, (5, 5), 0)
if monitor:
    cv2.namedWindow(rtsp_stream, cv2.WINDOW_NORMAL)

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

q = queue.Queue()
# Thread for receiving the stream's frames so they can be processed
# If camera disconnects it will automatically try to reconnect every 5 seconds
def receive_frames():
    global ffmpeg_pipe_proc
    frame_size = stream_width * stream_height * 3
    proc = open_ffmpeg_pipe()
    ffmpeg_pipe_proc = proc
    while loop:
        raw = proc.stdout.read(frame_size)
        if len(raw) < frame_size:
            if recording:
                stop_ffmpeg()
            now_time = datetime.now().strftime('%H-%M-%S')
            print(now_time + " Camera disconnected. Attempting to reconnect.")
            proc.kill()
            proc = None
            while loop:
                time.sleep(5)
                try:
                    proc = open_ffmpeg_pipe()
                    raw_test = proc.stdout.read(frame_size)
                    if len(raw_test) == frame_size:
                        now_time = datetime.now().strftime('%H-%M-%S')
                        print(now_time + " Camera successfully reconnected.")
                        ffmpeg_pipe_proc = proc
                        q.put(np.frombuffer(raw_test, np.uint8).reshape((stream_height, stream_width, 3)).copy())
                        break
                    proc.kill()
                except Exception:
                    if proc and proc.poll() is None:
                        proc.kill()
        else:
            q.put(np.frombuffer(raw, np.uint8).reshape((stream_height, stream_width, 3)).copy())

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
    global img

    if roi:
        x1, y1, x2, y2 = roi
        detect_img = img[y1:y2, x1:x2]
    else:
        detect_img = img
        x1, y1 = 0, 0

    results = model.predict(detect_img, conf=CONFIDENCE, verbose=False)[0]
    object_found = False

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

    return object_found


# Start the background threads
receive_thread = threading.Thread(target=receive_frames)
receive_thread.start()
keyboard_thread = threading.Thread(target=input_keyboard)
keyboard_thread.start()
timer_thread = threading.Thread(target=timer)
timer_thread.start()

# Main loop
while loop:
    if q.empty() != True:
        img = q.get()

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
            if ssim_val > thresh:
                activity_count += 1
                if activity_count >= start_frames:
                    if yolo_on:
                        if process_yolo():
                            yolo_count += 1
                        else:
                            yolo_count = 0
                    if not yolo_on or yolo_count > 1:
                        filedate = datetime.now().strftime('%H-%M-%S')
                        if not testing:
                            folderdate = datetime.now().strftime('%Y-%m-%d')
                            if not os.path.isdir(folderdate):
                                os.mkdir(folderdate)
                            filename = '%s/%s.mkv' % (folderdate,filedate)
                            ffmpeg_copy = (
                                FFmpeg()
                                .option("y")
                                .input(
                                    rtsp_stream,
                                    rtsp_transport="tcp",
                                    rtsp_flags="prefer_tcp",
                                )
                                .output(filename, vcodec="copy", acodec="copy")
                            )
                            ffmpeg_thread = threading.Thread(target=start_ffmpeg)
                            ffmpeg_thread.start()
                            print(filedate + " recording started")
                        else:
                            print(filedate + " recording started - Testing mode")
                        recording = True
                        activity_count = 0
                        yolo_count = 0
            else:
                activity_count = 0
                yolo_count = 0

        # If already recording, count the number of frames where there's no motion activity
        # or no object detected and stop recording if it exceeds the tail_length value
        else:
            if yolo_on and not process_yolo() or not yolo_on and ssim_val < thresh:
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
            cv2.imshow(rtsp_stream, img)
            if frame_click:
                cv_key = cv2.waitKey(0) & 0xFF
                if cv_key == ord("q"):
                    loop = False
                if cv_key == ord("n"):
                    continue
            else:
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    loop = False
    else:
        time.sleep(period/2)

# Gracefully end threads and exit
stop_listening()
if ffmpeg_copy:
    ffmpeg_copy.terminate()
    ffmpeg_thread.join()
if ffmpeg_pipe_proc and ffmpeg_pipe_proc.poll() is None:
    ffmpeg_pipe_proc.kill()
receive_thread.join()
keyboard_thread.join()
timer_thread.join()
cv2.destroyAllWindows()
print("Exiting")
