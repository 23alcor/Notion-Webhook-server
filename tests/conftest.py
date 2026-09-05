import os
import sys

# Make the project root importable so tests can `from app import app` etc.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
