from pathlib import Path


def import_files(sources: list, project_path: str, dest: str = "movies", platform: str = "universal") -> list[str]:
    """Import media files and return list of successfully imported filenames."""
    target = Path(project_path) / "media" / "videos" / dest
    target.mkdir(parents=True, exist_ok=True)
    
    imported_files = []

    for source in sources:
        src = Path(source).resolve()
        if not src.exists():
            print(f"Not found: {src}")
            continue
        if src.name.startswith("._"):
            continue
        if src.is_dir():
            files = [f for f in src.iterdir() if f.is_file() and not f.name.startswith("._")]
            if not files:
                print(f"Empty folder: {src}")
                continue
            for f in files:
                result = _transcode_into(f, target, project_path, dest, platform)
                if result:
                    imported_files.append(result.name)
        else:
            result = _transcode_into(src, target, project_path, dest, platform)
            if result:
                imported_files.append(result.name)
    
    return imported_files


def _transcode_into(src: Path, target_dir: Path, project_path: str, dest: str, platform: str) -> Path | None:
    from services.transcode import transcode_file
    return transcode_file(src, project_path, media_type=dest, platform=platform)
