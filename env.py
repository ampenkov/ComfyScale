import os

# ray
NUM_PARALLELISM = int(os.getenv("NUM_PARALLELISM", 4))
OBJECT_STORE_FRACTION = float(os.getenv("OBJECT_STORE_FRACTION", 0.3))

# dashboard
DASHBOARD_HOST = os.getenv("DDASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.getenv("METRICS_PORT", 8265))

# metrics
METRICS_PORT = int(os.getenv("METRICS_PORT", 8080))
HISTOGRAM_LIMIT = int(os.getenv("HISTOGRAM_LIMIT", 180))
HISTOGRAM_STEP = int(os.getenv("HISTOGRAM_STEP", 1))
