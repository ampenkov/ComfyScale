import itertools
from collections import OrderedDict
from typing import Dict, Mapping, Sequence

import ray

import nodes
from comfy_execution.graph_utils import is_link

NODE_CLASS_CONTAINS_UNIQUE_ID: Dict[str, bool] = {}


def include_unique_id_in_input(class_type: str) -> bool:
    if class_type in NODE_CLASS_CONTAINS_UNIQUE_ID:
        return NODE_CLASS_CONTAINS_UNIQUE_ID[class_type]
    class_def = nodes.NODE_CLASS_MAPPINGS[class_type]
    NODE_CLASS_CONTAINS_UNIQUE_ID[class_type] = (
        "UNIQUE_ID" in class_def.INPUT_TYPES().get("hidden", {}).values()
    )
    return NODE_CLASS_CONTAINS_UNIQUE_ID[class_type]


class Unhashable:
    def __init__(self):
        self.value = float("NaN")


def to_hashable(obj):
    # So that we don't infinitely recurse since frozenset and tuples
    # are Sequences.
    if isinstance(obj, (int, float, str, bool, type(None))):
        return obj
    elif isinstance(obj, Mapping):
        return frozenset(
            [(to_hashable(k), to_hashable(v)) for k, v in sorted(obj.items())]
        )
    elif isinstance(obj, Sequence):
        return frozenset(zip(itertools.count(), [to_hashable(i) for i in obj]))
    else:
        # TODO - Support other objects like tensors?
        return Unhashable()


def get_node_signature(prompt, node_id):
    signature = []
    ancestors, order_mapping = get_ordered_ancestry(prompt, node_id)
    signature.append(get_immediate_node_signature(prompt, node_id, order_mapping))
    for ancestor_id in ancestors:
        signature.append(
            get_immediate_node_signature(prompt, ancestor_id, order_mapping)
        )
    return to_hashable(signature)


def get_immediate_node_signature(prompt, node_id, ancestor_order_mapping):
    if node_id not in prompt:
        # This node doesn't exist -- we can't cache it.
        return [float("NaN")]
    node = prompt[node_id]
    class_type = node["class_type"]
    class_def = nodes.NODE_CLASS_MAPPINGS[class_type]
    signature = [class_type]
    if (
        (hasattr(class_def, "NOT_IDEMPOTENT") and class_def.NOT_IDEMPOTENT)
        or include_unique_id_in_input(class_type)
    ):
        signature.append(node_id)

    inputs = node["inputs"]
    for key in sorted(inputs.keys()):
        if is_link(inputs[key]):
            (ancestor_id, ancestor_socket) = inputs[key]
            ancestor_index = ancestor_order_mapping[ancestor_id]
            signature.append((key, ("ANCESTOR", ancestor_index, ancestor_socket)))
        else:
            signature.append((key, inputs[key]))
    return signature


# This function returns a list of all ancestors of the given node. The order of the list is
# deterministic based on which specific inputs the ancestor is connected by.
def get_ordered_ancestry(prompt, node_id):
    ancestors = []
    order_mapping = {}
    get_ordered_ancestry_internal(prompt, node_id, ancestors, order_mapping)
    return ancestors, order_mapping


def get_ordered_ancestry_internal(prompt, node_id, ancestors, order_mapping):
    if node_id not in prompt:
        return
    inputs = prompt[node_id]["inputs"]
    input_keys = sorted(inputs.keys())
    for key in input_keys:
        if is_link(inputs[key]):
            ancestor_id = inputs[key][0]
            if ancestor_id not in order_mapping:
                ancestors.append(ancestor_id)
                order_mapping[ancestor_id] = len(ancestors) - 1
                get_ordered_ancestry_internal(
                    prompt, ancestor_id, ancestors, order_mapping
                )


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
