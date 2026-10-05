import json, os, sqlite3, threading, time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(os.environ.get('DATA_DIR', HERE), 'data.db')
STATE = {'provider_up': True}
# Autenticación simulada: la cabecera X-User elige el usuario. En producción sería JWT/sesión.
USERS = {
    'ana': ('paciente', 'Ana Torres', None),
    'luis': ('paciente', 'Luis Vera', None),
    'recepcion': ('recepcion', 'Recepción', None),
    'perez': ('medico', 'Dr. Pérez', 'Dr. Pérez'),
    'salas': ('medico', 'Dra. Salas', 'Dra. Salas'),
}
DOCTORS = ['Dr. Pérez', 'Dra. Salas']
SLOTS = ['%02d:%02d' % (h, m) for h in range(8, 12) for m in (0, 30)]
BACKOFF_MIN = [5, 30, 120]

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def audit(c, uid, role, event, detail):
    c.execute('INSERT INTO audit(ts,actor,role,event,detail) VALUES(?,?,?,?,?)', (now(), uid, role, event, detail))

def init():
    c = db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, doctor TEXT, date TEXT, time TEXT, patient_user TEXT, patient_name TEXT, document TEXT, contact TEXT, status TEXT, created TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_slot ON appointments(doctor,date,time) WHERE status='ACTIVA';
    CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY, appt_id INTEGER, status TEXT, tries INTEGER, next_try REAL, last_error TEXT);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, role TEXT, event TEXT, detail TEXT);''')
    c.commit()
    c.close()

def rows(sql, args=()):
    c = db()
    out = [dict(x) for x in c.execute(sql, args).fetchall()]
    c.close()
    return out

def slots(doctor, date):
    taken = {r['time'] for r in rows("SELECT time FROM appointments WHERE doctor=? AND date=? AND status='ACTIVA'", (doctor, date))}
    return [{'time': t, 'free': t not in taken} for t in SLOTS]

def book(user, b):
    uid, role, name, _ = user
    if role == 'medico':
        return 403, {'error': 'El médico no puede crear citas'}
    doctor, date, tm = b.get('doctor'), b.get('date'), b.get('time')
    if doctor not in DOCTORS or tm not in SLOTS:
        return 400, {'error': 'médico u horario inválido'}
    try:
        datetime.strptime(date or '', '%Y-%m-%d')
    except ValueError:
        return 400, {'error': 'fecha inválida'}
    contact = (b.get('contact') or '').strip()
    if not contact:
        return 400, {'error': 'falta el contacto (correo o teléfono)'}
    if role == 'paciente':
        pname, puser = name, uid
    else:
        pname, puser = (b.get('patient_name') or '').strip(), None
        if not pname:
            return 400, {'error': 'falta el nombre del paciente'}
    c = db()
    try:
        try:
            cur = c.execute('INSERT INTO appointments(doctor,date,time,patient_user,patient_name,document,contact,status,created) VALUES(?,?,?,?,?,?,?,?,?)',
                            (doctor, date, tm, puser, pname, (b.get('document') or '').strip(), contact, 'ACTIVA', now()))
        except sqlite3.IntegrityError:
            audit(c, uid, role, 'RESERVA_RECHAZADA', f'{doctor} {date} {tm} ya ocupado')
            c.commit()
            return 409, {'error': 'Ese horario ya fue reservado. Elige otro.'}
        aid = cur.lastrowid
        c.execute('INSERT INTO reminders(appt_id,status,tries,next_try) VALUES(?,?,?,?)', (aid, 'PENDIENTE', 0, time.time()))
        audit(c, uid, role, 'RESERVA', f'#{aid} {doctor} {date} {tm} paciente={pname}')
        c.commit()
        return 201, {'id': aid}
    finally:
        c.close()

def list_appts(user):
    uid, role, name, doc = user
    if role == 'paciente':
        return 200, rows('SELECT * FROM appointments WHERE patient_user=? ORDER BY date,time', (uid,))
    if role == 'medico':  # el médico ve solo su agenda y sin datos de contacto ni documento
        return 200, rows('SELECT id,doctor,date,time,patient_name,status FROM appointments WHERE doctor=? ORDER BY date,time', (doc,))
    return 200, rows('SELECT * FROM appointments ORDER BY date,time')

def change(user, aid, action):
    uid, role, name, doc = user
    c = db()
    try:
        a = c.execute('SELECT * FROM appointments WHERE id=?', (aid,)).fetchone()
        if not a:
            return 404, {'error': 'no existe'}
        if action == 'cancel':
            if role == 'medico' or (role == 'paciente' and a['patient_user'] != uid):
                audit(c, uid, role, 'ACCESO_DENEGADO', f'cancelar #{aid}')
                c.commit()
                return 403, {'error': 'No tienes permiso para cancelar esta cita'}
            new = 'CANCELADA'
        else:  # noshow
            if role == 'paciente' or (role == 'medico' and a['doctor'] != doc):
                return 403, {'error': 'No tienes permiso'}
            new = 'AUSENTE'
        if a['status'] != 'ACTIVA':
            return 409, {'error': 'la cita ya no está activa'}
        c.execute('UPDATE appointments SET status=? WHERE id=?', (new, aid))
        c.execute("UPDATE reminders SET status='CANCELADO' WHERE appt_id=? AND status='PENDIENTE'", (aid,))
        audit(c, uid, role, new, f"#{aid} {a['doctor']} {a['date']} {a['time']}")
        c.commit()
        return 200, {'status': new}
    finally:
        c.close()

def process(user):
    uid, role, _, _ = user
    if role != 'recepcion':
        return 403, {'error': 'solo recepción/worker puede procesar la cola'}
    res = {'enviados': 0, 'reintentos': 0, 'fallidos': 0}
    c = db()
    try:
        for r in c.execute("SELECT * FROM reminders WHERE status='PENDIENTE'").fetchall():
            if STATE['provider_up']:
                c.execute("UPDATE reminders SET status='ENVIADO',last_error=NULL WHERE id=?", (r['id'],))
                audit(c, 'worker', 'sistema', 'RECORDATORIO_ENVIADO', f"cita #{r['appt_id']}")
                res['enviados'] += 1
            else:
                t = r['tries'] + 1
                if t >= len(BACKOFF_MIN):
                    c.execute("UPDATE reminders SET status='FALLIDO',tries=?,last_error='proveedor caído' WHERE id=?", (t, r['id']))
                    audit(c, 'worker', 'sistema', 'RECORDATORIO_FALLIDO', f"cita #{r['appt_id']} tras {t} intentos → alerta a recepción para llamar")
                    res['fallidos'] += 1
                else:
                    c.execute("UPDATE reminders SET tries=?,next_try=?,last_error='proveedor caído' WHERE id=?", (t, time.time() + BACKOFF_MIN[t - 1] * 60, r['id']))
                    audit(c, 'worker', 'sistema', 'RECORDATORIO_REINTENTO', f"cita #{r['appt_id']} intento {t}; próximo en {BACKOFF_MIN[t - 1]} min")
                    res['reintentos'] += 1
        c.commit()
    finally:
        c.close()
    return 200, res

def metrics():
    A = rows('SELECT status FROM appointments')
    R = rows('SELECT status FROM reminders')
    ok = len([r for r in R if r['status'] == 'ENVIADO'])
    done = len([r for r in R if r['status'] in ('ENVIADO', 'FALLIDO')])
    c = db()
    conflicts = c.execute("SELECT COUNT(*) FROM audit WHERE event='RESERVA_RECHAZADA'").fetchone()[0]
    dup = c.execute("SELECT COUNT(*) FROM (SELECT 1 FROM appointments WHERE status='ACTIVA' GROUP BY doctor,date,time HAVING COUNT(*)>1)").fetchone()[0]
    c.close()
    return {
        'citas_activas': len([a for a in A if a['status'] == 'ACTIVA']),
        'citas_canceladas': len([a for a in A if a['status'] == 'CANCELADA']),
        'ausencias': len([a for a in A if a['status'] == 'AUSENTE']),
        'citas_duplicadas': dup,
        'conflictos_bloqueados': conflicts,
        'recordatorios_entregados_pct': round(100 * ok / done) if done else None,
    }

def route(method, url, body, headers):
    u = urlparse(url)
    q = parse_qs(u.query)
    p = [x for x in u.path.split('/') if x][1:]
    if method == 'GET' and p == ['health']:
        return 200, {'ok': True}
    uid = headers.get('X-User', 'ana')
    if uid not in USERS:
        return 401, {'error': 'usuario desconocido'}
    role, name, doc = USERS[uid]
    user = (uid, role, name, doc)
    if method == 'GET' and p == ['me']:
        return 200, {'user': uid, 'role': role, 'name': name}
    if method == 'GET' and p == ['doctors']:
        return 200, DOCTORS
    if method == 'GET' and p == ['slots']:
        return 200, slots(q.get('doctor', [DOCTORS[0]])[0], q.get('date', [''])[0])
    if method == 'POST' and p == ['appointments']:
        return book(user, body)
    if method == 'GET' and p == ['appointments']:
        return list_appts(user)
    if method == 'POST' and len(p) == 3 and p[0] == 'appointments' and p[2] in ('cancel', 'noshow'):
        return change(user, int(p[1]), p[2])
    if method == 'POST' and p == ['provider']:
        STATE['provider_up'] = bool(body.get('up'))
        return 200, {'provider_up': STATE['provider_up']}
    if method == 'GET' and p == ['provider']:
        return 200, {'provider_up': STATE['provider_up']}
    if method == 'POST' and p == ['reminders', 'process']:
        return process(user)
    if method == 'GET' and p == ['reminders']:
        if role != 'recepcion':
            return 403, {'error': 'solo recepción'}
        return 200, rows('SELECT r.id,r.appt_id,r.status,r.tries,r.next_try,r.last_error,a.doctor,a.date,a.time,a.patient_name FROM reminders r JOIN appointments a ON a.id=r.appt_id ORDER BY r.id DESC LIMIT 40')
    if method == 'GET' and p == ['audit']:
        if role != 'recepcion':
            return 403, {'error': 'solo recepción'}
        return 200, rows('SELECT * FROM audit ORDER BY id DESC LIMIT 80')
    if method == 'GET' and p == ['metrics']:
        return 200, metrics()
    return 404, {'error': 'ruta no encontrada'}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def reply(self, code, obj, ctype='application/json'):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def handle_any(self, method):
        if method == 'GET' and self.path in ('/', '/index.html'):
            return self.reply(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html')
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        try:
            code, obj = route(method, self.path, body, self.headers)
        except Exception as e:
            code, obj = 500, {'error': str(e)}
        self.reply(code, obj)
    def do_GET(self): self.handle_any('GET')
    def do_POST(self): self.handle_any('POST')

if __name__ == '__main__':
    init()
    print('Servidor en http://localhost:8000', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8000), H).serve_forever()
