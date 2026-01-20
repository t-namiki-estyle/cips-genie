import os
import psycopg2
import logging


class AuthorityVerification:
    def __init__(self, upn: str):
        self.upn = upn
        self.authority_db_params = {
            "host": os.environ.get("AUTHORITY_DB_URL"),
            "port": "5432",
            "db_name": os.environ.get("AUTHORITY_DB_NAME"),
            "schema_name": "company_spo_authority",
            "table_name": "sites",
            "column_name": "user_principal_name",
            "user": "estyleuser",
            "password": os.environ.get("AUTHORITY_DB_PASSWORD")
        }
        self.user_authority_data = self._fetch_user_authority_data()

    def verify_eneka_authority(self) -> bool:
        try:
            return self._verify_user_authority('company-eneka')
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            return False

    def verify_kikai_authority(self) -> bool:
        try:
            return self._verify_user_authority('company-kikai')
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            return False

    def verify_food_authority(self) -> bool:
        try:
            return self._verify_user_authority('company-food')
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            return False

    def verify_jyuseikatsu_authority(self) -> bool:
        try:
            return self._verify_user_authority('company-jyuseikatsu')
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            return False

    def verify_kinzoku_authority(self) -> bool:
        try:
            return self._verify_user_authority('company-kinzoku')
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            return False

    def verify_tex_authority(self) -> bool:
        try:
            return self._verify_user_authority('company-tex')
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            return False

    def verify_joukin_authority(self) -> bool:
        try:
            return self._verify_user_authority('company-joukin')
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            return False

    def _fetch_user_authority_data(self) -> list:
        conn = None
        try:
            conn = psycopg2.connect(
                dbname=self.authority_db_params["db_name"],
                user=self.authority_db_params["user"],
                password=self.authority_db_params["password"],
                host=self.authority_db_params["host"],
                port=self.authority_db_params["port"],
                sslmode='require'
            )

            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {self.authority_db_params['schema_name']}.{self.authority_db_params['table_name']} WHERE {self.authority_db_params['column_name']} = %s", (self.upn,))
                user_authority_data = cur.fetchall()

            return user_authority_data

        except psycopg2.DatabaseError as e:
            logging.error(f"Database error occurred: {e}")
            return []
        except KeyError as e:
            logging.error(f"Key error: {e}")
            return []
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            return []
        finally:
            if conn is not None:
                conn.close()

    def _verify_user_authority(self, company: str) -> bool:
        return any([True for site_data in self.user_authority_data if company in site_data])
