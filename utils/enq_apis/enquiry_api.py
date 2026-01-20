import os
import traceback
import logging

from . import BaseAPI

from i_style.aiohttp import http_post


class EnquiryAPI(BaseAPI):
    def __init__(self, req_json):
        self.req_json = req_json
        self.mode = "enquiry"
        self.company_name = self.req_json.get("company")
        self.name = f"{self.company_name}ナレッジベース"
        self.url = os.environ.get("ENQ_SEARCH_URL")
        self.api_key = os.environ.get("ENQ_SEARCH_API_KEY")

    async def call(self):
        api_name = f"{self.mode} search"

        try:
            api_response = await http_post(json_data=self.req_json, url=self.url, api_key=self.api_key, process_name=api_name)

            return {
                "name": self.name,
                "mode": self.mode,
                "answer": api_response["answer"],
                "datasource": api_response["datasource"]
            }

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
