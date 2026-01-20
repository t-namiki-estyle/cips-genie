import logging
import os
import asyncio

from datetime import datetime
from zoneinfo import ZoneInfo


# OSSライブラリ
import openpyxl
import pandas as pd

from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import HttpResponseError

# log
from .kql import merchant_kql


def query_log(query, start_time, end_time) -> pd.DataFrame:
    credential = DefaultAzureCredential()
    client = LogsQueryClient(credential)

    try:
        response = client.query_workspace(
            workspace_id=os.environ.get("LOG_WORKSPACE_ID"),
            query=query,
            timespan=(start_time, end_time)
        )
        if response.status == LogsQueryStatus.PARTIAL:
            error = response.partial_error
            data = response.partial_data
            logging.critical(f"error in query log: {error}")
        elif response.status == LogsQueryStatus.SUCCESS:
            data = response.tables
        for table in data:
            df = pd.DataFrame(data=table.rows, columns=table.columns)
            # 取得内容をとりあえず出力
            logging.debug("Success!")
            logging.debug(f"keys: {df.keys()}")
            return df
    except HttpResponseError as err:
        # print("something fatal happened")
        # print(err)
        logging.critical(f"error in access log: {err}")
        return pd.DataFrame()


def get_unique_upn() -> int:
    """
    Logから現在時刻までの今日のユニークユーザー数を算出する
    値が取れない場合は例外をスロー
    """

    # タイムゾーンの設定
    jst = ZoneInfo('Asia/Tokyo')

    # 本日の0:00~現在まで
    start_time = datetime.now(jst).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end_time = datetime.now(jst)

    df = query_log(merchant_kql, start_time, end_time)
    return int(df["dcount_upn_value"][0])


def create_excel_file(log_df):
    """
    # ログデータを受け取り、ユニークユーザー数と総送信数を集計

    # 音声ファイルアップロード機能は、ミニッツライターの一部のため、処理を個別に行なっていることに注意！

    以下を返す
    unique_calc: ユニークユーザー数を記録したdf
    user_calc: 総送信数を記録したdf
    """
    def format_date(date):
        """
        日にちを、曜日を含む形式に変換して返す
        例: 2024-09-27 -> 9/27（金）
        """
        # Convert to datetime if the input is a valid date string (e.g., "2024-09-27")
        weekdays = ['月', '火', '水', '木', '金', '土', '日']
        try:
            if isinstance(date, str):  # Check if date is a string
                # This will raise an error if it's not a valid date
                date = pd.to_datetime(date, errors='raise')

            # 月、日、曜日を取得
            month = date.month
            day = date.day
            weekday = weekdays[date.weekday()]

            # フォーマットした文字列を返す
            return f"{month}/{day}({weekday})"
        except (ValueError, TypeError):  # Catch cases where it's not a date
            # If it's not a valid date, just return it as is
            return str(date)
    logging.info(f"columns: {log_df.columns}")
    log_df = log_df.drop('プロンプトの内容', axis=1)
    log_df['年月日'] = pd.to_datetime(log_df["実行日時"]).dt.date
    # 日ごとの利用者数を集計するため、実行日時をtimestamp型からdate型へ変換
    log_df['使った機能(音声アップロードをミニッツライターとする)'] = log_df['使った機能']
    log_df['使った機能(音声アップロードをミニッツライターとする)'] = log_df['使った機能(音声アップロードをミニッツライターとする)'].replace(
        {'音声アップロード': 'ミニッツライター'})
    service_columns = ['伊藤忠ChatGPT', 'イントラマスター', 'ミニッツライター', 'ドキュナビゲーター',
                       'Teams-Colleague', 'Mail-Colleague', 'Zoom-Bot', '音声アップロード']
    # ユニークユーザー数の集計
    daily_unique = log_df.drop_duplicates(subset=['年月日', '使った機能', 'UPN'])
    unique_calc = pd.pivot_table(
        daily_unique, index='年月日', columns='使った機能(音声アップロードをミニッツライターとする)', aggfunc='size', fill_value=0)
    unique_calc = unique_calc.reindex(columns=service_columns)
    audio_df = log_df[log_df['使った機能'] == "音声アップロード"]
    unique_users_per_day = audio_df.groupby(
        '年月日')['UPN'].nunique().reset_index(name='unique_user_count')
    audio_unique_count = unique_users_per_day.set_index('年月日')[
        'unique_user_count']
    unique_calc['音声アップロード'] = unique_calc.index.map(
        audio_unique_count).fillna(0)
    # 機能ごとのユニークユーザー数を計算する
    unique_users_per_service = log_df.groupby('使った機能(音声アップロードをミニッツライターとする)')[
        'UPN'].nunique().reindex(service_columns, fill_value=0)
    # Transpose to make services columns
    unique_users_row = pd.DataFrame(unique_users_per_service).T
    unique_users_row.index = ['機能ごとのユニークユーザー数']
    unique_calc = pd.concat([unique_calc, unique_users_row], axis=0)
    unique_calc.loc["機能ごとのユニークユーザー数", "音声アップロード"] = len(
        audio_df['UPN'].unique())
    # 日毎のユニークユーザー数を計算する
    each_day_unique = log_df.drop_duplicates(subset=['年月日', 'UPN'])
    each_day_unique_calc = pd.pivot_table(
        each_day_unique, index='年月日', aggfunc='size', fill_value=0)
    each_day_unique_calc = each_day_unique_calc.rename('日ごとのユニークユーザー数')
    each_day_unique_calc = pd.concat([each_day_unique_calc, pd.Series(
        [log_df['UPN'].nunique()], index=['機能ごとのユニークユーザー数'])])
    unique_calc['日ごとのユニークユーザー数'] = each_day_unique_calc.loc[unique_calc.index].values
    unique_calc.index = unique_calc.index.map(format_date)
    # 総送信数の集計
    user_calc = pd.pivot_table(
        log_df, index='年月日', columns='使った機能(音声アップロードをミニッツライターとする)', aggfunc='size', fill_value=0)
    user_calc = user_calc.reindex(columns=service_columns)
    num_users_per_day = audio_df.groupby('年月日').size()
    user_calc['音声アップロード'] = user_calc.index.map(num_users_per_day).fillna(0)
    total_row = user_calc.sum(numeric_only=True)
    total_row.name = '計'
    user_calc = pd.concat([user_calc, total_row.to_frame().T])
    user_calc['全体'] = user_calc.drop(columns=['音声アップロード']).sum(axis=1)
    user_calc.index = user_calc.index.map(format_date)
    return unique_calc, user_calc
