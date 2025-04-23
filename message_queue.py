import asyncio
import ray
import time

@ray.remote
class MessageQueue:
    def __init__(self):
        self.messages = asyncio.Queue()
    
    async def add(self, event, data, client_id=None, broadcast=False):
        data = {
            **data,
            "timestamp": int(time.time() * 1000),
        }
        if client_id is not None or broadcast:
            await self.messages.put((event, data, client_id))
    
    async def get(self):
        msg = await self.messages.get()
        msgs = [msg]
        while True:
            try:
                msg = self.messages.get_nowait()
                msgs.append(msg)
            except asyncio.QueueEmpty:
                break
        return msgs
