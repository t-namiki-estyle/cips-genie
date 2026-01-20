import azure.functions as func
import azure.durable_functions as d_func

import json
import base64
import logging

from config import BLOB_SERVICE_CLIENT, FILE_CONTAINER_NAME
from utils.token import decode_id_token

from i_style.aiohttp import AsyncHttpClient
from i_style.token import EntraIDTokenManager

bp = d_func.Blueprint()


@bp.route(route="genie/blob", methods=("GET",))
async def download_blob(req: func.HttpRequest) -> func.HttpResponse:
    """
    id tokenとblob名を受け取り、ファイルを返す
    id tokenの検証と id tokenから取得したupnがblobの持ち主か（命名ルールで判断）の検証を行う。
    """
    logging.info('download_blob processed a request.')
    async_http_client = AsyncHttpClient()

    blob_name: str = req.params.get("blob_name")
    # IDトークンによる認証処理
    try:
        id_token = req.params.get("id_token")
        keys = await EntraIDTokenManager.get_entra_openid_keys(async_http_client)
        upn, _, _ = decode_id_token(id_token, keys)

        if not blob_name.startswith(upn):
            raise ValueError(
                f"blobの持ち主とupnが異なります。id_token: {upn}, blob: {blob_name}")
    except Exception as e:
        logging.warning(f"token error: {e}")

        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=400
        )

    logging.info(f"upn: {upn}")
    logging.info(f"blob_name: {blob_name}")

    try:
        container_client = BLOB_SERVICE_CLIENT.get_container_client(
            FILE_CONTAINER_NAME)

        blob_client = container_client.get_blob_client(blob_name)
        blob_content = await blob_client.download_blob()
        blob_content = await blob_content.readall()

    except Exception as e:
        logging.error(f"blobからのダウンロードに失敗しました。{e}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500
        )

    encoded_content = base64.b64encode(blob_content).decode('utf-8')
    return func.HttpResponse(
        json.dumps({"data": encoded_content}),
        status_code=200
    )
