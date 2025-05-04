import os

# metrics
HISTOGRAM_LIMIT = int(os.getenv("HISTOGRAM_LIMIT", 180))
HISTOGRAM_STEP = int(os.getenv("HISTOGRAM_STEP", 1))
