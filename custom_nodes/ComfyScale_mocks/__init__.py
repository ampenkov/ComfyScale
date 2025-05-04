import random
import time

import ray


class NetworkRequestMocker:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "prompt": ("STRING",),
                "image": ("IMAGE",),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "mock"
    CATEGORY = "mocks/network"

    @ray.remote(num_returns=2)
    def mock(self, prompt, image):
        time.sleep(random.normalvariate(15, 5))
        return image, None


NODE_CLASS_MAPPINGS = {
    "NetworkRequestMocker": NetworkRequestMocker,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "NetworkRequestMocker": "Mock Network Request",
}
