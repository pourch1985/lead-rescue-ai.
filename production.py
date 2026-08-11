import os, json, secrets, urllib.request, urllib.parse, hmac, hashlib
from datetime import datetime
from flask import request, redirect, url_for, jsonify, flash, session, g, render_template
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash


def init_production(app, svc):
    db=svc['db']; now_iso=svc['now_iso']; login_required=svc['login_required']; business_id=svc['business_id']
    encrypt_json=svc['encrypt_json']; decrypt_json=svc['decrypt_json']; PLANS=svc['PLANS']

    def serializer():
        return URLSafeTimedSerializer(app.secret_key, salt='lead-rescue-account-v5')

    def base_url():
        return os.environ.get('APP_BASE_URL','').rstrip('/') or request.host_url.rstrip('/')

    def send_auth_email(to_email, subject, action_url):
        api_key=os.environ.get('RESEND_API_KEY','').strip(); sender=os.environ.get('AUTH_FROM_EMAIL','').strip()
        if not api_key or not sender:
            app.logger.warning('%s link for %s: %s', subject, to_email, action_url)
            return False
        payload=json.dumps({'from':f"{os.environ.get('AUTH_FROM_NAME','Lead Rescue AI')} <{sender}>",'to':[to_email],'subject':subject,'text':f'{subject}\n\n{action_url}\n\nIf you did not request this, ignore this email.'}).encode()
        req=urllib.request.Request('https://api.resend.com/emails',data=payload,method='POST',headers={'Authorization':f'Bearer {api_key}','Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=20): pass
            return True
        except Exception:
            return False

    def verified(user_id):
        conn=db(); row=conn.execute('SELECT email_verified_at FROM user_security WHERE user_id=?',(user_id,)).fetchone(); conn.close()
        return bool(row and row['email_verified_at'])

    @app.context_processor
    def production_context():
        return {'email_verified': verified(g.user['id']) if g.user else False}

    @app.get('/account/security')
    @login_required
    def account_security():
        return render_template('account_security.html', verified=verified(g.user['id']))

    @app.post('/account/send-verification')
    @login_required
    def send_verification():
        token=serializer().dumps({'purpose':'verify','uid':g.user['id'],'email':g.user['email']})
        link=base_url()+url_for('verify_email_v5',token=token)
        sent=send_auth_email(g.user['email'],'Verify your Lead Rescue AI email',link)
        flash('Verification email sent.' if sent else 'Email delivery is not configured yet. The secure verification route is ready.','success' if sent else 'error')
        return redirect(url_for('account_security'))

    @app.get('/account/verify/<token>')
    def verify_email_v5(token):
        try:
            data=serializer().loads(token,max_age=86400)
            if data.get('purpose')!='verify': raise BadSignature('purpose')
        except SignatureExpired:
            flash('Verification link expired. Sign in and request a new one.','error'); return redirect(url_for('login'))
        except BadSignature:
            flash('Verification link is invalid.','error'); return redirect(url_for('login'))
        conn=db(); user=conn.execute('SELECT id FROM users WHERE id=? AND email=?',(data.get('uid'),data.get('email'))).fetchone()
        if user:
            conn.execute('INSERT INTO user_security(user_id,email_verified_at) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET email_verified_at=excluded.email_verified_at',(user['id'],now_iso())); conn.commit()
        conn.close(); flash('Email verified.','success'); return redirect(url_for('dashboard') if g.user else url_for('login'))

    @app.route('/forgot-password',methods=['GET','POST'])
    def forgot_password_v5():
        if request.method=='POST':
            email=request.form.get('email','').strip().lower(); conn=db(); user=conn.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone(); conn.close()
            if user:
                token=serializer().dumps({'purpose':'reset','uid':user['id'],'email':email}); link=base_url()+url_for('reset_password_v5',token=token); send_auth_email(email,'Reset your Lead Rescue AI password',link)
            flash('If an account exists for that email, a reset link has been sent.','success'); return redirect(url_for('login'))
        return render_template('forgot_password.html')

    @app.route('/reset-password/<token>',methods=['GET','POST'])
    def reset_password_v5(token):
        try:
            data=serializer().loads(token,max_age=3600)
            if data.get('purpose')!='reset': raise BadSignature('purpose')
        except (SignatureExpired,BadSignature):
            flash('That reset link is invalid or expired.','error'); return redirect(url_for('forgot_password_v5'))
        if request.method=='POST':
            password=request.form.get('password','')
            if len(password)<8:
                flash('Password must be at least 8 characters.','error'); return render_template('reset_password.html',token=token)
            conn=db(); conn.execute('UPDATE users SET password_hash=? WHERE id=? AND email=?',(generate_password_hash(password),data['uid'],data['email'])); conn.commit(); conn.close(); session.clear(); flash('Password updated. Sign in with your new password.','success'); return redirect(url_for('login'))
        return render_template('reset_password.html',token=token)

    def stripe_signature_valid(payload,header,secret,tolerance=300):
        try:
            values={}
            for pair in header.split(','):
                if '=' in pair:
                    k,v=pair.split('=',1); values.setdefault(k,[]).append(v)
            ts=int(values['t'][0]);
            if abs(int(datetime.now().timestamp())-ts)>tolerance: return False
            expected=hmac.new(secret.encode(),f'{ts}.'.encode()+payload,hashlib.sha256).hexdigest()
            return any(hmac.compare_digest(expected,v) for v in values.get('v1',[]))
        except Exception: return False

    def plan_from_subscription(obj):
        meta=obj.get('metadata') or {}
        if meta.get('plan') in PLANS: return meta['plan']
        items=((obj.get('items') or {}).get('data') or [])
        price=((items[0].get('price') or {}).get('id') if items else None)
        for plan in PLANS:
            if price and price==os.environ.get(f'STRIPE_PRICE_{plan.upper()}',''): return plan
        return None

    @app.post('/stripe/webhook')
    def stripe_webhook_v5():
        secret=os.environ.get('STRIPE_WEBHOOK_SECRET','').strip(); payload=request.get_data(); signature=request.headers.get('Stripe-Signature','')
        if not secret or not stripe_signature_valid(payload,signature,secret): return jsonify({'error':'invalid signature'}),400
        event=request.get_json(silent=True) or {}; event_id=event.get('id'); etype=event.get('type','unknown'); obj=((event.get('data') or {}).get('object') or {})
        conn=db()
        if event_id and conn.execute('SELECT 1 FROM stripe_events WHERE event_id=?',(event_id,)).fetchone(): conn.close(); return jsonify({'ok':True,'duplicate':True})
        try:
            metadata=obj.get('metadata') or {}; bid=int(metadata.get('business_id') or obj.get('client_reference_id') or 0) or None
            if etype=='checkout.session.completed' and bid:
                plan=metadata.get('plan') if metadata.get('plan') in PLANS else 'starter'
                conn.execute('UPDATE businesses SET plan=?,subscription_status=?,stripe_customer_id=?,stripe_subscription_id=? WHERE id=?',(plan,'active',obj.get('customer'),obj.get('subscription'),bid))
            elif etype.startswith('customer.subscription.'):
                status=obj.get('status','canceled' if etype.endswith('.deleted') else 'active'); plan=plan_from_subscription(obj); customer=obj.get('customer')
                if bid: conn.execute('UPDATE businesses SET plan=COALESCE(?,plan),subscription_status=?,stripe_customer_id=COALESCE(?,stripe_customer_id),stripe_subscription_id=? WHERE id=?',(plan,status,customer,obj.get('id'),bid))
                elif customer: conn.execute('UPDATE businesses SET plan=COALESCE(?,plan),subscription_status=?,stripe_subscription_id=? WHERE stripe_customer_id=?',(plan,status,obj.get('id'),customer))
            elif etype in ('invoice.payment_failed','invoice.paid'):
                customer=obj.get('customer'); status='past_due' if etype=='invoice.payment_failed' else 'active'
                if customer: conn.execute('UPDATE businesses SET subscription_status=? WHERE stripe_customer_id=?',(status,customer))
            if event_id: conn.execute('INSERT INTO stripe_events(event_id,event_type,payload,processed_at) VALUES (?,?,?,?)',(event_id,etype,payload.decode('utf-8','replace'),now_iso()))
            conn.commit()
        except Exception:
            conn.rollback(); conn.close(); return jsonify({'error':'processing failed'}),500
        conn.close(); return jsonify({'ok':True})

    def oauth_state(provider):
        value=secrets.token_urlsafe(32); session[f'oauth_state_{provider}']=value; return value

    @app.get('/integrations/google/connect')
    @login_required
    def google_connect_v5():
        client_id=os.environ.get('GOOGLE_CLIENT_ID','').strip()
        if not client_id: flash('Google OAuth credentials are not configured yet.','error'); return redirect(url_for('integrations'))
        redirect_uri=base_url()+url_for('google_callback_v5'); params={'client_id':client_id,'redirect_uri':redirect_uri,'response_type':'code','scope':'openid email profile https://www.googleapis.com/auth/calendar.events','access_type':'offline','include_granted_scopes':'true','prompt':'consent','state':oauth_state('google')}
        return redirect('https://accounts.google.com/o/oauth2/v2/auth?'+urllib.parse.urlencode(params))

    @app.get('/integrations/google/callback')
    @login_required
    def google_callback_v5():
        state=session.pop('oauth_state_google',None)
        if not state or not hmac.compare_digest(state,request.args.get('state','')): flash('Google OAuth state check failed.','error'); return redirect(url_for('integrations'))
        try:
            redirect_uri=base_url()+url_for('google_callback_v5'); body=urllib.parse.urlencode({'code':request.args.get('code',''),'client_id':os.environ.get('GOOGLE_CLIENT_ID',''),'client_secret':os.environ.get('GOOGLE_CLIENT_SECRET',''),'redirect_uri':redirect_uri,'grant_type':'authorization_code'}).encode()
            req=urllib.request.Request('https://oauth2.googleapis.com/token',data=body,method='POST',headers={'Content-Type':'application/x-www-form-urlencoded'}); tokens=json.loads(urllib.request.urlopen(req,timeout=20).read().decode())
            req=urllib.request.Request('https://openidconnect.googleapis.com/v1/userinfo',headers={'Authorization':f"Bearer {tokens['access_token']}"}); profile=json.loads(urllib.request.urlopen(req,timeout=20).read().decode())
            conn=db(); now=now_iso(); conn.execute('INSERT INTO integrations(business_id,provider,account_id,display_name,credentials_enc,enabled,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?) ON CONFLICT(business_id,provider,account_id) DO UPDATE SET display_name=excluded.display_name,credentials_enc=excluded.credentials_enc,enabled=1,updated_at=excluded.updated_at',(business_id(),'google',profile.get('sub'),profile.get('email','Google Calendar'),encrypt_json(tokens),now,now)); conn.commit(); conn.close(); flash('Google Calendar connected.','success')
        except Exception: flash('Google connection failed. Check credentials and redirect URI.','error')
        return redirect(url_for('integrations'))

    def google_access_token(integration):
        tokens=decrypt_json(integration['credentials_enc']); refresh=tokens.get('refresh_token')
        if not refresh: return tokens.get('access_token')
        body=urllib.parse.urlencode({'client_id':os.environ.get('GOOGLE_CLIENT_ID',''),'client_secret':os.environ.get('GOOGLE_CLIENT_SECRET',''),'refresh_token':refresh,'grant_type':'refresh_token'}).encode()
        try:
            req=urllib.request.Request('https://oauth2.googleapis.com/token',data=body,method='POST',headers={'Content-Type':'application/x-www-form-urlencoded'}); refreshed=json.loads(urllib.request.urlopen(req,timeout=20).read().decode()); return refreshed.get('access_token') or tokens.get('access_token')
        except Exception: return tokens.get('access_token')

    @app.post('/appointments/<int:appointment_id>/google')
    @login_required
    def appointment_google_v5(appointment_id):
        conn=db(); appt=conn.execute('SELECT * FROM appointments WHERE id=? AND business_id=?',(appointment_id,business_id())).fetchone(); integration=conn.execute("SELECT * FROM integrations WHERE business_id=? AND provider='google' AND enabled=1 ORDER BY id DESC LIMIT 1",(business_id(),)).fetchone(); conn.close()
        if not appt or not integration: flash('Connect Google Calendar first.','error'); return redirect(url_for('appointments'))
        token=google_access_token(integration); event={'summary':appt['title'],'description':appt['notes'] or 'Created by Lead Rescue AI','start':{'dateTime':appt['starts_at'],'timeZone':appt['timezone']},'end':{'dateTime':appt['ends_at'],'timeZone':appt['timezone']}}
        try:
            req=urllib.request.Request('https://www.googleapis.com/calendar/v3/calendars/primary/events',data=json.dumps(event).encode(),method='POST',headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'}); result=json.loads(urllib.request.urlopen(req,timeout=20).read().decode()); flash('Appointment added to Google Calendar.','success') if result.get('id') else flash('Calendar event was not created.','error')
        except Exception: flash('Google Calendar event creation failed. Reconnect Google and try again.','error')
        return redirect(url_for('appointments'))

    @app.get('/integrations/meta/connect')
    @login_required
    def meta_connect_v5():
        app_id=os.environ.get('META_APP_ID','').strip(); version=os.environ.get('META_GRAPH_VERSION','v25.0')
        if not app_id: flash('Meta OAuth credentials are not configured yet.','error'); return redirect(url_for('integrations'))
        redirect_uri=base_url()+url_for('meta_callback_v5'); params={'client_id':app_id,'redirect_uri':redirect_uri,'state':oauth_state('meta'),'response_type':'code','scope':'pages_show_list,pages_read_engagement,leads_retrieval'}
        return redirect(f'https://www.facebook.com/{version}/dialog/oauth?'+urllib.parse.urlencode(params))

    @app.get('/integrations/meta/callback')
    @login_required
    def meta_callback_v5():
        state=session.pop('oauth_state_meta',None); version=os.environ.get('META_GRAPH_VERSION','v25.0')
        if not state or not hmac.compare_digest(state,request.args.get('state','')): flash('Meta OAuth state check failed.','error'); return redirect(url_for('integrations'))
        try:
            redirect_uri=base_url()+url_for('meta_callback_v5'); params={'client_id':os.environ.get('META_APP_ID',''),'client_secret':os.environ.get('META_APP_SECRET',''),'redirect_uri':redirect_uri,'code':request.args.get('code','')}
            token=json.loads(urllib.request.urlopen('https://graph.facebook.com/'+version+'/oauth/access_token?'+urllib.parse.urlencode(params),timeout=20).read().decode())['access_token']
            pages=json.loads(urllib.request.urlopen('https://graph.facebook.com/'+version+'/me/accounts?'+urllib.parse.urlencode({'fields':'id,name,access_token','access_token':token}),timeout=20).read().decode()).get('data',[])
            conn=db(); now=now_iso()
            for page in pages: conn.execute('INSERT INTO integrations(business_id,provider,account_id,display_name,credentials_enc,enabled,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?) ON CONFLICT(business_id,provider,account_id) DO UPDATE SET display_name=excluded.display_name,credentials_enc=excluded.credentials_enc,enabled=1,updated_at=excluded.updated_at',(business_id(),'meta',str(page.get('id')),page.get('name') or 'Meta Page',encrypt_json({'page_access_token':page.get('access_token','')}),now,now))
            conn.commit(); conn.close(); flash(f'Connected {len(pages)} Meta Page(s).','success')
        except Exception: flash('Meta connection failed. Check app permissions and redirect URI.','error')
        return redirect(url_for('integrations'))

    @app.get('/ready')
    def ready_v5():
        try:
            conn=db(); conn.execute('SELECT 1').fetchone(); is_pg=conn.is_postgres; conn.close(); return jsonify({'ok':True,'database':'postgres' if is_pg else 'sqlite'})
        except Exception as exc: return jsonify({'ok':False,'error':type(exc).__name__}),503
