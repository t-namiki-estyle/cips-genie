import os
import asyncio
import json
import time
import base64
import random
import logging
import traceback
import urllib.parse
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime, timedelta

import azure.functions as func
import azure.durable_functions as d_func

from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerinstance.models import (
    ContainerExecRequest,
    ContainerExecRequestTerminalSize,
)

import requests

from util import (
    BLOB_CONNECTION_STRING,
    AUDIO_CONTAINER_NAME,
    upload_blob,
    date_time_ite,
)

# env
# WEBSITE_OWNER_NAMEから環境変数を取得
SUBSCRIPTION_ID = os.environ.get("WEBSITE_OWNER_NAME").split("+")[0]
RESOURCE_GROUP_NAME = "-".join(
    os.environ.get("WEBSITE_OWNER_NAME").split("+")[1].split("-")[:-2]
)

ZOOM_ACI_PREFIX = os.environ.get("ZOOM_ACI_PREFIX")
ZOOM_ACI_NUM = int(os.environ.get("ZOOM_ACI_NUM"))
# aci_name = f"{ZOOM_ACI_PREFIX}-{num}""

# 音声の間隔
INTERVAL = 10
LEAD_MARGIN = 5
TRAIL_MARGIN = 3

AUDIO_EVENT_NAME = "audio_input"
ZOOM_ENTITY_NAME = "myZoomEntity"


class ZoomBot:
    """
    zoom bot関連の操作をまとめたclass
    botの状態をinstanceで管理する
    """

    # --- 初期化 ---

    def __init__(self, ip, join_url, access_token) -> None:
        """変数を格納し、botを開始する"""
        logging.info("init bot")
        self.client_id = os.environ.get("ZOOM_CLIENT_ID")
        self.client_secret = os.environ.get("ZOOM_CLIENT_SECRET")
        self.display_name = "I-Colleague"

        self.ip = ip
        self.join_url = join_url
        self.access_token = access_token

        self.time = LEAD_MARGIN  # 最初からマージンをとる
        self.max_index = 0
        self.fail_count = 0
        self.status = self.start()

    # --- クラスメソッド ---
    @classmethod
    def from_dict(cls, data: dict):
        """
        新しいインスタンスを作成し、__init__ を呼び出さずに属性を設定
        classmethodの方が良い？←継承後に更新可能
        """
        bot = cls.__new__(cls)  # __init__ を回避

        # 元のインスタンスの値を格納
        bot.client_id = data.get("client_id", "")
        bot.client_secret = data.get("client_secret", "")
        bot.display_name = data["display_name"]

        bot.ip = data["ip"]
        bot.join_url = data["join_url"]
        bot.access_token = data["access_token"]

        bot.time = data["time"]
        bot.max_index = data["max_index"]
        bot.fail_count = data.get("fail_count", 0)
        bot.status = data.get("status", "error")

        return bot

    # --- 静的メソッド ---
    @staticmethod
    def build_url(base_url: str, params_dict: dict) -> str:
        """
        helper関数
        urlとパラメーターをエンコードしてget用のurlを作成する
        """
        # NOTE: function codes are supplied via environment variables to avoid hard-coding secrets
        if "azu1011syprjpe-aoai-func-98.azurewebsites.net" in base_url:
            mode = base_url.split("/")[-1]
            base_url = (
                f"https://azu1011syprjpe-aoai-func-98.azurewebsites.net/api/bot/{mode}"
            )
            func_code = os.environ.get("ZOOM_FUNC_CODE")
            if func_code:
                params_dict["code"] = func_code
        elif "aci-proxy.azurewebsites.net" in base_url:
            mode = base_url.split("/")[-1]
            base_url = f"https://aci-proxy.azurewebsites.net/api/bot/{mode}"
            func_code = os.environ.get("ACI_PROXY_FUNC_CODE")
            if func_code:
                params_dict["code"] = func_code

        # 空でない値のみをフィルタリング
        filtered_params = {k: v for k, v in params_dict.items() if v}
        # クエリパラメータをエンコード
        query_string = urllib.parse.urlencode(filtered_params)
        # 完全なURLを構築
        return f"{base_url}?{query_string}" if query_string else base_url

    # --- パブリックなインスタンスメソッド ---
    def start(
        self,
    ) -> str:
        """
        botの参加を行う
        ACIへの接続に失敗時は"aci_restart"
        初期化の失敗時には"error"のレスポンスを返す
        init時に bot.statusにレスポンスが格納される
        """

        # init
        base_url = f"http://{self.ip}/end.php"

        # ACIの生存確認、初期化
        if not self.__check_aci_availability(base_url):
            return "aci_restart"

        base_url = f"http://{self.ip}/start.php"
        logging.info(base_url)
        # 一時的にコメントアウト
        params_dict = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "display_name": self.display_name,
            "join_url": self.join_url,
            "access_token": self.access_token,
        }
        return self.__initialize_bot(base_url, params_dict)

    def audio(
        self,
    ) -> bytearray:
        """
        音声の取得を行い、バイナリで返却
        インターバル、前後にマージンを設定している

        # エラーの条件
        ## 開始時
        - 三分間音声が取れなかったら自動的に退出
        ## 途中
        - indexが増えない場合は早めに終了したと判断
            - 一分間待機
        - ファイルの長さが計算と合わない場合のみ何度もリトライ
        """
        base_url = f"http://{self.ip}/resource.php"
        params_dict = self.__construct_params()
        url = self.build_url(base_url, params_dict)

        start_time = params_dict["start"]
        end_time = params_dict["end"]

        file_data, max_index = self.__retrieve_audio_data(url, end_time)

        # 音声が取得できなかった場合
        if not file_data:
            return bytearray()

        # timeの更新
        self.__update_time(max_index)

        # time更新後のstatusの確認
        if self.status == "end":
            return bytearray()

        # 整形してreturn
        return self.__convert_to_binary(file_data)

    def end(
        self,
    ) -> bool:
        """botの退出を行う"""
        logging.info("bot.end() start")
        base_url = f"http://{self.ip}/end.php"
        url = self.build_url(base_url, {})

        max_retries = 5
        counter = 0
        while True:
            if counter > max_retries:
                logging.critical("CANNOT END BOT")
                pass
                return 0
            counter += 1
            try:
                response = requests.get(url)
                res_json = json.loads(response.text)

                if int(res_json["status"]) == 200:
                    logging.info(f"bot_response: {response.text}")
                    self.status = "end"
                    return 200 <= response.status_code < 300
                elif (
                    int(res_json["status"]) == 500
                    and res_json["message"] == "ZOOM BOT is not started."
                ):
                    logging.warning(f"not started, bot_response: {response.text}")
                    self.status = "end"
                    return 200 <= response.status_code < 300
                else:
                    raise
            except Exception as e:
                logging.warning(f"error: {response.status_code}, {response.text}")
                continue

    # --- プライベートメソッド ---
    def __check_aci_availability(self, base_url):
        url = self.build_url(base_url, {})
        for _ in range(3):
            try:
                # timeout
                # https://blog.cosnomi.com/posts/1259/
                response = requests.get(url, timeout=15)
                return True
            except Exception as e:
                pass
        logging.warning(f"cannot access aci {self.ip}")
        return False

    def __initialize_bot(self, base_url, params_dict):
        url = self.build_url(base_url, params_dict)
        message = "error"
        try:
            response = requests.get(url)
            res_json = json.loads(response.text)
            if res_json["status"] == 200:
                message = "run"
            elif res_json["status"] == 201:
                logging.critical(
                    f"bot is already running! bot_response: {res_json['message']}"
                )
            else:
                logging.critical(f"illegal error. bot_response: {res_json['message']}")
        except:
            logging.critical(f"illegal error. bot_response: {response.text}")
        return message

    def __construct_params(self) -> dict:
        start_time = self.time - LEAD_MARGIN
        end_time = self.time + INTERVAL + TRAIL_MARGIN - 1
        logging.info(f"audio start: {start_time}, end: {end_time}")
        return {"start": start_time, "end": end_time, "access_token": self.access_token}

    def __retrieve_audio_data(self, url: str, end_time: int) -> tuple[list, int]:
        self.fail_count = 0
        max_index = self.max_index

        while True:
            pre_max_index = max_index
            response = requests.get(url)
            res_json = json.loads(response.text)

            # コンテナから正常レスポンスの場合
            if int(res_json["status"]) == 200:
                file_data = res_json["file_data"]
                try:
                    max_index = int(res_json["max_index"])
                    logging.debug(
                        f"max_index: {max_index}, file_data: {len(file_data)}"
                    )
                except Exception as e:
                    logging.warning(f"{e}")
                    continue

                # 音声の長さが指定通りの場合
                if len(file_data) == LEAD_MARGIN + INTERVAL + TRAIL_MARGIN:
                    self.fail_count = 0
                    logging.info(f"bot_response: {res_json['message']}")
                    return file_data, max_index
                # 音声の長さが足りない場合（頻出）
                if self.__handle_failure_conditions(pre_max_index, max_index, end_time):
                    return None, max_index

            # 録音が開始できているかの確認
            elif self.__handle_no_resource_file(res_json):
                return None, max_index

            # 想定外のレスポンスの場合
            else:
                logging.warning(f"no audio, {res_json['message']}")
                time.sleep(5)

    def __handle_failure_conditions(self, pre_max_index, max_index, end_time) -> bool:
        """
        音声の取得に失敗した場合に
        - 失敗した回数
        - 失敗の状況
        に応じて処理を分岐する関数
        """
        # 待機時間の設定（60 * 5s）
        max_retries = 60

        # 失敗した回数が上限を超えた場合
        if self.fail_count > max_retries:
            self.status = "end"
            logging.info("bot end: cannot get new audio")
            return True

        # 前回の失敗時から音声が更新されていない場合
        elif pre_max_index == max_index:
            logging.warning(f"retry: {self.fail_count}, reason: stop recording.")
            self.fail_count += 5
            time.sleep(5)
            return False

        # 音声は更新されているが、指定した長さではない場合
        else:
            sleep_time = end_time - max_index if max_index < end_time else 1
            logging.warning(f"retry: {self.fail_count}")
            self.fail_count += 1
            time.sleep(sleep_time)
            return False

    def __handle_no_resource_file(self, res_json) -> bool:
        """
        録音開始までの待機時間を超過しているか確認するための関数
        コンテナから正常レスポンスが返されなかった場合に呼び出される
        """
        # 待機時間の設定（35 * 5s）
        max_retries = 35

        # 録音が開始していない場合
        if (
            int(res_json["status"]) == 500
            and res_json["message"] == "No Resource File."
        ):
            # 待機時間超過
            if self.fail_count > max_retries:
                self.status = "end"
                logging.info("bot end: cannot start recording")
                return True
            # 待機時間内
            logging.warning(f"init retry: {self.fail_count}")
            self.fail_count += 1
            time.sleep(5)
            return False
        return False

    def __update_time(self, max_index):
        self.time += INTERVAL
        # 音声が書き込まれている場合
        if self.max_index != max_index:
            self.max_index = max_index
        # 音声の書き込みを開始していない場合
        elif max_index == 0:
            raise
        # 音声の書き込みが止まって、すでに音声をほぼ取得済みの場合 # おそらく呼ばれない
        elif max_index < self.time + INTERVAL + TRAIL_MARGIN - 1:
            self.status = "end"
            logging.warning("bot end: WARNING!!")

    def __convert_to_binary(self, file_data) -> bytearray:
        if isinstance(file_data, dict):
            file_data = [file_data[k] for k in sorted(file_data, key=int)]

        # ファイルの連結
        binary_data = bytearray()
        for encoded_data in file_data:
            # 空白や改行を削除してクリーンアップ
            clean_data = encoded_data.strip()
            # 16進数データをバイト列に変換して追加
            binary_data.extend(bytes.fromhex(clean_data))
        return binary_data


##
# blueprint
##
zoom_bp = d_func.Blueprint()


@zoom_bp.orchestration_trigger(context_name="context")
def zoom(context: d_func.DurableOrchestrationContext):
    """
    ### def
    zoom bot用のオーケストレーター
    初期化時に受け取ったwhisper用のオーケストレーターに対して定期的に音声を送信する

    ### zoom bot用関数
    - zoom_start
    - zoom_audio
    - zoom_leave

    途中参加用の関数もどこかで作成
    - http triggerで実装

    """
    # インスタンスIDの取得
    instance_id = context.instance_id
    logging.info(f"context id: {instance_id}")

    # 開始時に渡されたデータを取得
    payload = context.get_input()
    # access_token = payload["access_token"]

    # whisperのインスタンスのログ
    sendEventPostUri = payload["sendEventPostUri"]
    terminatePostUri = payload["terminatePostUri"]
    logging.info(f"sendEventPostUri: {sendEventPostUri}")

    # entityの取得
    entityId = d_func.EntityId("zoomEntity", ZOOM_ENTITY_NAME)

    # botの開始 continueかどうかで分岐
    if "bot_dict" not in payload.keys():
        fail_flag = False
        max_retry = 10
        while True:
            if max_retry <= 0:
                fail_flag = True
                break
            max_retry -= 1
            # Instanceのアサイン
            aci_name = yield context.call_entity(entityId, "hire")
            if not context.is_replaying:
                logging.info(f"Assign: {aci_name}")
            if aci_name == "error":
                # debug mode
                debug_ip = os.environ.get("ACI_IP_TEST")
                if debug_ip:
                    logging.warning("Debug mode!!")
                    payload["aci_name"] = aci_name
                    bot_dict = yield context.call_activity("zoom_start", payload)
                    bot = ZoomBot.from_dict(bot_dict)
                    break

                # 数秒待ってリトライ? 作成中
                max_retry -= 1
                deadline = context.current_utc_datetime + timedelta(seconds=3)
                yield context.create_timer(deadline)
                pass

                continue

            payload["aci_name"] = aci_name
            bot_dict = yield context.call_activity("zoom_start", payload)

            if "error" in bot_dict.keys():
                # 失敗した場合
                # Container Instanceをrelease
                context.signal_entity(entityId, "release", aci_name)
                continue

            bot = ZoomBot.from_dict(bot_dict)

            if bot.status == "run":
                break
            else:
                # fail時にContainer Instanceをrelease
                context.signal_entity(entityId, "release", aci_name)
                continue

        if fail_flag:
            # Container Instanceをrelease
            context.signal_entity(entityId, "release", aci_name)

            # whisperを終了
            headers = {
                "Content-Type": "application/json",
                # "x-functions-key":os.environ.get("DURABLE_TASK_KEY")
            }
            yield context.call_http(
                "post",
                terminatePostUri.format(text="cannot_start_zoom_bot"),
                headers=headers,
            )
            return 0

    else:
        aci_name = payload["aci_name"]
        bot_dict = payload["bot_dict"]
        bot = ZoomBot.from_dict(bot_dict)

    # taskの作成
    # botの終了
    terminate_task = context.wait_for_external_event("zoom_leave")

    while True:
        # 現在の時刻の取得
        deadline = context.current_utc_datetime

        # intervalの設定
        next_req = bot.time + INTERVAL + TRAIL_MARGIN - 1
        interval = next_req - bot.max_index if next_req > bot.max_index else 1
        if not context.is_replaying:
            logging.debug(f"interval: {interval}")

        # timerの作成・更新
        deadline += timedelta(seconds=interval)
        timer_task = context.create_timer(deadline)

        # taskの呼び出し
        tasks = [timer_task, terminate_task]
        winner = yield context.task_any(tasks)
        if not context.is_replaying:
            logging.debug(f"{winner}")

        if winner == timer_task:
            # audioの取得
            bot_dict = bot.__dict__
            input_dict = {
                "bot": bot_dict,
                "uri": payload["sendEventPostUri"],
                "blob_prefix": payload["blob_prefix"],
            }
            # audioを取得し、blobにアップロード
            bot_dict = yield context.call_activity("zoom_audio", input_dict)
            bot = ZoomBot.from_dict(bot_dict)

            # HealthCheck
            if bot.status != "run":
                break

        elif winner == terminate_task:
            if not context.is_replaying:
                logging.info(
                    f"terminate task. reason: zoom_leave posted from UI. {terminate_task.result}"
                )
            break

        # continue as new
        counter = int((bot.time - LEAD_MARGIN) / INTERVAL)
        if not context.is_replaying:
            logging.info(f"counter: {counter}")
        if counter > 0 and counter % 10 == 0:
            payload["bot_dict"] = bot.__dict__
            context.continue_as_new(input_=payload)
            return 0

    # 終了時の処理
    logging.info("complete task")

    # entityの更新
    context.signal_entity(entityId, "release", aci_name)

    # 退出の指示
    bot_dict = bot.__dict__
    _ = yield context.call_activity("zoom_leave", bot_dict)
    # bot = ZoomBot.from_dict(bot_dict)

    # timer taskを終了させる # ここで止まってしまっていることがある？？
    if not timer_task.is_completed:
        # All pending timers must be complete or canceled before the function exits.
        timer_task.cancel()  # type: ignore
    return 0


@zoom_bp.activity_trigger(input_name="input")
def zoom_start(input: dict) -> dict:
    """
    新しくzoom botを参加させるための関数
    同じurlが渡されても新しく開始する

    timeoutを追加する？
    """
    logging.info("zoom_start processed a request.")
    ########
    # init #
    ########
    req_json = input
    url = req_json["url"]
    access_token = req_json["access_token"]
    logging.info("zoomUrl: " + url)

    aci_name = req_json["aci_name"]

    # debug
    debug_ip = os.environ.get("ACI_IP_TEST")
    if debug_ip:
        ip = debug_ip
        logging.warning(f"ip debug mode: {ip}")
        bot = ZoomBot(ip=ip, join_url=url, access_token=access_token)
        return bot.__dict__

    # container instanceの名前からipを取得
    try:
        # 認証情報の設定
        credential = DefaultAzureCredential()

        # コンテナグループの情報の取得
        aci_client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)
        container_group = aci_client.container_groups.get(RESOURCE_GROUP_NAME, aci_name)

        # ipの確認
        ip = container_group.ip_address.ip
        logging.info(f"ACI_IP: {ip}")

        # 個々のコンテナのstatusの確認
        for container in container_group.containers:
            logging.info(
                f"{container.name}: {container.instance_view.current_state.state}"
            )
            # containerが立ち上げ済みかの確認
            assert container.instance_view.current_state.state == "Running"

    except Exception as e:
        tb = traceback.format_exc()
        logging.critical(f"ERROR ACI: {e}, traceback: {tb}")

        # 失敗を返す
        return {"error": ""}

    # containerにアクセスしてurlの受け渡し
    bot = ZoomBot(ip=ip, join_url=url, access_token=access_token)

    if bot.status == "aci_restart":
        # コンテナグループのリブート
        aci_client.container_groups.begin_restart(RESOURCE_GROUP_NAME, aci_name)
        logging.info(f"restart: {aci_name}")
        bot.status = "error"
    return bot.__dict__


@zoom_bp.activity_trigger(input_name="input")
def zoom_audio(input: dict) -> dict:
    """
    音声を取得してblobに格納、blobの名前を送ってeventを起こす
    コンテナの生存確認を追記中

    blob:
    container: audio-data
    命名規則:

    # エラーの条件
    ## 開始時
    - 三分間音声が取れなかったら自動的に退出
    ## 途中
    - indexが増えない場合は早めに終了したと判断
    - ファイルの長さが計算と合わない場合のみ何度もリトライ
    """
    # bot
    bot_dict = input["bot"]
    bot = ZoomBot.from_dict(bot_dict)
    pre_max_index = bot.max_index

    # audio_event
    counter = int((bot.time - LEAD_MARGIN) / INTERVAL)
    event_name = f"{AUDIO_EVENT_NAME}_{counter}"
    event_name = AUDIO_EVENT_NAME
    logging.info(f"event_name: {event_name}")

    # uri
    eventPostUri = input["uri"].format(eventName=event_name)
    logging.info(f"uri: {eventPostUri}")

    # blob_prefix
    blob_prefix = input["blob_prefix"]

    # blob_names: list= input["blob_names"]

    try:
        binary_audio = bot.audio()  # .encode('utf-8')  # 文字列をバイト列に変換

        # audio debug
        logging.debug(f"pre: {pre_max_index}, index: {bot.max_index}")

        # Healthcheck
        if bot.status == "end":
            return bot.__dict__

        if bot.max_index == pre_max_index:
            bot.status == "end"
            logging.info("bot end: same index")

        # blobに格納
        # if bot.time - INTERVAL - LEAD_MARGIN > 0 else 0
        start_time = bot.time - INTERVAL - LEAD_MARGIN
        end_time = bot.time + TRAIL_MARGIN
        blob_name = f"{blob_prefix}/{bot.access_token}_{start_time}_{end_time}.pcm"

        blob_service_client = BlobServiceClient.from_connection_string(
            conn_str=BLOB_CONNECTION_STRING
        )
        try:
            # コンテナが存在しない場合は作成, 初回のみ
            if start_time == 0:
                try:
                    # コンテナに接続
                    container_client = blob_service_client.get_container_client(
                        container=AUDIO_CONTAINER_NAME
                    )
                    container_client.create_container()
                except Exception as e:
                    logging.debug(
                        f"Container already exists or error creating container: {e}"
                    )

            # Blobクライアントを取得
            blob_client = blob_service_client.get_blob_client(
                container=AUDIO_CONTAINER_NAME, blob=blob_name
            )

            # ファイルをアップロード
            if blob_client.exists():
                # 例外
                logging.critical("repeating!!")
            else:
                blob_client.upload_blob(binary_audio, overwrite=False)
                logging.info(f"'{blob_name}' uploaded to blob storage")

        except Exception as e:
            logging.critical(f"Error uploading file: {e}")

    except Exception as e:
        # tracebackを使用してエラーの詳細情報を取得
        tb = traceback.format_exc()
        logging.critical(f"Error in zoom_audio: {e}, traceback: {tb}")

    return bot.__dict__


@zoom_bp.activity_trigger(input_name="input")
def zoom_leave(input: dict) -> dict:
    logging.info("zoom_leave processed a request.")

    bot_dict = input
    bot = ZoomBot.from_dict(bot_dict)
    bot.end()

    return bot.__dict__


# 状態管理
@zoom_bp.entity_trigger(context_name="context")
def zoomEntity(context: d_func.DurableEntityContext):
    """
    Zoom bot用のContainer Instanceの一覧を管理するためのエンティティ
    dict形式で管理を行う

    逐次実行ができるため、非同期の問題についてはあまり考えずにできそう
    どのurlがアサインされているかは管理する必要はないはず

    ## operation
    ### init
    - Container Instanceの一覧を登録し、statusをidleに登録する
    - 一日一回、夜間に定期実行する
    ### get
    - 利用可能なContainer Instanceの一覧を返す # 使わないでよさそう
        - アサイン中のものもまとめて返す←なくてもよい？
    ### hire
    - zoom botへのアサインを行うための操作
        - idle状態の一覧からランダムに一つのContainer Instanceを選択
        - 選択したContainer Instanceの状態をassignedに更新
        - 選択したContainer Instanceの名前を返す
    ### release
    - bot終了時にContainer Instanceのアサインを解除するための操作
        - Container Instanceの名前を受け取り、その状態をidleに更新
        - Container Instanceの初期化を挟むか要検討
    """
    try:
        state_dict = context.get_state(lambda: {})  # デフォルトの状態設定
        operation = context.operation_name
        logging.info(f"entity, operation: {operation}, state_dict: {state_dict}")

        # test
        if state_dict is None:
            logging.critical("Error in initialize state_dict")
            state_dict = {}

        if operation == "init":  # 初期化の場合
            state_dict = {}
            # ACIの命名ルール変更には注意！
            for i in range(ZOOM_ACI_NUM):
                num = i + 1
                aci_name = f"{ZOOM_ACI_PREFIX}-{num}"
                state_dict[aci_name] = "idle"
            logging.info(f"initialize state_dict")
        elif operation == "get":
            # 読み込みの場合状態をそのまま返す
            context.set_result(state_dict)
        elif operation == "hire":
            # idle状態の一覧を取得
            available = [
                name for name, status in state_dict.items() if status == "idle"
            ]
            if available:
                # random choice
                aci_name = random.choice(available)
                state_dict[aci_name] = "assigned"
            else:
                logging.critical("no idle Instance")
                aci_name = "error"
            # return
            logging.info(f"aci_name: {aci_name}")
            context.set_result(aci_name)
        elif operation == "release":
            # インスタンスをリリースする
            aci_name = context.get_input()
            if aci_name != "error":
                state_dict[aci_name] = "idle"

        # log
        logging.info(f"state_dict: {state_dict}")
        # 更新された状態を保存
        context.set_state(state_dict)
    except Exception as e:
        tb = traceback.format_exc()
        logging.error(f"{e}, traceback: {tb}")


# https://learn.microsoft.com/ja-jp/azure/azure-functions/functions-bindings-timer?tabs=python-v2%2Cisolated-process%2Cnodejs-v4&pivots=programming-language-python#ncrontab-expressions
@zoom_bp.timer_trigger(
    # 毎日AM3時に定期実行, {second} {minute} {hour} {day} {month} {day-of-week}
    schedule="0 0 18 * * *",
    arg_name="mytimer",
    run_on_startup=False,
)
@zoom_bp.durable_client_input(client_name="client")
async def zoom_container_manager(mytimer: func.TimerRequest, client) -> None:
    """
    毎日深夜に一度のみ実行
    - entityが存在しない場合は作成、すでに存在する場合は初期化を行う
        - code上では意識しないでよさそう
    - コンテナの一覧を取得し、状態をidleにしてentityに登録する

    コード更新時に呼び出す必要あり？
    コンテナインスタンスの再起動も行う？
    """
    entityId = d_func.EntityId("zoomEntity", ZOOM_ENTITY_NAME)
    await client.signal_entity(entityId, "init")
    logging.info("Send init ZoomEntity signal")

    # restart
    # コンテナグループの情報の取得
    credential = DefaultAzureCredential()
    aci_client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)
    # ACIの命名ルール変更には注意！
    for i in range(ZOOM_ACI_NUM):
        num = i + 1
        aci_name = f"{ZOOM_ACI_PREFIX}-{num}"
        try:
            aci_client.container_groups.begin_restart(RESOURCE_GROUP_NAME, aci_name)
        except Exception as e:
            logging.critical(f"error in restart {aci_name}: {e}")
        logging.debug(f"restart: {aci_name}")


# test
if __name__ == "__main__":
    ip = "localhost"
    url = "https://zoom.us/*/pwd=***"
    access_token = "<durable_client_id>"

    bot = ZoomBot(ip, url, access_token)
    print(bot.__dict__)

    print("----")

    new_bot = ZoomBot.from_dict(bot.__dict__)
    print(new_bot.__dict__)
