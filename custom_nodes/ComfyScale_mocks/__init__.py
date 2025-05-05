import random
import time

import ray


class NetworkRequestMocker:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image": ("IMAGE",),
                "text": ("STRING",),
                "latency_mu": ("FLOAT", {"default": 15}),
                "latency_sigma": ("FLOAT", {"default": 1}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "mock"
    CATEGORY = "mocks/network"

    @ray.remote(num_returns=2)
    def mock(self, image, text, latency_mu, latency_sigma):
        time.sleep(random.normalvariate(latency_mu, latency_sigma))
        return image, None


NODE_CLASS_MAPPINGS = {
    "NetworkRequestMocker": NetworkRequestMocker,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "NetworkRequestMocker": "Mock Network Request",
}
