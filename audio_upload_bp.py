import azure.functions as func
import base64
import json
import logging
from container_instance_util import exec_whisper_process, save_audio_to_blob, get_terminated_container_groups, list_blob_names, get_container_state, stop_container, delete_blob, save_to_blob, start_container, update_blob_metadata, blob_exists, list_container_names, list_blob_segments, get_running_container_groups, get_container_log
import uuid
import os
from azure.storage.blob import BlobServiceClient, BlobClient
from config import VARIABLE_LIST, ENVIRONMENT_SELECTED
from util import send_email
import random
import datetime
from dateutil import tz
import time
from collections import defaultdict
from zoneinfo import ZoneInfo
import gzip

from i_style.aiohttp import AsyncHttpClient, http_post

LLM_MINUTES_API_KEY = os.environ.get("LLM_MINUTES_API_KEY")
LLM_MINUTES_URL = os.environ.get("LLM_MINUTES_URL")

AZURE_STORAGE_CONNECTION_STRING = os.environ.get("BLOB_CONNECTION_STRING")
STORAGE_CONTAINER_NAME = "audio-data"

# BLOBトリガー用のBLOBプリフィックス
BLOB_PREFIX_AUDIO_MONITOR = "audio_upload/audio_for_monitor"
BLOB_PREFIX_TEXT_TRIGGER = "audio_upload/text_for_trigger"

# データ一時保存用のBLOBプリフィックス
BLOB_PREFIX_AUDIO_TMP = "audio_upload/audio_tmp"
BLOB_PREFIX_TEXT_TMP = "audio_upload/text_tmp"

BLOB_PREFIX_TEXT_SEGMENTS = "audio_upload/text_segments"

allowed_suffixes = [".mp3", ".wav", ".m4a", ".wave"]
max_retry = 10
max_containers = 40


bp = func.Blueprint()


# input
# data = {
#     "upn": "ユーザーを識別するID",
#     "mail": "メールアドレス"
#     "inputs": {
#         "audio": ["<Base64エンコードされた音声データ>"],
#         "language": [], # optional 入力される言語の指定 通常は空で 固定する場合は指定 必要なし
#         "suffix":[".mp3"] # optional 音声ファイルの形式,
#         "file_name":["アップロードされたファイル名"]
#     }
# }


# HTTPトリガーで音声ファイルをBLOBにアップロード
@bp.function_name(name="au_audio_upload")
@bp.route(route="genie/au_audio_upload", methods=("POST",))
def au_audio_upload(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('au_audio_upload: process started')

    try:
        # リクエストボディをJSONとして解析
        # Content-Encodingヘッダーの確認
        content_encoding = req.headers.get('Content-Encoding', '').lower()
        is_gzipped = 'gzip' in content_encoding

        if is_gzipped:
            # gzipデータの読み込みと解凍
            compressed_data = req.get_body()
            decompressed_data = gzip.decompress(compressed_data)
            req_json = json.loads(decompressed_data.decode('utf-8'))
        else:
            # 通常のJSONとして解析
            req_json = req.get_json()

        # バイナリ音声データを取得
        encoded_audio = req_json["inputs"]["audio"][0]
        binary_audio = base64.b64decode(encoded_audio)

        # その他情報
        upn = req_json["upn"]
        mail = req_json["mail"]
        file_name = req_json["inputs"]["file_name"][0]
        try:
            sendFrom = req_json["from"]
            assert sendFrom in ["web", "teams", "mail", "agent"]
        except Exception as e:
            req_json["from"] = sendFrom = "web"

        logging.info(
            f"au_audio_upload: User info, upn:{upn}, mail:{mail}, file_name:{file_name}")

        # データサイズの確認
        data_size_limit = VARIABLE_LIST[ENVIRONMENT_SELECTED]["data_size"]
        size_in_megabytes = len(binary_audio) / (1024 * 1024)
        if size_in_megabytes > data_size_limit:
            response = {
                "status": 500,
                "error": f"音声データのサイズが大きすぎます。アップロードするデータサイズを{data_size_limit}MB以下にしてください。"
            }
            logging.info(
                f"au_audio_upload: The uploaded audio data exceeds {data_size_limit}MB in size.")
            return func.HttpResponse(json.dumps(response), mimetype="application/json")

    except Exception as e:
        logging.critical(
            f"au_audio_upload: The data format of the request is invalid.{e}")
        response = {
            "status": 500,
            "error": "音声データの読み込みに失敗しました。音声データが破損している可能性があります。"
        }
        return func.HttpResponse(json.dumps(response), mimetype="application/json")

    # ファイル名からファイル拡張子取得
    suffix = "." + req_json["inputs"]["file_name"][0].split(".")[-1]
    suffix = suffix.lower()

    if suffix not in allowed_suffixes:
        response = {
            "status": 500,
            "error": "対応していないファイル拡張子です。'mp3', 'wav', 'm4a'形式にファイルを変換してください。"
        }
        return func.HttpResponse(json.dumps(response), mimetype="application/json")

    # BLOBに付与するメタデータ
    metadata = {
        "upn": upn,
        "mail": mail,
        "file_name": base64.b64encode(file_name.encode("utf-8")).decode("utf-8"),
        "suffix": suffix,
        "from": sendFrom
    }

    # 起動可能なコンテナが存在するか確認
    terminated_container_groups = get_terminated_container_groups(
        name_prefix="whisper")

    # 現在処理中の音声データBLOB数
    existing_audio_blob_names = list_blob_names(
        storage_container_name=STORAGE_CONTAINER_NAME,
        prefix=f"{BLOB_PREFIX_AUDIO_MONITOR}/"
    )

    # 起動可能なコンテナが存在しない or コンテナ数に余裕がない場合
    if (len(terminated_container_groups) == 0) | (len(existing_audio_blob_names) > max_containers * 0.8):
        logging.critical("au_audio_upload: No available containers.")
        response = {
            "status": 500,
            "error": "現在アクセスが集中しています。時間をおいてお試しください。"
        }
        return func.HttpResponse(json.dumps(response), mimetype="application/json")

    # 音声データをBLOBに保存, BLOB名取得
    try:
        # 一意のIDを生成 (コンテナー名, ファイル名に使用)
        unique_id = uuid.uuid4()
        file_name = f"{unique_id}{suffix}"

        audio_blob_name_for_monitor = save_audio_to_blob(
            binary_audio=binary_audio,
            storage_container_name=STORAGE_CONTAINER_NAME,
            file_name=file_name,
            metadata=metadata
        )
        logging.info(
            f'au_audio_upload: Audio file uploaded to blob {audio_blob_name_for_monitor}.')

    except Exception as e:
        logging.critical(f"au_audio_upload: Failed to save the audio. {e}")
        response = {
            "status": 500,
            "error": "音声ファイルのアップロードに失敗しました。時間をおいてお試しください。"
        }
        return func.HttpResponse(json.dumps(response), mimetype="application/json")

    # コンテナリストをシャッフル (ターゲットコンテナの重複を回避する)
    random.shuffle(terminated_container_groups)

    # Whisperコンテナの起動 & 処理実行
    try:
        for container_groups_name in terminated_container_groups:
            response = start_container(container_name=container_groups_name)
            if response.status() == "InProgress":
                # モニタリング用音声BLOBのメタデータを更新
                new_metadata = {
                    "status": "Container Started",
                    "num_retry": "0",
                    "container_group": container_groups_name
                }
                update_blob_metadata(
                    storage_container_name=STORAGE_CONTAINER_NAME,
                    blob_name=audio_blob_name_for_monitor,
                    new_metadata=new_metadata
                )
                logging.info(
                    f"au_audio_upload: Container {container_groups_name} Started for processing {file_name}.")
                break
            else:
                # 文字起こしに失敗した旨をメール送信する処理 # To Do
                raise Exception(
                    f"No available containers. Container status:{response.status()}")

    except Exception as e:
        logging.critical(f"au_audio_upload: {e}")
        response = {
            "status": 500,
            "error": "文字起こし処理の開始に失敗しました。時間をおいてお試しください。"
        }

        # アップロードしたBLOBを削除
        delete_blob(
            storage_container_name=STORAGE_CONTAINER_NAME,
            blob_name=audio_blob_name_for_monitor
        )
        return func.HttpResponse(json.dumps(response), mimetype="application/json")

    response = {
        "status": 200,
        "message": "文字起こし処理を開始しました。約30-60分後に結果をメールで送信します。処理完了までしばらくお待ちください。"
    }

    return func.HttpResponse(json.dumps(response), mimetype="application/json")


# BLOBトリガーでテキストファイルの議事録作成
@bp.function_name(name="au_summarize_and_send_mail")
@bp.blob_trigger(
    arg_name="blob",
    path=f"{STORAGE_CONTAINER_NAME}/{BLOB_PREFIX_TEXT_TRIGGER}/{{name}}",
    connection="BLOB_CONNECTION_STRING"
)
async def au_summarize_and_send_mail(blob: func.InputStream):

    logging.info(f"au_summarize_and_send_mail　： Start.")

    # 非同期http通信クライアントのインスタンス化
    async_http_client = AsyncHttpClient()

    # apiの呼び出し
    history_base_url = os.environ.get("HISTORY_API_URL")
    history_api_key = os.environ.get("HISTORY_API_KEY")

    # コンテナ名を除いたBLOB名を取得
    text_file_blob_name = "/".join(blob.name.split("/")[1:])

    blob_service_client = BlobServiceClient.from_connection_string(
        AZURE_STORAGE_CONNECTION_STRING)
    blob_client_text = blob_service_client.get_blob_client(
        container=STORAGE_CONTAINER_NAME,
        blob=text_file_blob_name
    )

    # 文字起こしテキストの取得
    blob_data = blob_client_text.download_blob()
    transcribed_text = blob_data.content_as_text()

    # メタデータの取得
    blob_properties = blob_client_text.get_blob_properties()
    metadata = blob_properties.metadata

    logging.info(f'au_summarize_and_send_mail:　text:{transcribed_text}')
    logging.info(f'au_summarize_and_send_mail:　metadata:{metadata}')

    # 一時保管用のBLOBをチェックして処理済みか否かを判断 (重複して議事録作成, メール送信を防ぐ)
    text_blob_name_tmp = text_file_blob_name.replace(
        BLOB_PREFIX_TEXT_TRIGGER, BLOB_PREFIX_TEXT_TMP)
    blob_exists_flag = blob_exists(
        storage_container_name=STORAGE_CONTAINER_NAME,
        blob_name=text_blob_name_tmp
    )
    if blob_exists_flag:
        logging.warning(
            f"au_summarize_and_send_mail: Transcription data {text_blob_name_tmp} already exists in the temporary storage.")

        # トリガー用BLOBを削除
        delete_blob(
            storage_container_name=STORAGE_CONTAINER_NAME,
            blob_name=text_file_blob_name
        )
        return

    # 日本時間のタイムゾーンを取得
    japan_tz = ZoneInfo('Asia/Tokyo')

    # 現在の日本時間を取得
    japan_time = datetime.datetime.now(japan_tz)

    # 日付を文字列としてフォーマット
    date_string = japan_time.strftime('%Y-%m-%d')

    # テキストファイルの議事録作成
    try:
        # mailを設定すると自動で送信されてしまうので注意！
        req_json = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": transcribed_text
                        }
                    ]
                }
            ]
        }
        api_response = await http_post(json_data=req_json, url=LLM_MINUTES_URL, api_key=LLM_MINUTES_API_KEY, process_name="LLM_MINUTES")
        minutes_text = api_response["choices"][0]["message"]["content"]
        logging.info(f'minutes of the meeting: {minutes_text}')
        logging.info('The minutes of the meeting have been completed.')

        subject = "【I-Colleague】文字起こし結果と議事録を送付いたします。"
        attachment_list = [
            {
                "file_name": f"minutes_{date_string}.txt",
                "file_content": minutes_text.encode("utf-8")
            },
            {
                "file_name": f"transcribe_{date_string}.txt",
                "file_content": transcribed_text.encode("utf-8")
            }
        ]

    except Exception as e:
        logging.critical(
            f"au_summarize_and_send_mail　：Error occurred while summarizing text: {e}")

        subject = "【I-Colleague】文字起こし結果を送付いたします。"
        attachment_list = [
            {
                "file_name": f"transcribe_{date_string}.txt",
                "file_content": transcribed_text.encode("utf-8")
            }
        ]

    finally:
        mail = metadata.get("mail")
        file_name = metadata.get("file_name")
        file_name = base64.b64decode(file_name.encode("utf-8")).decode("utf-8")
        if mail:
            # メール送信
            try:
                flag = send_email(
                    subject=subject,
                    content=f"文字起こしが完了しました。添付ファイルをご確認ください。\nこのメールに添付ファイル付きで返信していただくと、内容についてメールにて回答できます。（例：「添付ファイルの内容を要点にまとめて」）\n音声ファイル名:{file_name}",
                    attachment_list=attachment_list,
                    send_to=[mail]
                )
                logging.info(
                    f"au_summarize_and_send_mail: Send an email to {mail}. File name: {file_name}")
            except Exception as e:
                logging.critical(
                    f"au_summarize_and_send_mail: Failed to send an email to {mail}. File name: {file_name}, {e}")
        else:
            logging.critical(
                f"au_summarize_and_send_mail: There is no email address in the metadata. File name: {file_name}")

        # 履歴の登録
        upn = metadata.get("upn")
        sendFrom = metadata.get("from")

        session_id = str(uuid.uuid4())
        logging.info(f"session_id: {session_id}")

        # 文字起こし部分
        request_data = {
            "items": [
                {
                    "upn": upn,
                    "content": [
                        {"type": "file", "name": file_name}
                    ],
                    "role": "user",
                    "submode": "file",
                    "model": "whisper",
                    "from": sendFrom,
                    "sessionId": session_id
                },
                {
                    "upn": upn,
                    "content": [
                        {"type": "text", "text": transcribed_text}
                    ],
                    "role": "assistant",
                    "submode": "file",
                    "model": "whisper",
                    "from": sendFrom,
                    "sessionId": session_id
                }
            ]
        }

        # 議事録作成に成功しているか確認
        if "minutes_text" in locals():
            request_data["items"].append(
                {
                    "upn": upn,
                    "content": [
                        {"type": "text", "text": minutes_text}
                    ],
                    "role": "assistant",
                    "submode": "",
                    "model": "o4-mini",
                    "from": sendFrom,
                    "sessionId": session_id
                }
            )

        # history_base_url: https://itc-history-functions.azurewebsites.net
        url = f"{history_base_url}/api/history/minutes"

        api_name = "add_history"
        try:
            response = await async_http_client.post(url=url, api_key=history_api_key, json_data=request_data, process_name=api_name)
        except Exception as e:
            logging.warning(f"履歴の追加: {e}")

    # 音声データの一時保存 & モニタリング用音声データの削除
    is_segment = int(metadata.get("is_segment", 0))

    # 分割されていない場合
    if is_segment != 1:
        try:
            audio_blob_name_for_monitor = metadata.get(
                "audio_blob_name_for_monitor")
            blob_client_audio = blob_service_client.get_blob_client(
                container=STORAGE_CONTAINER_NAME,
                blob=audio_blob_name_for_monitor
            )
            blob_properties = blob_client_audio.get_blob_properties()
            metadata = blob_properties.metadata
            binary_audio = blob_client_audio.download_blob().readall()

            # 一時保存用のBLOBパス
            audio_blob_name_tmp = audio_blob_name_for_monitor.replace(
                BLOB_PREFIX_AUDIO_MONITOR, BLOB_PREFIX_AUDIO_TMP)

            # 音声データの一時保存
            save_to_blob(
                data=binary_audio,
                storage_container_name=STORAGE_CONTAINER_NAME,
                blob_name=audio_blob_name_tmp,
                metadata=metadata
            )

            # モニタリング用音声データの削除
            blob_client_audio.delete_blob()
            logging.info(
                f"Blob '{blob_client_audio.blob_name}' was deleted successfully.")

        except Exception as e:
            logging.critical(
                f"au_summarize_and_send_mail: Failed to read audio data. {e}")

    # テキストデータの一時保存
    try:
        # 一時保存用のBLOBパス
        text_blob_name_tmp = text_file_blob_name.replace(
            BLOB_PREFIX_TEXT_TRIGGER, BLOB_PREFIX_TEXT_TMP)
        minutes_blob_name_tmp = text_blob_name_tmp.replace(
            ".txt", "") + "_minutes" + ".txt"

        # テキストデータ
        save_to_blob(
            data=transcribed_text,
            storage_container_name=STORAGE_CONTAINER_NAME,
            blob_name=text_blob_name_tmp,
            metadata=metadata
        )
        logging.info(
            f"Transcribed text has been temporarily saved to {text_blob_name_tmp}.")

        # 議事録データ
        save_to_blob(
            data=minutes_text,
            storage_container_name=STORAGE_CONTAINER_NAME,
            blob_name=minutes_blob_name_tmp,
            metadata=metadata
        )
        logging.info(
            f"Minutes has been temporarily saved to {minutes_blob_name_tmp}.")

    except Exception as e:
        logging.critical(
            f"au_summarize_and_send_mail　: Failed to save minutes and transcribed text. {e}")

    # トリガー/モニター用ファイルの削除
    try:
        # トリガー用テキストデータの削除
        blob_client_text.delete_blob()
        logging.info(
            f"Text blob {blob_client_text.blob_name} was deleted successfully.")

    except Exception as e:
        logging.critical(
            f"au_summarize_and_send_mail: Error occurred while deleting the blob {blob_client_text.blob_name}. {e}")


@bp.function_name(name="au_monitor_blob_state")
@bp.timer_trigger(schedule="0 */5 * * * *", arg_name="mytimer", run_on_startup=False)
def au_monitor_blob_state(mytimer: func.TimerRequest) -> None:
    """
    定期的にコンテナインスタンスのログとBLOB内の音声データのメタデータを確認して、処理が開始されていない音声データがあれば再度文字起こし処理を開始する
    1. 処理中のBLOBがあるか確認
    2. 稼働中のコンテナインスタンスのログから処理中のBLOB名を取得
        - 複数のコンテナで処理されているBLOBの確認
        - どのコンテナでも処理されていないBLOBの確認
    3. BLOBの処理時間を確認
        - 処理時間超過を確認
        - max_retry超過を確認
        - 開始・再試行の場合
            - ランダムにコンテナを選択して起動→コンテナ側でその後の処理は実施
        - 終了処理の場合
            - 失敗した音声をバックアップ
            - 元の音声の削除
    """

    # モニタリング用のBLOB名を取得
    blob_names = list_blob_names(
        storage_container_name=STORAGE_CONTAINER_NAME,
        prefix=f"{BLOB_PREFIX_AUDIO_MONITOR}/"
    )

    if len(blob_names) == 0:
        logging.info("au_monitor_blob_state : No processes running.")
        return

    blob_service_client = BlobServiceClient.from_connection_string(
        AZURE_STORAGE_CONNECTION_STRING)
    try:
        # 稼働中のコンテナ名の取得
        running_container_names = get_running_container_groups(
            name_prefix="whisper")

        if len(running_container_names) == 0:
            logging.info("au_monitor_blob_state : No running containers.")

        # 稼働中のコンテナのログを取得
        log_dict = {}
        for container_name in running_container_names:
            log = get_container_log(
                container_group_name=container_name
            )
            log_dict[container_name] = log

        log_text = "\n".join(log_dict.values())
        logging.info(f"au_monitor_blob_state :  Containers logs :{log_text}")

        # 音声データとその処理を行っているコンテナの対応表を作成
        blob2container = defaultdict(list)
        for blob_name in blob_names:
            for container, log in log_dict.items():
                if blob_name in log:
                    blob2container[blob_name].append(container)

        # 重複の確認
        # 同じ音声データを重複して処理しているコンテナの確認
        duplicated_container_names = []
        for blob_name, containers in blob2container.items():
            if len(containers) >= 2:
                random.shuffle(containers)
                duplicated_container_names.extend(containers[1:])

        # 重複したコンテナグループを停止
        if len(duplicated_container_names) != 0:
            logging.warning(
                f"au_monitor_blob_state :The transcription process is duplicated. Stopping the following container. {duplicated_container_names}")

            try:
                for container_name in duplicated_container_names:
                    stop_container(container_name=container_name)
            except Exception as e:
                logging.critical(
                    f"au_monitor_blob_state : Failed to stop containers. {e}")

        # コンテナで処理されていないblobの再試行
        # 起動しているコンテナインスタンスのログにBLOB名が含まれない場合 → whisperプロセスが異常終了した可能性あり
        terminated_blob_names = set(blob_names) - set(blob2container.keys())

        # blobのメタデータの確認、更新
        abnormal_blob_names = []
        for blob_name in terminated_blob_names:
            blob_client = blob_service_client.get_blob_client(
                container=STORAGE_CONTAINER_NAME, blob=blob_name)

            if blob_client.exists():
                blob_properties = blob_client.get_blob_properties()
                metadata = blob_properties.metadata
                if metadata.get("status") == "Transcribe Started":
                    abnormal_blob_names.append(blob_name)
                    # 異常終了した可能性のあるBLOBのStatusを "Container Started" に変更することで、再処理が走るようにする
                    update_blob_metadata(
                        storage_container_name=STORAGE_CONTAINER_NAME,
                        blob_name=blob_name,
                        new_metadata={
                            "status": "Container Started"
                        }
                    )

        if len(abnormal_blob_names) != 0:
            logging.warning(
                f"au_monitor_blob_state : The process may have terminated abnormally.: BLOBs {abnormal_blob_names}")

    except Exception as e:
        logging.critical(
            f"au_monitor_blob_state : Unable to check abnormally terminated containers. {e}")

    # blobの確認
    for blob_name in blob_names:
        try:
            blob_client = blob_service_client.get_blob_client(
                container=STORAGE_CONTAINER_NAME,
                blob=blob_name
            )

            # 作成された時間を取得
            creation_time = blob_client.get_blob_properties()['creation_time']
            creation_time = creation_time.replace(tzinfo=tz.tzutc())

            # 時間差を計算
            current_time = datetime.datetime.now(tz=tz.tzutc())
            time_difference = current_time - creation_time

            # メタデータの取得
            metadata = blob_client.get_blob_properties().metadata
            status = metadata.get("status")
            num_retry = int(metadata.get("num_retry"))
            is_segment = int(metadata.get("is_segment", 0))
            mail = metadata.get("mail")

            file_name = metadata.get("file_name")
            file_name = base64.b64decode(
                file_name.encode("utf-8")).decode("utf-8")

        except Exception as e:
            logging.critical(
                f"au_monitor_blob_state : Unable to load blob {blob_name}. {e}")
            continue

        # 終了条件の確認
        is_continue = True
        terminate_reason = ""

        if time_difference <= datetime.timedelta(minutes=90):
            # 状態が"Container Started"でなければ既に処理が始まっていると判断しパス
            if status != "Container Started":
                continue
            logging.info(
                f"au_monitor_blob_state: {blob_name} has not been processed.")

            if num_retry >= max_retry:
                is_continue = False
                terminate_reason = "Abnormal termination: Max retry"
        else:
            is_continue = False
            terminate_reason = "Abnormal termination: Time out."

        if is_continue:
            # Whisperコンテナの起動 & 処理実行
            # 起動可能なコンテナが存在するか確認
            terminated_container_groups = get_terminated_container_groups(
                name_prefix="whisper")

            # ターゲットコンテナの重複を回避
            random.shuffle(terminated_container_groups)
            try:
                for container_groups_name in terminated_container_groups:
                    response = start_container(
                        container_name=container_groups_name)
                    if response.status() == "InProgress":
                        # モニタリング用音声BLOBのメタデータを更新
                        update_blob_metadata(
                            storage_container_name=STORAGE_CONTAINER_NAME,
                            blob_name=blob_name,
                            new_metadata={
                                "num_retry": str(num_retry + 1),
                                "container_group": container_groups_name
                            },
                        )
                        logging.info(
                            f"au_monitor_blob_state: Container {container_groups_name} Started.")
                        break
                    else:
                        raise Exception(
                            f"au_monitor_blob_state: No available containers. Container status:{response.status()}")
            except Exception as e:
                logging.critical(
                    f"au_monitor_blob_state: Failed to start the container for transcription.{e}")

        else:
            try:
                # 音声データを一時保存
                binary_audio = blob_client.download_blob().readall()
                audio_blob_name_tmp = blob_name.replace(
                    BLOB_PREFIX_AUDIO_MONITOR, BLOB_PREFIX_AUDIO_TMP)
                metadata.update({
                    "status": terminate_reason
                })
                save_to_blob(
                    data=binary_audio,
                    storage_container_name=STORAGE_CONTAINER_NAME,
                    blob_name=audio_blob_name_tmp,
                    metadata=metadata
                )
            except Exception as e:
                logging.critical(
                    f"au_monitor_blob_state : Unable to save {blob_name}. {e}")

            try:
                # モニタリング用BLOBデータを削除する
                blob_client.delete_blob()
                logging.critical(
                    f"au_monitor_blob_state : The audio data will be deleted and the process will be terminated.{blob_name} has not been processed even after more than 90 minutes since creation.")

                # セグメントの場合、関連する音声とテキストのセグメント全てを削除する
                if is_segment == 1:
                    blob_audio_segments = list_blob_segments(
                        storage_container_name=STORAGE_CONTAINER_NAME,
                        prefix=f"{BLOB_PREFIX_AUDIO_MONITOR}/",
                        audio_blob_name_for_monitor=metadata.get(
                            "audio_blob_name_for_monitor")
                    )
                    blob_text_segments = list_blob_segments(
                        storage_container_name=STORAGE_CONTAINER_NAME,
                        prefix=f"{BLOB_PREFIX_TEXT_SEGMENTS}/",
                        audio_blob_name_for_monitor=metadata.get(
                            "audio_blob_name_for_monitor")
                    )

                    for blob_segment in (blob_audio_segments + blob_text_segments):
                        delete_blob(
                            storage_container_name=STORAGE_CONTAINER_NAME,
                            blob_name=blob_segment
                        )
                    logging.critical(
                        f"au_monitor_blob_state : Segment blobs was deleted. File: {blob_audio_segments} & {blob_text_segments}")

                # blobを削除できた場合のみメール送信
                logging.critical(
                    f"au_monitor_blob_state: Transcription process has failed. mail:{mail}, blob:{blob_name}, File name: {file_name}")
                if mail:
                    message = "【I-Colleague】文字起こし処理に失敗しました。時間をおいてお試しください。"
                    send_email(
                        subject=message,
                        content=f"ファイル名:{file_name}",
                        send_to=[mail]
                    )
                    logging.critical(
                        f"au_monitor_blob_state : An email was sent because the transcription process of the audio data terminated abnormally. Deleted {blob_name}. File name: {file_name}")
                else:
                    logging.critical(
                        f"au_monitor_blob_state : There is no email address in the metadata. Deleted {blob_name}. File name: {file_name}")

            except Exception as e:
                logging.error(
                    f"au_monitor_blob_state : Unable to delete and send mail {blob_name}. {e}")

    logging.info(f"au_monitor_blob_state: Existing all blob is checked")


# 定期的に出力された文字起こしセグメントを確認して結果が揃っていれば、トリガー用BLOBに結果を保存する
@bp.function_name(name="au_monitor_text_segment")
@bp.timer_trigger(schedule="0 */5 * * * *", arg_name="mytimer", run_on_startup=False)
def au_monitor_text_segment(mytimer: func.TimerRequest) -> None:
    logging.info(f"au_monitor_text_segment: Called")

    # 全てのテキストセグメント格納BLOB名を取得
    blob_names = list_blob_names(
        storage_container_name=STORAGE_CONTAINER_NAME,
        prefix=f"{BLOB_PREFIX_TEXT_SEGMENTS}/"
    )

    if len(blob_names) == 0:
        logging.info(
            f"au_monitor_text_segment: No existing blob text segments")
        return

    logging.info(
        f"au_monitor_text_segment: Existing BLOB text segments: {blob_names}")
    state_dict = {}
    for blob_name in blob_names:
        blob_service_client = BlobServiceClient.from_connection_string(
            AZURE_STORAGE_CONNECTION_STRING)
        blob_client = blob_service_client.get_blob_client(
            container=STORAGE_CONTAINER_NAME,
            blob=blob_name
        )
        metadata = blob_client.get_blob_properties().metadata

        # 元のモニター用音声BLOB名をキーとして、メタデータを辞書形式に整理
        if metadata["audio_blob_name_for_monitor"] not in state_dict.keys():
            state_dict[metadata["audio_blob_name_for_monitor"]] = {
                "num_segments": int(metadata["num_segments"]),
                "segment_ids": [int(metadata["segment_id"])],
                "blob_segments": [blob_name],
                "metadata": metadata
            }
        else:
            state_dict[metadata["audio_blob_name_for_monitor"]
                       ]["blob_segments"].append(blob_name)
            state_dict[metadata["audio_blob_name_for_monitor"]
                       ]["segment_ids"].append(int(metadata["segment_id"]))

    for monitor_blob in state_dict.keys():

        # ID順になるようにBLOB名をソート
        blob_segments = state_dict[monitor_blob]["blob_segments"]
        segment_ids = state_dict[monitor_blob]["segment_ids"]

        zipped_lists = zip(segment_ids, blob_segments)
        sorted_zipped_lists = sorted(zipped_lists)
        segment_ids, blob_segments = zip(*sorted_zipped_lists)
        blob_segments = list(blob_segments)

        # BLOBセグメントのテキストデータを読み込み
        all_texts = []
        creation_times = []
        for blob_name in blob_segments:
            blob_service_client = BlobServiceClient.from_connection_string(
                AZURE_STORAGE_CONNECTION_STRING)
            blob_client = blob_service_client.get_blob_client(
                container=STORAGE_CONTAINER_NAME,
                blob=blob_name
            )

            # 文字起こしテキストの取得 & 結合
            blob_data = blob_client.download_blob()
            text = blob_data.content_as_text()
            all_texts.append(text)

            # 作成日時の取得
            creation_time = blob_client.get_blob_properties()['creation_time']
            creation_time = creation_time.replace(tzinfo=tz.tzutc())
            creation_times.append(creation_time)

        # 現在のUTC時刻をタイムゾーン付きで取得
        current_time = datetime.datetime.now(tz=tz.tzutc())

        # 直近に作成された文字起こし結果の作成日時と現在時刻の時間差を計算
        time_difference = current_time - max(creation_times)

        # 分割数が存在するBLOBセグメント数と同じ場合 (文字起こし完了シグナル)
        is_finished = state_dict[monitor_blob]["num_segments"] == len(
            blob_segments)

        # トリガー用音声BLOBの存在確認
        blob_audio_segments = list_blob_segments(
            storage_container_name=STORAGE_CONTAINER_NAME,
            prefix=f"{BLOB_PREFIX_AUDIO_MONITOR}/",
            audio_blob_name_for_monitor=monitor_blob
        )

        # 音声データが存在しない場合、テキストセグメントも削除する (既にメールが送信されている)
        if len(blob_audio_segments) == 0:
            # テキストBLOBセグメントを削除
            for blob_name in blob_segments:
                delete_blob(
                    storage_container_name=STORAGE_CONTAINER_NAME,
                    blob_name=blob_name
                )
            logging.critical(
                f"Since there is no audio data, text segments {blob_segments} will be deleted.")
            continue

        # 直近の文字起こし結果作成から50分以上経過している場合 (一部の文字起こし処理が異常終了したと判断) 処理ずみの結果のみ送付する
        is_abnormal = (time_difference > datetime.timedelta(minutes=50))
        if is_abnormal:
            logging.critical(
                f"au_monitor_text_segment: There is a possibility that the transcription process  of {monitor_blob} terminated abnormally in part.")

        # 正常終了 or 異常終了の場合、文字起こし結果を結合し、トリガー用BLOBに保存する
        if is_finished | is_abnormal:
            # 文字起こし結果の結合
            all_text = "\n".join(all_texts)

            # トリガー用BLOBに文字起こし結果を保存
            output_text_blob_name = monitor_blob.replace(
                BLOB_PREFIX_AUDIO_MONITOR, BLOB_PREFIX_TEXT_TRIGGER)
            suffix = "." + monitor_blob.split(".")[-1]
            output_text_blob_name = output_text_blob_name.replace(
                suffix, ".txt")

            save_to_blob(
                data=all_text.encode('utf-8'),
                storage_container_name=STORAGE_CONTAINER_NAME,
                blob_name=output_text_blob_name,
                metadata=state_dict[monitor_blob]["metadata"],
                overwrite=False
            )

            logging.info(
                f"au_monitor_text_segment: Combined the transcription results of {monitor_blob} and saved to {output_text_blob_name}.")

            # トリガー用音声BLOBを削除
            for blob_name in blob_audio_segments:
                delete_blob(
                    storage_container_name=STORAGE_CONTAINER_NAME,
                    blob_name=blob_name
                )
            logging.info(
                f"au_monitor_text_segment: The audio BLOB used for triggering {blob_audio_segments} has been deleted.")
            # テキストBLOBセグメントを削除
            for blob_name in blob_segments:
                delete_blob(
                    storage_container_name=STORAGE_CONTAINER_NAME,
                    blob_name=blob_name
                )
            logging.info(
                f"au_monitor_text_segment: Text BLOB segments {blob_segments} has been deleted.")
    return


@bp.function_name(name="au_reset_container_state")
@bp.timer_trigger(
    # 毎日AM3時に定期実行, {second} {minute} {hour} {day} {month} {day-of-week}
    schedule="0 0 18 * * *",
    arg_name="mytimer",
    run_on_startup=False
)
def au_reset_container_state(mytimer: func.TimerRequest) -> None:
    """
    深夜に一回実行
    未処理の音声データの有無を確認し、無ければコンテナを停止する
    """
    # モニター用のBLOB名を取得
    blob_names = list_blob_names(
        storage_container_name=STORAGE_CONTAINER_NAME,
        prefix=f"{BLOB_PREFIX_AUDIO_MONITOR}/"
    )

    # モニタリング用BLOBが存在しない場合、停止していないコンテナを停止する
    if len(blob_names) == 0:
        # 全whisperコンテナ名を取得
        container_names = list_container_names(name_prefix="whisper")

        # コンテナを停止
        for container_name in container_names:
            stop_container(container_name)

        logging.info(
            "Since there is no audio data being processed, all containers will be stopped.")

    return
