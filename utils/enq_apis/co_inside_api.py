import os
import traceback
import logging

from . import BaseAPI

from i_style.aiohttp import http_post


class CoAPI(BaseAPI):
    def __init__(self, name, req_json):
        self.name = name
        self.req_json = req_json
        self.mode = "co_inside"
        self.url = os.environ.get("ENE_SEARCH_URL")
        self.api_key = os.environ.get("ENE_SEARCH_API_KEY")

    async def call(self):
        upn = self.req_json["upn"]
        messages = self.req_json["messages"]
        # リクエストボディのデータを準備
        json_data = {
            "index_name": "prod-vector-cpnint-index",
            "prompt": next((content["text"] for content in messages[-1]["content"] if content["type"] == "text"), ""),
            "upn_ex": upn,
            "history": [{
                message["role"]: next(
                    (content["text"] for content in message["content"] if content["type"] == "text"), "")
            } for message in messages[1:-1]],
            "overrides": {
                "semanticRanker": True,
                "vectorSearch": True,
                "temperature": 0.0,
                "top": 7
            }
        }

        # Documents Search API呼び出し
        api_name = "Ene Documents search"
        try:
            cs_response = await http_post(json_data=json_data, url=self.url, api_key=self.api_key, process_name=api_name)
            # responseの整形
            ans = cs_response["answer"]
            logging.info("ene_cs answer: " + ans)

        except Exception as e:
            error = api_name + ": " + str(e)
            tb = traceback.format_exc()
            logging.critical(f"{error}, {tb}")
            return {
                "name": self.name,
                "mode": self.mode,
                "answer": f"{self.name}との接続が出来ませんでした。時間をおいてお試しください。",
                "datasource": []
            }

        # 整形用
        return {
            "name": self.name,
            "mode": self.mode,
            "answer": cs_response["answer"],
            "datasource": cs_response["datasource"]
        }
