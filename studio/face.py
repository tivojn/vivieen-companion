"""MediaPipe FaceLandmarker wrapper + the landmark groups the pipeline needs.

mediapipe 0.10.35 dropped mp.solutions - everything goes through
mediapipe.tasks.python.vision.FaceLandmarker with a downloaded .task model.
"""
import os, math, warnings, hashlib, tempfile, threading, urllib.request
warnings.filterwarnings("ignore")
import numpy as np, cv2
import mediapipe as mp
from mediapipe.tasks import python as mpp
from mediapipe.tasks.python import vision

CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.abspath(os.environ.get(
    "VIVIEEN_FACE_MODEL",
    os.path.join(CODE_ROOT, "models", "face_landmarker.task")))
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")
MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
MODEL_LIMIT = 8 * 1024 * 1024

# Rigid anchors - everything that must NOT move with speech.  Nose, alar base,
# nose bridge, cheeks, infraorbital, jaw hinge, philtrum.  Every lip and chin
# point is deliberately excluded so an affine fitted here captures head-pose
# error WITHOUT flattening the viseme's mouth shape.
RIGID = [1, 2, 164, 98, 327, 6, 168, 4, 5, 195, 197,
         205, 425, 50, 280, 101, 330, 36, 266, 203, 423,
         234, 454, 93, 323, 132, 361, 58, 288]

OUTER_LIP = [61,146,91,181,84,17,314,405,321,375,291,409,270,269,267,0,37,39,40,185]
CHIN      = [17,18,200,199,175,152,148,377,176,400,32,262,171,396,140,369,150,149,378,379]
FACE_OVAL = [10,338,297,332,284,251,389,356,454,323,361,288,397,365,379,378,400,377,152,
             148,176,149,150,136,172,58,132,93,234,127,162,21,54,103,67,109]
EYE_L = [263,249,390,373,374,380,381,382,362,398,384,385,386,387,388,466]
EYE_R = [33,7,163,144,145,153,154,155,133,173,157,158,159,160,161,246]
BROW_L = [336,296,334,293,300,276,283,282,295,285]
BROW_R = [107,66,105,63,70,46,53,52,65,55]

MOUTH_L, MOUTH_R, PHILTRUM = 61, 291, 164
EYE_L_OUT, EYE_R_OUT = 263, 33
NOSE_TIP = 1

_det = None
_det_lock = threading.Lock()


def _model_hash(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _ensure_model():
    if _model_hash(MODEL) == MODEL_SHA256:
        return
    directory = os.path.dirname(MODEL)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".face-landmarker-", dir=directory)
    try:
        digest = hashlib.sha256()
        total = 0
        print("[viv] downloading the public Face Landmarker model...", flush=True)
        with os.fdopen(descriptor, "wb") as output, \
             urllib.request.urlopen(MODEL_URL, timeout=90) as response:
            if not response.geturl().startswith("https://"):
                raise RuntimeError("Face Landmarker download was redirected to an insecure URL")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MODEL_LIMIT:
                    raise RuntimeError("Face Landmarker download exceeded its size limit")
                output.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            raise RuntimeError("Face Landmarker download failed checksum verification")
        os.chmod(temporary, 0o600)
        os.replace(temporary, MODEL)
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            "Face Landmarker download failed; connect to the internet and retry") from error
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def detector():
    global _det
    if _det is None:
        with _det_lock:
            if _det is None:
                _ensure_model()
                _det = vision.FaceLandmarker.create_from_options(
                    vision.FaceLandmarkerOptions(
                        base_options=mpp.BaseOptions(model_asset_path=MODEL),
                        running_mode=vision.RunningMode.IMAGE, num_faces=1,
                        output_facial_transformation_matrixes=True,
                        min_face_detection_confidence=0.3))
    return _det


def detect(bgr):
    """-> (478x2 landmark array in pixels, 4x4 facial transform) or (None, None)."""
    h, w = bgr.shape[:2]
    r = detector().detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                   data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))
    if not r.face_landmarks:
        return None, None
    lm = np.array([[l.x * w, l.y * h] for l in r.face_landmarks[0]], np.float32)
    M = (np.array(r.facial_transformation_matrixes[0])
         if getattr(r, "facial_transformation_matrixes", None) else None)
    return lm, M


def pose_angles(M):
    """Decompose the facial transformation matrix -> (yaw, pitch, roll) degrees."""
    if M is None:
        return None
    R = np.asarray(M)[:3, :3]
    sy = math.hypot(R[0, 0], R[1, 0])
    return (math.degrees(math.atan2(-R[2, 0], sy)),
            math.degrees(math.atan2(R[2, 1], R[2, 2])),
            math.degrees(math.atan2(R[1, 0], R[0, 0])))


def foreshortening(lm):
    """Philtrum projected onto the mouth-corner line, as a left/right ratio.

    1.00 = perfectly frontal mouth.  A turned head pushes it away from 1.
    This is the metric that exposed 'the mouth was drawn on a frontal prior
    but the head is turned'.
    """
    a, b, p = lm[MOUTH_L], lm[MOUTH_R], lm[PHILTRUM]
    d = b - a
    t = float(np.dot(p - a, d) / (np.dot(d, d) + 1e-9))
    t = min(max(t, 1e-3), 1 - 1e-3)
    return t / (1 - t)


def metrics(lm, M=None):
    yaw = pitch = roll = None
    if M is not None:
        yaw, pitch, roll = pose_angles(M)
    return dict(
        eye_span=float(np.linalg.norm(lm[EYE_L_OUT] - lm[EYE_R_OUT])),
        nose=[float(lm[NOSE_TIP][0]), float(lm[NOSE_TIP][1])],
        mouth_centre=[float(lm[OUTER_LIP].mean(0)[0]), float(lm[OUTER_LIP].mean(0)[1])],
        foreshortening=foreshortening(lm),
        yaw=yaw, pitch=pitch, roll=roll)


def hull_mask(shape, lm, idx, dilate=0, close_poly=True):
    m = np.zeros(shape[:2], np.uint8)
    pts = cv2.convexHull(lm[idx].astype(np.int32))
    cv2.fillConvexPoly(m, pts, 255)
    if dilate:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate, dilate))
        m = cv2.dilate(m, k)
    return m
