import azure.functions as func
from azure.storage.blob import BlobServiceClient
import logging
import json
import requests
import io
import os
import re
import asyncio
from config import VARIABLE_LIST, ENVIRONMENT_SELECTED
from util import error_response, convert_url_to_a
from urllib.parse import quote

from utils.token import decode_id_token
from i_style.aiohttp import AsyncHttpClient
from i_style.token import EntraIDTokenManager

# Box APIのOAuth設定値
CLIENT_ID = os.environ.get("BOX_CLIENT_ID")
CLIENT_SECRET = os.environ.get("BOX_CLIENT_SECRET")

# トークンキャッシュ用のBLOB設定値
CONNECTION_NAME = os.environ.get("BLOB_CONNECTION_STRING")
CONTAINER_NAME = 'boxauth'

box_bp = func.Blueprint()

# エンドポイント: genie/box/callback
# 処理内容: Box OAuth認証のコールバック処理
#   - Boxから返却された認可コードをcodeパラメタから取得
#   - 認可コードを使用してアクセストークンを取得
#   - アクセストークンを使用してユーザーのUPNを取得
#   - アクセストークンをBLOBストレージにキャッシュ（OAuth認証の度にアクセストークンが変更されるため必ず更新する）
#   - レスポンスを返却
#    -　アクセストークンを取得できた場合は、呼び出した画面をクローズするJavaScriptを応答
#    -  取得できない場合はHTTP200以外の応答と、エラーメッセージを表示するレスポンスを返却する
#
# リクエスト例（BoxAPIからのコールバックでのみ呼び出し）:
#   GET https://xxxx/api/genie/box/callback?code=xxxxxxxxx
#
# レスポンス例（成功時）:
#   <script>window.close();</script>  ... ウインドウを消去する
#
# レスポンス例（失敗時）:
#   リクエストパラメタが不足しています。呼び出しが適切か再度確認してください。


@box_bp.function_name(name='callback')
@box_bp.route(route='genie/box/callback', methods=('GET', 'POST'))
def callback(req: func.HttpRequest) -> func.HttpResponse:
    logging.info(f'[Triggerd] Box OAuth Callback')

    try:
        # レスポンスされた引換用のコードを取得する
        # AzureFunctionsの関数キーとBoxの返却するコードのパラメータ名が同じくcodeであるため、正規表現で取得する
        # コールバックの際にBoxAPIが「status」など別のパラメータをつける場合もあるため、最後のcodeを取得するようにする
        # 例）https://xxxx/api/genie/box/callback?code=xxxxxxxxx&status=&code=yyyyyyyyy ... この場合yyyyyyyyyを取得する
        logging.info(f'Requested url is {req.url}')
        url = req.url
        match = re.search(r'code=([^&]+)&?$', url)

        # Boxから連携される認証コードがない場合はエラーとして処理を中断する
        if not match:
            raise ValueError(
                'OAuth code is missing. Check the request parameter.')

        code = match.group(1)

        logging.info(f'OAuth code is {code}')

        # 引換用のコードをもとにしてアクセストークンをリクエストする
        tokens = get_access_token_from_oauth(code)
        logging.info(
            f'Access token is {tokens.get("access_token")}, refresh token is {tokens.get("refresh_token")}')

        # 取得したアクセストークンを使ってBox APIからUPNを取得する
        user_upn = get_user_upn(tokens.get('access_token'))

        # アクセストークンをBLOBにストアする
        if not store_access_token_to_cache(user_id=user_upn, access_token=tokens.get('access_token'), refresh_token=tokens.get('refresh_token')):
            raise IOError('Failed to store access token')

        http_status_code = 200  # OK
        response_body = f'<script>window.close();</script>'  # 認証に使ったウィンドウを閉じる

    # BoxからOAuth認証エラーが返却され、アクセストークンが取得できなかった場合
    except requests.exceptions.RequestException as e:
        logging.error(f'Failed to get access token from Box API.')
        http_status_code = 401  # Unauthorized
        response_body = f'アクセストークンを取得できませんでした。認証コードもしくはクライアントIDが正しくない可能性があります。'

    # パラメタチェック不正の場合
    except ValueError as e:
        logging.error(f'Required parameter "code" is missing.')
        http_status_code = 400  # Bad Request
        response_body = f'リクエストパラメタが不足しています。呼び出しが適切か再度確認してください。'

    # アクセストークン取得後、BLOBへのキャッシュ保存に失敗した場合
    except IOError as e:
        logging.info(
            f'Failed to store access token. But access token is valid.')
        http_status_code = 206  # Partial Content
        response_body = f'アクセストークンの取得に失敗しました。時間をおいて再度お試しください。'

    # 予期せぬ例外が発生した場合
    except Exception as e:
        logging.error(f'Failed to get access token from Box API. {e}')
        http_status_code = 500  # Internal Server Error
        response_body = f'アクセストークンの取得に失敗しました。時間をおいて再度お試しください。'

    # 正常系・異常系問わずレスポンスを返却する
    finally:
        return func.HttpResponse(
            status_code=http_status_code,
            body=json.dumps(response_body),
            mimetype='text/html'
        )

# エンドポイント: genie/box/auth
# 処理内容: アクセストークンのキャッシュ有無を確認し、必要に応じてOAuth認証へリダイレクトする
#   - クエリパラメータにuser_idがある場合
#     - キャッシュからアクセストークンを取得
#     - アクセストークンが有効期限内であれば返却
#     - 無効期限切れであればOAuth認証へリダイレクト
#   - クエリパラメータにuser_idがない場合
#     - OAuth認証へリダイレクト
#
# リクエスト例:
#   GET https://xxx/api/genie/box/auth?user_id=user@example.com
#
# レスポンス例（キャッシュヒットし認証不要の場合）:
#   <script>window.close();</script>  ... ウインドウを消去する
#
# レスポンス例（キャッシュヒットなし、もしくはキャッシュヒットしたがアクセストークンが有効期限切れの場合）:
#   302 Found (LocationヘッダーにOAuth認証URLが設定され、BoxのOAuth認証ページへリダイレクトする)


@box_bp.function_name(name='auth')
@box_bp.route(route='genie/box/auth', methods=('GET', 'POST'))
def auth(req: func.HttpRequest) -> func.HttpResponse:
    logging.info(f'[Triggerd] Box OAuth')

    # リクエストパラメタチェック
    user_id = req.params.get('user_id')

    # リクエストパラメタにユーザIDが付与されていない場合は、初回と同様にOAuth認証へリダイレクトする
    if user_id is None:
        logging.info(f'user_id is missing. Redirect to OAuth.')
        return redirect_to_oauth()

    # ユーザIDが付与されている場合は、キャッシュからのアクセストークン取得を試みる
    # アクセストークンが有効期限切れの場合は、リフレッシュトークンを使用して更新を試みる
    access_token = get_access_token(user_id)

    # アクセストークンがキャッシュされていない場合や、更新もできない場合は、初回と同様にOAuth認証へリダイレクトする
    if access_token is None:
        logging.info(
            f'UserID {user_id}, OAuth token is not found or refresh failed. Redirect to OAuth.')
        return redirect_to_oauth()

    # 有効期限内であれば、OAuth認証不要として終了する
    logging.info(
        f'UserID {user_id}, OAuth token is valid. No need to re-authenticate.')
    response_body = f'<script>window.close();</script>'  # 認証に使ったウィンドウを閉じるようにJSをレスポンスする

    return func.HttpResponse(
        status_code=200,
        body=json.dumps(response_body),
        mimetype='text/html'
    )

# エンドポイント: llm/box
# 処理内容: Box内のドキュメントを検索し、ユーザーの質問に回答する
#  - ユーザーからの質問とメールアドレスをリクエストボディから取得
#  - 質問内にBoxのURLが含まれている場合は、パースしてフォルダIDを取得し、URLを削除
#  - ユーザーのメールアドレスに対応するBoxアクセストークンをキャッシュから取得
#    - キャッシュに存在しない場合はエラーを返却
#    - アクセストークンが有効期限切れの場合はエラーを返却
#  - CTCさん作成のBox文章検索APIを呼び出し、質問に対する回答を取得
#    - API呼び出しに失敗した場合はエラーを返却
#  - 回答に関連するBoxドキュメントのURLを3つまで取得し、回答に追加
#  - 回答を整形し、JSON形式で返却
#
# リクエストボディ例（必要部分のみ記載）:
# {
#   "messages": [
#     {"content": "Boxの使い方を教えてください"},
#   ],
#   "mail": "user@example.com",
#   "folder_path": "Boxフォルダパス",（任意）
#   "folder_id": "BoxフォルダID"　（任意）
# }
#
# レスポンスボディ例:
# {
#   "id": "***********",
#   "object": "LLM_BOX",
#   "created": 1234567890,
#   "model": "gpt-35-turbo",
#   "choices": [
#     {
#       "index": 0,
#       "finish_reason": "stop",
#       "message": {
#         "role": "assistant",
#         "content": "Boxの使い方についての回答〜〜<br><a href='https://example.com/document1'>document1</a><br>..."
#       }
#     }
#   ],
#   "usage": {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}
# }


@box_bp.function_name(name='box')
@box_bp.route(route="llm/box", methods=("POST",))
def llm_box(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('[Triggered] LLM Box')

    # JSONのリクエストボディを取得
    req_json = req.get_json()
    messages = req_json["messages"]
    user_mail = req_json["mail"]

    # ユーザーの入力を取得
    user_input = ""
    box_link = ""

    for item in messages[-1]["content"]:
        if item["type"] == "text":
            user_input = item["text"]
        elif item["type"] == "link":
            box_link = item["link"]

    box_folder_path = req_json.get("folder_path")  # フォルダパスは任意（存在しない場合はNone）
    box_folder_id = req_json.get("folder_id")  # フォルダIDは任意（存在しない場合はNone）
    box_folder_share_url = req_json.get(
        "folder_share_url")  # フォルダ共有URLは任意（存在しない場合はNone）
    logging.info(
        f'UserID is {user_mail}. User input: {user_input}. Folder path: {box_folder_path}, Folder ID: {box_folder_id}, Folder share URL: {box_folder_share_url}, Box link: {box_link}')

    if box_link:
        # box_link内にBoxURLが含まれている場合は、パースしてフォルダIDを取得しURLを削除する
        # ドメインは「app.box.com」もしくは「itochu.*.box.com」を対象とする
        box_url_pattern = r"https://(?:app|itochu\.\w+)\.box\.com/folder/(\d+)/?.*"
        match = re.search(box_url_pattern, box_link)
        if match:
            box_folder_id = match.group(1)  # BoxのフォルダIDを取得する
            # ユーザーの入力からURLを削除する
            box_link = re.sub(box_url_pattern, '', box_link)

        # box_link内にBox共有リンクが含まれている場合は、そのリンクのみを抽出する
        # 共有リンクの形式は「https://app.box.com/s/jsq3a0wafrrh....」もしくは「https://itochu.box.com/s/31sfr2r5y9xrv....」となる
        box_share_link_pattern = r"https://(?:app|itochu)\.box\.com/s/\w+"
        match = re.search(box_share_link_pattern, box_link)
        if match:
            box_folder_share_url = match.group(0)

        # box_link内にBoxパスが含まれている場合は、パースしてパスを取得し削除する
        # パスの形式は「C:¥Box¥xxxxxx¥xxxxxx」もしくは「c:¥Box¥xxxxxx¥xxxxxx」
        box_path_pattern = r"[Cc]:¥Box¥.+"
        match = re.search(box_path_pattern, box_link)
        if match:
            box_folder_path = match.group(0)  # Boxのフォルダパスを取得する
            box_link = re.sub(box_path_pattern, '', box_link)

    # Box APIのアクセストークンを取得する
    # もしもトークンの有効期限が切れていれば、リフレッシュトークンを使用して更新したものが返却される
    access_token = get_access_token(user_mail)

    # アクセストークンが返却されなかった場合は再認証が必要なためエラーを返却する
    if access_token is None:
        logging.error(
            f'Access token is not found in cache. Need to re-authenticate.')
        return func.HttpResponse(json.dumps(oauth_request_error_response("Boxとの接続に失敗しました。まだBoxとの認証が完了していないか、ログアウトされている可能性があります。")))

    logging.info(f'Access token is valid. Proceed to CTC Box API.')

    # 履歴の整形
    history = []
    for message in messages[:-1]:
        # roleの確認
        if message["role"] not in ("assistant", "user"):
            continue

        # contentの変換
        content = message["content"]
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = next(
                (item["text"] for item in message["content"] if item["type"] == "text"), "")
        history.append({message["role"]: text})

    # CTCさん作成のBox文章検索APIのエンドポイントを取得
    BOX_FUNCTION_API_KEY = os.environ.get(
        "BOX_FUNCTION_API_KEY")  # TODO: BOX連携用のシークレットキーを入手
    BOX_FUNCTION_URL = os.environ.get("BOX_FUNCTION_URL")

    # CTCさん作成API用のリクエストを準備
    ctc_box_function_headers = {
        'Content-Type': 'application/json',
        "x-functions-key": BOX_FUNCTION_API_KEY,
    }
    ctc_box_function_request_body = {
        key: value
        for key, value in [
            ('access_token', access_token),
            ('userquestion', user_input),
            ('search_folder_id', box_folder_id),
            ('search_folder_path', box_folder_path),
            ('search_shared_link', box_folder_share_url),
            ('history', history)
        ]
        if value is not None
    }  # 各項目がNoneになる時はリクエストボディに含めない
    request_body_data = json.dumps(
        ctc_box_function_request_body).encode('utf-8')

    # CTC Box APIの呼び出し
    try:
        ctc_response = requests.post(
            BOX_FUNCTION_URL, headers=ctc_box_function_headers, data=request_body_data)

        # CTC Box APIのレスポンスが200以外の場合はエラーを返却する
        if ctc_response.status_code >= 300:
            logging.error(
                f'CTC Box API response error. HTTP status code is {ctc_response.status_code}.')
            return func.HttpResponse(json.dumps(error_response("Boxとの接続が出来ませんでした。時間をおいてお試しください。")))

        # CTC Box APIのレスポンスを取得
        ctc_response_json = ctc_response.json()
        ctc_answer = ctc_response_json["answer"]
        logging.info(f'CTC Box API response: {ctc_answer}')

    # JSONのパースに失敗した場合はエラーを返却する
    except json.JSONDecodeError as e:
        logging.error(f'Failed to parse CTC Box API response. {e}')
        return func.HttpResponse(json.dumps(error_response("boxとの接続が出来ませんでした。時間をおいてお試しください。")))

    # 予期せぬ例外が発生した場合はエラーを返却する
    except Exception as e:
        logging.error(
            f'Failed to connect to CTC Box API. Check the connection. {e}')
        return func.HttpResponse(json.dumps(error_response("boxとの接続が出来ませんでした。時間をおいてお試しください。")))

    # 検索結果をリターン
    response = {
        'id': '***********',
        'object': 'LLM_BOX',
        'created': 1234567890,
        'model': 'gpt-35-turbo',
        'choices': [{
            'index': 0,
            'finish_reason': 'stop',
            'message': {
                'role': 'assistant', 'content': ctc_answer}}],
        'usage': {'completion_tokens': 0, 'prompt_tokens': 0, 'total_tokens': 0}}

    return func.HttpResponse(json.dumps(response))


@box_bp.function_name(name='box_initial_message')
@box_bp.route(route='genie/box/init_message', methods=('GET',))
async def box_initial_message(req: func.HttpRequest) -> func.HttpResponse:
    logging.info(f'[Triggerd] Box initial message')

    # 非同期http通信クライアントのインスタンス化
    async_http_client = AsyncHttpClient()
    try:
        id_token = req.params.get("id_token")
        keys = await EntraIDTokenManager.get_entra_openid_keys(async_http_client)
        _, user_id, user_name = decode_id_token(id_token, keys)
        user_name += "さん"
    except Exception as e:
        logging.warning(f"token error: {e}")
        user_id = req.params.get('mail')
        user_name = "あなた"

    # パラメータからユーザのメールアドレスを取得する
    user_id = req.params.get('mail')

    # パラメーターが不正の場合は、Box認証が必要な旨を返却する
    if user_id is None:
        logging.info(f'User ID is missing. Need to re-authenticate.')
        return func.HttpResponse(json.dumps(oauth_request_error_response(f'I-Colleagueへようこそ！<br>{user_name}がアクセス権限を持っているBoxのファイルを参照して回答します！')))

    # ユーザIDが正常に取得できた場合は、アクセストークンの有効期限を確認する
    access_token = get_access_token(user_id)

    # アクセストークンが取得できなかった場合は、再認証が必要な旨を返却する
    if access_token is None:
        logging.info(
            f'Access token is not found in cache. Need to re-authenticate.')
        return func.HttpResponse(json.dumps(oauth_request_error_response(f'I-Colleagueへようこそ！<br>{user_name}がアクセス権限を持っているBoxのファイルを参照して回答します！')))

    # アクセストークンが取得できた場合は、Boxとの接続が完了している旨を返却する
    logging.info(f'Access token is valid. No need to re-authenticate.')
    response_message = f'I-Colleagueへようこそ！<br>{user_name}がアクセス権限を持っているBoxのファイルを参照して回答します！'
    response = {
        'id': '***********',
        'object': 'LLM_BOX',
        'created': 1234567890,
        'model': 'gpt-35-turbo',
        'choices': [{
            'index': 0,
            'finish_reason': 'stop',
            'message': {
                'role': 'assistant', 'content': response_message}}],
        'usage': {'completion_tokens': 0, 'prompt_tokens': 0, 'total_tokens': 0},
        'blobs': []}

    return func.HttpResponse(json.dumps(response))


# Box API アクセストークン取得関数（内部用）
def get_access_token(user_id: str):
    """Box API アクセストークン取得関数
    この関数は、指定されたユーザーのアクセストークンを取得します。

    Args:
        user_id (str): Box API のユーザーID
    Returns:
        str | None: 
            Box API のアクセストークン
            アクセストークンが取得できない場合、もしくは有効期限が切れている場合は None を返します。
    """
    # パラメータチェック
    if (user_id is None):
        return None

    # OAuth認証完了しているはずなので、キャッシュから読み込む
    tokens = get_access_token_from_cache(user_id)
    if tokens is None:
        # キャッシュにアクセストークンがない場合はエラー
        logging.error(
            f'UserID {user_id}, Access token is not found in cache. Need to re-authenticate.')
        return None

    # アクセストークンの有効期限切れチェック
    # また、アクセストークンは取得できたが、有効期限が切れている場合はまずはリフレッシュトークンを使ってアクセストークンの更新を試みる
    if oauth_token_expired(tokens.get('access_token')):
        # アクセストークンの更新を試みる
        tokens = oauth_token_update(tokens.get('refresh_token'))

        # アクセストークンの更新に失敗した場合は、利用できるトークンはないのでNoneを返却する
        if tokens is None:
            logging.error(
                f'UserID {user_id}, Failed to update access token. Need to re-authenticate')
            return None

        # アクセストークンの更新に成功した場合は、キャッシュに保存しておく
        else:
            logging.info(
                f'UserID {user_id}, OAuth token is refreshed. No need to re-authenticate.')
            if store_access_token_to_cache(user_id, tokens.get('access_token'), tokens.get('refresh_token')):
                logging.info(
                    f'UserID {user_id}, Access token is stored in cache.')

            # キャッシュへの保存に失敗した場合でも、取得したアクセストークンを返却する
            else:
                logging.error(
                    f'UserID {user_id}, Failed to store access token to cache.')
                return tokens.get('access_token')

    return tokens.get('access_token')


# ユーティリティ関数

def oauth_request_error_response(response_message: str):
    """
    Box認証のリクエストエラーが発生した場合に、エラーレスポンスを生成します。

    Args:
        response_message (str): エラーメッセージ
    Returns:
        dict: エラーレスポンスのテンプレート
    """
    ## エスタイル環境向けのBox認証URL
    auth_url = VARIABLE_LIST[ENVIRONMENT_SELECTED]["box_auth_url"]
    response_message += f"""<hr>この機能を利用するためには、Boxにログインする必要があります。<br>下記のボタンから、Boxへログインしてください。<br><br><button type="button" class="btn-primary btn-sm btn-border-key" onclick="window.open('{auth_url}', '_blank')">Box認証</button>"""

    response_template = {
        'id': '***********',
        'object': 'LLM_BOX',
        'created': 1234567890,
        'model': 'gpt-35-turbo',
        'choices': [{
            'index': 0,
            'finish_reason': 'stop',
            'message': {
                'role': 'assistant', 'content': response_message}}],
        'usage': {'completion_tokens': 0, 'prompt_tokens': 0, 'total_tokens': 0},
        'blobs': []}

    return response_template


def get_oauth_url() -> str:
    """Box API OAuth認証URL取得関数
    この関数は、Box API の OAuth 認証に必要なパラメータ（client_id、response_type）を付与した認可エンドポイントの URL を生成します。

    Returns:
        str: Box API の OAuth 認可エンドポイントの URL
    """

    url = 'https://account.box.com/api/oauth2/authorize'
    # BOXコールバックURLは環境変数から取得
    callback_url = os.environ.get("BOX_CALLBACK_URL")
    # コールバック先URLを安全のためにURLエンコードする
    encoded_callback_url = quote(callback_url, safe='')
    auth_url = f'{url}?client_id={CLIENT_ID}&redirect_uri={encoded_callback_url}&response_type=code'
    return auth_url


def get_access_token_from_oauth(code: str):
    """Box API アクセストークン取得関数

    OAuthコードを使用してBox APIのアクセストークンを取得する関数です。

    Parameters:
        code (str): OAuthコード

    Returns:
        dict: アクセストークンとリフレッシュトークンを含む辞書。
              辞書の構造は以下のようになります:
              {
                  'access_token': str,
                  'refresh_token': str
              }
              トークンの取得に失敗した場合は例外を発生させます。
    Raises:
        requests.exceptions.RequestException: リクエストが失敗した場合に発生します
    """

    url = 'https://account.box.com/api/oauth2/token'
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET
    }

    token_response = requests.post(url, data=data)

    if token_response.status_code >= 300:
        try:
            error_msg = token_response.json()["error"]
        except:
            error_msg = token_response.text
        raise requests.exceptions.RequestException(error_msg)

    response_json = token_response.json()

    return {
        'access_token': response_json['access_token'],
        'refresh_token': response_json['refresh_token']
    }


def get_user_upn(token: str) -> str:
    """Box API ユーザー情報取得関数
    この関数は、Box API のユーザー情報を取得します。

    Args:
        token (str): Box API のアクセストークン
    Returns:
        str: Box APIから取得したユーザのUPN
    Raises:
        Exception: ユーザー情報の取得に失敗した場合
    """

    url = 'https://api.box.com/2.0/users/me'
    headers = {'Authorization': f'Bearer {token}'}
    response = requests.get(url, headers=headers)

    if response.status_code >= 300:
        try:
            error_msg = response.json()["error"]
        except:
            error_msg = response.text
        raise requests.exceptions.RequestException(error_msg)

    return response.json()['login']


def store_access_token_to_cache(user_id: str, access_token: str, refresh_token: str) -> bool:
    """Azure Blob Storageに保存されたCSVファイルに、ユーザーIDとアクセストークンを保存・更新する関数。

    Args:
        user_id (str): ユーザーのID
        access_token (str): アクセストークン
        refresh_token (str): リフレッシュトークン
    Returns:
        bool: トークンの保存・更新が成功した場合はTrue、それ以外はFalse
    Raises:
        Exception: アクセストークンの保存に失敗した場合に発生する例外
    """
    try:
        blob_service_client = BlobServiceClient.from_connection_string(
            conn_str=CONNECTION_NAME)
        container_client = blob_service_client.get_container_client(
            CONTAINER_NAME)

        # コンテナがない場合は作成
        if not container_client.exists():
            try:
                container_client.create_container()
            except Exception as e:
                logging.debug(
                    f"Container already exists or error creating container: {e}")

        # ファイル名は'{user_id}.json'として、ユーザごとに異なったファイルに格納する
        # user_idにはメールアドレスが入るため、安全のためにエンコードする
        encoded_user_id = quote(user_id, safe='')
        filename = f'boxauth/{encoded_user_id}.json'
        blob_client = container_client.get_blob_client(blob=filename)

        # BLOBの存在確認
        if blob_client.exists():
            # BLOBから返却されるのはバイナリデータであるため、デコードしてjsonとして読み込む
            # JSON:
            # {user_id: 'UserID', access_token: 'Access Token'}
            binary_data = blob_client.download_blob().readall()
            decoded_data = binary_data.decode('utf-8')
            json_data = json.loads(decoded_data)
        else:
            logging.info(f'[Info] Blob file is not found. Create a new file.')
            # ファイルが存在しない場合は、空のリストで初期化
            json_data = {}

        # ファイル内に格納されているアクセストークンを更新する
        json_data['user_id'] = user_id
        json_data['access_token'] = access_token
        json_data['refresh_token'] = refresh_token

        # 更新したデータをBLOBにアップロードする
        output_io = io.StringIO()
        json.dump(json_data, output_io)
        json_data_str = output_io.getvalue()
        blob_client.upload_blob(json_data_str, overwrite=True)

        logging.info(
            f'Access token is stored in cache. Cache filename is {filename}.')
        token_updated = True  # トークンの更新が成功した場合はTrueを返す

    except Exception as e:
        logging.info(
            f'[Exception] Failed to store access token. Check the connection string and container name. {e}')
        token_updated = False

    finally:
        return token_updated


def get_access_token_from_cache(user_id: str):
    """Azure Blob Storageにキャッシュされたアクセストークンを取得する関数

    Args:
        user_id (str): ユーザのID

    Returns:
        dict: アクセストークンとリフレッシュトークンを含む辞書。
              辞書の構造は以下のようになります:
              {
                  'access_token': str,
                  'refresh_token': str
              }
              トークンの取得に失敗した場合はNoneを返します。
    """

    try:
        # 返却するアクセストークンはNoneを初期値とする
        access_token = None

        blob_service_client = BlobServiceClient.from_connection_string(
            conn_str=CONNECTION_NAME)
        container_client = blob_service_client.get_container_client(
            CONTAINER_NAME)

        # BLOBよりUPNとアクセストークンのストアファイルをダウンロードする
        # ファイル名は'{user_id}.json'として、ユーザごとに異なったファイルに格納する
        # user_idにはメールアドレスが入るため、安全のためにエンコードする
        encoded_user_id = quote(user_id, safe='')
        filename = f'boxauth/{encoded_user_id}.json'
        blob_client = container_client.get_blob_client(blob=filename)
        binary_data = blob_client.download_blob().readall()

        # BLOBから返却されるのはバイナリデータであるため、デコードしてJSONとして読み込む
        # JSON:
        # {user_id: 'UserID', access_token: 'Access Token'}
        decoded_data = binary_data.decode('utf-8')
        json_data = json.loads(decoded_data)

        # ファイル内に格納されているアクセストークンを取得する
        access_token = json_data['access_token']
        refresh_token = json_data['refresh_token']

        # アクセストークンが取得できなかった場合はNoneを返却する
        if access_token is None:
            logging.info(
                f'Access token is not found in cache. Cache filename is {filename}.')
            return None

        logging.info(
            f'Access token is found in cache. Cache filename is {filename}.')
        return {
            'access_token': access_token,
            'refresh_token': refresh_token
        }

    except Exception as e:
        logging.info(
            f'[Exception] Failed to get access token from cache. Check the connection string and container name.')
        return None


def oauth_token_expired(token: str) -> bool:
    """Box API OAuth認証期限チェック関数
    この関数は、Box API のOAuth認証の有効期限をチェックします。

    Args:
        token (str): Box API のアクセストークン
    Returns:
        bool: Box APIのOAuth認証の有効期限が切れている場合はTrue、有効期限が切れていない場合はFalseを返す。
    """

    # ユーザ情報取得関数を呼び出し、有効期限内か検証する
    url = 'https://api.box.com/2.0/users/me'
    headers = {'Authorization': f'Bearer {token}'}
    response = requests.get(url, headers=headers)

    if response.status_code >= 300:
        return True

    return False


def oauth_token_update(refresh_token: str):
    """
    提供されたリフレッシュトークンを使用してOAuthトークンを更新します。

    Args:
        refresh_token (str): 新しいアクセストークンを取得するために使用されるリフレッシュトークン。

    Returns:
        dict: 新しいアクセストークンとリフレッシュトークンを含む辞書。
              辞書の構造は以下のようになります:
              {
                  'access_token': str,
                  'refresh_token': str
              }
              トークンの更新に失敗した場合はNoneを返します。
    """

    url = 'https://api.box.com/oauth2/token'
    data = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET
    }

    token_response = requests.post(url, data=data)

    if token_response.status_code >= 300:
        return None

    response_json = token_response.json()

    return {
        'access_token': response_json['access_token'],
        'refresh_token': response_json['refresh_token']
    }


def redirect_to_oauth() -> func.HttpResponse:
    """OAuth 認証 URL へのリダイレクトレスポンスを生成する。

    Returns:
        func.HttpResponse: ステータスコード 302 (Found) で、Location ヘッダーに OAuth 認証 URL を設定した HttpResponse。
    """
    auth_url = get_oauth_url()
    logging.info(f'[Redirect to {auth_url}]')

    return func.HttpResponse(
        status_code=302,
        headers={'Location': auth_url}
    )
