import argparse

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
    
    return parser.parse_args()