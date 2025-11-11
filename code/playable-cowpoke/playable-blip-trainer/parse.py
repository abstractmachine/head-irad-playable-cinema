import argparse
import sys

def parse_arguments():
    """
    Parse command line arguments.
    """
    parser = argparse.ArgumentParser(description="BLIP trainer/annotation tool")
    parser.add_argument(
        "--project-root",
        default="/Volumes/PLAYABLE-D/project/",
        help="Root directory for the project"
    )
    parser.add_argument(
        "--index",
        type=int,
        default=-1,
        help="Film index from cinematheque.csv (-1 = none)"
    )
    parser.add_argument(
        "--action",
        choices=['erase', 'annotate'],
        help="Action to perform on the selected film"
    )
    parser.add_argument(
        "--type",
        choices=['scene', 'shot'],
        help="Type of caption to operate on (scene or shot)"
    )
    # New options
    parser.add_argument(
        "--model",
        default="gemma3:27b",
        help="Ollama model to use (default: gemma3:27b)"
    )
    parser.add_argument(
        "--shot_index",
        type=int,
        default=1,
        help="Starting shot index (1-based) for annotation"
    )
    parser.add_argument(
        "--annotation_count",
        type=int,
        default=None,
        help="How many shots to annotate (default: until end)"
    )

    # If no args were provided, print help and exit
    if len(sys.argv) == 1:
        parser.print_help()
        parser.exit()

    return parser.parse_args()