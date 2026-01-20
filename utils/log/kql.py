import os
import logging

WEBSITE_SITE_NAME = os.environ.get("WEBSITE_SITE_NAME")
##
# kql
##
logging.info(f"WEBSITE_SITE_NAME: {WEBSITE_SITE_NAME}")
kql = f"""
// tracesを取得
let traces = AppTraces;
// UPNを取得
let upnQuery = traces
| where Message startswith "upn:"
| project upn = substring(Message, 5), OperationId, TimeGenerated;
// ユーザーの入力内容を取得
let userInputQuery = traces
| where Message startswith "User input"
| project user_input = Message, OperationId;
// ミニッツライターのログを取得して結合
let minutesQuery = traces
| where Message == "mode: minutes"
// 汎用履歴機能で（history_session）もmode: minutesが保存されているため、OperationName == "genie"に指定
| where OperationName == "genie"
| project mode = Message, OperationId;
let minutesWriterQuery = minutesQuery
| join kind=inner (upnQuery) on OperationId;
// イントラマスターのログを取得して結合
let insideQuery = traces
| where Message == "mode: inside"
| where OperationName == "genie"
| project mode = Message, OperationId;
let insideModeUpnJoinQuery = insideQuery
| join kind=inner (upnQuery) on OperationId;
let intraMasterQuery = insideModeUpnJoinQuery
| join kind=inner (userInputQuery) on OperationId;
// 伊藤忠Chat GPTのログを取得して結合 (重複を避けるため、メールとTeamsのログと重複するOperation_Idを除外)
let otherQuery = traces
| where Message == "mode: other"
| where OperationName == "genie"
| project mode = Message, OperationId;
let ohterModeUpnJoinQuery = otherQuery
| join kind=inner (upnQuery) on OperationId;
let itochuChatGptQuery = ohterModeUpnJoinQuery
| join kind=inner (userInputQuery) on OperationId
| where OperationId !in ((traces | where Message == "from: mail" or Message == "from: teams") | project OperationId); 
// Teams-Colleagueのログを取得して結合
let teamsQuery = traces
| where Message == "from: teams"
| where OperationName == "genie"
| project mode = Message, OperationId;
let teamsJoinQuery = teamsQuery
| join kind=inner (upnQuery) on OperationId;
let teamsGPTQuery = teamsJoinQuery
| join kind=inner (userInputQuery) on OperationId;
// メールGPTのログを取得して結合
let mailQuery = traces
| where Message == "from: mail"
| where OperationName == "genie"
| project mode = Message, OperationId;
let mailUpnJoinQuery = mailQuery
| join kind=inner (upnQuery) on OperationId;
let mailGPTQuery = mailUpnJoinQuery
| join kind=inner (userInputQuery) on OperationId;
// ドキュナビゲーターのログを取得して結合
let docuNavigaterQuery = traces
| where OperationName == "ocr"
| where Message startswith "upn"
| extend mode = 'ドキュナビゲーター'
| project TimeGenerated, upn = substring(Message,5), mode;
// 音声アップロードのログを取得して結合
let audioData = traces
| where Message contains ("au_audio_upload: User info, upn:")
| extend mode = "音声アップロード"
| extend upn = extract(@"upn:([^,]+)", 1, Message)
| project TimeGenerated, upn, Message, mode, OperationId;
// Zoom-Botのログを取得
let zoomQuery = traces
| where Message contains "start zoom bot: "
| project mode = "Zoom-Bot", OperationId;
let zoomWriterQuery = zoomQuery
| join kind=inner (upnQuery) on OperationId
| project TimeGenerated, upn, mode;
// 全てのログを結合して編集して出力
minutesWriterQuery
| union intraMasterQuery
| union teamsGPTQuery
| union mailGPTQuery
| union itochuChatGptQuery
| union docuNavigaterQuery
| union audioData
| union zoomWriterQuery
// デフォルトのtimestampがUTCなので日本時間に変換
| extend TimeGenerated = datetime_add('hour',9,TimeGenerated)
| extend mode = replace_string(mode, 'mode: minutes', 'ミニッツライター')
| extend mode = replace_string(mode, 'mode: inside', 'イントラマスター')
| extend mode = replace_string(mode, 'from: teams', 'Teams-Colleague')
| extend mode = replace_string(mode, 'from: mail', 'Mail-Colleague')
| extend mode = replace_string(mode, 'mode: other', '伊藤忠ChatGPT')
// 重複を避けるためのdistinct
| distinct TimeGenerated, upn, mode
| where upn !in ('A228395@intra.itochu.co.jp') // エネ化イントラのメアドを除外（死活監視用）
| sort by TimeGenerated desc
"""

merchant_kql = """AppTraces
| where Message startswith ("au_audio_upload: User info, upn:") or Message startswith "upn:"
| where OperationName != "history_session" // 汎用履歴機能（history_session）のログを考慮しないための文（汎用履歴機能でもupnとmodeを集計するため）
| extend upn_value = extract(@"upn:\s*([^,]+)", 1, Message)
| where upn_value !in ('A228395@intra.itochu.co.jp') // エネ化イントラのメアドを除外（死活監視用）
| project upn_value
| summarize dcount(upn_value)"""
