import os

# ray
NUM_GPUS = int(os.getenv("NUM_GPUS", 0))
NUM_PARALLELISM = int(os.getenv("NUM_PARALLELISM", 4))

# dashboard
DASHBOARD_HOST = os.getenv("DDASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", 8265))

# metrics
METRICS_PORT = int(os.getenv("METRICS_PORT", 8080))
HISTOGRAM_LIMIT = int(os.getenv("HISTOGRAM_LIMIT", 180))
HISTOGRAM_STEP = int(os.getenv("HISTOGRAM_STEP", 1))
