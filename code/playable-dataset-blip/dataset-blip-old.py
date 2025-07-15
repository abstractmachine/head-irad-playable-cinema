import os
import csv
import argparse
import shutil
from datetime import datetime

parser = argparse.ArgumentParser(description="Create a manifest CSV for BLIP2 training.")
parser.add_argument('--dataset_dir', type=str, required=False, help='Path to the dataset folder containing jpg and txt files.')
args = parser.parse_args()

# Default dataset directory
DATASET_DIR = "./dataset-input"
if args.dataset_dir:
    DATASET_DIR = args.dataset_dir

# Create datestamped output folder and CSV filename with current date + hour-minute-second
datestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
#OUTPUT_FOLDER = f"dataset-{datestamp}"
OUTPUT_FOLDER = "./dataset-output"
OUTPUT_CSV = os.path.join(OUTPUT_FOLDER, f"dataset.csv")

# Create output folder
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

entries = []
for fname in os.listdir(DATASET_DIR):
    if fname.endswith(".jpg"):
        img_path = os.path.join(DATASET_DIR, fname)
        txt_path = img_path.replace(".jpg", ".txt")
        if os.path.exists(txt_path):
            with open(txt_path, "r") as f:
                caption = f.read().strip()
            # Copy image to output folder
            out_img_path = os.path.join(OUTPUT_FOLDER, fname)
            shutil.copy2(img_path, out_img_path)
            entries.append((fname, caption))  # Use just the filename in the CSV

with open(OUTPUT_CSV, "w", newline='', encoding="utf-8") as csvfile:
    writer = csv.writer(csvfile)
    writer.writerow(["image", "caption"])
    for img_name, caption in entries:
        writer.writerow([img_name, caption])
        print(f"Processed {img_name} with caption: {caption}")

print(f"Manifest saved to {OUTPUT_CSV} with {len(entries)} entries.")
print(f"Images copied to {OUTPUT_FOLDER}/")