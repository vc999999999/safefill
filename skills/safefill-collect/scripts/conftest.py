"""Tests use the collector as the canonical source and the adjacent filler."""
import sys
from pathlib import Path
scripts = Path(__file__).resolve().parent
sys.path[:0] = [str(scripts), str(scripts.parents[1] / 'safefill-fill/scripts')]
import collection  # Load the canonical shared module before employee test imports.
