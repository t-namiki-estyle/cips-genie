import azure.functions as func
import azure.durable_functions as d_func
import logging
import traceback
import uuid
import json
import os
import functools

from utils.token import decode_id_token

from i_style.aiohttp import AsyncHttpClient
from i_style.token import EntraIDTokenManager

##
# blueprint
##
history_bp = d_func.Blueprint()


@history_bp.route(route="genie/session", methods=("GET",))
async def history_create_session(req: func.HttpRequest) -> func.HttpResponse:
    """
    プロンプト履歴の機能共通で利用するためのsession_idの発行を行います。
    """
    logging.info("history_create_session processed a request.")
    response = {
        "status": 200,
        "data": [
            {
                "session_id": str(uuid.uuid4())
            }
        ]
    }
    return func.HttpResponse(json.dumps(response))


@history_bp.route(route="genie/history/{mode}/{upn}", methods=("GET", "POST", "DELETE"))
async def history_session(req: func.HttpRequest) -> func.HttpResponse:
    """
    各モード（メニュー）ごとのプロンプト履歴のタイトル一覧を表示、更新するためのエンドポイント
    - GET: セッション一覧の取得
    - POST: タイトルの更新、お気に入り登録、（セッションの削除からの復旧）
    - DELETE: お気に入りの削除、セッションの削除
    """
    logging.info('history_session processed a request.')
    if req.method == "GET":
        response = await history_get_session(req)
    elif req.method == "POST":
        response = await history_post_session(req)
    elif req.method == "DELETE":
        response = await history_delete_session(req)

    status_code = int(response.pop("status"))
    if status_code >= 300:
        # response["message"] = "エラーが発生しました。"
        pass

    return func.HttpResponse(
        json.dumps(response),
        mimetype="application/json",
        status_code=status_code
    )


@history_bp.route(route="genie/history/{mode}/{upn}/{session_id}", methods=("GET", "POST", "DELETE"))
async def history_messages(req: func.HttpRequest) -> func.HttpResponse:
    """
    各セッションごとのメッセージ一覧を表示、更新するためのエンドポイント
    - GET: メッセージ一覧の取得
    - POST: メッセージの更新、お気に入り登録、（メッセージの削除からの復旧）
    - DELETE: お気に入りの削除、メッセージの削除
    """
    logging.info('history_messages processed a request.')
    if req.method == "GET":
        response = await history_get_message(req)
    elif req.method == "POST":
        response = await history_post_message(req)
    elif req.method == "DELETE":
        response = await history_delete_session(req)

    status_code = int(response.pop("status"))
    if status_code >= 300:
        # response["message"] = "エラーが発生しました。"
        pass

    return func.HttpResponse(
        json.dumps(response),
        mimetype="application/json",
        status_code=status_code
    )


@history_bp.route(route="genie/history/{mode}/{upn}/agents/{agent_id}", methods=("GET",))
async def history_agents(req: func.HttpRequest) -> func.HttpResponse:
    """
    特定のエージェントの履歴を取得するためのエンドポイント
    - GET: エージェントの履歴の取得
    """
    logging.info('history_agents processed a request.')
    if req.method == "GET":
        response = await history_get_agent(req)

    status_code = int(response.pop("status"))
    if status_code >= 300:
        # response["message"] = "エラーが発生しました。"
        pass

    return func.HttpResponse(
        json.dumps(response),
        mimetype="application/json",
        status_code=status_code
    )


def history_setup(func_handler):
    """
    プロンプト履歴用API特有の初期化の処理を共通化するためのラッパー
    """
    @functools.wraps(func_handler)
    async def wrapper(req: func.HttpRequest):
        # 初期化処理
        async_http_client = AsyncHttpClient()
        history_base_url = os.environ.get("HISTORY_API_URL")
        history_api_key = os.environ.get("HISTORY_API_KEY")

        # modeの設定
        mode = req.route_params.get("mode")
        if mode not in (
            "inside", "minutes", "ocr", "research",
            "panel_sales_enquiry", "audit_precheck", "tariff", "product",
            "company_info", "audit_flowchart", "accounting_fraud",
            "genie_agent", "finance_department", "expat_reimbursement", "hana",
            "market_analyst", "competitor_analyst", "customer_analyst", "target_analyst", 
            "idea_generator", "marketing_support_integrator"
        ):
            mode = "genie"

        # session_id取得
        session_id = req.route_params.get("session_id")

        # agent_id取得
        agent_id = req.route_params.get("agent_id")

        # IDトークンによる認証処理
        try:
            id_token = req.params.get("id_token")
            keys = await EntraIDTokenManager.get_entra_openid_keys(async_http_client)
            upn, _, _ = decode_id_token(id_token, keys)

            assert upn == req.route_params.get(
                'upn'), f"upnが異なります。id_token: {upn}, route: {req.route_params.get('upn')}"
        except Exception as e:
            logging.warning(f"token error: {e}")
            response = {
                'status': 500,
                'data': []
            }

            return response

        logging.info(f"mode: {mode}")
        logging.info(f"upn: {upn}")
        if session_id != None:
            logging.info(f"session_id: {session_id}")

        # URL 生成
        url = f"{history_base_url}/api/history/{mode}/{upn}"

        # 共通パラメータを辞書にまとめる
        common_params = {
            "async_http_client": async_http_client,
            "history_api_key": history_api_key,
            "url": url,
            "mode": mode,
            "session_id": session_id,
            "upn": upn
        }
        if agent_id is not None:
            common_params["agent_id"] = agent_id

        try:
            # ビジネスロジック（各ハンドラ）を呼び出す
            return await func_handler(req, **common_params)
        except Exception as e:
            tb = traceback.format_exc()
            logging.critical(f"処理中にエラーが発生しました: {e}, {tb}")
            response = {"status": 500, "data": []}
            return response
    return wrapper


@history_setup
async def history_get_message(req: func.HttpRequest,
                              async_http_client,
                              history_api_key,
                              url: str,
                              mode: str,
                              session_id: str,
                              upn: str) -> dict:
    """
    指定されたセッション内のメッセージ履歴を取得する関数
    """
    api_name = "get_history_detail"
    logging.info("詳細履歴を取得しています。")

    params = {'sessionId': session_id}

    try:
        history_detail_json = await async_http_client.get(
            url=url,
            api_key=history_api_key,
            params=params,
            process_name=api_name
        )

        if mode == "minutes":
            # 全てのmessageを汎用履歴から取得する
            response_data = []
            for message in history_detail_json["messages"]:
                # 文字起こしか議事録かの確認
                if message.get("model") in {"whisper", "gpt-4o-transcribe"}:
                    _type = "transcribe"
                else:
                    _type = "minutes"

                # 翻訳を行っている場合は翻訳を除去する
                if message["role"] == "assistant" and _type == "transcribe":
                    for item in message["content"]:
                        if item["type"] == "text":
                            item["text"] = item["text"].split(
                                "\n" + "*" * 20 + "\n")[0]

                # 必要な情報のみ追加
                response_data.append({
                    "date": message["updatedAt"],
                    "message_id": message["id"],
                    "role": message["role"],
                    "content": message["content"],
                    "type": _type
                })

        else:
            response_data = [
                {
                    'date': message['updatedAt'],
                    'message_id': message['id'],
                    'role': message['role'],
                    'content': message['content'],
                    **({"agent_id": message['agentId']} if message.get('agentId') else {})
                }
                for message in history_detail_json['messages']
            ]

        response = {
            'status': 200,
            'data': response_data
        }

    except Exception as e:
        error = api_name + ": " + str(e)
        tb = traceback.format_exc()
        logging.critical(f"{error}, {tb}")

        response = {
            'status': 500,
            'data': [],
            'error': error
        }

    return response


@history_setup
async def history_get_session(req: func.HttpRequest,
                              async_http_client,
                              history_api_key,
                              url: str,
                              mode: str,
                              session_id: str,
                              upn: str) -> dict:
    """
    指定されたモードのセッション履歴を取得する関数
    """
    api_name = "get_history_summary"
    logging.info("履歴の概要一覧を取得しています。")

    try:
        history_summary_json = await async_http_client.get(
            url=url,
            api_key=history_api_key,
            process_name=api_name
        )

        response_data = [
            {
                'date': item['updatedAt'],
                'title': item['title'],
                'session_id': item['sessionId'],
                'favorite': item['favorite']
            }
            for item in history_summary_json['titles']
        ]

        response = {
            'status': 200,
            'data': response_data
        }

    except Exception as e:
        error = api_name + ": " + str(e)
        tb = traceback.format_exc()
        logging.critical(f"{error}, {tb}")

        response = {
            'status': 500,
            'data': [],
            'error': error
        }

    return response


@history_setup
async def history_get_agent(req: func.HttpRequest,
                            async_http_client,
                            history_api_key,
                            url: str,
                            mode: str,
                            session_id: str,
                            agent_id: str,
                            upn: str) -> dict:
    """
    指定されたエージェントの実行履歴を取得する関数
    """
    api_name = "get_history_agent"
    logging.info("エージェント実行履歴を取得しています。")

    params = {'agentId': agent_id}

    try:
        history_agent_json = await async_http_client.get(
            url=url,
            api_key=history_api_key,
            params=params,
            process_name=api_name
        )

        response_data = [
            {
                'id': item['id'],
                'agent_id': item['agentId'],
                'date': item['updatedAt'],
                'role': item['role'],
                'content': item['content'],
                'toolInfo': item['toolInfo'],
                'type': item['type'],
            }
            for item in history_agent_json['agents']
        ]

        response = {
            'status': 200,
            'data': response_data
        }

    except Exception as e:
        error = api_name + ": " + str(e)
        tb = traceback.format_exc()
        logging.critical(f"{error}, {tb}")

        response = {
            'status': 500,
            'data': [],
            'error': error
        }

    return response


@history_setup
async def history_post_session(req: func.HttpRequest,
                               async_http_client,
                               history_api_key,
                               url: str,
                               mode: str,
                               session_id: str,
                               upn: str) -> dict:
    """
    指定されたセッション情報を更新する関数
    セッションのタイトル更新、お気に入り登録、削除フラグの設定を行う
    """
    req_json = req.get_json()
    session_id = req_json.get("session_id")
    if not session_id:
        raise ValueError("更新時はsession_idが必須です。")

    api_name = "update_history"
    try:
        history_update_json = await async_http_client.post(
            url=url,
            api_key=history_api_key,
            process_name=api_name,
            json_data=req_json
        )

        updated_session = history_update_json.get('updated_session')
        response_data = [
            {
                'date': updated_session['updatedAt'],
                'title': updated_session['title'],
                'session_id': updated_session['sessionId'],
                'favorite': updated_session['favorite']
            }
        ]
        if 'delete_flag' in req_json:
            response_data[0]['delete_flag'] = updated_session['delete_flag']

        response = {
            'status': 200,
            'data': response_data
        }

    except Exception as e:
        error = api_name + ": " + str(e)
        tb = traceback.format_exc()
        logging.critical(f"{error}, {tb}")

        response = {
            'status': 500,
            'data': [],
            'error': error
        }

    return response


@history_setup
async def history_post_message(req: func.HttpRequest,
                               async_http_client,
                               history_api_key,
                               url: str,
                               mode: str,
                               session_id: str,
                               upn: str) -> dict:
    """
    指定されたセッション内のメッセージ情報を更新する関数
    メッセージの内容更新、お気に入り登録、削除フラグの設定を行う
    """
    req_json = req.get_json()
    if not session_id:
        raise ValueError("更新時はsession_idが必須です。")

    api_name = "update_history"
    try:
        history_update_json = await async_http_client.post(
            url=url,
            api_key=history_api_key,
            process_name=api_name,
            json_data=req_json
        )

        updated_messages = history_update_json.get('updated_messages')
        response_data = [
            {
                'message_id': updated_message['id'],
                'role': updated_message['role'],
                'content': updated_message['content']
            }
            for updated_message in updated_messages
        ]
        if 'delete_flag' in req_json:
            for i, updated_message in enumerate(updated_messages):
                response_data[i]['delete_flag'] = updated_message['delete_flag']

        response = {
            'status': 200,
            'data': response_data
        }

    except Exception as e:
        error = api_name + ": " + str(e)
        tb = traceback.format_exc()
        logging.critical(f"{error}, {tb}")

        response = {
            'status': 500,
            'data': [],
            'error': error
        }

    return response


@history_setup
async def history_delete_session(req: func.HttpRequest,
                                 async_http_client,
                                 history_api_key,
                                 url: str,
                                 mode: str,
                                 session_id: str,
                                 upn: str) -> dict:
    """
    指定されたセッションを削除する関数
    """
    req_json = req.get_json()
    session_id = req_json.get("session_id")
    if not session_id:
        raise ValueError("更新時はsession_idが必須です。")

    api_name = "delete_history"
    try:
        history_update_json = await async_http_client.post(
            url=url,
            api_key=history_api_key,
            process_name=api_name,
            json_data=req_json
        )

        updated_session = history_update_json.get('updated_session')
        response_data = [
            {
                'date': updated_session['updatedAt'],
                'title': updated_session['title'],
                'session_id': updated_session['sessionId'],
                'favorite': updated_session['favorite']
            }
        ]
        if 'delete_flag' in req_json:
            response_data[0]['delete_flag'] = updated_session['delete_flag']

        response = {
            'status': 200,
            'data': response_data
        }

    except Exception as e:
        error = api_name + ": " + str(e)
        tb = traceback.format_exc()
        logging.critical(f"{error}, {tb}")

        response = {
            'status': 500,
            'data': [],
            'error': error
        }

    return response


@history_setup
async def history_delete_message(req: func.HttpRequest,
                                 async_http_client,
                                 history_api_key,
                                 url: str,
                                 mode: str,
                                 session_id: str,
                                 upn: str) -> dict:
    """
    指定されたセッション内のメッセージを削除する関数
    """
    req_json = req.get_json()
    if not session_id:
        raise ValueError("更新時はsession_idが必須です。")

    api_name = "delete_history"
    try:
        history_update_json = await async_http_client.post(
            url=url,
            api_key=history_api_key,
            process_name=api_name,
            json_data=req_json
        )

        updated_messages = history_update_json.get('updated_messages')
        response_data = [
            {
                'message_id': updated_message['id'],
                'role': updated_message['role'],
                'content': updated_message['content']
            }
            for updated_message in updated_messages
        ]
        if 'delete_flag' in req_json:
            for i, updated_message in enumerate(updated_messages):
                response_data[i]['delete_flag'] = updated_message['delete_flag']

        response = {
            'status': 200,
            'data': response_data
        }

    except Exception as e:
        error = api_name + ": " + str(e)
        tb = traceback.format_exc()
        logging.critical(f"{error}, {tb}")

        response = {
            'status': 500,
            'data': [],
            'error': error
        }

    return response
