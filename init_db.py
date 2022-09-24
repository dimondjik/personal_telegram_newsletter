import sqlite3

with sqlite3.connect('channel_to_users.db') as db:
    sql_1 = '''CREATE TABLE "channels" 
    (
    "id"    TEXT UNIQUE,
    "username"	TEXT,
    "title"	TEXT,
    "users"	TEXT,
    PRIMARY KEY("id")
    );'''
    sql_2 = '''CREATE TABLE "states" 
    (
    "user"	TEXT UNIQUE,
    "state"	TEXT,
    PRIMARY KEY("user")
    );'''
    cursor = db.cursor()
    cursor.execute(sql_1)
    cursor.execute(sql_2)
