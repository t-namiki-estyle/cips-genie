import prompt
import sys
import os
import pandas as pd

# 親ディレクトリ（projectディレクトリ）をモジュール検索パスに追加
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

# promptモジュールをインポート

prompt_json = prompt.prompt_json_ja["others"]

# while True:
#     _type = type(prompt_json)
#     if _type == dict:
#         key = list(prompt_json.keys())[0]
#         prompt_json = prompt_json[key]
#         print(_type, key)
#     elif _type == list:
#         prompt_json = prompt_json[0]
#         print(_type)
#     else:
#         break

df_ls = []

for key, value in prompt_json.items():
    key = key.split("</svg>")[-1]
    print(key)
    for val in value:
        _ls = [key, val["title"], val["prompt"]]
        df_ls.append(_ls)
        print(_ls)
df = pd.DataFrame(df_ls)
df.columns = ["category", "title", "prompt"]
df.head()
df.to_excel("./work/プロンプト集.xlsx")
