from prometheus_client import Counter, Histogram
from env import HISTOGRAM_LIMIT, HISTOGRAM_STEP


REQUEST_COUNT = Counter(
    'request_count',
    'request count',
    ('workflow', 'status')
)

REQUEST_LATENCY = Histogram(
    'request_latency',
    'request latency',
    ('workflow',),
    buckets=[i for i in range(1, HISTOGRAM_LIMIT, HISTOGRAM_STEP)],
)
