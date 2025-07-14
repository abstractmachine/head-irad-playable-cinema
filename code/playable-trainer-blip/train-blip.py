import os
from PIL import Image
from torch.utils.data import Dataset
from transformers import Blip2Processor, Blip2ForConditionalGeneration, TrainingArguments, Trainer

DATASET_DIR = "../playable-annotate/Dataset"
LOCAL_MODEL_PATH = "/path/to/your/local/blip2-opt-2.7b"  # <-- update this path

class ImageCaptionDataset(Dataset):
    def __init__(self, dataset_dir, processor):
        self.entries = []
        self.processor = processor
        for fname in os.listdir(dataset_dir):
            if fname.endswith(".jpg"):
                img_path = os.path.join(dataset_dir, fname)
                txt_path = img_path.replace(".jpg", ".txt")
                if os.path.exists(txt_path):
                    with open(txt_path, "r") as f:
                        caption = f.read().strip()
                    self.entries.append((img_path, caption))

    def __len__(self):
        return len(self.entries)

    def __getitem__(self, idx):
        img_path, caption = self.entries[idx]
        image = Image.open(img_path).convert("RGB")
        inputs = self.processor(images=image, text=caption, return_tensors="pt", padding="max_length", truncation=True)
        inputs = {k: v.squeeze(0) for k, v in inputs.items()}
        inputs["labels"] = inputs["input_ids"]
        return inputs

processor = Blip2Processor.from_pretrained(LOCAL_MODEL_PATH)
model = Blip2ForConditionalGeneration.from_pretrained(LOCAL_MODEL_PATH)

dataset = ImageCaptionDataset(DATASET_DIR, processor)

training_args = TrainingArguments(
    output_dir="./blip2-finetuned",
    per_device_train_batch_size=2,
    num_train_epochs=1,
    logging_steps=10,
    save_steps=100,
    remove_unused_columns=False,
    fp16=True,
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=dataset,
    tokenizer=processor,
)

if __name__ == "__main__":
    trainer.train()