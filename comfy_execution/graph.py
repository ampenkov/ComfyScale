from __future__ import annotations
from typing import Type, Literal

import nodes
from comfy_execution.graph_utils import is_link
from comfy.comfy_types.node_typing import ComfyNodeABC, InputTypeDict, InputTypeOptions


class DependencyCycleError(Exception):
    pass


class NodeInputError(Exception):
    pass


class NodeNotFoundError(Exception):
    pass


def get_input_info(
    class_def: Type[ComfyNodeABC],
    input_name: str,
    valid_inputs: InputTypeDict | None = None,
) -> (
    tuple[str, Literal["required", "optional", "hidden"], InputTypeOptions]
    | tuple[None, None, None]
):
    """Get the input type, category, and extra info for a given input name.

    Arguments:
        class_def: The class definition of the node.
        input_name: The name of the input to get info for.
        valid_inputs: The valid inputs for the node, or None to use the class_def.INPUT_TYPES().

    Returns:
        tuple[str, str, dict] | tuple[None, None, None]: The input type, category, and extra info for the input name.
    """

    valid_inputs = valid_inputs or class_def.INPUT_TYPES()
    input_info = None
    input_category = None
    if "required" in valid_inputs and input_name in valid_inputs["required"]:
        input_category = "required"
        input_info = valid_inputs["required"][input_name]
    elif "optional" in valid_inputs and input_name in valid_inputs["optional"]:
        input_category = "optional"
        input_info = valid_inputs["optional"][input_name]
    elif "hidden" in valid_inputs and input_name in valid_inputs["hidden"]:
        input_category = "hidden"
        input_info = valid_inputs["hidden"][input_name]
    if input_info is None:
        return None, None, None
    input_type = input_info[0]
    if len(input_info) > 1:
        extra_info = input_info[1]
    else:
        extra_info = {}
    return input_type, input_category, extra_info


class TopologicalSort:
    def __init__(self, prompt):
        self.prompt = prompt
        self.pendingNodes = {}
        self.blockCount = {}  # Number of nodes this node is directly blocked by
        self.blocking = {}  # Which nodes are blocked by this node

    def get_input_info(self, unique_id, input_name):
        class_type = self.prompt[unique_id]["class_type"]
        class_def = nodes.NODE_CLASS_MAPPINGS[class_type]
        return get_input_info(class_def, input_name)

    def make_input_strong_link(self, to_node_id, to_input):
        inputs = self.prompt[to_node_id]["inputs"]
        if to_input not in inputs:
            raise NodeInputError(
                f"Node {to_node_id} says it needs input {to_input}, but there is no input to that node at all"
            )
        value = inputs[to_input]
        if not is_link(value):
            raise NodeInputError(
                f"Node {to_node_id} says it needs input {to_input}, but that value is a constant"
            )
        from_node_id, from_socket = value
        self.add_strong_link(from_node_id, from_socket, to_node_id)

    def add_strong_link(self, from_node_id, from_socket, to_node_id):
        if not self.is_cached(from_node_id):
            self.add_node(from_node_id)
            if to_node_id not in self.blocking[from_node_id]:
                self.blocking[from_node_id][to_node_id] = {}
                self.blockCount[to_node_id] += 1
            self.blocking[from_node_id][to_node_id][from_socket] = True

    def add_node(self, node_unique_id, include_lazy=False, subgraph_nodes=None):
        node_ids = [node_unique_id]
        links = []

        while len(node_ids) > 0:
            unique_id = node_ids.pop()
            if unique_id in self.pendingNodes:
                continue

            self.pendingNodes[unique_id] = True
            self.blockCount[unique_id] = 0
            self.blocking[unique_id] = {}

            inputs = self.prompt[unique_id]["inputs"]
            for input_name in inputs:
                value = inputs[input_name]
                if is_link(value):
                    from_node_id, from_socket = value
                    if (
                        subgraph_nodes is not None
                        and from_node_id not in subgraph_nodes
                    ):
                        continue
                    _, _, input_info = self.get_input_info(unique_id, input_name)
                    is_lazy = (
                        input_info is not None
                        and "lazy" in input_info
                        and input_info["lazy"]
                    )
                    if (include_lazy or not is_lazy) and not self.is_cached(
                        from_node_id
                    ):
                        node_ids.append(from_node_id)
                        links.append((from_node_id, from_socket, unique_id))

        for link in links:
            self.add_strong_link(*link)

    def is_cached(self, node_id):
        return False

    def get_ready_nodes(self):
        return [
            node_id for node_id in self.pendingNodes if self.blockCount[node_id] == 0
        ]

    def pop_node(self, unique_id):
        del self.pendingNodes[unique_id]
        for blocked_node_id in self.blocking[unique_id]:
            self.blockCount[blocked_node_id] -= 1
        del self.blocking[unique_id]

    def is_empty(self):
        return len(self.pendingNodes) == 0


class StageList(TopologicalSort):
    """
    StageList implements a topological dissolve of the graph.
    """

    def __init__(self, prompt):
        super().__init__(prompt)
        self.staged_node_id = None

    def stage_node_execution(self):
        assert self.staged_node_id is None
        if self.is_empty():
            return None, None, None
        available = self.get_ready_nodes()
        if len(available) == 0:
            cycled_nodes = self.get_nodes_in_cycle()
            # Because cycles composed entirely of static nodes are caught during initial validation,
            # we will 'blame' the first node in the cycle that is not a static node.
            blamed_node = cycled_nodes[0]
            for node_id in cycled_nodes:
                display_node_id = self.prompt[node_id]
                if display_node_id != node_id:
                    blamed_node = display_node_id
                    break
            ex = DependencyCycleError("Dependency cycle detected")
            error_details = {
                "node_id": blamed_node,
                "exception_message": str(ex),
                "exception_type": "graph.DependencyCycleError",
                "traceback": [],
                "current_inputs": [],
            }
            return None, error_details, ex

        self.staged_node_id = self.ux_friendly_pick_node(available)
        return self.staged_node_id, None, None

    def ux_friendly_pick_node(self, node_list):
        # If an output node is available, do that first.
        # Technically this has no effect on the overall length of execution, but it feels better as a user
        # for a PreviewImage to display a result as soon as it can
        # Some other heuristics could probably be used here to improve the UX further.
        def is_output(node_id):
            class_type = self.prompt[node_id]["class_type"]
            class_def = nodes.NODE_CLASS_MAPPINGS[class_type]
            if hasattr(class_def, "OUTPUT_NODE") and class_def.OUTPUT_NODE == True:
                return True
            return False

        for node_id in node_list:
            if is_output(node_id):
                return node_id

        # This should handle the VAEDecode -> preview case
        for node_id in node_list:
            for blocked_node_id in self.blocking[node_id]:
                if is_output(blocked_node_id):
                    return node_id

        # This should handle the VAELoader -> VAEDecode -> preview case
        for node_id in node_list:
            for blocked_node_id in self.blocking[node_id]:
                for blocked_node_id1 in self.blocking[blocked_node_id]:
                    if is_output(blocked_node_id1):
                        return node_id

        # TODO: this function should be improved
        return node_list[0]

    def unstage_node_execution(self):
        assert self.staged_node_id is not None
        self.staged_node_id = None

    def complete_node_staging(self):
        node_id = self.staged_node_id
        self.pop_node(node_id)
        self.staged_node_id = None

    def get_nodes_in_cycle(self):
        # We'll dissolve the graph in reverse topological order to leave only the nodes in the cycle.
        # We're skipping some of the performance optimizations from the original TopologicalSort to keep
        # the code simple (and because having a cycle in the first place is a catastrophic error)
        blocked_by = {node_id: {} for node_id in self.pendingNodes}
        for from_node_id in self.blocking:
            for to_node_id in self.blocking[from_node_id]:
                if True in self.blocking[from_node_id][to_node_id].values():
                    blocked_by[to_node_id][from_node_id] = True
        to_remove = [node_id for node_id in blocked_by if len(blocked_by[node_id]) == 0]
        while len(to_remove) > 0:
            for node_id in to_remove:
                for to_node_id in blocked_by:
                    if node_id in blocked_by[to_node_id]:
                        del blocked_by[to_node_id][node_id]
                del blocked_by[node_id]
            to_remove = [
                node_id for node_id in blocked_by if len(blocked_by[node_id]) == 0
            ]
        return list(blocked_by.keys())


class ExecutionBlocker:
    """
    Return this from a node and any users will be blocked with the given error message.
    If the message is None, execution will be blocked silently instead.
    Generally, you should avoid using this functionality unless absolutely necessary. Whenever it's
    possible, a lazy input will be more efficient and have a better user experience.
    This functionality is useful in two cases:
    1. You want to conditionally prevent an output node from executing. (Particularly a built-in node
       like SaveImage. For your own output nodes, I would recommend just adding a BOOL input and using
       lazy evaluation to let it conditionally disable itself.)
    2. You have a node with multiple possible outputs, some of which are invalid and should not be used.
       (I would recommend not making nodes like this in the future -- instead, make multiple nodes with
       different outputs. Unfortunately, there are several popular existing nodes using this pattern.)
    """

    def __init__(self, message):
        self.message = message
