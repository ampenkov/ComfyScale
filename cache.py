import ray

from collections import defaultdict

from comfy_execution.caching import get_node_signature


@ray.remote
class Cache:
    def __init__(self):
        self.cache = {}
        self.ref_counts = defaultdict(int)

    def get(self, prompt):
        sigs = {}
        outputs = {}
        for node_id in prompt.keys():
            sig = get_node_signature(prompt, node_id)
            sigs[node_id] = sig

            obj = self.cache.get(sig)
            if obj is not None:
                outputs[node_id] = obj
                self.ref_counts[sig] += 1

        return sigs, outputs

    def get_used(self):
        return self.cache.keys()

    def set(self, outputs, prompt):
        for node_id, obj_ref in outputs.items():
            sig = get_node_signature(prompt, node_id)
            if sig not in self.cache:
                self.cache[sig] = obj_ref
                self.ref_counts[sig] = 1

    def set_unused(self, prompt):
        for node_id in prompt.keys():
            sig = get_node_signature(prompt, node_id)
            if sig in self.ref_counts:
                self.ref_counts[sig] -= 1
                if self.ref_counts[sig] <= 0:
                    del self.cache[sig]
                    del self.ref_counts[sig]
