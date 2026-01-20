import azure.functions as func
import logging
import urllib.request
import urllib.parse
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import base64
import io
import asyncio

# OSS
import pandas as pd

# 自作
from config import LLM_REGISTRY
from util import change_system_content, download_blob
from prompt import crm_query_content

from i_style.aiohttp import http_post
from i_style.llm import AzureOpenAI

##
# conv2query
##


async def conv2query(req_json):
    model_name = "gpt4.1"
    # if "model_name" in req_json.keys():
    #     model_name = req_json["model_name"]

    messages = req_json["messages"]
    # 仮実装 importに追加していない
    messages = change_system_content(messages, crm_query_content)
    response = await AzureOpenAI(
        messages,
        temperature=0,
        timeout=230,
        model_name=model_name,
        registry=LLM_REGISTRY
    )
    query = response["choices"][0]["message"]["content"]
    return query


##
# vector search
##
# インプットから検索のスケジューリングの作成

# 検索の実行
async def vector_search(query):
    # 結果の整形用
    def result2csv_data(vector_search_response):
        res_content = vector_search_response["choices"][0]["message"]["content"]

        csv_data = res_content
        return csv_data
    search_json = {
        "messages": [
            {
                "role": "user",
                "content": query
            }
        ]
    }

    vector_search_response = await http_post(
        json_data=search_json,
        url=os.environ.get("VECTOR_SEARCH_URL"),
        api_key=os.environ.get("VECTOR_SEARCH_API_KEY"),
        process_name="VECTOR_SEARCH"
    )
    csv_data = result2csv_data(vector_search_response)

    return csv_data


def csv_search(**options) -> pd.DataFrame:
    """
    '担当者':'user_name'
    '知りたい情報':概要、抽出、
    '対象期間':'mtg_date'
    '対象':背景->'background', 目的->'purpose', 、商談内容->'next_action', 備考->'remarks'、
    """
    key_dict = {
        "担当者": "user_name",
        "対象期間": "mtg_date",
        "対象": {
            "背景": "background",
            "目的": "purpose",
            "商談内容": "next_action",
            "備考": "remarks",
        }
    }

    def load_csv():
        binary_data = download_blob(
            file_name="food_co/crm.csv",
            container_name="crm"
        )
        csv_data = binary_data.decode()

        # 文字列をStringIOオブジェクトに変換
        string_io = io.StringIO(csv_data)

        # DataFrameに読み込む
        df = pd.read_csv(string_io)

        # 日付カラムを日付型に変換
        df[key_dict["対象期間"]] = pd.to_datetime(df[key_dict["対象期間"]])
        return df

    # main
    crm_df = load_csv()

    if "担当者" in options.keys():
        # 条件に合うデータのみをフィルタリング
        crm_df = crm_df[crm_df[key_dict["担当者"]] == options["担当者"]]
        logging.info(f'担当者: {options["担当者"]}, 該当件数: {len(crm_df)}件')

    if "対象期間" in options.keys():
        try:
            start = options["対象期間"]["start"]
            end = options["対象期間"]["end"]
        except:
            logging.critical("no date")
            start = "1800/01/01"  # 仮置き
            end = "2500/12/31"  # 仮置き

        # startとendを日付型に変換
        start_date = pd.to_datetime(start, utc=True)
        end_date = pd.to_datetime(end, utc=True)

        date = key_dict["対象期間"]
        # 条件に合うデータのみをフィルタリング
        crm_df = crm_df[(crm_df[date] >= start_date)
                        & (crm_df[date] <= end_date)]
        logging.info(f'対象期間: {start} ~ {end}, 該当件数: {len(crm_df)}件')

    if "対象" in options.keys():
        col_name = key_dict["対象"][options["対象"]]
        # 条件に合うデータのみをフィルタリング, mtg_dateは残す
        crm_df = crm_df[["mtg_date", col_name]]
        crm_df.rename(columns={col_name: options["対象"]}, inplace=True)
        logging.info(f'対象: {col_name}')

    return crm_df
