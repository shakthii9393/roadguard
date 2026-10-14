"""Train a YOLO model, then copy best weights to backend/ml/weights/best.pt

    pip install ultralytics
    python backend/ml/train.py --epochs 50

Datasets to start from (check licences):
  * RDD2022 (Road Damage Dataset) - cracks, potholes; includes India
  * Roboflow Universe - search "pothole", "waterlogging", "road sign damage"
  * Your own photos, labelled with Roboflow / CVAT / LabelImg (YOLO format)
"""
import argparse
import shutil
from pathlib import Path

from ultralytics import YOLO

HERE = Path(__file__).parent

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--base", default="yolov8n.pt", help="pretrained checkpoint to fine-tune")
    args = ap.parse_args()

    model = YOLO(args.base)
    model.train(data=str(HERE / "data.yaml"), epochs=args.epochs, imgsz=args.imgsz, project=str(HERE / "runs"), name="roadguard")

    best = HERE / "runs" / "roadguard" / "weights" / "best.pt"
    shutil.copy(best, HERE / "weights" / "best.pt")
    print("Saved ->", HERE / "weights" / "best.pt")
