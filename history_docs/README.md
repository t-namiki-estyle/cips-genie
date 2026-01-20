# プロンプト履歴のAPIリファレンス

## セッション一覧の取得

`/api/genie/history/{mode}/{upn}?code={key}&id_token={id_token}`

```python
response = requests.get(url)
res_json = response.json()

res_json
```

```python
{'data': [{'date': '2025-03-06T19:39:13.491644+09:00',
   'title': 'コードリファクタリングと共通処理',
   'session_id': 'e79a4b83-3eed-4c51-aa2b-bab1330a5bdd',
   'favorite': 0},
  {'date': '2025-03-06T18:40:20.441358+09:00',
   'title': 'SVG 回転 アニメーション CSS',
   'session_id': '21ea26f4-fdbe-4cbd-b200-5829d3d622cd',
   'favorite': 0},
  {'date': '2025-03-06T15:57:27.423342+09:00',
   'title': 'GitHubのissueとmilesto',
   'session_id': '88683b9f-c74d-4732-8903-fd845238bd14',
   'favorite': 0}]}
```

## セッションの詳細取得

`/api/genie/history/{mode}/{upn}/{session_id}?code={key}&id_token={id_token}`

```python
response = requests.get(url)
response.json()
```

```python
{'data': [{'date': '2025-02-18T10:14:55.244124+09:00',
   'message_id': '2920aea3-ebfe-40f9-95c2-bd5b57618e8e',
   'role': 'user',
   'content': [{'type': 'text',
     'text': 'プルリクエストで...'}]},
  {'date': '2025-02-18T10:14:55.255309+09:00',
   'message_id': 'c9b41689-5d9e-44b6-bb19-e3c4d9dcd66d',
   'role': 'assistant',
   'content': [{'type': 'text',
     'text': '例えば、明確に...'}]},
  {'date': '2025-02-18T10:16:34.594025+09:00',
   'message_id': '2ad3047d-1150-4655-9213-22fca1e2d011',
   'role': 'user',
   'content': [{'type': 'text',
     'text': 'テンプレートが...'}]},
  {'date': '2025-02-18T10:16:34.600451+09:00',
   'message_id': '2b203c0d-a60c-4d5a-a1df-5544e62bffb4',
   'role': 'assistant',
   'content': [{'type': 'text',
     'text': '「CHANGELOG_TEMPLATE.md」という名前は、...'}]}]}
```

## セッションのお気に入り登録・解除

`/api/genie/history/{mode}/{upn}?code={key}&id_token={id_token}`

### 登録

```python
data = {
    "session_id": session_id,
    "favorite": 1
}

response = requests.post(url, json=data)
response.json()
```

```python
{'data': [{'date': '2025-02-18T10:16:34.600451+09:00',
   'title': 'CHANGELOGテンプレートファイル名',
   'session_id': 'ad369e6c-2f10-4f22-917c-d25e8a62a6bc',
   'favorite': 1}]}
```

### 削除

```python
data = {
    "session_id": session_id,
    "favorite": 0
}

response = requests.post(url, json=data)
response.json()
```

```python
{'data': [{'date': '2025-02-18T10:16:34.600451+09:00',
   'title': 'CHANGELOGテンプレートファイル名',
   'session_id': 'ad369e6c-2f10-4f22-917c-d25e8a62a6bc',
   'favorite': 0}]}
```

## セッションの削除

`/api/genie/history/{mode}/{upn}?code={key}&id_token={id_token}`

```python
data = {
    "session_id": session_id,
    "delete_flag": 1
}

response = requests.delete(url, json=data)
response.json()
```

```python
{'data': [{'date': '2025-02-18T10:16:34.600451+09:00',
   'title': 'CHANGELOGテンプレートファイル名',
   'session_id': 'ad369e6c-2f10-4f22-917c-d25e8a62a6bc',
   'favorite': 0,
   'delete_flag': 1}]}
```

## タイトルの編集

`/api/genie/history/{mode}/{upn}?code={key}&id_token={id_token}`

```python
data = {
    "session_id": session_id,
    "title": 'CHANGELOGテンプレート'
}

response = requests.post(url, json=data)
response.json()
```

```python
{'data': [{'date': '2025-03-06T22:28:10.920976+09:00',
   'title': 'CHANGELOGテンプレート',
   'session_id': 'ad369e6c-2f10-4f22-917c-d25e8a62a6bc',
   'favorite': 0}]}
```
