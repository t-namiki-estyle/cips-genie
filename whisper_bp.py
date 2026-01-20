import asyncio
from azure.storage.blob.aio import BlobServiceClient
import azure.functions as func
import azure.durable_functions as d_func
import openai
from openai import AsyncAzureOpenAI
import io
import os
import logging
import json
from datetime import timedelta
from urllib.parse import urlparse, parse_qs

from zoom_bp import INTERVAL, LEAD_MARGIN, TRAIL_MARGIN, date_time_ite
from prompt import transcribe_prompt_dict, DUPLICATION_EXTRACTION_SYSTEM_CONTENT, TranscriptionDeduplication
from whisper_util import remove_repeated_words, pcm_to_wav

from i_style.llm import AzureOpenAI
from i_style.aiohttp import AsyncHttpClient
from config import (
    GPT_API_VERSION, GPT4O_TRANSCRIBE_API_ENDPOINT, GPT4O_TRANSCRIBE_API_KEY, GPT4O_TRANSCRIBE_DEPLOYMENT_NAME, LLM_REGISTRY
)
from util import BLOB_CONNECTION_STRING, AUDIO_CONTAINER_NAME


##
# env
##
# API KEYとURLの取得
WHISPER_API_KEY = os.environ.get("WHISPER_API_KEY")
WHISPER_URL = os.environ.get("WHISPER_URL")

##
# blueprint
##
whisper_bp = d_func.Blueprint()

# @whisper_bp.function_name("whisper")


@whisper_bp.orchestration_trigger(context_name="context")
def whisper_(context: d_func.DurableOrchestrationContext):
    """
    INTERVALなどの環境変数とblob_prefixをもとにblobを検索し、存在するなら文字起こし、しない場合は待機。

    最初以外は一分間新しい音声が来なければタイムアウトで終了
    - fail_counterが6を超えたら終了
    - whisperの終了までは一分以上かかる
    """
    # インスタンスIDの取得
    access_token = instance_id = context.instance_id
    logging.info(f"context id: {instance_id}")

    # responseの型の作成
    res_json = {
        "access_token": access_token,
        "text": {},
        "max_index": 0
    }

    # polling用の変数の作成
    counter = 0
    max_fail = 3
    fail_counter = max_fail - 1  # 初回のみ一回失敗したら終了
    interval = 30
    whisper_results = []
    max_index = 0

    # continue as new
    # 開始時に渡されたデータを取得
    payload = context.get_input()
    if "whisper_results" in payload.keys():
        # int型の読み込み
        counter = int(payload["counter"])
        fail_counter = int(payload["fail_counter"])
        interval = int(payload["interval"])
        max_index = int(payload["max_index"])

        # customStatusの更新
        # すべてのキーを整数にキャスト
        res_json["text"] = {int(k): v for k, v in payload["text"].items()}
        # res_json["text"] = payload["text"]
        res_json["max_index"] = max_index

        # whisper results
        whisper_results = payload["whisper_results"]

        if not context.is_replaying:
            logging.debug(f"continue as new, counter: {counter}")

    # 履歴用
    upn = payload["upn"]
    res_json["session_id"] = session_id = payload.get("session_id")
    res_json["title"] = payload.get("title", "No title")

    # customStatusの設定
    context.set_custom_status(res_json)

    deadline = context.current_utc_datetime
    if interval > 0:
        deadline = context.current_utc_datetime + timedelta(seconds=interval)
        yield context.create_timer(deadline)

    # 定期的に文字起こしデータを取得
    whisper_data = {}
    whisper_data["access_token"] = access_token
    whisper_data["counter"] = counter
    whisper_data["blob_prefix"] = payload["blob_prefix"]
    whisper_data["results"] = whisper_results
    whisper_data["fail_counter"] = fail_counter

    # 履歴用
    whisper_data["upn"] = upn
    whisper_data["session_id"] = session_id

    # transcribe
    try:
        whisper_data = yield context.call_activity("transcribe_", whisper_data)
    except Exception as e:
        if not context.is_replaying:
            logging.critical(f"Durable transcribe error: {e}")
        whisper_data = {
            "text": "文字起こしできませんでした。",
            "results": [],
            "interval": INTERVAL
        }
        if "loop transcribe:" in str(e):
            # loopなので結果に追加しない
            whisper_data["text"] = ""

    # 結果の格納
    # 空でないことを確認, かつ5文字以上
    if whisper_data["text"] and len(whisper_data["text"]) >= 5:
        # 仮置き
        if whisper_data["text"] == "文字起こしできませんでした。":
            whisper_data["text"] = "_"

        # 辞書に追加
        max_index += 1
        res_json["max_index"] = max_index
        res_json["text"][max_index] = whisper_data["text"]

        # 要素が10を超えた場合
        if len(res_json["text"]) > 10:
            min_key = min(res_json["text"].keys())
            res_json["text"].pop(min_key)

        context.set_custom_status(res_json)

    # logging
    try:
        whisper_results = whisper_data["results"]
        text = whisper_data["text"]

        counter += 1
        fail_counter = whisper_data["fail_counter"]
        interval = whisper_data["interval"]

    except Exception as e:
        logging.warning(f"no key: {e}")

    if not context.is_replaying:
        logging.info(
            f"text: {text}, results: {whisper_results}, interval: {interval}, ")

    # 継続の判断
    if fail_counter >= max_fail:
        logging.warning("whisper end: no new audio")
        return res_json

    # continue as new
    payload["title"] = whisper_data.get("title")
    payload["counter"] = counter
    payload["fail_counter"] = fail_counter
    payload["interval"] = interval
    payload["max_index"] = max_index

    payload["text"] = res_json["text"]
    payload["whisper_results"] = whisper_results
    logging.debug("call continue as new")
    context.continue_as_new(input_=payload)


@whisper_bp.activity_trigger(input_name="input")
async def transcribe_(input: dict) -> dict:
    """
    GPT-4o-transcribeに音声を送信して文字起こしの結果を取得する

    blobの命名ルールに基づいて順番に探す
    - 10回リトライ、5秒スリープ
        - 初回のみ15秒スリープ

    - blobが存在しない場合、fail_counterを1増やし、空の結果を返す
    - blobが存在して、whisperからのresponseが返ってくる場合、fail_counterを0にする

    intervalの変数名を被せてしまったので注意！！

    翻訳機能はどうするか？
    """
    logging.debug("start transcribe!!")

    # apiの呼び出し
    history_base_url = os.environ.get("HISTORY_API_URL")
    history_api_key = os.environ.get("HISTORY_API_KEY")

    # history
    upn = input["upn"]
    session_id = input.get("session_id")
    title = "No title"

    try:
        sendFrom = input["from"]
        assert sendFrom in ["web", "teams", "mail", "agent"]
    except Exception as e:
        input["from"] = sendFrom = "web"

    # fail_counter
    fail_counter = int(input["fail_counter"])

    # 前回の文字起こしの結果
    prev_text = input.get("results", [])

    # blob name
    access_token = input["access_token"]
    counter = int(input["counter"])
    blob_prefix = input["blob_prefix"]
    start_time = counter*INTERVAL
    end_time = start_time + LEAD_MARGIN + INTERVAL + TRAIL_MARGIN

    blob_name = f"{blob_prefix}/{access_token}_{start_time}_{end_time}.pcm"
    next_blob_name = f"{blob_prefix}/{access_token}_{start_time+INTERVAL}_{end_time+INTERVAL}.pcm"

    # Blobクライアントを取得
    blob_service_client = BlobServiceClient.from_connection_string(
        BLOB_CONNECTION_STRING)
    blob_client = blob_service_client.get_blob_client(
        container=AUDIO_CONTAINER_NAME, blob=blob_name)
    next_blob_client = blob_service_client.get_blob_client(
        container=AUDIO_CONTAINER_NAME, blob=next_blob_name)

    # blobがあるか確認
    max_retries = 6
    while not await blob_client.exists():
        logging.warning(f"no blob: {blob_name}")
        if max_retries <= 0:
            # 失敗
            logging.warning(
                f"fail: no blob  {blob_name}, fail: {fail_counter}")
            data = {
                "results": [],
                "text": "",
                "interval": 0 if await next_blob_client.exists() else 5,
                "fail_counter": fail_counter + 1
            }
            return data
        max_retries -= 1

        # 初回のみ長めのtimeoutを設定
        sleep_time = 20 if counter == 0 else 5
        await asyncio.sleep(sleep_time)

    # バイナリの取得 (update)
    blob_download_stream = await blob_client.download_blob()
    pcm_audio: bytes = await blob_download_stream.readall()

    try:
        client = AsyncAzureOpenAI(
            api_version=GPT_API_VERSION,
            azure_endpoint=GPT4O_TRANSCRIBE_API_ENDPOINT,
            api_key=GPT4O_TRANSCRIBE_API_KEY
        )
    except Exception as e:
        logging.critical(f"Failed to initialize AsyncAzureOpenAI client: {e}")
        return {
            "results": [],
            "text": "文字起こしできませんでした。",
            "interval": 5,
            "fail_counter": fail_counter + 1,
            "session_id": session_id,
            "title": title
        }

    # デフォルトで日本語のプロンプトを選択。
    # 基本的にプロンプトの言語ではなく、音声データの言語によって文字起こしされている模様。
    selected_transcribe_prompt = transcribe_prompt_dict.get("ja", "")

    # 　最大繰り返し回数
    max_number_of_attempts = 2
    attempts_count = 0
    text = ""

    try:
        wav_data = pcm_to_wav(pcm_audio)
    except Exception as e:
        logging.critical(f"Failed to convert PCM to WAV: {e}")
        return {
            "results": [],
            "text": "文字起こしできませんでした。",
            "interval": 5,
            "fail_counter": fail_counter + 1,
            "session_id": session_id,
            "title": title
        }
    finally:
        del pcm_audio  # メモリ解放

    while True:
        try:
            with io.BytesIO(wav_data) as wav_buffer:
                wav_buffer.name = "audio.wav"  # ファイル名を設定。これがないとエラーになる。

                # transcribeモデルでの文字起こし実行
                transcription = await client.audio.transcriptions.create(
                    file=wav_buffer,
                    model=GPT4O_TRANSCRIBE_DEPLOYMENT_NAME,
                    prompt=selected_transcribe_prompt,
                    temperature=0.0,
                    response_format="json"
                )

                # 文字起こしの結果の後処理
                text = transcription.text
                # 成功した場合はループを抜ける
                break

        except Exception as e:
            attempts_count += 1
            error_str = str(e)
            logging.warning(
                f"Transcription attempt {attempts_count} failed: {error_str}")

            # 最大試行回数に達した場合は終了
            if attempts_count >= max_number_of_attempts:
                logging.error(
                    f"Transcription failed after {max_number_of_attempts} attempts")
                break

            # レート制限エラーの場合は少し長めに待機
            if (hasattr(e, 'status_code') and e.status_code == 429) or "429" in error_str:
                logging.warning("429 error. Retry!")
                await asyncio.sleep(5)
            else:
                await asyncio.sleep(1)

    # 最終的に失敗した場合
    if not text and attempts_count >= max_number_of_attempts:
        return {
            "results": [],
            "text": "文字起こしできませんでした。",
            "interval": 5,
            "fail_counter": fail_counter + 1,
            "session_id": session_id,
            "title": title
        }

    # transcribeからのresponseがあった場合
    data = {
        "results": [],
        "text": "",
        "interval": 0,
        "fail_counter": 0  # 成功したためリセット
    }

    # 前回の文字起こしがある場合、今回の文字起こしとの重複を排除する
    if prev_text and len(prev_text) > 0:
        try:
            # 前回のテキストを取得する
            last_prev_text = prev_text[-1] if isinstance(
                prev_text, list) and prev_text else str(prev_text)

            system_prompt = DUPLICATION_EXTRACTION_SYSTEM_CONTENT.format(
                previous_text=last_prev_text,
                current_text=text
            )

            tools = [openai.pydantic_function_tool(TranscriptionDeduplication)]

            messages = [{"role": "system", "content": system_prompt}]

            duplication_extract_response = await AzureOpenAI(
                messages=messages,
                model_name="gpt4.1",
                tools=tools,
                tool_choice="required",
                timeout=60,
                max_retries=1,
                raise_for_error=False,
                registry=LLM_REGISTRY
            )

            if duplication_extract_response and "choices" in duplication_extract_response:
                args = duplication_extract_response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
                result = json.loads(args)

                # 重複排除された新しい内容を使用
                if result.get("new_content"):
                    text = result["new_content"]
                    if result.get("duplicate_content"):
                        logging.debug(
                            f"重複した文章: {result['duplicate_content'][:100]}...")

        except Exception as e:
            logging.warning(f"重複排除処理でエラー: {e}")
            # エラーが発生した場合は元のテキストをそのまま使用

    if text.strip():
        # 重複した単語を正規表現を使用して取り除く
        text = remove_repeated_words(text)

        # テキストが有効な場合のみ設定
        data["text"] = text

        # 次回の重複排除用に今回のテキストを結果配列に追加
        data["results"] = [text]
    else:
        data["text"] = ""
        data["results"] = []

    # 履歴の登録
    if session_id is not None:
        request_data = {
            "items": [
                {
                    "upn": upn,
                    "content": [{"type": "blob", "name": blob_name}],
                    "role": "user",
                    "submode": "zoom",
                    "model": "gpt-4o-transcribe",
                    "from": sendFrom,
                    "sessionId": session_id
                },
                {
                    "upn": upn,
                    "content": [{"type": "text", "text": text}],
                    "role": "assistant",
                    "submode": "zoom",
                    "model": "gpt-4o-transcribe",
                    "from": sendFrom,
                    "sessionId": session_id
                }
            ]
        }

        async_http_client = AsyncHttpClient()

        # history_base_url: https://itc-history-functions.azurewebsites.net
        url = f"{history_base_url}/api/history/minutes"
        api_name = "add_history"
        try:
            response = await async_http_client.post(url=url, api_key=history_api_key, json_data=request_data, process_name=api_name)
        except Exception as e:
            logging.warning(f"履歴の追加: {e}")
        else:
            title = response.get("title", "タイトルの取得に失敗しました。")

    # intervalの更新
    data["interval"] = 0 if await next_blob_client.exists() else 5

    # レスポンスに追加
    data["session_id"] = session_id
    data["title"] = title

    return data


@whisper_bp.route(route="genie/durable/whisper", methods=("POST",))
async def start_durable_whisper(req: func.HttpRequest) -> func.HttpResponse:
    """
    durable functionsでwhisperを起動する
    UI側での新規開始か途中参加かはsession_idで判別する

    mode: zoomの場合、whisperのeventPostUriを渡してzoom botを起動する

    `itochu.zoom.us`以外のリンクについてはブロックする

    エラー時のメッセージについては要調整
    """
    client_ip = req.headers.get("x-forwarded-for")
    logging.info(f"client_ip: {client_ip}")
    req_json = req.get_json()

    try:
        upn = req_json["upn"]
        session_id = req_json.get("session_id")
        logging.info(f"upn: {upn}")
        logging.info(f"session_id: {session_id}")
        mode = req_json["mode"]
    except:
        mode = "ui"

    json_response = {"status": 500}

    if mode == "zoom":
        url = req_json["url"]
        logging.info(f"url: {url}")
        # zoomUrlの確認
        # https://us05web.zoom.us/j/81992655884?pwd=jL2HKgdyrjVGS5vcz7a7gDag5Gyg6o.1
        # URLをパースしてクエリ部分を取得
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)

        try:
            # スキーム、ネットロケーション（ドメイン）、パスを組み合わせてベースURLを作成
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
            pwd = query_params.get("pwd")[0]
            room_id = parsed_url.path.split("/")[-1]
            # 一時的にコメントアウト
            # assert "https://itochu.zoom.us" in base_url ,AssertionError("https://itochu.zoom.us 以外のurlが入力されています。")
        except AssertionError as e:
            json_response["error"] = "伊藤忠のzoomのみ参加可能です。"
            return func.HttpResponse(json.dumps(json_response))
        except Exception as e:
            logging.warning(f"illegal url: {e}")
            json_response["error"] = "不正なurlです。参加パスワードの漏れなどがないか確認してください。"
            return func.HttpResponse(json.dumps(json_response))

    # 非同期http通信クライアントのインスタンス化
    async_http_client = AsyncHttpClient()

    # requestの準備
    base_url = os.environ.get("DURABLE_URL")
    api_key = os.environ.get("DURABLE_API_KEY")

    headers = {
        "Content-Type": "application/json",
        "x-functions-key": api_key
    }

    # start whisper
    data = req_json

    # file_prefix
    data["blob_prefix"] = f"zoom/{next(date_time_ite)}/{room_id}"

    # data_encode = json.dumps(data)
    # response = requests.post(base_url.format(functionName="whisper_"), headers=headers, data=data_encode)

    api_name = "start_whisper"
    try:
        # res_json = json.loads(response.text)
        res_json = await async_http_client.post(url=base_url.format(functionName="whisper_"), api_key=api_key, json_data=data, process_name=api_name)
        logging.info(f"start whisper: {res_json}")

        id = res_json["id"]
        statusUri = res_json["statusQueryGetUri"]
        sendEventPostUri = res_json["sendEventPostUri"]
        terminatePostUri = res_json["terminatePostUri"]
    except Exception as e:
        # logging.critical(f"ERROR start whisper: {response.status_code}, {response.text}")
        logging.critical(f"ERROR start whisper: {e}")

        json_response["error"] = "whisperを開始できませんでした。"
        return func.HttpResponse(json.dumps(json_response))

    if mode == "zoom":
        # start zoom bot
        data["access_token"] = id
        data["sendEventPostUri"] = sendEventPostUri
        data["terminatePostUri"] = terminatePostUri
        # data_encode = json.dumps(data)
        # response = requests.post(base_url.format(functionName="zoom"), headers=headers, data=data_encode)

        api_name = "start_zoom"
        try:
            # res_json = json.loads(response.text)
            res_json = await async_http_client.post(url=base_url.format(functionName="zoom"), api_key=api_key, json_data=data, process_name=api_name)

            logging.info(f"start zoom bot: {res_json}")
        except Exception as e:
            # logging.critical(f"ERROR start zoom bot: {response.status_code}, {response.text}")
            logging.critical(f"ERROR start zoom bot: {e}")

            json_response["error"] = "zoom botを開始できませんでした。"
            return func.HttpResponse(json.dumps(json_response))

        # responseの内容を作成
        # whisperのものに上書き
        res_json["statusQueryGetUri"] = statusUri
        res_json["terminatePostUri"] = terminatePostUri

    return func.HttpResponse(json.dumps(res_json))
