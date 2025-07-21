mkdir -p ../converted

for f in *.mp4 *.mkv; do
  # Get the height of the input video
  HEIGHT=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$f")

  # Decide on scaling filter based on resolution
  if [ "$HEIGHT" -gt 1080 ]; then
    SCALE_FILTER="-vf scale=1920:1080"
  else
    SCALE_FILTER=""
  fi

  ffmpeg -i "$f" \
    -c:v libx264 -preset fast -crf 23 \
    -g 30 -pix_fmt yuv420p \
    $SCALE_FILTER \
    -c:a aac -b:a 128k \
    -movflags +faststart \
    "../converted/${f%.*}.mp4"
done