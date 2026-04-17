from pathlib import Path


def import_files(sources: list, project_path: str, dest: str = "movies", platform: str | None = None) -> list[str]:
    """Import media files and return list of successfully imported filenames.

    If platform is None (default), files are copied as-is.
    If platform is 'universal' or 'pi5', files are re-encoded using that profile.
    """
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
                result = _import_into(f, target, project_path, dest, platform)
                if result:
                    imported_files.append(result.name)
        else:
            result = _import_into(src, target, project_path, dest, platform)
            if result:
                imported_files.append(result.name)

    return imported_files


def _import_into(src: Path, target_dir: Path, project_path: str, dest: str, platform: str | None) -> Path | None:
    if platform is None:
        import shutil
        from services.normalize import normalize_filename
        dest_path = target_dir / normalize_filename(src.name)
        if dest_path.exists():
            print(f"  skip  {dest_path.name}  (already exists)")
            return dest_path
        print(f"  copying  {src.name} →  {target_dir}")
        shutil.copy2(src, dest_path)
        return dest_path
    from services.transcode import transcode_file
    return transcode_file(src, project_path, media_type=dest, platform=platform)
