import os
from data import Cinematheque, Gameplay
from annotate import annotate_shots, annotate_scenes, has_scenes
from ollama import OllamaClient
from parse import parse_arguments

def main():
    args = parse_arguments()
    
    # Load appropriate media library based on --media argument
    if args.media == 'gameplay':
        metadata_path = os.path.join(args.project_root, "metadata", "gameplay.csv")
        library = Gameplay(metadata_path, args.project_root)
        media_type = "gameplay"
    else:  # movie (default)
        metadata_path = os.path.join(args.project_root, "metadata", "cinematheque.csv")
        library = Cinematheque(metadata_path, args.project_root)
        media_type = "film"
    
    print(f"Loaded {library}")
    
    if args.index >= 0:
        item = library.get(args.index)
        if not item:
            print(f"Index {args.index} out of range (0-{len(library)-1})")
            return
        
        print(f"\n{media_type.capitalize()} [{args.index}]: {library.get_title(item)}")
        
        if args.action == 'erase':
            if args.type == 'shot':
                print("Erasing Shot_Caption entries...")
                if library.erase_shot_captions(item):
                    print("✓ Shot captions erased successfully")
                else:
                    print("✗ Failed to erase shot captions")
            elif args.type == 'scene':
                print("Erasing Scene_Caption entries...")
                if library.erase_scene_captions(item):
                    print("✓ Scene captions erased successfully")
                else:
                    print("✗ Failed to erase scene captions")
        
        elif args.action == 'annotate':
            shotlist = library.load_shotlist(item)
            if not shotlist:
                print("✗ No shotlist found for this item")
                return
            
            if args.type == 'shot':
                video_path = library.get_video_path(item)
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
                    film=item,
                    ollama=ollama,
                    frames_dir=frames_dir,
                    project_root=args.project_root,
                    limit=args.annotation_count,
                    start_index=args.shot_index
                )
                if library.save_shotlist(item, shotlist):
                    print("\n✓ Shot captions annotated successfully")
                else:
                    print("\n✗ Failed to save shot annotations")
            
            elif args.type == 'scene':
                if not has_scenes(shotlist):
                    print("✗ Cannot annotate scenes: No scene information found in shotlist")
                else:
                    print("Annotating scenes...")
                    shotlist = annotate_scenes(shotlist)
                    if library.save_shotlist(item, shotlist):
                        print("✓ Scene captions annotated successfully")
                    else:
                        print("✗ Failed to save scene annotations")
        else:
            shotlist = library.load_shotlist(item)
            if shotlist:
                print(f"Shotlist loaded: {len(shotlist)} shots")
                print("Scene information:", "Available" if has_scenes(shotlist) else "Not available")
            else:
                print("No shotlist found for this item")
    else:
        # No index specified - print all items with their indices
        print(f"\nAvailable {media_type} items:")
        print("=" * 60)
        for i, item in enumerate(library.items):
            title = library.get_title(item)
            print(f"{i}\t{title}")
        print("=" * 60)
        print(f"Total: {len(library.items)} items")

if __name__ == "__main__":
    main()