"""
Main app entrypoint. Run: python app.py
Keeps boot logic out of gui.py.
"""
import sys
from gui import run

if __name__ == "__main__":
    sys.exit(run(sys.argv))