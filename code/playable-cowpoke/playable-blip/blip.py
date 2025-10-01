import cv2, torch, time
from transformers import BlipProcessor, BlipForConditionalGeneration

# --- settings ---
MOVIES_DIR = "/Volumes/PLAYABLE-D/project/movies/"
MOVIE_FILE = "3-10-To-Yuma(1957){tmdb-14168}.mp4"
MODEL_DIR  = "./models/blip-image-captioning-base"
N_SECONDS  = 2
START_TIME = "00:02:15.000"   # start point in hh:mm:ss.mmm
WARMUP_SAMPLES = 0

# --- helpers ---
def parse_timecode(tc: str) -> float:
    hh, mm, ss = tc.split(":")
    secs = int(hh) * 3600 + int(mm) * 60 + float(ss)
    return secs

def ts_from_frame(idx, fps):
    secs = idx / fps
    h = int(secs // 3600); m = int((secs % 3600) // 60); s = int(secs % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

# --- load model ---
processor = BlipProcessor.from_pretrained(MODEL_DIR, use_fast=True)
model = BlipForConditionalGeneration.from_pretrained(MODEL_DIR)
device = "cuda" if torch.cuda.is_available() else "cpu"
model.to(device)

# --- open video ---
cap = cv2.VideoCapture(MOVIES_DIR + MOVIE_FILE)
fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
frame_jump = int(fps * N_SECONDS)

# seek to start time
start_secs = parse_timecode(START_TIME)
start_frame = int(start_secs * fps)
cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

# --- timing accumulators ---
n, total = 0, 0.0

cv2.namedWindow("BLIP preview", cv2.WINDOW_NORMAL)
cv2.resizeWindow("BLIP preview", 480, 270)

try:
    with torch.inference_mode():
        while True:
            ok, frame_bgr = cap.read()
            if not ok: break
            frame_idx = int(cap.get(cv2.CAP_PROP_POS_FRAMES))

            # preview & quit on 'q'
            cv2.imshow("BLIP preview", frame_bgr)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                print("\n⏹ Quit by user.")
                break

            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

            if device == "cuda":
                torch.cuda.synchronize()
            t0 = time.perf_counter()

            inputs = processor(images=frame_rgb, return_tensors="pt").to(device)
            out = model.generate(**inputs, max_new_tokens=20)

            if device == "cuda":
                torch.cuda.synchronize()
            dt = time.perf_counter() - t0

            caption = processor.decode(out[0], skip_special_tokens=True)
            if n >= WARMUP_SAMPLES:
                total += dt
            n += 1

            print(f"[{ts_from_frame(frame_idx, fps)}] {caption}  ({dt:.3f}s)")

            # jump ahead by N seconds
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx + frame_jump)

except KeyboardInterrupt:
    print("\n⏹ Interrupted by user.")
finally:
    cap.release()
    cv2.destroyAllWindows()

# --- summary stats ---
effective = max(0, n - WARMUP_SAMPLES)
if effective > 0:
    avg = total / effective
    print("\n--- Inference timing ---")
    print(f"Samples timed: {effective}")
    print(f"Avg latency per caption: {avg:.3f}s")
else:
    print("\nNo timed samples recorded.")