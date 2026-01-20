from abc import ABC, abstractmethod


class BaseAPI(ABC):
    abstractmethod

    async def call(self, req_json):
        pass
