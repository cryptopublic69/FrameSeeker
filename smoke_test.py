from __future__ import annotations

import argparse
from pathlib import Path

import imagehash
import numpy as np
import timm
import torch
from PIL import Image, ImageDraw
from transformers import AutoModel, AutoProcessor, AutoTokenizer


SIGLIP_MODEL = "google/siglip2-base-patch16-224"
DINO_MODEL = "hf_hub:timm/vit_base_patch16_dinov3.lvd1689m"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def normalize(x: torch.Tensor) -> torch.Tensor:
    return torch.nn.functional.normalize(x.float(), dim=-1)


def load_images(folder: Path) -> tuple[list[Path], list[Image.Image]]:
    paths = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS)
    if not paths:
        raise SystemExit(f"No supported images found in: {folder}")
    images = [Image.open(path).convert("RGB") for path in paths]
    return paths, images


def demo_images() -> tuple[list[Path], list[Image.Image]]:
    images = []
    paths = []
    for name, background, shape in [
        ("red_circle.png", "white", "circle"),
        ("red_circle_copy.png", "white", "circle"),
        ("blue_square.png", "white", "square"),
    ]:
        image = Image.new("RGB", (512, 512), background)
        draw = ImageDraw.Draw(image)
        if shape == "circle":
            draw.ellipse((96, 96, 416, 416), fill="red")
        else:
            draw.rectangle((96, 96, 416, 416), fill="blue")
        images.append(image)
        paths.append(Path(name))
    return paths, images


def rank(scores: torch.Tensor, paths: list[Path], limit: int = 5) -> None:
    for position in torch.argsort(scores, descending=True)[:limit]:
        i = int(position)
        print(f"  {float(scores[i]):.4f}  {paths[i].name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal SigLIP 2 + DINOv3 + pHash test")
    parser.add_argument("image_dir", nargs="?", type=Path, help="Folder containing test images")
    parser.add_argument("--text", default="a person indoors", help="Text query for SigLIP 2")
    parser.add_argument("--query-image", type=Path, help="Image used for image-to-image search")
    parser.add_argument("--skip-dino", action="store_true", help="Run without gated DINOv3 weights")
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    paths, images = load_images(args.image_dir) if args.image_dir else demo_images()
    print(f"Device: {device}; images: {len(images)}")

    siglip_processor = AutoProcessor.from_pretrained(SIGLIP_MODEL)
    siglip_tokenizer = AutoTokenizer.from_pretrained(SIGLIP_MODEL)
    siglip = AutoModel.from_pretrained(SIGLIP_MODEL, dtype=dtype).to(device).eval()
    with torch.inference_mode():
        image_inputs = siglip_processor(images=images, return_tensors="pt").to(device)
        semantic = normalize(siglip.get_image_features(**image_inputs))
        text_inputs = siglip_tokenizer(
            [args.text], padding="max_length", max_length=64, truncation=True, return_tensors="pt"
        ).to(device)
        text_vector = normalize(siglip.get_text_features(**text_inputs))

    visual = None
    if not args.skip_dino:
        dino = timm.create_model(DINO_MODEL, pretrained=True, num_classes=0)
        dino = dino.to(device=device, dtype=dtype).eval()
        dino_config = timm.data.resolve_model_data_config(dino)
        dino_transform = timm.data.create_transform(**dino_config, is_training=False)
        with torch.inference_mode():
            dino_inputs = torch.stack([dino_transform(image) for image in images])
            visual = normalize(dino(dino_inputs.to(device=device, dtype=dtype)))

    hashes = [str(imagehash.phash(image)) for image in images]
    visual_size = visual.shape[1] if visual is not None else "skipped"
    print(f"Vectors: semantic={semantic.shape[1]}, visual={visual_size}")
    print(f"\nText search: {args.text!r}")
    rank((text_vector @ semantic.T)[0].cpu(), paths)

    query_path = args.query_image or paths[0]
    query = Image.open(query_path).convert("RGB") if args.query_image else images[0]
    with torch.inference_mode():
        query_semantic = normalize(siglip.get_image_features(
            **siglip_processor(images=[query], return_tensors="pt").to(device)
        ))
    semantic_scores = (query_semantic @ semantic.T)[0]
    if visual is not None:
        with torch.inference_mode():
            query_input = dino_transform(query).unsqueeze(0).to(device=device, dtype=dtype)
            query_visual = normalize(dino(query_input))
        visual_scores = (query_visual @ visual.T)[0]
        combined = semantic_scores * 0.58 + visual_scores * 0.42
    else:
        combined = semantic_scores
    print(f"\nImage search: {query_path.name}")
    rank(combined.cpu(), paths)

    query_hash = imagehash.phash(query)
    distances = np.array([query_hash - imagehash.hex_to_hash(value) for value in hashes])
    print("\npHash nearest:")
    for i in np.argsort(distances)[:5]:
        print(f"  distance={distances[i]:2d}  {paths[i].name}")


if __name__ == "__main__":
    main()
