import logging
import os
import time
import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerinstance.models import ContainerExecRequest, ContainerExecRequestTerminalSize
from azure.storage.blob import BlobServiceClient, BlobClient
import websocket
import random


# 環境変数から情報を取得
# WEBSITE_OWNER_NAMEから環境変数を取得
SUBSCRIPTION_ID = os.environ.get("WEBSITE_OWNER_NAME").split("+")[0]
RESOURCE_GROUP_NAME = "-".join(os.environ.get(
    "WEBSITE_OWNER_NAME").split("+")[1].split("-")[:-2])
AZURE_STORAGE_CONNECTION_STRING = os.environ.get("BLOB_CONNECTION_STRING")

BLOB_PREFIX_AUDIO_MONITOR = "audio_upload/audio_for_monitor"
BLOB_PREFIX_TEXT_TRIGGER = "audio_upload/text_for_trigger"

# 　コンテナーグループ内のコンテナ名
INNER_CONTAINER_NAME = "whisper-container"

# whisperコンテナを立ち上げて文字起こし処理をコマンド実行する関数


def exec_whisper_process(
    storage_container_name,
    audio_file_blob_name,
    name_prefix="whisper"
):

    # モニタリング用のBLOB名の取得
    audio_file_blob_name_for_monitor = audio_file_blob_name.replace(
        BLOB_PREFIX_AUDIO_TRIGGER, BLOB_PREFIX_AUDIO_MONITOR)

    # 認証情報の設定
    credential = DefaultAzureCredential()

    # クライアントの初期化
    client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)

    # 停止しているコンテナの取得
    terminated_container_groups_names = get_terminated_container_groups(
        name_prefix=name_prefix)
    logging.info(f"Terminated containers: {terminated_container_groups_names}")

    if len(terminated_container_groups_names) == 0:
        raise Exception("No available containers.")

    # コンテナリストをシャッフル (ターゲットコンテナの重複を回避する)
    random.shuffle(terminated_container_groups_names)

    # コンテナを起動
    for container_groups_name in terminated_container_groups_names:
        response = start_container(container_name=container_groups_name)
        if response.status() == "InProgress":
            selected_container_group_name = container_groups_name

            # モニタリング用音声BLOBのメタデータを更新
            update_blob_metadata(
                storage_container_name=storage_container_name,
                blob_name=audio_file_blob_name_for_monitor,
                new_metadata={
                    "status": "Container Started",
                    "container_group_name": selected_container_group_name
                }
            )
            logging.info(f"Container {selected_container_group_name} Started.")
            break
    else:
        # 文字起こしに失敗した旨をメール送信する処理 # To Do
        raise Exception(
            f"No available containers. Container status:{response.status()}")

    # 実行コマンド
    python_command = f"python main.py {storage_container_name} {audio_file_blob_name}"

    # コンテナの状態を取得し、Running状態になるのを待ち、コマンド実行
    while True:
        container = client.container_groups.get(
            RESOURCE_GROUP_NAME, selected_container_group_name).containers[0]
        if container.instance_view != None:
            if container.instance_view.current_state.state == "Running":

                # コンテナ内でコマンド実行
                command_exec_in_container(
                    container_name=selected_container_group_name,
                    python_command=python_command
                )

                # モニタリング用音声BLOBのメタデータを更新
                update_blob_metadata(
                    storage_container_name=storage_container_name,
                    blob_name=audio_file_blob_name_for_monitor,
                    new_metadata={
                        "status": "Command executed"
                    }
                )
                logging.info("Command executed.")
                break
        time.sleep(10)


# BLOBに音声データをアップロードする処理
def save_audio_to_blob(
    binary_audio,
    storage_container_name,
    file_name,
    metadata
):
    audio_blob_name_for_monitor = f"{BLOB_PREFIX_AUDIO_MONITOR}/{file_name}"

    # テキスト保存BLOB名をメタデータとして追加
    metadata.update(
        {
            "audio_blob_name_for_monitor": audio_blob_name_for_monitor,
            "status": "Audio data saved."
        }
    )

    save_to_blob(
        data=binary_audio,
        storage_container_name=storage_container_name,
        blob_name=audio_blob_name_for_monitor,
        metadata=metadata,
        overwrite=False
    )

    return audio_blob_name_for_monitor


# BLOBにデータを保存する関数
def save_to_blob(
    data,
    storage_container_name,
    blob_name,
    metadata=None,
    overwrite=False
):
    blob_service_client = BlobServiceClient.from_connection_string(
        AZURE_STORAGE_CONNECTION_STRING)
    blob_client = blob_service_client.get_blob_client(
        container=storage_container_name,
        blob=blob_name
    )
    blob_client.upload_blob(
        data,
        overwrite=overwrite,
        metadata=metadata
    )
    return

# BLOBを削除する関数


def delete_blob(
    storage_container_name,
    blob_name
):
    blob_service_client = BlobServiceClient.from_connection_string(
        AZURE_STORAGE_CONNECTION_STRING)
    blob_client = blob_service_client.get_blob_client(
        container=storage_container_name,
        blob=blob_name
    )

    # BLOBが存在する場合、削除
    if blob_client.exists():
        blob_client.delete_blob()
    return


# 稼働していないコンテナグループ名を取得
def get_terminated_container_groups(name_prefix="whisper"):
    # 認証情報の設定
    credential = DefaultAzureCredential()

    # クライアントの初期化
    client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)

    # 現在のコンテナーグループを取得
    container_groups = client.container_groups.list_by_resource_group(
        resource_group_name=RESOURCE_GROUP_NAME
    )

    # コンテナグループの状態を確認し、稼働していないコンテナグループ名を取得
    terminated_container_groups_names = []
    for container_group in container_groups:
        if (name_prefix in container_group.name):
            # コンテナーを取得
            container = client.container_groups.get(
                RESOURCE_GROUP_NAME, container_group.name).containers[0]
            if (container.instance_view != None):
                if container.instance_view.current_state.state == "Terminated":
                    terminated_container_groups_names.append(
                        container_group.name)

    return terminated_container_groups_names


# コンテナグループの名前をリストで取得する関数
def list_container_names(
    name_prefix="whisper"
):
    # 認証情報の設定
    credential = DefaultAzureCredential()

    # クライアントの初期化
    client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)

    # 現在のコンテナーグループを取得
    container_groups = client.container_groups.list_by_resource_group(
        resource_group_name=RESOURCE_GROUP_NAME
    )

    # コンテナグループの名前を確認
    container_groups_names = []
    for container_group in container_groups:
        if (name_prefix in container_group.name):
            container_groups_names.append(container_group.name)

    return container_groups_names


# コンテナグループを起動する関数
def start_container(container_name):
    # 認証情報の設定
    credential = DefaultAzureCredential()

    # クライアントの初期化
    client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)

    # コンテナを起動
    response = client.container_groups.begin_start(
        resource_group_name=RESOURCE_GROUP_NAME,
        container_group_name=container_name
    )
    return response


# コンテナグループを停止する関数
def stop_container(container_name):
    # 認証情報の設定
    credential = DefaultAzureCredential()

    # クライアントの初期化
    client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)

    # コンテナを停止
    client.container_groups.stop(
        resource_group_name=RESOURCE_GROUP_NAME,
        container_group_name=container_name
    )
    return


# コンテナを指定してコマンドを実行する関数
def command_exec_in_container(container_name, python_command):
    # 認証情報の設定
    credential = DefaultAzureCredential()

    # クライアントの初期化
    client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)

    terminalsize = os.terminal_size((80, 24))
    terminal_size = ContainerExecRequestTerminalSize(
        rows=terminalsize.lines, cols=terminalsize.columns)
    exec_request = ContainerExecRequest(
        command=python_command, terminal_size=terminal_size)

    # コマンドを実行するためのwebsocket URI取得
    execContainerResponse = client.containers.execute_command(
        resource_group_name=RESOURCE_GROUP_NAME,
        container_group_name=container_name,
        container_name=INNER_CONTAINER_NAME,
        container_exec_request=exec_request
    )

    # websocket.connectで接続 & パスワード送信
    ws = websocket.create_connection(execContainerResponse.web_socket_uri)
    ws.send(execContainerResponse.password)
    ws.close()


# BLOBのメタデータを更新する関数
def update_blob_metadata(
    storage_container_name: str,
    blob_name: str,
    new_metadata: dict
):
    blob_service_client = BlobServiceClient.from_connection_string(
        AZURE_STORAGE_CONNECTION_STRING)

    # BlobClientのインスタンスを作成
    blob_client_audio = blob_service_client.get_blob_client(
        container=storage_container_name, blob=blob_name)

    if blob_client_audio.exists():
        # メタデータを辞書で取得
        blob_properties = blob_client_audio.get_blob_properties()
        metadata = blob_properties.metadata

        # statusを更新
        metadata.update(new_metadata)
        blob_client_audio.set_blob_metadata(metadata)


# BLOBの存在を確認する関数
def blob_exists(
    storage_container_name: str,
    blob_name: str
):

    blob_service_client = BlobServiceClient.from_connection_string(
        AZURE_STORAGE_CONNECTION_STRING)
    # BlobClientのインスタンスを作成
    blob_client = blob_service_client.get_blob_client(
        container=storage_container_name, blob=blob_name)

    return blob_client.exists()


# 指定した名前で始まるBLOB名を取得する関数
def list_blob_names(
    storage_container_name: str,
    prefix: str = 'audio/'
):

    blob_service_client = BlobServiceClient.from_connection_string(
        AZURE_STORAGE_CONNECTION_STRING)
    blob_client = blob_service_client.get_container_client(
        storage_container_name)

    # 指定した名前で始まるBLOB名を取得
    blobs = blob_client.list_blobs(name_starts_with=prefix)
    blob_names = []
    for blob in blobs:
        blob_names.append(blob.name)

    return blob_names


# 指定した名前で始まるBLOB名の内同じ元データを有するBLOBセグメントを取得するn
def list_blob_segments(
    storage_container_name: str,
    prefix: str,
    audio_blob_name_for_monitor
):

    blob_service_client = BlobServiceClient.from_connection_string(
        AZURE_STORAGE_CONNECTION_STRING)
    blob_client = blob_service_client.get_container_client(
        storage_container_name)

    # 指定した名前で始まるBLOB名を取得
    blobs = blob_client.list_blobs(name_starts_with=prefix)
    blob_names = []
    for blob in blobs:
        blob_client = blob_service_client.get_blob_client(
            container=storage_container_name, blob=blob)
        metadata = blob_client.get_blob_properties().metadata
        if metadata["audio_blob_name_for_monitor"] == audio_blob_name_for_monitor:
            blob_names.append(blob.name)

    return blob_names


# コンテナの状態を取得する関数
def get_container_state(
    container_group_name
):
    # 認証情報の設定
    credential = DefaultAzureCredential()

    # クライアントの初期化
    client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)
    container = client.container_groups.get(
        RESOURCE_GROUP_NAME, container_group_name).containers[0]

    if container.instance_view != None:
        state = container.instance_view.current_state.state
    else:
        state = "Unknown"

    return state

# 稼働しているコンテナグループ名を取得


def get_running_container_groups(name_prefix="whisper"):
    # 認証情報の設定
    credential = DefaultAzureCredential()

    # クライアントの初期化
    client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)

    # 現在のコンテナーグループを取得
    container_groups = client.container_groups.list_by_resource_group(
        resource_group_name=RESOURCE_GROUP_NAME
    )

    # コンテナグループの状態を確認し、稼働していないコンテナグループ名を取得
    running_container_groups_names = []
    for container_group in container_groups:
        if (name_prefix in container_group.name):
            # コンテナーを取得
            container = client.container_groups.get(
                RESOURCE_GROUP_NAME, container_group.name).containers[0]
            if (container.instance_view != None):
                if container.instance_view.current_state.state == "Running":
                    running_container_groups_names.append(container_group.name)

    return running_container_groups_names

# コンテナインスタンスのログを取得する関数


def get_container_log(
    container_group_name
):
    # 認証情報の設定
    credential = DefaultAzureCredential()

    # クライアントの初期化
    client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)

    log = client.containers.list_logs(
        resource_group_name=RESOURCE_GROUP_NAME,
        container_group_name=container_group_name,
        container_name=INNER_CONTAINER_NAME,
        tail=20
    )

    return log.as_dict()["content"]
