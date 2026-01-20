import azure.functions as func
import azure.durable_functions as d_func
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import io
import traceback
from styleframe import StyleFrame

import ast
import csv
import asyncio

# OSS
import pandas as pd

# 自作
from config import VARIABLE_LIST, ENVIRONMENT_SELECTED
from util import *
from utils.log.legacy import query_log, create_excel_file, get_unique_upn
from utils.log.kql import kql

from i_style.aiohttp import AsyncHttpClient, http_post

MERCHANT_ENTITY_NAME = "myMerchantEntity"


##
# blueprint
##
log_bp = d_func.Blueprint()


@log_bp.timer_trigger(schedule="0 0 20 * * *", arg_name="logweeklytimer", run_on_startup=False)
async def logAnalytics(logweeklytimer: func.TimerRequest) -> None:
    """
    log取得の確認用API
    timer triggerに書き換え予定

    日付の操作
    ```
    # 現在の時間を取得
    current_time = datetime.now(jst)

    # n日前を指定
    n = 10  # 例: 10日前
    n_days_ago = current_time - timedelta(days=n)

    # 特定の時間を指定（例：2023年6月1日 10:00:00）
    specific_time = datetime(2023, 6, 1, 10, 0, 0, tzinfo=jst)

    毎日日本時間午前5時に前日のログを取得し、Blobに格納し、メール送信を行う
    ```
    """
    logging.info('logAnalytics processed a request.')
    # get json input
    # req_json = req.get_json()

    # タイムゾーンの設定
    jst = ZoneInfo('Asia/Tokyo')

    # 本日の0:00を指定
    end_time = datetime.now(jst).replace(
        hour=0, minute=0, second=0, microsecond=0)

    # 1日前を指定
    start_time = end_time - timedelta(days=1)
    log_file_name = "I-Colleague利用ログ"+str(start_time.date())+".csv"  # ログのファイル名
    # KQLでログを取得
    logging.info("starting query_log")
    try:
        log_df = query_log(kql, start_time, end_time)  # KQLでログを取得
        logging.info("finished query_log")
        log_df = log_df.rename(columns={  # カラム名を変更
            'TimeGenerated': '実行日時',
            'upn': 'UPN',
            'mode': '使った機能',
            'user_input': 'プロンプトの内容'})
        # ログをBlobにアップロードする
        logging.info("uploading to blob")
        csv_buffer = io.StringIO()
        log_df.to_csv(csv_buffer, index=False)
        csv_buffer.seek(0)
        log_uploaded_flag = upload_blob(
            file_name=log_file_name,
            file_content=csv_buffer.getvalue(),
            container_name="weeklylog",  # コンテナがない場合は、コンテナを自動作成
            overwrite=True
        )
        if log_uploaded_flag:
            logging.info("uploaded log csv to blob")
        else:
            logging.critical("unsuccessful uploading log csv to blob")
    except Exception as e:
        logging.critical(f"ログ取得に失敗しました: {e}")
        return

    csv_buffer.seek(0)
    logging.info("sending email with log attached")
    try:  # メール送信
        attachment_list = [
            {"file_content": csv_buffer.getvalue().encode("utf-8"),  # ログの内容
             "file_name": log_file_name,  # ログのファイル名
             "maintype": "text",
             "subtype": "csv"
             }]
        # メールを送信
        logging.info('send email')
        email_success = send_email(
            subject="【I-Colleague】ログデータ",
            content="I-Colleagueのログを送信いたします。",
            send_to=[VARIABLE_LIST[ENVIRONMENT_SELECTED]["log_mail_address"]],
            attachment_list=attachment_list
        )
        if email_success:
            logging.info("sent log email")
        else:
            logging.critical("unsuccessful sending log email")
    except Exception as e:
        logging.critical(f"unsuccessful sending log email {e}")
    return None


@log_bp.timer_trigger(schedule="0 */10 * * * *", arg_name="mytimer", run_on_startup=False)
async def update_merchant_rate(mytimer: func.TimerRequest) -> None:
    """
    timerで定期的に更新
    """
    logging.info(f"update_merchant_rate: Called")
    global MERCHANT_RATE

    # 更新の時刻の取得 # 現在の時刻が入ってしまう
    current_schedule_str = mytimer.schedule_status["Next"]
    current_schedule = datetime.fromisoformat(current_schedule_str)

    # 10分追加
    next_schedule = current_schedule + timedelta(minutes=10)
    next_schedule_str = next_schedule.isoformat()

    # merchant_rateの更新
    MERCHANT_RATE = await access_merchant_rate("update", next_schedule_str)
    logging.info(f"updated merchant_rate: {MERCHANT_RATE}")


async def access_merchant_rate(operation: str = "get", date: str = "") -> dict:
    """
    オーケストレーターを起動してentityから情報を更新/取得する
    """
    # requestの準備
    base_url = os.environ.get("DURABLE_URL")
    api_key = os.environ.get("DURABLE_API_KEY")

    # オーケストレータの開始
    instance_info = await http_post(
        json_data={
            "operation": operation,
            "next_update": date
        },
        url=base_url.format(functionName="get_merchant_state_dict"),
        api_key=api_key,
        process_name="get_merchant_state_dict"
    )
    statusUri = instance_info["statusQueryGetUri"]

    # ポーリング処理
    state_dict = {}
    client = AsyncHttpClient()

    interval = 1 if operation == "get" else 5
    await asyncio.sleep(0.5)
    for _ in range(60):
        status = await client.get(
            url=statusUri,
            params={},
            process_name="polling merchant rate"
        )
        # runtime statusで確認
        if status.get("output") is not None:
            state_dict = status["output"]
            break

        await asyncio.sleep(interval)

    # 必要な情報の抽出
    return state_dict


@log_bp.orchestration_trigger(context_name="context")
def get_merchant_state_dict(context: d_func.DurableOrchestrationContext):
    """
    operationとnext_updateを受け取る
    """
    # 開始時に渡されたデータを取得
    payload = context.get_input()
    operation = payload.get("operation", "get")
    date = payload.get("next_update", "0001-01-01T00:00:00")

    # entityの呼び出し
    entityId = d_func.EntityId("merchantEntity", MERCHANT_ENTITY_NAME)
    state_dict = yield context.call_entity(entityId, operation, date)
    return state_dict

# 状態管理


@log_bp.entity_trigger(context_name="context")
def merchantEntity(context: d_func.DurableEntityContext):
    """
    商人数の管理用のエンティティ
    dict形式で管理を行う
    ```
    {
        "daily_user": 10,
        "next_update": "0001-01-01T00:00:00",
    }
    ```

    逐次実行ができるため、非同期の問題についてはあまり考えずにできそう
    どのurlがアサインされているかは管理する必要はないはず

    ## operation
    ### update
    - Container Instanceの一覧を登録し、statusをidleに登録する
    - `update_merchant_rate`から10分毎に呼び出され定期実行される
    ### get
    - 商人数を返す
    - オーケストレーターからしか呼び出せないとのこと
        - top用のAPIの呼び出し時に呼ぶ想定

    """
    try:
        state_dict = context.get_state(lambda: {})  # デフォルトの状態設定
        operation = context.operation_name
        logging.debug(
            f"merchant_entity, operation: {operation}, state_dict: {state_dict}")

        # init
        if state_dict == {}:
            logging.info("Initialize state_dict")
            state_dict = {
                "daily_user": 0,
                "next_update": "0001-01-01T00:00:00"
            }

        # 更新
        if operation == "update":
            state_dict = {
                "daily_user": get_unique_upn(),
                "next_update": context.get_input()
            }
            logging.info(f"update merchant_state_dict: {state_dict}")
            context.set_result(state_dict)

        # 取得
        elif operation == "get":
            # 読み込みの場合状態をそのまま返す
            context.set_result(state_dict)

        # 更新された状態を保存
        logging.debug(f"state_dict: {state_dict}")
        context.set_state(state_dict)

    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"{e}, traceback: {tb}")
