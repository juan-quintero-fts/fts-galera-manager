import os, sqlite3, datetime
DB='/app/data/audit.db'

def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with sqlite3.connect(DB) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, host TEXT, action TEXT, ok INTEGER, detail TEXT)''')

def log(actor, host, action, ok, detail=''):
    init_db()
    with sqlite3.connect(DB) as c:
        c.execute('INSERT INTO audit(ts,actor,host,action,ok,detail) VALUES(?,?,?,?,?,?)', (datetime.datetime.now().isoformat(timespec='seconds'), actor, host, action, 1 if ok else 0, detail[:4000]))

def recent(limit=100):
    init_db()
    with sqlite3.connect(DB) as c:
        c.row_factory=sqlite3.Row
        return c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
