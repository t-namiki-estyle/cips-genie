"""
hanabi関連のAPIを提供
法人番号をキーにCosmosDBにアクセスして取得できた値を渡す。
"""
import azure.functions as func
import azure.durable_functions as d_func

import json
import logging
from typing import Optional

from azure.cosmos.exceptions import CosmosHttpResponseError
from config import COSMOS_CLIENT

bp = d_func.Blueprint()


class HanabiFetchService:
    """
    CosmosDBからhanabiの情報を取得するためのクラス
    """

    def __init__(self, cosmos_client):
        self.client = cosmos_client
        self.cosmos_db_params = {
            "database": "company",
            "container": "hanabi"
        }

    @staticmethod
    def normalize_digits(s: Optional[str]) -> str:
        """
        全角0-9→半角、ゼロ幅空白類の除去のみ（安全で速い最小正規化）。
        """
        if s is None:
            return ""
        table = {ord(f): ord(t) for f, t in zip("０１２３４５６７８９", "0123456789")}
        s2 = s.translate(table)
        return s2.replace("\u200b", "").replace("\ufeff", "")

    # 現在のビューから項目名をハードコード
    columns = [
        # "統合会社",
        "法人番号",
        "企業名",
        "検索用企業名(ｶﾅ)",
        "調査年月日",
        "COSMOS2更新年月日",
        "企業コード",
        "法人情報",
        "上場区分",
        "評点",
        "全国ランキング",
        "都道府県別ランキング",
        "目的",
        "代表者氏名",
        "役職名",
        "所在地",
        "電話番号",
        "資本金(千円)",
        "従業員数",
        "創業年月",
        "設立年月",
        "全国社数",
        "都道府県別社数",
        "事業所数",
        "外資企業",
        "額面株価",
        "親会社情報",
        "親企業情報",
        "TDB産業分類名称1",
        "TDB産業分類名称2",
        "TDB産業分類名称3",
        "TDB産業分類名称4",
        "TDB産業分類名称5",
        "Naics産業分類名",
        "決算期年月(最新)",
        "業績決算書有無(最新)",
        "業績売上高(百万円)(最新)",
        "税引後利益(百万円)(最新)",
        "自己資本比率(%)(最新)",
        "配当率(%)(最新)",
        "決算期年月(前期)",
        "業績決算書有無(前期)",
        "業績売上高(百万円)(前期)",
        "税引後利益(百万円)(前期)",
        "自己資本比率(%)(前期)",
        "配当率(%)(前期)",
        "決算期年月(前々期)",
        "業績決算書有無(前々期)",
        "業績売上高(百万円)(前々期)",
        "税引後利益(百万円)(前々期)",
        "自己資本比率(%)(前々期)",
        "配当率(%)(前々期)",
        "株主数",
        "株主1",
        "株主2",
        "株主3",
        "株主4",
        "株主5",
        "代表者出身県",
        "代表者出身校",
        "役員1",
        "役員2",
        "役員3",
        "役員4",
        "役員5",
        "役員6",
        "役員7",
        "役員8",
        "役員9",
        "役員10",
        "算出年月(最新)",
        "算出年月(前回)",
        "算出年月(2回前)",
        "算出年月(3回前)",
        "算出年月(4回前)",
        "算出年月(5回前)",
        "算出年月(6回前)",
        "算出年月(7回前)",
        "算出年月(8回前)",
        "算出年月(9回前)",
        "算出年月(10回前)",
        "算出年月(11回前)",
        "予測値グレード(最新)",
        "予測値グレード(前回)",
        "予測値グレード(2回前)",
        "予測値グレード(3回前)",
        "予測値グレード(4回前)",
        "予測値グレード(5回前)",
        "予測値グレード(6回前)",
        "予測値グレード(7回前)",
        "予測値グレード(8回前)",
        "予測値グレード(9回前)",
        "予測値グレード(10回前)",
        "予測値グレード(11回前)",
        "主要仕入先漢字企業名1",
        "主要仕入先漢字企業名2",
        "主要仕入先漢字企業名3",
        "主要仕入先漢字企業名4",
        "主要仕入先漢字企業名5",
        "主要販売先漢字企業名1",
        "主要販売先漢字企業名2",
        "主要販売先漢字企業名3",
        "主要販売先漢字企業名4",
        "主要販売先漢字企業名5",
        "取引銀行1",
        "取引銀行2",
        "取引銀行3",
        "取引銀行4",
        "取引銀行5",
        "取引銀行6",
        "取引銀行7",
        "取引銀行8",
        "取引銀行9",
        "取引銀行10",
    ]

    def fetch_company_info(self, company_number: str) -> Optional[dict]:
        """
        法人番号をキーにCosmosDBから対象企業のデータを取得する
        """
        database = self._get_cosmos_db_client()
        container = database.get_container_client(
            self.cosmos_db_params["container"])

        query = "SELECT " + \
            ", ".join([f"c[\"{col}\"]" for col in self.columns]) + \
            " FROM c WHERE c[\"法人番号\"] = @company_number"
        parameters = [{"name": "@company_number", "value": company_number}]

        result = container.query_items(
            query=query,
            parameters=parameters,
            partition_key=company_number
        )

        try:
            return next(result)
        except StopIteration:
            return None

    def _get_cosmos_db_client(self):
        """
        CosmosDBのDBプロキシーの取得
        """
        database = self.client.get_database_client(
            database=self.cosmos_db_params["database"])
        logging.debug(f"loaded: {database.id}")
        return database


@bp.route(route="hanabi", methods=("GET",))
async def search_company(req: func.HttpRequest) -> func.HttpResponse:
    """
    法人番号idを受け取り、CosmosDBから該当データを検索して返す。
    """
    logging.info("search_company processed a request.")

    company_id: str = req.params.get("company_id")
    logging.info(f"company_id: {company_id}")
    if not company_id:
        return func.HttpResponse(
            json.dumps({
                "message": "company_id is required",
                "data": []
            }),
            status_code=400
        )

    # 数字のみかチェック
    normalized_id = HanabiFetchService.normalize_digits(company_id)
    if not normalized_id.isdigit():
        return func.HttpResponse(
            json.dumps({
                "message": "company_id must contain only digits (full-width allowed)",
                "data": []
            }),
            status_code=400
        )

    try:
        # HanabiFetchServiceを使用してCosmosDBから検索
        service = HanabiFetchService(COSMOS_CLIENT)
        company_info = service.fetch_company_info(normalized_id)

        if company_info:
            logging.info(f"Found matching record for company_id: {company_id}")
            return func.HttpResponse(
                json.dumps({
                    "message": "OK",
                    "data": [company_info, ]
                }),
                status_code=200
            )
        else:
            # 処理自体は正常のため200で返却
            logging.warning("No matching record found")
            return func.HttpResponse(
                json.dumps({
                    "message": "No matching record found",
                    "data": []
                }),
                status_code=200
            )

    except CosmosHttpResponseError as e:
        logging.error(f"Cosmos DB error: {e}")
        return func.HttpResponse(
            json.dumps({
                "message": "Database error occurred",
                "data": []
            }),
            status_code=500
        )
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return func.HttpResponse(
            json.dumps({
                "message": "An unexpected error occurred",
                "data": []
            }),
            status_code=500
        )
