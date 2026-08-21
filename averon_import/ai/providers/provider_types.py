from typing import Protocol

class ProviderProtocol(Protocol):
    async def review(self, context): ...
