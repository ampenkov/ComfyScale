import uuid
import random

import torch
import ray

from comfy.comfy_types.node_typing import IO


class ModelRef:
    def __init__(self, model_id: str):
        self.id = model_id

    def __repr__(self):
        return f"<ModelRef id={self.id}>"


GPU_MODEL_IO_TYPES = {
    IO.CLIP,
    IO.CONTROL_NET,
    IO.VAE,
    IO.MODEL,
    IO.CLIP_VISION,
    IO.STYLE_MODEL,
    IO.GLIGEN,
    IO.UPSCALE_MODEL,
}


@ray.remote
class GPUWorker:
    def __init__(self):
        self._models = {}

    def execute(self, node_obj, func, **inputs):
        provided_ids = inputs.pop("_model_ids", {})

        resolved_inputs = {
            k: self._models[v.id] if isinstance(v, ModelRef) else v
            for k, v in inputs.items()
        }

        with torch.inference_mode():
            outputs = getattr(node_obj, func)(**resolved_inputs)

        return_types = getattr(node_obj, "RETURN_TYPES", ()) or (None,)
        mapped_outputs = []
        for i, value in enumerate(outputs):
            output_type = return_types[i] if i < len(return_types) else None
            if output_type in GPU_MODEL_IO_TYPES:
                model_id = provided_ids.get(i, str(uuid.uuid4()))
                self._models[model_id] = value
                mapped_outputs.append(ModelRef(model_id))
            else:
                mapped_outputs.append(value)

        return mapped_outputs


class GPUPool:
    def __init__(self):
        num_gpus = int(ray.cluster_resources().get("GPU", 0))
        self.workers = [GPUWorker.remote() for _ in range(num_gpus)]

    def should_execute(self, node_obj) -> bool:
        if not self.workers:
            return False

        if getattr(node_obj, "IS_GPU", False):
            return True

        return any(rt in GPU_MODEL_IO_TYPES for rt in getattr(node_obj, "RETURN_TYPES", []))

    def execute(self, node_obj, func, inputs):
        return_types = getattr(node_obj, "RETURN_TYPES", ()) or (None,)
        num_returns = len(return_types)

        if any(rt in GPU_MODEL_IO_TYPES for rt in return_types):
            common_model_ids = {
                i: str(uuid.uuid4())
                for i, rt in enumerate(return_types)
                if rt in GPU_MODEL_IO_TYPES
            }
            shared_inputs = {**inputs, "_model_ids": common_model_ids}
            outputs = []
            for worker in self.workers:
                outputs.extend(worker.execute.options(num_returns=num_returns + 1).remote(node_obj, func, **shared_inputs))
            return outputs

        worker = random.choice(self.workers)
        outputs = worker.execute.options(num_returns=num_returns + 1).remote( node_obj, func, **inputs)
        return outputs
