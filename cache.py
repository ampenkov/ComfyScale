import ray
from collections import OrderedDict

from comfy_execution.caching import get_node_signature

@ray.remote
class LRUCache:
    def __init__(self, max_size=100):
        self.max_size = max_size
        self.cache = OrderedDict()

    def get(self, prompt):
        result = {}
        for node_id in prompt.keys():
            sig = get_node_signature(prompt, node_id)
            obj = self.cache.get(sig)
            if obj is not None:
                self.cache.move_to_end(sig)
            result[node_id] = obj
        return result

    def set(self, outputs, prompt) -> None:
        for node_id, obj_ref in outputs.items():
            sig = get_node_signature(prompt, node_id)
            if sig in self.cache:
                self.cache.move_to_end(sig)
            self.cache[sig] = obj_ref
            if len(self.cache) > self.max_size:
                self.cache.popitem(last=False)
