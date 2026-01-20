import asyncio
import psycopg2
import logging

from . import BaseAPI
from .inside_api import InsideAPI
from .co_inside_api import CoAPI
from .enquiry_api import EnquiryAPI
from .authority_verification import AuthorityVerification


class EnqAPICall:
    def __init__(self, req_json: dict, authority_verification: AuthorityVerification):
        self._req_json = req_json
        self.authority_verification = authority_verification(
            upn=self._req_json['upn'])

    async def call_apis(self) -> dict:
        api_instances = self._get_api_instances()
        futures = [asyncio.create_task(self._call_api(
            api_instance)) for api_instance in api_instances]

        return await asyncio.gather(*futures)

    def _get_api_instances(self) -> list:
        api_instances = [InsideAPI(self._req_json)]

        if self.authority_verification.verify_eneka_authority():
            enquiry_req_json = self._req_json.copy()
            enquiry_req_json.update({'company': 'エネ化'})
            api_instances += [CoAPI('エネ化イントラ', self._req_json),
                              EnquiryAPI(enquiry_req_json)]
            # api_instances += [CoAPI('エネ化イントラ', self._req_json)]
            return api_instances

        elif self.authority_verification.verify_kikai_authority():
            api_instances += [CoAPI('機械イントラ', self._req_json)]
            return api_instances

        elif self.authority_verification.verify_food_authority():
            api_instances += [CoAPI('食料イントラ', self._req_json)]
            return api_instances

        elif self.authority_verification.verify_jyuseikatsu_authority():
            api_instances += [CoAPI('住生活イントラ', self._req_json)]
            return api_instances

        elif self.authority_verification.verify_kinzoku_authority():
            api_instances += [CoAPI('金属イントラ', self._req_json)]
            return api_instances

        elif self.authority_verification.verify_tex_authority():
            api_instances += [CoAPI('繊維イントラ', self._req_json)]
            return api_instances

        elif self.authority_verification.verify_joukin_authority():
            api_instances += [CoAPI('情報金融イントラ', self._req_json)]
            return api_instances

        return api_instances

    async def _call_api(self, api_instance: BaseAPI) -> dict:
        return await api_instance.call()
