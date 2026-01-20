import pandas as pd
from typing import List, Dict, Union, Tuple
import re
from functools import reduce
import io
import wave


# 採用する区間の単語データフレーム取得
def get_intermediate_words(
    words: List[Dict[str, Union[int, str]]],
    lead_margin: float,
    interval: float
) -> pd.DataFrame:
    """
    args
        words：以下のようなリスト形式
        [{'word': '開', 'start': 17.37, 'end': 17.42},
        {'word': '発', 'start': 17.42, 'end': 17.5}]
    return
        df_words：採用する区間INTERVAL区間の単語データフレーム
    """
    # word, start, endの３つのカラムを有するデータフレーム
    df_words = pd.DataFrame(words)[["word", "start", "end"]]
    df_words = df_words.drop_duplicates(subset=["word", "start"], keep="first")
    df_words = df_words.drop_duplicates(subset=["word", "end"], keep="first")

    df_words = df_words.loc[
        lambda df: (
            (df["start"] >= lead_margin) &
            (df["start"] < lead_margin + interval)
        )
    ]
    return df_words

# 単語出力データフレームから無音区間に該当する単語を除外する関数


def remove_non_audio_intervel_from_df_words(
    df_words: pd.DataFrame,
    non_audio_intervals: List[Tuple[float, float]]
) -> pd.DataFrame:
    """
    args
        df_words:単語と開始/終了それぞれのタイムスタンプを含むデータフレーム
        non_audio_intervals:無音区間をタプルとして格納したリスト
    return
        df_words_vad:無音区間に該当する単語を除外したデータフレーム
    """

    df_words_vad = df_words.copy()

    # 無音区間がない場合(そのまま返す)
    if len(non_audio_intervals) == 0:
        return df_words_vad
    else:
        # startが無音区間に含まれる場合は単語を除外する
        conditions = [
            f"(start < {start} | start > {end})" for start, end in non_audio_intervals]
        combined_condition = reduce(lambda a, b: f"{a} & {b}", conditions)
        df_words_vad = df_words_vad.query(combined_condition)

    return df_words_vad


# hullucinationを除去する関数
def remove_hallucination(df, margin=0.5):
    """
    args
        df:単語と開始/終了それぞれのタイムスタンプを含むデータフレーム
        margin:重複単語のスタート位置を中心にhallucinationの単語を除外する範囲
    return
        df:単語と開始/終了それぞれのタイムスタンプを含むデータフレーム
    """

    # word, start, endが重複しているデータ
    df_duplicates = df[df.duplicated(keep=False, subset=["start", "end"])].drop_duplicates(
        keep="first"
    )

    # 重複単語の削除
    df = df.drop_duplicates(subset=["word", "start"], keep=False)
    df = df.drop_duplicates(subset=["word", "end"], keep=False)
    df = df.drop_duplicates(subset=["start", "end"], keep=False)

    if len(df_duplicates) == 0:
        return df

    # 重複している単語(hallucination)
    duplicated_words = df_duplicates["word"].unique()

    # hallucinationスタート位置
    hallucination_starts = df_duplicates["start"].unique()

    # スタート位置を中心にhallucinationの単語を除外する
    for hallucination_start in hallucination_starts:
        df = df.loc[
            lambda df: ~(
                (df["start"] < hallucination_start + margin) &
                (df["start"] > hallucination_start - margin) &
                (df["word"].isin(duplicated_words))
            )
        ]

    return df


# 特定の文字列の3回以上の繰り返しを削除
def remove_repeated_words(text):
    # 文字列とパターンを定義
    pattern = r"((.+?)\2{2,})"
    text = re.sub(pattern, "", text)

    return text


# PCMデータをWAV形式に変換する
def pcm_to_wav(pcm_data: bytes) -> bytes:
    """
    Args:
        pcm_data (bytes): PCM形式の音声データ
    Returns:
        bytes: WAV形式の音声データ

    チャネル数、ビット深度、サンプルレートはZoomで録音された音声ファイルの技術仕様に合わせて設定
    """
    with io.BytesIO() as wav_buffer:
        with wave.open(wav_buffer, 'wb') as wf:
            wf.setnchannels(1)  # モノラル
            wf.setsampwidth(2)  # 16ビット = 2バイト
            wf.setframerate(32000)  # 32kHz
            wf.writeframes(pcm_data)
        return wav_buffer.getvalue()
