import os
import logging
from collections.abc import Iterator

import pymssql

from azure.cosmos import CosmosClient, DatabaseProxy


class UserDivisionFetchService:
    """
    userのメニューの閲覧権限を取得するためのクラス
    - CTCさん作成のDBに接続してSG情報の取得を行う

    [構成資料](https://itochu.box.com/s/ad258i54ecthoh78kadacbrhuqenkv24)
    """

    def __init__(self):
        self.authority_db_params = {
            "server": os.environ.get("MENU_AUTHORITY_DB_URL"),
            "port": 1433,
            "database": os.environ.get("MENU_AUTHORITY_DB_NAME"),
            "schema_name": "dbo",
            "table_name": "authority",
            "column_name": "user_principal_name",
            "user": "estyle_svc_readonly_001",
            "password": os.environ.get("MENU_AUTHORITY_DB_PASSWORD")
        }

    def fetch_user_attributes(self, upn: str) -> list:
        """
        SGの情報を取得し、必要な情報に整形する
        ----
        サンプル
            ```python
            user_authority_data = 	[
                (7212, 'A227003@intra.itochu.co.jp', 'R_50001136', 'IT・デジタル戦略部', datetime.datetime(2025, 1, 27, 0, 5, 55, 870000), datetime.datetime(2025, 1, 27, 0, 5, 55, 870000)),
                (16175, 'A227003@intra.itochu.co.jp', 'R_50062460', 'CXO', datetime.datetime(2025, 1, 27, 0, 5, 58, 587000), datetime.datetime(2025, 1, 27, 0, 5, 58, 587000))
            ]

            user_attributes = [
                'A227003@intra.itochu.co.jp',
                'IT・デジタル戦略部',
                'CXO'
            ]
            ```
        """
        user_authority_data = self._fetch_user_authority_data(upn)

        # datetime型やSGのIDをドロップし、ユーザーの所属情報のみ抽出する
        user_attributes = [upn] + [item[3] for item in user_authority_data]

        return user_attributes

    def _fetch_user_authority_data(self, upn: str) -> list:
        """
        upnを用いてSGの情報を取得する
        """
        try:
            with pymssql.connect(
                server=self.authority_db_params["server"],
                user=self.authority_db_params["user"],
                password=self.authority_db_params["password"],
                database=self.authority_db_params["database"],
                port=self.authority_db_params["port"]
            ) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"SELECT * FROM {self.authority_db_params['schema_name']}.{self.authority_db_params['table_name']} WHERE {self.authority_db_params['column_name']} = %s", (upn,))
                    user_authority_data = cursor.fetchall()
                    return user_authority_data

        except pymssql.Error as e:
            logging.error(f"pymssql error occurred: {e}")
            raise

        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            raise


class MenuPermissionService:
    """
    ユーザーの所属情報をもとにCosmosDBに接続してメニューへのアクセス制御情報を取得するクラス
    """

    def __init__(self, cosmos_client: CosmosClient):
        self.client = cosmos_client
        self.cosmos_db_params = {
            "database": "general_auth",
            "containers": {
                "auth": "control",
                "allow": "allow"
            }
        }

    def fetch_menu_permissions(self, user_attributes: list) -> dict:
        """
        CosmosDBにアクセスし、upnと所属情報を用いて、表示可能なメニューの一覧を返す
        - すべてのmenu名とpermission typeを取得
        - CUSTOM typeのmenuのみ、SG情報を用いて許可リスト内に存在するか確認する
        """
        allowed = []
        guide = []
        # メニューの制御の取得
        all_menu = self._fetch_permission_type_list()

        for menu in all_menu:
            resource_id = menu.get("ID")
            permission_type: str = menu.get("permission_type")

            if permission_type == "ALL":
                allowed.append(resource_id)
                continue
            assert permission_type in ("CUSTOM", "GUIDE")

            # 許可リストの確認
            if self._is_allowed(user_attributes, resource_id):
                allowed.append(resource_id)
            elif permission_type == "GUIDE":
                guide.append(resource_id)
            elif permission_type == "CUSTOM":
                pass
            else:
                logging.error(f"invalid permission_type: {permission_type}")

        permissions = {
            "allowed": allowed,
            "guide": guide
        }

        # 参照可能なメニュー一覧を返す
        return permissions

    def _fetch_permission_type_list(self) -> Iterator[dict]:
        """
        menu名とpermission_typeのセットの一覧を取得する
        """
        container_name = self.cosmos_db_params["containers"]["auth"]
        database = self._get_cosmos_db_client()
        container = database.get_container_client(container_name)

        return container.read_all_items()

    def _is_allowed(self, user_attributes: list, resource_id: str) -> bool:
        """
        ユーザーの所属、対象メニュー名を受け取り、対象メニューが許可リストに入っているか確認する
        """
        container_name = self.cosmos_db_params["containers"]["allow"]
        database = self._get_cosmos_db_client()
        container = database.get_container_client(container_name)

        # sqlインジェクション対策をした
        placeholders = ", ".join(
            [f"@attr{i}" for i in range(len(user_attributes))])
        query = f"""
                SELECT *
                FROM c
                WHERE c.allowed_attribute IN ({placeholders})
                ORDER BY c._ts DESC
                OFFSET 0 LIMIT 1
                """

        # 各プレースホルダに対応するパラメータを定義
        parameters = [{"name": f"@attr{i}", "value": attr}
                      for i, attr in enumerate(user_attributes)]

        try:
            result = container.query_items(
                query=query,
                parameters=parameters,
                partition_key=resource_id
            )
            item = next(result)
            logging.debug(f"allowed: {item}")

            return True

        except StopIteration:
            logging.debug("not allowed")
        except Exception as e:
            logging.critical(f"SQL Error: {e}")

        return False

    def _get_cosmos_db_client(self) -> DatabaseProxy:
        """
        CosmosDBのDBプロキシーの取得
        """
        client = self.client

        try:
            database = client.get_database_client(
                database=self.cosmos_db_params["database"])
            logging.debug(f"loaded: {database.id}")
            return database

        except Exception as e:
            logging.error(f"Load DB Error: {e}")
            raise
