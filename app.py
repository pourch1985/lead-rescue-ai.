from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session, g
import sqlite3, os, secrets, json, urllib.request, urllib.parse, base64, hashlib, hmac
from cryptography.fernet import Fernet
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('DATABASE_PATH', os.path.join(APP_DIR, 'lead_rescue.db'))
DATABASE_URL = os.environ.get('DATABASE_URL','').strip()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'lead-rescue-dev-key-change-me')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE','0')=='1')

STATUSES = ['New', 'Contacted', 'Replied', 'Booked', 'Won', 'Lost']
SOURCES = ['Website', 'Meta Ads', 'Google Ads', 'Referral', 'Manual', 'Webhook', 'Other']
PLANS = {
    'starter': {'name': 'Starter', 'price': 49, 'lead_limit': 500},
    'growth': {'name': 'Growth', 'price': 99, 'lead_limit': 3000},
    'pro': {'name': 'Pro', 'price': 199, 'lead_limit': 10000},
}


def now_iso():
    return datetime.now().isoformat(timespec='minutes')


class DBConnection:
    def __init__(self):
        self.is_postgres = DATABASE_URL.startswith(('postgres://','postgresql://'))
        if self.is_postgres:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError('DATABASE_URL is PostgreSQL but psycopg is not installed') from exc
            self.conn = psycopg.connect(DATABASE_URL.replace('postgres://','postgresql://',1), row_factory=dict_row)
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute('PRAGMA foreign_keys=ON')
    def _sql(self, sql):
        return sql.replace('?', '%s') if self.is_postgres else sql
    def execute(self, sql, params=()):
        return self.conn.execute(self._sql(sql), params)
    def executescript(self, script):
        if self.is_postgres:
            for statement in script.split(';'):
                if statement.strip(): self.conn.execute(statement)
        else:
            return self.conn.executescript(script)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()

def db():
    return DBConnection()

def insert_id(conn, sql, params):
    if conn.is_postgres:
        return conn.execute(sql.rstrip().rstrip(';') + ' RETURNING id', params).fetchone()['id']
    return conn.execute(sql, params).lastrowid


def init_db():
    conn = db()
    id_col = 'SERIAL PRIMARY KEY' if conn.is_postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    schema = '''
    CREATE TABLE IF NOT EXISTS businesses (
        id {ID_COL},
        name TEXT NOT NULL,
        plan TEXT DEFAULT 'starter',
        subscription_status TEXT DEFAULT 'trialing',
        trial_ends_at TEXT,
        stripe_customer_id TEXT,
        stripe_subscription_id TEXT,
        webhook_key TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
        id {ID_COL},
        business_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'owner',
        created_at TEXT NOT NULL,
        FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS user_security (
        user_id INTEGER PRIMARY KEY,
        email_verified_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS stripe_events (
        id {ID_COL},
        event_id TEXT UNIQUE NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT,
        processed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS leads (
        id {ID_COL},
        business_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        company TEXT,
        source TEXT DEFAULT 'Manual',
        status TEXT DEFAULT 'New',
        value REAL DEFAULT 0,
        score INTEGER DEFAULT 50,
        consent_email INTEGER DEFAULT 1,
        consent_sms INTEGER DEFAULT 0,
        opted_out INTEGER DEFAULT 0,
        notes TEXT,
        created_at TEXT NOT NULL,
        last_contacted_at TEXT,
        next_followup_at TEXT,
        FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS activities (
        id {ID_COL},
        business_id INTEGER NOT NULL,
        lead_id INTEGER NOT NULL,
        activity_type TEXT NOT NULL,
        content TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
        FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS settings (
        business_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY(business_id, key),
        FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS sequences (
        id {ID_COL},
        business_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        channel TEXT DEFAULT 'Email',
        step_number INTEGER NOT NULL,
        delay_hours INTEGER DEFAULT 24,
        intent TEXT DEFAULT 'followup',
        enabled INTEGER DEFAULT 1,
        FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS integrations (
        id {ID_COL},
        business_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        account_id TEXT,
        display_name TEXT,
        credentials_enc TEXT,
        enabled INTEGER DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(business_id, provider, account_id),
        FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS inbound_events (
        id {ID_COL},
        business_id INTEGER,
        provider TEXT NOT NULL,
        external_id TEXT,
        payload TEXT,
        status TEXT DEFAULT 'received',
        error TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(provider, external_id),
        FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS appointments (
        id {ID_COL},
        business_id INTEGER NOT NULL,
        lead_id INTEGER,
        title TEXT NOT NULL,
        starts_at TEXT NOT NULL,
        ends_at TEXT NOT NULL,
        timezone TEXT DEFAULT 'America/New_York',
        status TEXT DEFAULT 'confirmed',
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
        FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE SET NULL
    );
    CREATE INDEX IF NOT EXISTS idx_leads_business ON leads(business_id);
    CREATE INDEX IF NOT EXISTS idx_leads_followup ON leads(business_id,next_followup_at);
    CREATE INDEX IF NOT EXISTS idx_activities_lead ON activities(business_id,lead_id);
    CREATE INDEX IF NOT EXISTS idx_integrations_provider ON integrations(provider,account_id);
    CREATE INDEX IF NOT EXISTS idx_appointments_business ON appointments(business_id,starts_at);
    '''.replace('{ID_COL}', id_col)
    conn.executescript(schema)
    conn.commit(); conn.close()


def seed_business_defaults(conn, business_id):
    defaults = {
        'business_name': 'My Business',
        'brand_voice': 'Professional, warm, concise, helpful',
        'booking_link': '',
        'followup_delay_hours': '24',
        'auto_followup_enabled': '1',
        'openai_model': 'gpt-4.1-mini',
        'sender_name': '',
        'sender_email': '',
        'timezone': 'America/New_York',
        'appointment_duration_minutes': '30',
        'onboarding_completed': '0',
        'industry': '',
        'business_goal': 'Book more qualified appointments',
    }
    for k, v in defaults.items():
        conn.execute('INSERT INTO settings(business_id,key,value) VALUES (?,?,?) ON CONFLICT(business_id,key) DO NOTHING', (business_id, k, v))
    count = conn.execute('SELECT COUNT(*) c FROM sequences WHERE business_id=?', (business_id,)).fetchone()['c']
    if not count:
        for step, delay, intent in [(1,0,'initial'), (2,24,'followup'), (3,72,'followup'), (4,168,'reactivate')]:
            conn.execute('INSERT INTO sequences(business_id,name,channel,step_number,delay_hours,intent,enabled) VALUES (?,?,?,?,?,?,1)',
                         (business_id, 'Default Rescue Sequence', 'Email', step, delay, intent))


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for('login', next=request.path))
        return fn(*args, **kwargs)
    return wrapped


@app.before_request
def load_user():
    g.user = None
    g.business = None
    uid = session.get('user_id')
    if uid:
        conn = db()
        g.user = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if g.user:
            g.business = conn.execute('SELECT * FROM businesses WHERE id=?', (g.user['business_id'],)).fetchone()
        conn.close()


def business_id():
    return g.user['business_id']


def get_settings(bid=None):
    bid = bid or business_id()
    conn = db(); rows = conn.execute('SELECT key,value FROM settings WHERE business_id=?', (bid,)).fetchall(); conn.close()
    return {r['key']: r['value'] for r in rows}


def lead_or_404(lead_id):
    conn = db(); lead = conn.execute('SELECT * FROM leads WHERE id=? AND business_id=?', (lead_id, business_id())).fetchone(); conn.close()
    return lead


def fallback_reply(lead, intent, settings):
    first = (lead['name'] or 'there').split()[0]
    booking = settings.get('booking_link','').strip()
    if intent == 'initial':
        msg = f"Hi {first}, thanks for reaching out. I wanted to make sure you got a quick response. What can we help you with today?"
    elif intent == 'reactivate':
        msg = f"Hi {first}, checking back in in case this is still something you want help with. If the timing is better now, I’m happy to pick this back up with you."
    else:
        msg = f"Hi {first}, just following up so your request doesn’t get lost. Are you still interested in moving forward?"
    if booking:
        msg += f" You can choose a time here: {booking}"
    return msg


def real_ai_reply(lead, intent='followup'):
    settings = get_settings()
    api_key = os.environ.get('OPENAI_API_KEY','').strip()
    if not api_key:
        return fallback_reply(lead, intent, settings), 'template'

    prompt = f"""You are the lead recovery assistant for {settings.get('business_name','this business')}.
Write one concise {intent} message for a sales lead. Brand voice: {settings.get('brand_voice')}.
Lead name: {lead['name']}. Company: {lead['company'] or 'unknown'}. Source: {lead['source']}.
Notes: {lead['notes'] or 'none'}. Booking link: {settings.get('booking_link') or 'none'}.
Do not invent discounts or claims. End with one easy question or CTA. Return only the message."""
    payload = json.dumps({
        'model': settings.get('openai_model') or 'gpt-4.1-mini',
        'input': prompt,
        'max_output_tokens': 220,
    }).encode('utf-8')
    req = urllib.request.Request('https://api.openai.com/v1/responses', data=payload, method='POST', headers={
        'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        # Responses API commonly exposes output_text; otherwise inspect output content.
        text = data.get('output_text')
        if not text:
            chunks=[]
            for item in data.get('output',[]):
                for content in item.get('content',[]):
                    if content.get('type') in ('output_text','text') and content.get('text'):
                        chunks.append(content['text'])
            text='\n'.join(chunks).strip()
        return (text or fallback_reply(lead,intent,settings)), 'openai'
    except Exception:
        return fallback_reply(lead, intent, settings), 'template_fallback'




def send_email_message(to_email, subject, text, settings):
    api_key = os.environ.get('RESEND_API_KEY','').strip()
    sender = settings.get('sender_email','').strip()
    sender_name = settings.get('sender_name','').strip() or settings.get('business_name','Lead Rescue AI')
    if not api_key or not sender or not to_email:
        return False, 'Resend is not configured or the lead has no email.'
    payload = json.dumps({
        'from': f'{sender_name} <{sender}>',
        'to': [to_email],
        'subject': subject,
        'text': text,
    }).encode('utf-8')
    req = urllib.request.Request('https://api.resend.com/emails', data=payload, method='POST', headers={
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'User-Agent': 'Lead-Rescue-AI/1.0',
        'Idempotency-Key': secrets.token_hex(16),
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return True, data.get('id','sent')
    except Exception as exc:
        return False, f'Email provider error: {type(exc).__name__}'


def send_sms_message(to_phone, text):
    sid = os.environ.get('TWILIO_ACCOUNT_SID','').strip()
    token = os.environ.get('TWILIO_AUTH_TOKEN','').strip()
    from_phone = os.environ.get('TWILIO_FROM_NUMBER','').strip()
    if not sid or not token or not from_phone or not to_phone:
        return False, 'Twilio is not configured or the lead has no phone.'
    url = f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json'
    body = urllib.parse.urlencode({'To':to_phone,'From':from_phone,'Body':text}).encode('utf-8')
    auth = base64.b64encode(f'{sid}:{token}'.encode()).decode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Authorization': f'Basic {auth}',
        'Content-Type': 'application/x-www-form-urlencoded',
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return True, data.get('sid','queued')
    except Exception as exc:
        return False, f'SMS provider error: {type(exc).__name__}'


def record_sent_message(conn, bid, lead_id, channel, content, provider_id=None):
    settings = get_settings(bid)
    delay = int(settings.get('followup_delay_hours','24') or 24)
    sent_at = now_iso()
    next_fu = (datetime.now()+timedelta(hours=delay)).isoformat(timespec='minutes')
    conn.execute("UPDATE leads SET last_contacted_at=?,next_followup_at=?,status=CASE WHEN status='New' THEN 'Contacted' ELSE status END WHERE id=? AND business_id=?", (sent_at,next_fu,lead_id,bid))
    meta = f' · provider id {provider_id}' if provider_id else ''
    conn.execute('INSERT INTO activities(business_id,lead_id,activity_type,content,created_at) VALUES (?,?,?,?,?)', (bid,lead_id,f'{channel.lower()}_sent',content+meta,sent_at))

def compute_score(name, email, phone, company, source, value, notes):
    score = 35
    if email: score += 12
    if phone: score += 12
    if company: score += 8
    if source in ('Referral','Website','Meta Ads','Google Ads'): score += 8
    if value >= 500: score += 10
    if value >= 2000: score += 8
    if notes and len(notes.strip()) > 20: score += 7
    return min(score, 100)


def _fernet():
    secret = app.secret_key.encode('utf-8')
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_json(data):
    return _fernet().encrypt(json.dumps(data).encode('utf-8')).decode('utf-8')


def decrypt_json(token):
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode('utf-8')).decode('utf-8'))
    except Exception:
        return {}


def capture_lead_for_business(conn, bid, data, source='Webhook', activity_type='webhook_capture'):
    settings = {r['key']: r['value'] for r in conn.execute('SELECT key,value FROM settings WHERE business_id=?', (bid,)).fetchall()}
    delay = int(settings.get('followup_delay_hours', '24') or 24)
    now = now_iso()
    next_fu = (datetime.now() + timedelta(hours=delay)).isoformat(timespec='minutes') if settings.get('auto_followup_enabled') == '1' else None
    name = str(data.get('name') or data.get('full_name') or 'New Lead').strip()
    email = str(data.get('email') or '').strip()
    phone = str(data.get('phone') or data.get('phone_number') or '').strip()
    company = str(data.get('company') or data.get('company_name') or '').strip()
    try:
        value = float(data.get('value') or 0)
    except Exception:
        value = 0
    notes = str(data.get('notes') or '').strip()
    score = compute_score(name, email, phone, company, source, value, notes)
    lid = insert_id(conn,
        """INSERT INTO leads(business_id,name,email,phone,company,source,status,value,score,consent_email,consent_sms,notes,created_at,next_followup_at)
        VALUES (?,?,?,?,?,?, 'New',?,?,?,?,?,?,?)""",
        (bid, name, email, phone, company, source, value, score,
         1 if data.get('consent_email', True) else 0,
         1 if data.get('consent_sms', False) else 0,
         notes, now, next_fu)
    )
    conn.execute(
        'INSERT INTO activities(business_id,lead_id,activity_type,content,created_at) VALUES (?,?,?,?,?)',
        (bid, lid, activity_type, f'Lead captured from {source} · score {score}', now)
    )
    return lid, score


def meta_signature_valid(raw_body):
    app_secret = os.environ.get('META_APP_SECRET', '').strip()
    signature = request.headers.get('X-Hub-Signature-256', '')
    if not app_secret:
        return True
    if not signature.startswith('sha256='):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


def meta_fetch_lead(leadgen_id, token):
    version = os.environ.get('META_GRAPH_VERSION', 'v25.0').strip() or 'v25.0'
    url = f"https://graph.facebook.com/{version}/{urllib.parse.quote(str(leadgen_id))}?" + urllib.parse.urlencode({'access_token': token})
    req = urllib.request.Request(url, headers={'User-Agent': 'Lead-Rescue-AI/2.0'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = json.loads(resp.read().decode('utf-8'))
    fields = {}
    for item in raw.get('field_data', []):
        values = item.get('values') or []
        fields[item.get('name', '')] = values[0] if values else ''
    return {
        'name': fields.get('full_name') or fields.get('name') or 'Meta Lead',
        'email': fields.get('email', ''),
        'phone': fields.get('phone_number') or fields.get('phone') or '',
        'company': fields.get('company_name') or fields.get('company') or '',
        'notes': 'Meta Lead Ads form submission',
        'consent_email': True,
        'consent_sms': False,
    }, raw


def automation_process_business(bid, max_items=25):
    settings = get_settings(bid)
    conn = db()
    due = conn.execute(
        "SELECT * FROM leads WHERE business_id=? AND opted_out=0 AND next_followup_at IS NOT NULL AND next_followup_at<=? AND status NOT IN ('Won','Lost') ORDER BY next_followup_at LIMIT ?",
        (bid, now_iso(), max_items)
    ).fetchall()
    sent = 0
    skipped = 0
    for lead in due:
        reply, _ = real_ai_reply_for_business(lead, bid, 'followup')
        if lead['consent_email'] and lead['email'] and os.environ.get('RESEND_API_KEY') and settings.get('sender_email'):
            ok, result = send_email_message(lead['email'], 'Quick follow-up', reply, settings)
            channel = 'Email'
        elif lead['consent_sms'] and lead['phone'] and os.environ.get('TWILIO_ACCOUNT_SID'):
            ok, result = send_sms_message(lead['phone'], reply)
            channel = 'SMS'
        else:
            ok = False
            result = 'No configured permitted channel'
            channel = 'Automation'
        if ok:
            record_sent_message(conn, bid, lead['id'], channel, reply, result)
            sent += 1
        else:
            skipped += 1
    conn.commit()
    conn.close()
    return {'processed': len(due), 'sent': sent, 'skipped': skipped}


def real_ai_reply_for_business(lead, bid, intent='followup'):
    settings = get_settings(bid)
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not api_key:
        return fallback_reply(lead, intent, settings), 'template'
    prompt = f"""You are the lead recovery assistant for {settings.get('business_name','this business')}.
Write one concise {intent} message for a sales lead. Brand voice: {settings.get('brand_voice')}.
Lead name: {lead['name']}. Company: {lead['company'] or 'unknown'}. Source: {lead['source']}.
Notes: {lead['notes'] or 'none'}. Booking link: {settings.get('booking_link') or 'none'}.
Do not invent discounts or claims. End with one easy question or CTA. Return only the message."""
    payload = json.dumps({
        'model': settings.get('openai_model') or 'gpt-4.1-mini',
        'input': prompt,
        'max_output_tokens': 220,
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.openai.com/v1/responses', data=payload, method='POST',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        text = data.get('output_text')
        if not text:
            chunks = []
            for item in data.get('output', []):
                for content in item.get('content', []):
                    if content.get('type') in ('output_text', 'text') and content.get('text'):
                        chunks.append(content['text'])
            text = '\n'.join(chunks).strip()
        return (text or fallback_reply(lead, intent, settings)), 'openai'
    except Exception:
        return fallback_reply(lead, intent, settings), 'template_fallback'

@app.route('/register', methods=['GET','POST'])
def register():
    if g.user: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        business_name=request.form.get('business_name','').strip() or f"{name}'s Business"
        if not name or not email or len(password) < 8:
            flash('Enter your name, email, and a password of at least 8 characters.','error'); return render_template('register.html')
        conn=db()
        if conn.execute('SELECT 1 FROM users WHERE email=?',(email,)).fetchone():
            conn.close(); flash('An account with that email already exists.','error'); return render_template('register.html')
        now=now_iso(); trial=(datetime.now()+timedelta(days=14)).isoformat(timespec='minutes')
        bid=insert_id(conn,'INSERT INTO businesses(name,plan,subscription_status,trial_ends_at,webhook_key,created_at) VALUES (?,?,?,?,?,?)',
                         (business_name,'starter','trialing',trial,secrets.token_urlsafe(24),now)); seed_business_defaults(conn,bid)
        uid=insert_id(conn,'INSERT INTO users(business_id,name,email,password_hash,role,created_at) VALUES (?,?,?,?,?,?)',
                       (bid,name,email,generate_password_hash(password),'owner',now))
        conn.commit(); conn.close(); session['user_id']=uid
        flash('Your Lead Rescue AI workspace is ready. Let’s finish setup.','success'); return redirect(url_for('onboarding'))
    return render_template('register.html')


@app.route('/login', methods=['GET','POST'])
def login():
    if g.user: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        conn=db(); user=conn.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone(); conn.close()
        if user and check_password_hash(user['password_hash'],password):
            session.clear(); session['user_id']=user['id']; return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Email or password was incorrect.','error')
    return render_template('login.html')


@app.post('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    conn=db(); bid=business_id()
    stats=conn.execute('''SELECT COUNT(*) total,
      SUM(CASE WHEN status='New' THEN 1 ELSE 0 END) new_count,
      SUM(CASE WHEN status='Replied' THEN 1 ELSE 0 END) replied,
      SUM(CASE WHEN status='Booked' THEN 1 ELSE 0 END) booked,
      SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) won,
      COALESCE(SUM(CASE WHEN status='Won' THEN value ELSE 0 END),0) revenue,
      COALESCE(SUM(CASE WHEN status NOT IN ('Won','Lost') THEN value ELSE 0 END),0) pipeline
      FROM leads WHERE business_id=?''',(bid,)).fetchone()
    due=conn.execute("SELECT * FROM leads WHERE business_id=? AND opted_out=0 AND next_followup_at IS NOT NULL AND next_followup_at<=? AND status NOT IN ('Won','Lost') ORDER BY next_followup_at LIMIT 8",(bid,now_iso())).fetchall()
    recent=conn.execute('SELECT * FROM leads WHERE business_id=? ORDER BY id DESC LIMIT 8',(bid,)).fetchall()
    recent_messages=conn.execute("""SELECT a.*,l.name lead_name,l.status lead_status FROM activities a JOIN leads l ON l.id=a.lead_id AND l.business_id=a.business_id WHERE a.business_id=? AND (a.activity_type LIKE '%message%' OR a.activity_type LIKE '%sent%') ORDER BY a.id DESC LIMIT 6""",(bid,)).fetchall()
    upcoming=conn.execute("""SELECT a.*,l.name lead_name FROM appointments a LEFT JOIN leads l ON l.id=a.lead_id WHERE a.business_id=? AND a.starts_at>=? ORDER BY a.starts_at LIMIT 4""",(bid,now_iso())).fetchall()
    integration_rows=conn.execute('SELECT provider,COUNT(*) count FROM integrations WHERE business_id=? AND enabled=1 GROUP BY provider',(bid,)).fetchall()
    integrations={r['provider']:r['count'] for r in integration_rows}
    settings_map={r['key']:r['value'] for r in conn.execute('SELECT key,value FROM settings WHERE business_id=?',(bid,)).fetchall()}
    conn.close()
    setup_items=[bool(integrations.get('meta')),bool(integrations.get('google')),bool(settings_map.get('sender_email')),bool(os.environ.get('OPENAI_API_KEY')),settings_map.get('onboarding_completed')=='1']
    setup_percent=round(sum(setup_items)/len(setup_items)*100)
    return render_template('dashboard.html',stats=stats,due=due,recent=recent,recent_messages=recent_messages,upcoming=upcoming,integrations=integrations,setup_percent=setup_percent,business=g.business)


@app.route('/conversations')
@login_required
def conversations():
    conn=db(); bid=business_id()
    leads_rows=conn.execute("""SELECT l.*, MAX(a.id) last_activity_id, MAX(a.created_at) last_message_at FROM leads l LEFT JOIN activities a ON a.lead_id=l.id AND a.business_id=l.business_id AND (a.activity_type LIKE '%message%' OR a.activity_type LIKE '%sent%') WHERE l.business_id=? GROUP BY l.id ORDER BY COALESCE(MAX(a.id),0) DESC,l.id DESC""",(bid,)).fetchall()
    selected_id=request.args.get('lead',type=int) or (leads_rows[0]['id'] if leads_rows else None)
    selected=None; messages=[]
    if selected_id:
        selected=conn.execute('SELECT * FROM leads WHERE id=? AND business_id=?',(selected_id,bid)).fetchone()
        if selected:
            messages=conn.execute("""SELECT * FROM activities WHERE lead_id=? AND business_id=? AND (activity_type LIKE '%message%' OR activity_type LIKE '%sent%' OR activity_type='status_change') ORDER BY id""",(selected_id,bid)).fetchall()
    conn.close()
    return render_template('conversations.html',leads=leads_rows,selected=selected,messages=messages)


@app.route('/leads')
@login_required
def leads():
    q=request.args.get('q','').strip(); status=request.args.get('status','').strip(); conn=db(); sql='SELECT * FROM leads WHERE business_id=?'; params=[business_id()]
    if q:
        sql += ' AND (name LIKE ? OR email LIKE ? OR phone LIKE ? OR company LIKE ?)'; term=f'%{q}%'; params += [term]*4
    if status:
        sql += ' AND status=?'; params.append(status)
    sql += ' ORDER BY id DESC'; rows=conn.execute(sql,params).fetchall(); conn.close()
    return render_template('leads.html',leads=rows,statuses=STATUSES,q=q,selected_status=status)


@app.route('/leads/new',methods=['GET','POST'])
@login_required
def new_lead():
    if request.method=='POST':
        settings=get_settings(); delay=int(settings.get('followup_delay_hours','24') or 24); now=now_iso()
        next_fu=(datetime.now()+timedelta(hours=delay)).isoformat(timespec='minutes') if settings.get('auto_followup_enabled')=='1' else None
        name=request.form.get('name','').strip() or 'New Lead'; email=request.form.get('email','').strip(); phone=request.form.get('phone','').strip(); company=request.form.get('company','').strip(); source=request.form.get('source','Manual'); value=float(request.form.get('value') or 0); notes=request.form.get('notes','').strip()
        score=compute_score(name,email,phone,company,source,value,notes); conn=db(); bid=business_id()
        lid=insert_id(conn,'''INSERT INTO leads(business_id,name,email,phone,company,source,status,value,score,consent_email,consent_sms,notes,created_at,next_followup_at)
          VALUES (?,?,?,?,?,?, 'New',?,?,?,?,?,?,?)''',(bid,name,email,phone,company,source,value,score,1 if request.form.get('consent_email') else 0,1 if request.form.get('consent_sms') else 0,notes,now,next_fu)); conn.execute('INSERT INTO activities(business_id,lead_id,activity_type,content,created_at) VALUES (?,?,?,?,?)',(bid,lid,'lead_created',f'Lead added to pipeline · score {score}',now)); conn.commit(); conn.close()
        flash('Lead added. Recovery workflow started.','success'); return redirect(url_for('lead_detail',lead_id=lid))
    return render_template('lead_form.html',sources=SOURCES)


@app.route('/leads/<int:lead_id>')
@login_required
def lead_detail(lead_id):
    conn=db(); bid=business_id(); lead=conn.execute('SELECT * FROM leads WHERE id=? AND business_id=?',(lead_id,bid)).fetchone()
    if not lead: conn.close(); return ('Lead not found',404)
    acts=conn.execute('SELECT * FROM activities WHERE lead_id=? AND business_id=? ORDER BY id DESC',(lead_id,bid)).fetchall(); conn.close()
    return render_template('lead_detail.html',lead=lead,activities=acts,statuses=STATUSES)


@app.post('/leads/<int:lead_id>/status')
@login_required
def update_status(lead_id):
    status=request.form.get('status','New'); status=status if status in STATUSES else 'New'; conn=db(); bid=business_id()
    if not conn.execute('SELECT 1 FROM leads WHERE id=? AND business_id=?',(lead_id,bid)).fetchone(): conn.close(); return ('Lead not found',404)
    conn.execute('UPDATE leads SET status=? WHERE id=? AND business_id=?',(status,lead_id,bid)); conn.execute('INSERT INTO activities(business_id,lead_id,activity_type,content,created_at) VALUES (?,?,?,?,?)',(bid,lead_id,'status_change',f'Status changed to {status}',now_iso())); conn.commit(); conn.close(); return redirect(url_for('lead_detail',lead_id=lead_id))


@app.post('/leads/<int:lead_id>/message')
@login_required
def log_message(lead_id):
    content=request.form.get('content','').strip(); channel=request.form.get('channel','Email'); conn=db(); bid=business_id(); lead=conn.execute('SELECT * FROM leads WHERE id=? AND business_id=?',(lead_id,bid)).fetchone()
    if not lead: conn.close(); return ('Lead not found',404)
    if lead['opted_out']: conn.close(); flash('This lead opted out. Outreach was not logged.','error'); return redirect(url_for('lead_detail',lead_id=lead_id))
    if content:
        settings=get_settings(); delay=int(settings.get('followup_delay_hours','24') or 24); now=now_iso(); next_fu=(datetime.now()+timedelta(hours=delay)).isoformat(timespec='minutes')
        conn.execute("UPDATE leads SET last_contacted_at=?,next_followup_at=?,status=CASE WHEN status='New' THEN 'Contacted' ELSE status END WHERE id=? AND business_id=?",(now,next_fu,lead_id,bid))
        conn.execute('INSERT INTO activities(business_id,lead_id,activity_type,content,created_at) VALUES (?,?,?,?,?)',(bid,lead_id,f'{channel.lower()}_message',content,now)); conn.commit(); flash('Message logged and follow-up timer reset.','success')
    conn.close(); return redirect(url_for('lead_detail',lead_id=lead_id))




@app.post('/leads/<int:lead_id>/send')
@login_required
def send_message_now(lead_id):
    channel=request.form.get('channel','Email'); content=request.form.get('content','').strip(); subject=request.form.get('subject','Following up').strip() or 'Following up'
    conn=db(); bid=business_id(); lead=conn.execute('SELECT * FROM leads WHERE id=? AND business_id=?',(lead_id,bid)).fetchone()
    if not lead: conn.close(); return ('Lead not found',404)
    if lead['opted_out']:
        conn.close(); flash('This lead is marked do-not-contact. Nothing was sent.','error'); return redirect(url_for('lead_detail',lead_id=lead_id))
    if not content:
        conn.close(); flash('Write or generate a message first.','error'); return redirect(url_for('lead_detail',lead_id=lead_id))
    settings=get_settings()
    if channel=='SMS':
        if not lead['consent_sms']:
            conn.close(); flash('SMS consent is not enabled for this lead.','error'); return redirect(url_for('lead_detail',lead_id=lead_id))
        ok, result=send_sms_message(lead['phone'],content)
    else:
        if not lead['consent_email']:
            conn.close(); flash('Email consent is not enabled for this lead.','error'); return redirect(url_for('lead_detail',lead_id=lead_id))
        ok, result=send_email_message(lead['email'],subject,content,settings)
    if ok:
        record_sent_message(conn,bid,lead_id,channel,content,result); conn.commit(); flash(f'{channel} sent successfully.','success')
    else:
        flash(result,'error')
    conn.close(); return redirect(url_for('lead_detail',lead_id=lead_id))


@app.post('/automation/run')
@login_required
def run_automation():
    bid=business_id(); settings=get_settings(); conn=db(); due=conn.execute("SELECT * FROM leads WHERE business_id=? AND opted_out=0 AND next_followup_at IS NOT NULL AND next_followup_at<=? AND status NOT IN ('Won','Lost') ORDER BY next_followup_at LIMIT 25",(bid,now_iso())).fetchall()
    sent=0; skipped=0
    for lead in due:
        reply,_=real_ai_reply(lead,'followup')
        if lead['consent_email'] and lead['email'] and os.environ.get('RESEND_API_KEY') and settings.get('sender_email'):
            ok,result=send_email_message(lead['email'],'Quick follow-up',reply,settings); channel='Email'
        elif lead['consent_sms'] and lead['phone'] and os.environ.get('TWILIO_ACCOUNT_SID'):
            ok,result=send_sms_message(lead['phone'],reply); channel='SMS'
        else:
            ok=False; result='No configured permitted channel'; channel='Automation'
        if ok:
            record_sent_message(conn,bid,lead['id'],channel,reply,result); sent+=1
        else:
            skipped+=1
    conn.commit(); conn.close(); flash(f'Automation processed {len(due)} due leads: {sent} sent, {skipped} skipped.','success'); return redirect(url_for('automation'))

@app.post('/leads/<int:lead_id>/optout')
@login_required
def optout(lead_id):
    conn=db(); bid=business_id(); conn.execute("UPDATE leads SET opted_out=1,next_followup_at=NULL WHERE id=? AND business_id=?",(lead_id,bid)); conn.execute('INSERT INTO activities(business_id,lead_id,activity_type,content,created_at) VALUES (?,?,?,?,?)',(bid,lead_id,'opt_out','Lead marked do-not-contact',now_iso())); conn.commit(); conn.close(); flash('Lead marked do-not-contact.','success'); return redirect(url_for('lead_detail',lead_id=lead_id))


@app.get('/api/leads/<int:lead_id>/ai-reply')
@login_required
def generate_reply(lead_id):
    lead=lead_or_404(lead_id)
    if not lead: return jsonify({'error':'not found'}),404
    reply,engine=real_ai_reply(lead,request.args.get('intent','followup'))
    return jsonify({'reply':reply,'engine':engine})


@app.route('/automation')
@login_required
def automation():
    conn=db(); bid=business_id(); rows=conn.execute("SELECT * FROM leads WHERE business_id=? AND status NOT IN ('Won','Lost') ORDER BY opted_out,next_followup_at",(bid,)).fetchall(); seq=conn.execute('SELECT * FROM sequences WHERE business_id=? ORDER BY step_number',(bid,)).fetchall(); conn.close()
    return render_template('automation.html',leads=rows,sequences=seq,now=now_iso(),business=g.business)


@app.route('/settings',methods=['GET','POST'])
@login_required
def settings():
    if request.method=='POST':
        conn=db(); bid=business_id()
        for key in ['business_name','brand_voice','booking_link','followup_delay_hours','auto_followup_enabled','openai_model','sender_name','sender_email','timezone','appointment_duration_minutes','industry','business_goal']:
            val=request.form.get(key,'0' if key=='auto_followup_enabled' else '')
            conn.execute('INSERT INTO settings(business_id,key,value) VALUES (?,?,?) ON CONFLICT(business_id,key) DO UPDATE SET value=excluded.value',(bid,key,val))
        business_name=request.form.get('business_name','').strip()
        if business_name: conn.execute('UPDATE businesses SET name=? WHERE id=?',(business_name,bid))
        conn.commit(); conn.close(); flash('Settings saved.','success'); return redirect(url_for('settings'))
    return render_template('settings.html',settings=get_settings(),business=g.business)


@app.route('/billing')
@login_required
def billing():
    return render_template('billing.html',plans=PLANS,business=g.business,stripe_ready=bool(os.environ.get('STRIPE_SECRET_KEY')))


@app.post('/billing/checkout/<plan>')
@login_required
def billing_checkout(plan):
    if plan not in PLANS: return ('Plan not found',404)
    secret=os.environ.get('STRIPE_SECRET_KEY','').strip(); price_id=os.environ.get(f'STRIPE_PRICE_{plan.upper()}','').strip()
    if not secret or not price_id:
        flash('Stripe is not configured yet. Add STRIPE_SECRET_KEY and the plan price IDs to activate checkout.','error'); return redirect(url_for('billing'))
    host=request.host_url.rstrip('/')
    fields={
        'mode':'subscription','line_items[0][price]':price_id,'line_items[0][quantity]':'1',
        'success_url':host+url_for('billing')+'?checkout=success','cancel_url':host+url_for('billing')+'?checkout=cancelled',
        'client_reference_id':str(business_id()),
        'metadata[business_id]':str(business_id()),'metadata[plan]':plan,
        'subscription_data[metadata][business_id]':str(business_id()),'subscription_data[metadata][plan]':plan,
    }
    if g.business['stripe_customer_id']:
        fields['customer']=g.business['stripe_customer_id']
    else:
        fields['customer_email']=g.user['email']
    req=urllib.request.Request('https://api.stripe.com/v1/checkout/sessions',data=urllib.parse.urlencode(fields).encode(),method='POST',headers={'Authorization':f'Bearer {secret}','Content-Type':'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req,timeout=20) as resp: data=json.loads(resp.read().decode())
        return redirect(data['url'])
    except Exception:
        flash('Checkout could not be started. Check your Stripe environment variables.','error'); return redirect(url_for('billing'))


@app.post('/api/webhook/lead')
def lead_webhook():
    data=request.get_json(silent=True) or {}; key=request.headers.get('X-Lead-Rescue-Key') or request.args.get('key')
    if not key: return jsonify({'error':'webhook key required'}),401
    conn=db(); biz=conn.execute('SELECT * FROM businesses WHERE webhook_key=?',(key,)).fetchone()
    if not biz: conn.close(); return jsonify({'error':'invalid webhook key'}),403
    if not data.get('name') and not data.get('email') and not data.get('phone'): conn.close(); return jsonify({'error':'name, email, or phone required'}),400
    bid=biz['id']; settings={r['key']:r['value'] for r in conn.execute('SELECT key,value FROM settings WHERE business_id=?',(bid,)).fetchall()}; delay=int(settings.get('followup_delay_hours','24') or 24); now=now_iso(); next_fu=(datetime.now()+timedelta(hours=delay)).isoformat(timespec='minutes') if settings.get('auto_followup_enabled')=='1' else None
    name=data.get('name') or 'New Lead'; email=data.get('email',''); phone=data.get('phone',''); company=data.get('company',''); source=data.get('source','Webhook'); value=float(data.get('value') or 0); notes=data.get('notes',''); score=compute_score(name,email,phone,company,source,value,notes)
    lid=insert_id(conn,'''INSERT INTO leads(business_id,name,email,phone,company,source,status,value,score,consent_email,consent_sms,notes,created_at,next_followup_at) VALUES (?,?,?,?,?,?, 'New',?,?,?,?,?,?,?)''',(bid,name,email,phone,company,source,value,score,1 if data.get('consent_email',True) else 0,1 if data.get('consent_sms',False) else 0,notes,now,next_fu)); conn.execute('INSERT INTO activities(business_id,lead_id,activity_type,content,created_at) VALUES (?,?,?,?,?)',(bid,lid,'webhook_capture',f'Lead captured by webhook · score {score}',now)); conn.commit(); conn.close(); return jsonify({'ok':True,'lead_id':lid,'score':score}),201


@app.route('/onboarding', methods=['GET','POST'])
@login_required
def onboarding():
    current = get_settings()
    if request.method == 'POST':
        conn = db(); bid = business_id()
        updates = {
            'business_name': request.form.get('business_name','').strip() or g.business['name'],
            'industry': request.form.get('industry','').strip(),
            'business_goal': request.form.get('business_goal','').strip(),
            'brand_voice': request.form.get('brand_voice','').strip() or current.get('brand_voice',''),
            'booking_link': request.form.get('booking_link','').strip(),
            'timezone': request.form.get('timezone','America/New_York').strip(),
            'sender_name': request.form.get('sender_name','').strip(),
            'sender_email': request.form.get('sender_email','').strip(),
            'onboarding_completed': '1',
        }
        for key, value in updates.items():
            conn.execute('INSERT INTO settings(business_id,key,value) VALUES (?,?,?) ON CONFLICT(business_id,key) DO UPDATE SET value=excluded.value', (bid,key,value))
        conn.execute('UPDATE businesses SET name=? WHERE id=?', (updates['business_name'],bid))
        conn.commit(); conn.close()
        flash('Onboarding complete. Your recovery workspace is ready.','success')
        return redirect(url_for('dashboard'))
    return render_template('onboarding.html', settings=current)


@app.route('/integrations', methods=['GET','POST'])
@login_required
def integrations():
    conn = db(); bid = business_id()
    if request.method == 'POST':
        provider = request.form.get('provider','meta')
        if provider == 'meta':
            page_id = request.form.get('page_id','').strip()
            page_name = request.form.get('page_name','').strip()
            token = request.form.get('page_access_token','').strip()
            if not page_id or not token:
                conn.close(); flash('Meta Page ID and Page access token are required.','error'); return redirect(url_for('integrations'))
            enc = encrypt_json({'page_access_token': token})
            now = now_iso()
            conn.execute(
                '''INSERT INTO integrations(business_id,provider,account_id,display_name,credentials_enc,enabled,created_at,updated_at)
                VALUES (?,?,?,?,?,1,?,?)
                ON CONFLICT(business_id,provider,account_id) DO UPDATE SET display_name=excluded.display_name,credentials_enc=excluded.credentials_enc,enabled=1,updated_at=excluded.updated_at''',
                (bid,'meta',page_id,page_name or f'Meta Page {page_id}',enc,now,now)
            )
            conn.commit(); flash('Meta Page connection saved.','success')
        conn.close(); return redirect(url_for('integrations'))
    rows = conn.execute('SELECT id,provider,account_id,display_name,enabled,created_at,updated_at FROM integrations WHERE business_id=? ORDER BY id DESC',(bid,)).fetchall()
    conn.close()
    return render_template('integrations.html', integrations=rows)


@app.post('/integrations/<int:integration_id>/delete')
@login_required
def delete_integration(integration_id):
    conn=db(); conn.execute('DELETE FROM integrations WHERE id=? AND business_id=?',(integration_id,business_id())); conn.commit(); conn.close()
    flash('Integration disconnected.','success'); return redirect(url_for('integrations'))


@app.get('/integrations/meta/webhook')
def meta_webhook_verify():
    verify = os.environ.get('META_VERIFY_TOKEN','').strip()
    if request.args.get('hub.mode') == 'subscribe' and verify and request.args.get('hub.verify_token') == verify:
        return request.args.get('hub.challenge',''), 200
    return 'Verification failed', 403


@app.post('/integrations/meta/webhook')
def meta_webhook_receive():
    raw = request.get_data()
    if not meta_signature_valid(raw):
        return jsonify({'error':'invalid signature'}),403
    payload = request.get_json(silent=True) or {}
    conn = db(); captured = 0; errors = []
    for entry in payload.get('entry',[]):
        page_id = str(entry.get('id') or '')
        for change in entry.get('changes',[]):
            value = change.get('value') or {}
            leadgen_id = str(value.get('leadgen_id') or '')
            if change.get('field') != 'leadgen' or not leadgen_id:
                continue
            integ = conn.execute("SELECT * FROM integrations WHERE provider='meta' AND account_id=? AND enabled=1 ORDER BY id DESC LIMIT 1",(page_id,)).fetchone()
            if not integ:
                errors.append(f'No workspace connected for Page {page_id}')
                continue
            if conn.execute("SELECT 1 FROM inbound_events WHERE provider='meta' AND external_id=?",(leadgen_id,)).fetchone():
                continue
            try:
                creds = decrypt_json(integ['credentials_enc'])
                lead_data, raw_lead = meta_fetch_lead(leadgen_id, creds.get('page_access_token',''))
                capture_lead_for_business(conn, integ['business_id'], lead_data, 'Meta Ads', 'meta_lead_capture')
                conn.execute('INSERT INTO inbound_events(business_id,provider,external_id,payload,status,created_at) VALUES (?,?,?,?,?,?)',
                             (integ['business_id'],'meta',leadgen_id,json.dumps(raw_lead),'captured',now_iso()))
                captured += 1
            except Exception as exc:
                conn.execute('INSERT INTO inbound_events(business_id,provider,external_id,payload,status,error,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider,external_id) DO NOTHING',
                             (integ['business_id'],'meta',leadgen_id,json.dumps(value),'error',type(exc).__name__,now_iso()))
                errors.append(f'{leadgen_id}: {type(exc).__name__}')
    conn.commit(); conn.close()
    return jsonify({'ok':True,'captured':captured,'errors':errors}),200


@app.post('/api/automation/cron')
def automation_cron():
    expected = os.environ.get('AUTOMATION_CRON_TOKEN','').strip()
    supplied = request.headers.get('Authorization','').replace('Bearer ','',1).strip()
    if not expected or not hmac.compare_digest(expected,supplied):
        return jsonify({'error':'unauthorized'}),401
    conn=db(); businesses=conn.execute("SELECT id FROM businesses WHERE subscription_status IN ('trialing','active')").fetchall(); conn.close()
    totals={'businesses':0,'processed':0,'sent':0,'skipped':0}
    for business in businesses:
        result=automation_process_business(business['id'])
        totals['businesses'] += 1
        for key in ('processed','sent','skipped'):
            totals[key] += result[key]
    return jsonify({'ok':True, **totals})


@app.route('/appointments', methods=['GET','POST'])
@login_required
def appointments():
    conn=db(); bid=business_id()
    if request.method == 'POST':
        lead_id = request.form.get('lead_id') or None
        starts = request.form.get('starts_at','').strip()
        title = request.form.get('title','Lead consultation').strip() or 'Lead consultation'
        notes = request.form.get('notes','').strip()
        current = get_settings()
        duration = int(current.get('appointment_duration_minutes','30') or 30)
        try:
            start_dt = datetime.fromisoformat(starts)
            end_dt = start_dt + timedelta(minutes=duration)
        except Exception:
            conn.close(); flash('Choose a valid appointment date and time.','error'); return redirect(url_for('appointments'))
        conn.execute('INSERT INTO appointments(business_id,lead_id,title,starts_at,ends_at,timezone,status,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                     (bid,lead_id,title,start_dt.isoformat(timespec='minutes'),end_dt.isoformat(timespec='minutes'),current.get('timezone','America/New_York'),'confirmed',notes,now_iso()))
        if lead_id:
            conn.execute("UPDATE leads SET status='Booked' WHERE id=? AND business_id=?",(lead_id,bid))
            conn.execute('INSERT INTO activities(business_id,lead_id,activity_type,content,created_at) VALUES (?,?,?,?,?)',
                         (bid,lead_id,'appointment_booked',f'Appointment booked for {start_dt.isoformat(timespec="minutes")}',now_iso()))
        conn.commit(); conn.close(); flash('Appointment booked.','success'); return redirect(url_for('appointments'))
    appts=conn.execute('''SELECT a.*,l.name lead_name,l.email lead_email FROM appointments a LEFT JOIN leads l ON l.id=a.lead_id WHERE a.business_id=? ORDER BY a.starts_at''',(bid,)).fetchall()
    lead_rows=conn.execute("SELECT id,name,email FROM leads WHERE business_id=? AND status NOT IN ('Won','Lost') ORDER BY name",(bid,)).fetchall(); conn.close()
    return render_template('appointments.html', appointments=appts, leads=lead_rows, settings=get_settings())


@app.get('/appointments/<int:appointment_id>/ics')
@login_required
def appointment_ics(appointment_id):
    conn=db(); a=conn.execute('SELECT * FROM appointments WHERE id=? AND business_id=?',(appointment_id,business_id())).fetchone(); conn.close()
    if not a:
        return ('Not found',404)
    def ics_dt(value):
        return datetime.fromisoformat(value).strftime('%Y%m%dT%H%M%S')
    description=(a['notes'] or '').replace('\n',' ')
    body='\r\n'.join([
        'BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//Lead Rescue AI//EN','BEGIN:VEVENT',
        f'UID:lead-rescue-{a["id"]}@local',f'DTSTART:{ics_dt(a["starts_at"])}',f'DTEND:{ics_dt(a["ends_at"])}',
        f'SUMMARY:{a["title"]}',f'DESCRIPTION:{description}','END:VEVENT','END:VCALENDAR',''
    ])
    return app.response_class(body,mimetype='text/calendar',headers={'Content-Disposition':f'attachment; filename=lead-rescue-{a["id"]}.ics'})


@app.get('/api/metrics')
@login_required
def api_metrics():
    conn=db(); bid=business_id()
    row=conn.execute('''SELECT COUNT(*) total, SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) won, SUM(CASE WHEN status='Booked' THEN 1 ELSE 0 END) booked, COALESCE(SUM(CASE WHEN status='Won' THEN value ELSE 0 END),0) revenue FROM leads WHERE business_id=?''',(bid,)).fetchone(); conn.close()
    total=row['total'] or 0; won=row['won'] or 0
    return jsonify({'total_leads':total,'won':won,'booked':row['booked'] or 0,'recovered_revenue':row['revenue'] or 0,'close_rate':round((won/total*100),1) if total else 0})

@app.get('/health')
def health():
    return jsonify({'ok':True,'service':'Lead Rescue AI'})


from production import init_production
init_production(app, globals())

init_db()

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=os.environ.get('FLASK_DEBUG')=='1')
