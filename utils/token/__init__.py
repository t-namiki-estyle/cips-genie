from i_style.token import EntraIDTokenManager


def decode_id_token(id_token, keys):
    """
    id_tokenの検証、upn, mail, nameの抽出を行う
    """
    client = EntraIDTokenManager(id_token, keys)
    client.validate_token()

    user_info = client.get_user_info()
    return user_info["upn"], user_info["mail"], user_info["name"]
