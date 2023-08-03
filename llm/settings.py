import os


LOCAL_MODELS_DIR = os.getenv("LOCAL_MODELS_DIR", "models/")
SATURNFS_MODELS_DIR = os.getenv("SATURNFS_MODELS_DIR", None)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
