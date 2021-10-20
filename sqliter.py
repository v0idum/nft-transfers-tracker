import sqlite3


class SQLighter:
    def __init__(self, db_file):
        self.connection = sqlite3.connect(db_file)
        self.cursor = self.connection.cursor()

    def get_tracking_wallets(self, chat_id):
        with self.connection:
            return self.cursor.execute("SELECT name, address FROM 'wallets' where chat_id = ?",
                                       (chat_id,)).fetchall()

    def get_all_wallets(self):
        with self.connection:
            return self.cursor.execute("SELECT address, last_block, chat_id, name FROM 'wallets'").fetchall()

    def add_wallet(self, name, block, address, chat_id):
        with self.connection:
            return self.cursor.execute(
                "INSERT INTO 'wallets' ('name', 'last_block', 'address', 'chat_id') VALUES (?, ?, ?, ?)",
                (name, block, address, chat_id))

    def delete_wallet(self, name, chat_id):
        with self.connection:
            return self.cursor.execute("DELETE FROM 'wallets' WHERE name = ? and chat_id = ?", (name, chat_id))

    def wallet_exists(self, name, chat_id):
        with self.connection:
            result = self.cursor.execute("SELECT * FROM 'wallets' WHERE name = ? and chat_id = ?",
                                         (name, chat_id)).fetchall()
            return bool(len(result))

    def update_block(self, block, address, chat_id):
        with self.connection:
            return self.cursor.execute(
                "UPDATE 'wallets' SET last_block = ? WHERE address = ? and chat_id = ?",
                (block, address, chat_id))

    def close(self):
        self.connection.close()
