"""Train a small local segmenter that identifies visible public area pixels.

Inputs are created by label_property_video.py.  This deliberately keeps every
source video entirely in either train or validation so nearly identical frames
cannot make validation results look better than they really are.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import torch
from torch import Tensor, nn
from torch.utils.data import DataLoader, Dataset


@dataclass(frozen=True)
class Sample:
    image_path: Path
    mask_path: Path
    source_video: str


def load_samples(dataset_dir: Path) -> list[Sample]:
    annotation_dir = dataset_dir / "annotations"
    image_dir = dataset_dir / "images"
    mask_dir = dataset_dir / "masks"
    samples = []
    for annotation_path in sorted(annotation_dir.glob("*.json")):
        stem = annotation_path.stem
        image_path = image_dir / f"{stem}.jpg"
        mask_path = mask_dir / f"{stem}.png"
        if not image_path.exists() or not mask_path.exists():
            raise RuntimeError(f"Missing image or mask for {annotation_path.name}")
        metadata = json.loads(annotation_path.read_text(encoding="utf-8"))
        samples.append(Sample(image_path, mask_path, metadata["source_video"]))
    if not samples:
        raise RuntimeError(f"No labeled samples found in {dataset_dir}")
    return samples


def split_by_video(samples: list[Sample], validation_fraction: float, seed: int) -> tuple[list[Sample], list[Sample], list[str]]:
    by_video: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        by_video[sample.source_video].append(sample)
    videos = sorted(by_video)
    if len(videos) < 2:
        raise RuntimeError("At least two source videos are required for a trustworthy validation split.")
    random.Random(seed).shuffle(videos)
    validation_count = max(1, min(len(videos) - 1, round(len(videos) * validation_fraction)))
    validation_videos = set(videos[:validation_count])
    train = [sample for sample in samples if sample.source_video not in validation_videos]
    validation = [sample for sample in samples if sample.source_video in validation_videos]
    return train, validation, sorted(validation_videos)


class PublicAreaDataset(Dataset):
    def __init__(self, samples: list[Sample], image_size: int, augment: bool):
        self.samples = samples
        self.image_size = image_size
        self.augment = augment

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> tuple[Tensor, Tensor]:
        sample = self.samples[index]
        image = cv2.imread(str(sample.image_path), cv2.IMREAD_COLOR)
        mask = cv2.imread(str(sample.mask_path), cv2.IMREAD_GRAYSCALE)
        if image is None or mask is None:
            raise RuntimeError(f"Could not read {sample.image_path.name}")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = cv2.resize(image, (self.image_size, self.image_size), interpolation=cv2.INTER_AREA)
        mask = cv2.resize(mask, (self.image_size, self.image_size), interpolation=cv2.INTER_NEAREST)

        if self.augment and random.random() < 0.5:
            image = np.ascontiguousarray(image[:, ::-1])
            mask = np.ascontiguousarray(mask[:, ::-1])
        image_tensor = torch.from_numpy(image.transpose(2, 0, 1)).float() / 255.0
        if self.augment:
            image_tensor = (image_tensor * random.uniform(0.8, 1.2) + random.uniform(-0.08, 0.08)).clamp(0, 1)
        mask_tensor = torch.from_numpy((mask == 1).astype(np.float32)).unsqueeze(0)
        return image_tensor, mask_tensor


class DoubleConv(nn.Module):
    def __init__(self, in_channels: int, out_channels: int):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, 3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )

    def forward(self, x: Tensor) -> Tensor:
        return self.layers(x)


class TinyUNet(nn.Module):
    """Small enough for CPU training, while retaining spatial detail for masks."""
    def __init__(self, base_channels: int = 16):
        super().__init__()
        b = base_channels
        self.down1 = DoubleConv(3, b)
        self.down2 = DoubleConv(b, b * 2)
        self.down3 = DoubleConv(b * 2, b * 4)
        self.bridge = DoubleConv(b * 4, b * 8)
        self.pool = nn.MaxPool2d(2)
        self.up3 = nn.ConvTranspose2d(b * 8, b * 4, 2, stride=2)
        self.dec3 = DoubleConv(b * 8, b * 4)
        self.up2 = nn.ConvTranspose2d(b * 4, b * 2, 2, stride=2)
        self.dec2 = DoubleConv(b * 4, b * 2)
        self.up1 = nn.ConvTranspose2d(b * 2, b, 2, stride=2)
        self.dec1 = DoubleConv(b * 2, b)
        self.output = nn.Conv2d(b, 1, 1)

    def forward(self, x: Tensor) -> Tensor:
        x1 = self.down1(x)
        x2 = self.down2(self.pool(x1))
        x3 = self.down3(self.pool(x2))
        x4 = self.bridge(self.pool(x3))
        y3 = self.dec3(torch.cat([self.up3(x4), x3], dim=1))
        y2 = self.dec2(torch.cat([self.up2(y3), x2], dim=1))
        y1 = self.dec1(torch.cat([self.up1(y2), x1], dim=1))
        return self.output(y1)


def metrics_from_logits(logits: Tensor, targets: Tensor) -> tuple[int, int, int, int]:
    predicted = torch.sigmoid(logits) >= 0.5
    actual = targets >= 0.5
    true_positive = int((predicted & actual).sum().item())
    false_positive = int((predicted & ~actual).sum().item())
    false_negative = int((~predicted & actual).sum().item())
    true_negative = int((~predicted & ~actual).sum().item())
    return true_positive, false_positive, false_negative, true_negative


def summarize_metrics(counts: tuple[int, int, int, int]) -> dict[str, float]:
    tp, fp, fn, tn = counts
    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    iou = tp / max(1, tp + fp + fn)
    return {"precision": precision, "recall": recall, "iou": iou, "accuracy": (tp + tn) / max(1, tp + fp + fn + tn)}


def evaluate(model: nn.Module, loader: DataLoader, loss_function: nn.Module, device: torch.device) -> tuple[float, dict[str, float]]:
    model.eval()
    loss_total = 0.0
    counts = np.zeros(4, dtype=np.int64)
    with torch.no_grad():
        for images, masks in loader:
            images, masks = images.to(device), masks.to(device)
            logits = model(images)
            loss_total += float(loss_function(logits, masks).item()) * len(images)
            counts += np.array(metrics_from_logits(logits, masks))
    return loss_total / len(loader.dataset), summarize_metrics(tuple(int(value) for value in counts))


def main() -> None:
    parser = argparse.ArgumentParser(description="Train a local public-area segmentation model from labeled camera frames.")
    parser.add_argument("--dataset_dir", default="dataset/labels", help="Directory produced by label_property_video.py")
    parser.add_argument("--output_dir", default="models/public-area", help="Where model.pt and metrics are written")
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--batch_size", type=int, default=4)
    parser.add_argument("--image_size", type=int, default=384, help="Square training resolution; must be divisible by 8")
    parser.add_argument("--learning_rate", type=float, default=0.001)
    parser.add_argument("--validation_fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dry_run", action="store_true", help="Validate the dataset and split without training.")
    args = parser.parse_args()
    if args.epochs < 1 or args.batch_size < 1 or args.image_size < 64 or args.image_size % 8:
        parser.error("epochs/batch_size must be positive and image_size must be at least 64 and divisible by 8")
    if not 0 < args.validation_fraction < 0.5:
        parser.error("validation_fraction must be between 0 and 0.5")

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    dataset_dir = Path(args.dataset_dir)
    output_dir = Path(args.output_dir)
    samples = load_samples(dataset_dir)
    train_samples, validation_samples, validation_videos = split_by_video(samples, args.validation_fraction, args.seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    split = {
        "total_samples": len(samples), "train_samples": len(train_samples), "validation_samples": len(validation_samples),
        "validation_videos": validation_videos,
    }
    (output_dir / "split.json").write_text(json.dumps(split, indent=2), encoding="utf-8")
    print(json.dumps(split, indent=2))
    if args.dry_run:
        return

    device = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
    train_loader = DataLoader(PublicAreaDataset(train_samples, args.image_size, augment=True), batch_size=args.batch_size, shuffle=True, num_workers=0)
    validation_loader = DataLoader(PublicAreaDataset(validation_samples, args.image_size, augment=False), batch_size=args.batch_size, shuffle=False, num_workers=0)

    public_pixels = 0
    all_pixels = 0
    for sample in train_samples:
        mask = cv2.imread(str(sample.mask_path), cv2.IMREAD_GRAYSCALE)
        public_pixels += int(np.count_nonzero(mask == 1))
        all_pixels += int(mask.size)
    positive_weight = min(8.0, max(1.0, (all_pixels - public_pixels) / max(1, public_pixels)))
    model = TinyUNet().to(device)
    loss_function = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([positive_weight], device=device))
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-4)
    best_iou = -1.0
    history = []
    print(f"Training on {device.type}; public pixel weight={positive_weight:.2f}")

    for epoch in range(1, args.epochs + 1):
        model.train()
        train_loss = 0.0
        for images, masks in train_loader:
            images, masks = images.to(device), masks.to(device)
            optimizer.zero_grad()
            logits = model(images)
            loss = loss_function(logits, masks)
            loss.backward()
            optimizer.step()
            train_loss += float(loss.item()) * len(images)
        validation_loss, validation_metrics = evaluate(model, validation_loader, loss_function, device)
        row = {"epoch": epoch, "train_loss": train_loss / len(train_loader.dataset), "validation_loss": validation_loss, **validation_metrics}
        history.append(row)
        print("epoch {epoch:03d} train_loss={train_loss:.4f} val_loss={validation_loss:.4f} val_iou={iou:.3f} precision={precision:.3f} recall={recall:.3f}".format(**row))
        if validation_metrics["iou"] > best_iou:
            best_iou = validation_metrics["iou"]
            torch.save({
                "state_dict": model.state_dict(), "base_channels": 16, "image_size": args.image_size,
                "threshold": 0.5, "class_mapping": {"0": "not_public", "1": "public_area"},
                "validation_metrics": validation_metrics,
            }, output_dir / "model.pt")

    (output_dir / "metrics.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
    print(f"Best model saved to {output_dir / 'model.pt'} (validation IoU={best_iou:.3f})")


if __name__ == "__main__":
    main()
