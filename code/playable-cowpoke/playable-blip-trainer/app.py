import os
from data import Cinematheque
from annotate import annotate_shots, annotate_scenes, has_scenes
from ollama import OllamaClient
from parse import parse_arguments

def main():
    args = parse_arguments()
    metadata_path = os.path.join(args.project_root, "metadata", "cinematheque.csv")
    cinematheque = Cinematheque(metadata_path, args.project_root)
    print(f"Loaded {cinematheque}")
    if args.index >= 0:
        film = cinematheque.get(args.index)
        if not film:
            print(f"Index {args.index} out of range (0-{len(cinematheque)-1})")
            return
        print(f"\nFilm [{args.index}]: {film['title']} ({film['year']})")
        if args.action == 'erase':
            if not args.type:
                print("✗ Error: --type required for erase action (scene or shot)")
            elif args.type == 'shot':
                print("Erasing Shot_Caption entries...")
                if cinematheque.erase_shot_captions(film):
                    print("✓ Shot captions erased successfully")
                else:
                    print("✗ Failed to erase shot captions")
            elif args.type == 'scene':
                print("Erasing Scene_Caption entries...")
                if cinematheque.erase_scene_captions(film):
                    print("✓ Scene captions erased successfully")
                else:
                    print("✗ Failed to erase scene captions")
        elif args.action == 'annotate':
            if not args.type:
                print("✗ Error: --type required for annotate action (scene or shot)")
            else:
                shotlist = cinematheque.load_shotlist(film)
                if not shotlist:
                    print("✗ No shotlist found for this film")
                    return
                if args.type == 'shot':
                    video_filename = film.get('filename', '')
                    video_path = os.path.join(args.project_root, "movies", video_filename)
                    if not os.path.exists(video_path):
                        print(f"✗ Video file not found: {video_path}")
                        return
                    frames_dir = os.path.join(args.project_root, "frames")

                    # Use model from CLI (default gemma3:27b)
                    ollama = OllamaClient(model=args.model)

                    print(f"Found video: {video_path}")
                    print(f"Saving frames to: {frames_dir}")
                    print("Annotating shots...")

                    shotlist = annotate_shots(
                        shotlist=shotlist,
                        video_path=video_path,
                        film=film,
                        ollama=ollama,
                        frames_dir=frames_dir,
                        project_root=args.project_root,
                        limit=args.annotation_count,
                        start_index=args.shot_index
                    )
                    if cinematheque.save_shotlist(film, shotlist):
                        print("\n✓ Shot captions annotated successfully")
                    else:
                        print("\n✗ Failed to save shot annotations")
                elif args.type == 'scene':
                    if not has_scenes(shotlist):
                        print("✗ Cannot annotate scenes: No scene information found in shotlist")
                    else:
                        print("Annotating scenes...")
                        shotlist = annotate_scenes(shotlist)
                        if cinematheque.save_shotlist(film, shotlist):
                            print("✓ Scene captions annotated successfully")
                        else:
                            print("✗ Failed to save scene annotations")
        else:
            shotlist = cinematheque.load_shotlist(film)
            if shotlist:
                print(f"Shotlist loaded: {len(shotlist)} shots")
                print("Scene information:", "Available" if has_scenes(shotlist) else "Not available")
            else:
                print("No shotlist found for this film")
    else:
        print("No film index specified (--index)")

if __name__ == "__main__":
    main()