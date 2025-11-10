from huggingface_hub import snapshot_download

snapshot_download(
    "Salesforce/blip-image-captioning-base",
    local_dir="models/blip-image-captioning-base",
    local_dir_use_symlinks=False,   # <-- write real files here
)
print("✅ Downloaded to models/blip-image-captioning-base")