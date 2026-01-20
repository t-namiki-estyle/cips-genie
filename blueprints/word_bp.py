import azure.functions as func
import azure.durable_functions as d_func

import json
import logging
import os
import re
from urllib.parse import quote

from i_style.document_converter import MarkdownDocxConverter

bp = d_func.Blueprint()

GEMINI_IGNORE_SIGN = "<!-- gemini検索結果です。使用しません。 -->"


def remove_gemini_results(markdown_text: str) -> str:
    """
    I-Colleagueメッセージ内のGemini検索結果（GEMINI_IGNORE_SIGN以降）を除外する

    マークダウンは以下の形式を想定:
        **I-Colleague**

        メッセージ内容...
        <!-- gemini検索結果です。使用しません。 -->
        除外したい内容...

        ---
    """
    # メッセージを区切り線(---)で分割
    messages = re.split(r'\n---\n', markdown_text)
    cleaned_messages = []

    for message in messages:
        if GEMINI_IGNORE_SIGN in message:
            # GEMINI_IGNORE_SIGN以降を除外
            message = message.split(GEMINI_IGNORE_SIGN)[0].rstrip()
        cleaned_messages.append(message)

    return '\n---\n'.join(cleaned_messages)


@bp.route(route="genie/md_to_docx", methods=("POST",))
async def convert_markdown_to_docx(req: func.HttpRequest) -> func.HttpResponse:
    """
    Markdownテキストを受け取り、Wordファイル（.docx）を返すHTTP Trigger

    Request:
        {
            "markdown": "# タイトル\n\n本文テキスト...",
            "filename": "output.docx"  // optional
        }

    Response:
        DOCXファイル
    """
    logging.info('convert_markdown_to_docx processed a request.')

    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON in request body"}),
            status_code=400,
            mimetype="application/json"
        )

    markdown_text = req_body.get("markdown")
    logging.info(f"markdown text: {markdown_text}")
    if not markdown_text:
        return func.HttpResponse(
            json.dumps({"error": "Missing 'markdown' field in request body"}),
            status_code=400,
            mimetype="application/json"
        )

    filename = req_body.get("filename", "document.docx")
    _, ext = os.path.splitext(filename)
    if ext.lower() != ".docx":
        filename += ".docx"

    # Gemini検索結果を除外
    markdown_text = remove_gemini_results(markdown_text)

    try:
        docx_bytes = MarkdownDocxConverter.to_bytes(markdown_text)
    except Exception as e:
        logging.error(f"Failed to convert markdown to docx: {e}")
        return func.HttpResponse(
            json.dumps({"error": f"Conversion failed: {str(e)}"}),
            status_code=500,
            mimetype="application/json"
        )

    return func.HttpResponse(
        docx_bytes,
        status_code=200,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        }
    )
