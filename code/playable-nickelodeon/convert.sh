#!/bin/bash
SRC="/Volumes/PLAYABLE-D/project/movies"
DST="/Volumes/PLAYABLE-D/project/HEVC"
mkdir -p "$DST"
shopt -s nullglob

progress_bar() {
  local cur="$1" total="$2" w=40
  local pct=$(awk -v c="$cur" -v t="$total" 'BEGIN{if(t>0) printf "%.1f", (c/t)*100; else print "0.0"}')
  local filled=$(awk -v c="$cur" -v t="$total" -v w="$w" 'BEGIN{printf "%d", (t>0)? (c/t)*w : 0}')
  local bar=$(printf "%${filled}s" | tr " " "#")
  printf "[%-*s] %5.1f%%" "$w" "$bar" "$pct"
}

for f in "$SRC"/*.mp4; do
  codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
           -of csv=p=0 "$f")
  if [[ "$codec" != "h264" ]]; then
    echo "⏩  Skipping (already $codec): $(basename "$f")"
    continue
  fi

  filename=$(basename "$f")
  base="${filename%.mp4}"
  out="$DST/${base}.mp4"

  # Duration in seconds (float)
  dur=$(ffprobe -v error -select_streams v:0 -show_entries format=duration \
        -of default=nk=1:nw=1 "$f")
  dur=${dur%.*}  # integer seconds is fine for progress

  echo "▶️  Transcoding: $filename"
  # Run ffmpeg and read machine-readable progress from stdout
  ffmpeg -hide_banner -nostdin -i "$f" \
    -c:v libx265 -pix_fmt yuv420p -preset medium -crf 22 \
    -x265-params "level=4.1:high-tier=1:repeat-headers=1:aud=1:vbv-bufsize=15000:vbv-maxrate=15000:keyint=48:min-keyint=48:scenecut=0" \
    -tag:v hvc1 -c:a aac -b:a 160k -movflags +faststart \
    -progress - -loglevel error "$out" 2>/dev/null | \
  awk -v dur="$dur" '
    function fmt(t,  h,m,s) {
      if (t<0) t=0; h=int(t/3600); m=int((t%3600)/60); s=int(t%60);
      return sprintf("%02d:%02d:%02d",h,m,s)
    }
    /^out_time_ms=/ {
      ms=$0; sub("out_time_ms=","",ms);
      sec=int(ms/1000000);
      pct=(dur>0)? (100*sec/dur):0;
      rem=(dur-sec);
      if (rem<0) rem=0;
      filled=int((sec/dur)*40);
      bar=""; for(i=0;i<filled;i++) bar=bar "#";
      for(i=filled;i<40;i++) bar=bar " ";
      printf "\r[%-40s] %5.1f%%  elapsed %s  ETA %s", bar, pct, fmt(sec), fmt(rem);
      fflush();
    }
    END { print "" }
  '
  echo "✅  Done: $out"
done

echo "All HEVC files are in: $DST"