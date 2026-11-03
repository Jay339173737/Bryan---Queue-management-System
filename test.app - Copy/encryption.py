import os
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy.types import Text, TypeDecorator

load_dotenv()
_fernet = Fernet(os.environ["FIELD_ENCRYPTION_KEY"].encode())


class EncryptedText(TypeDecorator):
    """Use instead of db.String: encrypts on save, decrypts on read."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return _fernet.encrypt(value.encode()).decode() if value else value

    def process_result_value(self, value, dialect):
        if not value:
            return value
        try:
            return _fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            return value  # old plain-text rows still work
